"""MaintenanceLoop — in-process scheduled maintenance orchestrator.

The ``MaintenanceLoop`` runs a single trigger loop (``_trigger_loop``) that:

* Waits on ``asyncio.wait_for(_trigger_event.wait(), timeout=interval_seconds)``
  where ``timeout=None`` when ``interval_hours=0`` (wait indefinitely for a
  manual trigger) and ``timeout=interval_seconds`` for periodic operation.
* Fires ``_run_one_pass`` on interval timeout (``asyncio.TimeoutError``) or when
  the trigger event is set (``POST /maintenance/trigger``).
* Clears ``_trigger_event`` after each pass completes.

All operations are synchronous within the loop — no completion loop is needed.

State is persisted in ``data_dir / ".maintenance-state.json"`` after each pass.
Writes are atomic (temp file + ``os.replace``).

See ``Documentation/Backlog/D5-maintenance-jobs-policies-team-plan.md`` Task BE-2.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from archon_search._durable_io import atomic_write_json
from archon_search.constants import INGEST_LOCK_TIMEOUT_S
from archon_search.store import FTSIndexNotFoundError
from archon_search.types import (
    IngestJob,
    JobStatus,
)

if TYPE_CHECKING:
    from archon_search.config import MaintenanceConfig
    from archon_search.jobs.store import JobStore
    from archon_search.store import SearchStore

logger = logging.getLogger(__name__)

_SECONDS_PER_HOUR: int = 3600

# Empty health entry shape written per collection each pass.
_EMPTY_HEALTH_ENTRY: dict[str, Any] = {
    "fts_optimized_at": None,
    "orphans_removed_last_run": 0,
    "last_retry_at": None,
    "last_error": None,
    "meta_chunk_count": 0,
}

# Top-level state keys for the .maintenance-state.json file (C3 contract).
_EMPTY_STATE: dict[str, Any] = {
    "last_run_at": None,
    "next_run_at": None,
    "collection_health": {},
    "retry_counts": {},
}


class MaintenanceLoop:
    """Drives scheduled per-collection maintenance passes.

    Owns the ``.maintenance-state.json`` file. Implements all three
    per-collection policies inline: FTS optimize (``_run_fts_optimize``),
    orphan chunk cleanup (``_run_orphan_cleanup``), and failed-ingest
    retry (``_run_failed_ingest_retry``).
    """

    def __init__(
        self,
        job_store: "JobStore",
        search_store: "SearchStore",
        config: "MaintenanceConfig",
        data_dir: Path,
    ) -> None:
        self._job_store = job_store
        self._search_store = search_store
        self._config = config
        self._state_file: Path = data_dir / ".maintenance-state.json"
        # Manual trigger signal: POST /maintenance/trigger sets this event.
        self._trigger_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Exclusion
    # ------------------------------------------------------------------

    def _is_excluded(self, ns: str, col: str) -> bool:
        """Match against ``config.exclude`` patterns.

        Patterns are either bare ``"{col}"`` (matches the collection in any
        namespace) or qualified ``"{ns}/{col}"`` (matches only that namespace/collection).
        Same syntax as ``BackupLoop._is_excluded``.
        """
        qualified = f"{ns}/{col}"
        for pattern in self._config.exclude:
            if "/" in pattern:
                if pattern == qualified:
                    return True
            else:
                if pattern == col:
                    return True
        return False

    # ------------------------------------------------------------------
    # State file
    # ------------------------------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        """Read the maintenance state. Returns empty state on missing or corrupt file."""
        if not self._state_file.exists():
            return {"last_run_at": None, "next_run_at": None, "collection_health": {}, "retry_counts": {}}
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "MaintenanceLoop: state file %s unreadable (%s); treating as empty",
                self._state_file,
                exc,
            )
            return {"last_run_at": None, "next_run_at": None, "collection_health": {}, "retry_counts": {}}
        if not isinstance(raw, dict):
            logger.warning(
                "MaintenanceLoop: state file %s has unexpected format; treating as empty",
                self._state_file,
            )
            return {"last_run_at": None, "next_run_at": None, "collection_health": {}, "retry_counts": {}}
        # Ensure required top-level keys are present (tolerate partial/old files).
        return {
            "last_run_at": raw.get("last_run_at"),
            "next_run_at": raw.get("next_run_at"),
            "collection_health": raw.get("collection_health", {}),
            "retry_counts": raw.get("retry_counts", {}),
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        """Atomic durable write of the state file via the shared helper."""
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._state_file, state)

    # ------------------------------------------------------------------
    # Per-pass policies
    # ------------------------------------------------------------------

    async def _run_fts_optimize(self, collection: str, namespace: str) -> None:
        """FTS optimize for a single collection (BE-5).

        Acquires the per-collection lock (timeout = INGEST_LOCK_TIMEOUT_S).
        On ``FTSIndexNotFoundError``: WARNING + skip (no fts_optimized_at update).
        On lock timeout (``asyncio.TimeoutError``): DEBUG + skip.
        On success: updates ``fts_optimized_at`` in the current collection health dict.

        ``self._current_health`` is set by ``_run_one_pass`` to the per-collection
        health dict before calling this method.
        """
        if not self._config.fts_optimize:
            return

        # Hold the lock through the entire optimize_fts call to prevent concurrent
        # ingest from writing new chunks between the optimize and the return.
        # Note: store.optimize_fts() documents that callers are responsible for
        # concurrency — this is an explicit choice in the maintenance context.
        # Unlike delete_document (which releases before optimize to reduce contention),
        # maintenance optimize runs infrequently and brief blocking is acceptable.
        lock = self._search_store.lock_for(collection)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.debug(
                "MaintenanceLoop: FTS optimize skipped for %s/%s — collection locked",
                namespace,
                collection,
            )
            return

        try:
            await self._search_store.optimize_fts(collection)
        except FTSIndexNotFoundError:
            logger.warning(
                "MaintenanceLoop: FTS optimize skipped for %s/%s — no FTS index found; "
                "rebuild_fts_index() must be called first",
                namespace,
                collection,
            )
            return
        finally:
            lock.release()

        # Update fts_optimized_at in the current pass health dict.
        if hasattr(self, "_current_health"):
            self._current_health["fts_optimized_at"] = datetime.now(timezone.utc).isoformat()

    async def _run_orphan_cleanup(self, collection: str, namespace: str) -> None:
        """Orphan chunk cleanup for a single collection (BE-6).

        Algorithm:
        1. Iterate all chunks via ``store.list_chunks_raw(collection, namespace)``.
        2. Group by unique ``source_path``, skipping URLs (http:// or https://) and
           empty paths (handles multi-chunk and multi-doc-id files).
        3. For each unique file path that no longer exists on disk, call
           ``store.delete_by_source_path(source_path, skip_fts_optimize=True)``.
           Errors on individual paths are logged as WARNING; the loop continues.
        4. After all deletions, acquire the per-collection lock and call
           ``store.optimize_fts(collection)`` once.
           On lock timeout (``asyncio.TimeoutError``): WARNING + skip FTS optimize.
           On ``FTSIndexNotFoundError``: WARNING + skip (index not built yet).
        5. Log WARNING if total elapsed time (scan + delete + FTS) exceeds 60 s.
        6. Update ``orphans_removed_last_run`` in the current collection health dict.

        The lock is NOT pre-acquired for the scan/delete phase:
        ``delete_by_source_path`` acquires/releases the lock internally per call.
        Holding an external lock over the full scan + multiple deletes would create
        a reentrant-lock deadlock because ``asyncio.Lock`` is not reentrant.
        The separate post-deletion lock acquisition for ``optimize_fts`` is fine
        because by that point all deletions (and their internal lock releases) are done.
        """
        if not self._config.orphan_cleanup:
            return

        _ELAPSED_LIMIT_S: float = 60.0
        start = time.monotonic()

        # Phase 1: collect all unique source_paths with their chunks.
        source_paths_seen: set[str] = set()

        async for chunk in self._search_store.list_chunks_raw(collection, namespace):
            source_path: str = chunk.get("source_path", "") or ""
            if not source_path:
                continue

            # Skip URLs — these are not on the local filesystem.
            if source_path.startswith("http://") or source_path.startswith("https://"):
                logger.debug(
                    "MaintenanceLoop: skipping URL source_path for %s/%s: %s",
                    namespace,
                    collection,
                    source_path,
                )
                continue

            source_paths_seen.add(source_path)

        # Phase 2: identify orphans (paths that no longer exist on disk).
        # Sorted for deterministic deletion order.
        orphan_count = 0
        for sp in sorted(source_paths_seen):
            if not Path(sp).exists():
                try:
                    await self._search_store.delete_by_source_path(
                        collection, sp, namespace=namespace, skip_fts_optimize=True
                    )
                    orphan_count += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "MaintenanceLoop: delete_by_source_path failed for %s in %s/%s: %s",
                        sp,
                        namespace,
                        collection,
                        exc,
                    )

        # Phase 3: post-deletion FTS optimize (only when at least one orphan was removed).
        if orphan_count > 0:
            lock = self._search_store.lock_for(collection)
            try:
                await asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning(
                    "MaintenanceLoop: could not acquire lock for post-orphan FTS optimize "
                    "for %s/%s; FTS index may be stale",
                    namespace,
                    collection,
                )
            else:
                try:
                    await self._search_store.optimize_fts(collection)
                except FTSIndexNotFoundError:
                    logger.warning(
                        "MaintenanceLoop: FTS optimize skipped after orphan cleanup for %s/%s "
                        "— no FTS index found; rebuild_fts_index() must be called first",
                        namespace,
                        collection,
                    )
                finally:
                    lock.release()

        # Phase 4: elapsed time warning (covers full operation: scan + delete + FTS).
        elapsed = time.monotonic() - start
        if elapsed > _ELAPSED_LIMIT_S:
            logger.warning(
                "MaintenanceLoop: orphan cleanup for %s/%s took %.1f s (> 60 s); "
                "consider increasing interval_hours or reducing collection size",
                namespace,
                collection,
                elapsed,
            )

        # Update per-collection health dict.
        if hasattr(self, "_current_health"):
            self._current_health["orphans_removed_last_run"] = orphan_count

    async def _run_failed_ingest_retry(
        self, health: dict[str, Any], retry_counts: dict[str, int]
    ) -> None:
        """Pass-level failed-ingest retry (BE-8).

        Called once per pass, after all per-collection policies complete.
        Processes ALL namespaces and collections.

        Mutates ``health`` and ``retry_counts`` in-place; the caller
        (``_run_one_pass``) is responsible for persisting them.

        Algorithm:
        1. Check for DONE resets: if the latest job for a source_path is DONE,
           reset its retry count to 0.
        2. Prune stale keys: remove keys where the source_path no longer appears
           in JobStore AND count == 0.
        3. Filter JobStore.list() for FAILED IngestJobs (only base IngestJob, not
           subclasses like ExportJob/MigrationJob).
        4. Skip jobs where source_path == '' (pre-D5 jobs — log DEBUG).
           Skip jobs with unparseable created_at (log WARNING).
        5. Aged-out jobs (job_created < cutoff) → transition to FAILED_EXPIRED via
           ``transition(from_statuses={FAILED}, to_status=FAILED_EXPIRED)``.
           Retry-exhausted jobs (retry_count >= max_attempts, within age) → same.
           If transition() returns None, the job was already handled or evicted (log DEBUG).
        6. Re-enqueue eligible jobs via JobStore.create(source="maintenance").
           On create() failure: log WARNING; retry_count still incremented.
        7. Increment retry_counts keyed '{namespace}/{collection}/{source_path}'.
        8. Update last_retry_at in collection_health for each collection that had
           at least one re-enqueued job.
        9. Deduplicate: only ONE transition/re-enqueue per unique retry_key per pass.
        """
        if not self._config.failed_ingest_retry:
            return

        # Step 1: collect all jobs from JobStore.
        all_jobs: list[Any] = self._job_store.list()

        # Build a set of all source_paths currently tracked in JobStore
        # (keyed by '{namespace}/{collection}/{source_path}').
        job_store_paths: set[str] = set()
        for job in all_jobs:
            if job.source_path:
                job_store_paths.add(f"{job.namespace}/{job.collection}/{job.source_path}")

        # Step 1: reset retry_counts for source paths where the latest job is DONE.
        # Group all jobs by key to find the most recent one per path.
        jobs_by_key: dict[str, list[Any]] = defaultdict(list)
        for job in all_jobs:
            if job.source_path:
                key = f"{job.namespace}/{job.collection}/{job.source_path}"
                jobs_by_key[key].append(job)

        for key, key_jobs in jobs_by_key.items():
            # Use ISO timestamp comparison for correct chronological ordering.
            try:
                latest = max(key_jobs, key=lambda j: datetime.fromisoformat(j.created_at))
            except (ValueError, TypeError):
                logger.debug(
                    "MaintenanceLoop: skipping DONE-reset for key %s — unparseable created_at in job group",
                    key,
                )
                continue
            if latest.status == JobStatus.DONE and key in retry_counts:
                retry_counts[key] = 0

        # Step 2: prune stale keys (not in JobStore AND count == 0).
        keys_to_prune = [
            k for k, count in retry_counts.items()
            if k not in job_store_paths and count == 0
        ]
        for k in keys_to_prune:
            del retry_counts[k]

        # Step 3: filter for FAILED IngestJobs within age and attempt limits.
        # Only exact IngestJob instances — exclude all subclasses.
        max_age_hours = self._config.retry_max_age_hours
        max_attempts = self._config.retry_max_attempts
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=max_age_hours) if max_age_hours > 0 else None

        # Track collections that had at least one re-enqueue.
        retried_collections: set[str] = set()
        # Deduplicate: only one transition/re-enqueue per unique retry_key per pass.
        seen_keys: set[str] = set()

        for job in all_jobs:
            # Only process exact base IngestJob instances (no subclasses).
            if type(job) is not IngestJob:
                continue
            if job.status != JobStatus.FAILED:
                continue

            # Step 4: skip pre-D5 jobs with no source path.
            if not job.source_path:
                logger.debug(
                    "MaintenanceLoop: skipping pre-D5 FAILED job %s (no source_path)",
                    job.job_id,
                )
                continue

            # Compute retry_key before age filter — needed for dedup and FAILED_EXPIRED transition.
            retry_key = f"{job.namespace}/{job.collection}/{job.source_path}"

            # Deduplicate — skip if already processed this key in this pass.
            if retry_key in seen_keys:
                continue

            current_count = retry_counts.get(retry_key, 0)

            # Age filter — aged-out jobs transition to FAILED_EXPIRED regardless of retry count.
            if cutoff is not None:
                try:
                    job_created = datetime.fromisoformat(job.created_at)
                    aged_out = job_created < cutoff
                except (ValueError, TypeError):
                    logger.warning(
                        "MaintenanceLoop: FAILED job %s for %s in %s/%s has an unparseable "
                        "created_at timestamp (%r); skipping",
                        job.job_id,
                        job.source_path,
                        job.namespace,
                        job.collection,
                        job.created_at,
                    )
                    seen_keys.add(retry_key)
                    continue
                if aged_out:
                    logger.warning(
                        "MaintenanceLoop: FAILED job %s for %s in %s/%s has aged out "
                        "(created %s, cutoff %s); transitioning to FAILED_EXPIRED",
                        job.job_id,
                        job.source_path,
                        job.namespace,
                        job.collection,
                        job.created_at,
                        cutoff.isoformat(),
                    )
                    result = self._job_store.transition(
                        job.job_id,
                        from_statuses={JobStatus.FAILED},
                        to_status=JobStatus.FAILED_EXPIRED,
                    )
                    if result is None:
                        logger.debug(
                            "MaintenanceLoop: transition to FAILED_EXPIRED for job %s "
                            "returned None — already handled or evicted",
                            job.job_id,
                        )
                    seen_keys.add(retry_key)
                    continue

            # Retry-exhausted jobs (within age cutoff) also transition to FAILED_EXPIRED.
            if current_count >= max_attempts:
                logger.warning(
                    "MaintenanceLoop: FAILED job %s for %s in %s/%s has reached "
                    "max retry attempts (%d); transitioning to FAILED_EXPIRED",
                    job.job_id,
                    job.source_path,
                    job.namespace,
                    job.collection,
                    max_attempts,
                )
                result = self._job_store.transition(
                    job.job_id,
                    from_statuses={JobStatus.FAILED},
                    to_status=JobStatus.FAILED_EXPIRED,
                )
                if result is None:
                    logger.debug(
                        "MaintenanceLoop: transition to FAILED_EXPIRED for job %s "
                        "returned None — already handled or evicted",
                        job.job_id,
                    )
                seen_keys.add(retry_key)
                continue

            # Re-enqueue eligible jobs via JobStore.create().
            try:
                self._job_store.create(
                    path=job.source_path,
                    collection=job.collection,
                    namespace=job.namespace,
                    source="maintenance",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "MaintenanceLoop: failed to re-enqueue job for %s in %s/%s: %s",
                    job.source_path,
                    job.namespace,
                    job.collection,
                    exc,
                )

            # Increment retry count regardless of create() success/failure.
            retry_counts[retry_key] = current_count + 1
            retried_collections.add(f"{job.namespace}/{job.collection}")
            seen_keys.add(retry_key)

        # Update last_retry_at in collection_health for retried collections.
        now_str = now.isoformat()
        for col_key in retried_collections:
            if col_key not in health:
                health[col_key] = {
                    "fts_optimized_at": None,
                    "orphans_removed_last_run": 0,
                    "last_retry_at": None,
                    "last_error": None,
                    "meta_chunk_count": 0,
                }
            health[col_key]["last_retry_at"] = now_str

    # ------------------------------------------------------------------
    # Main pass
    # ------------------------------------------------------------------

    async def _run_one_pass(self) -> None:
        """Execute one full maintenance pass across all non-excluded collections.

        Per-collection processing:
        - Collect meta_chunk_count from store.
        - Run per-collection policies (FTS optimize, orphan cleanup) each wrapped
          in a per-policy try/except so a failure in one policy does not abort others.
        - Record last_error in collection health state on any unhandled exception.

        After all per-collection work: run _run_failed_ingest_retry() once.
        Then write the state file atomically.
        """
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()

        state = self._load_state()
        health: dict[str, Any] = state.get("collection_health", {})
        retry_counts: dict[str, int] = state.get("retry_counts", {})

        # Compute next_run_at for the state file.
        interval_hours = self._config.interval_hours
        if interval_hours > 0:
            next_run_at: str | None = (now + timedelta(hours=interval_hours)).isoformat()
        else:
            next_run_at = None

        # Discover all collections.
        try:
            collections = await self._search_store.list_collections()
        except Exception as exc:  # noqa: BLE001
            logger.error("MaintenanceLoop: list_collections failed during pass: %s", exc)
            return

        for info in collections:
            col = info.name
            ns = info.namespace
            key = f"{ns}/{col}"

            if self._is_excluded(ns, col):
                logger.debug("MaintenanceLoop: skipping excluded collection %s", key)
                continue

            # Initialise or carry over health entry for this collection.
            col_health: dict[str, Any] = health.get(key, dict(_EMPTY_HEALTH_ENTRY))
            col_health["last_error"] = None

            # Collect O(1) metadata values.
            meta_chunk_count = 0
            mutations_since_recompute = 0
            try:
                meta = await self._search_store.get_collection_meta(col, ns)
                if meta is not None:
                    meta_chunk_count = meta.chunk_count
                    mutations_since_recompute = meta.mutations_since_recompute
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "MaintenanceLoop: get_collection_meta failed for %s: %s", key, exc
                )

            col_health["meta_chunk_count"] = meta_chunk_count
            col_health["mutations_since_recompute"] = mutations_since_recompute

            # Expose current collection's health dict to per-policy methods so they
            # can update it (e.g. fts_optimized_at, orphans_removed_last_run).
            self._current_health = col_health

            # Per-policy try/except: failures in one policy do not abort others.
            try:
                await self._run_fts_optimize(col, ns)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "MaintenanceLoop: _run_fts_optimize failed for %s: %s", key, exc
                )
                col_health["last_error"] = str(exc)

            try:
                await self._run_orphan_cleanup(col, ns)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "MaintenanceLoop: _run_orphan_cleanup failed for %s: %s", key, exc
                )
                col_health["last_error"] = str(exc)

            health[key] = col_health

        # Pass-level retry (once per pass, after all per-collection work).
        # Passes the pass-level health and retry_counts dicts so the method
        # mutates them in-place; no internal save is done by the method.
        try:
            await self._run_failed_ingest_retry(health, retry_counts)
        except Exception as exc:  # noqa: BLE001
            logger.error("MaintenanceLoop: _run_failed_ingest_retry failed: %s", exc)

        # Write state atomically.
        new_state: dict[str, Any] = {
            "last_run_at": now_str,
            "next_run_at": next_run_at,
            "collection_health": health,
            "retry_counts": retry_counts,
        }
        self._save_state(new_state)

    # ------------------------------------------------------------------
    # Trigger loop
    # ------------------------------------------------------------------

    async def _trigger_loop(self) -> None:
        """Single trigger loop.

        ``interval_hours > 0``: uses ``asyncio.wait_for(_trigger_event.wait(),
        timeout=interval_seconds)`` — catches ``asyncio.TimeoutError`` to fire a
        scheduled pass; event being set fires an immediate pass.

        ``interval_hours == 0``: waits indefinitely (``timeout=None``) on
        ``_trigger_event`` — only manual triggers via ``POST /maintenance/trigger``
        will fire a pass.
        """
        interval_hours = self._config.interval_hours
        interval_seconds: float | None = (
            interval_hours * _SECONDS_PER_HOUR if interval_hours > 0 else None
        )

        while True:
            try:
                await asyncio.wait_for(
                    self._trigger_event.wait(),
                    timeout=interval_seconds,
                )
            except asyncio.TimeoutError:
                # Scheduled interval elapsed.
                pass

            await self._run_one_pass()
            self._trigger_event.clear()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the trigger loop. Cancellable on server shutdown."""
        await self._trigger_loop()

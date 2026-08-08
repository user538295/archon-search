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
import copy
import json
import logging
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
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
    from archon_search.community_builder import CommunityBuilder
    from archon_search.config import GraphConfig, MaintenanceConfig
    from archon_search.graph_enrichment_protocol import LLMEnrichmentClientProtocol
    from archon_search.graph_store import GraphStore
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
    "expired_chunks_removed_last_run": 0,
    "communities_invalidated": False,
}

# Top-level state keys for the .maintenance-state.json file (C3 contract).
_EMPTY_STATE: dict[str, Any] = {
    "last_run_at": None,
    "next_run_at": None,
    "collection_health": {},
    "retry_counts": {},
    "last_expired_pruned_at": None,
    "last_graph_gc_at": None,
    "stale_mention_count": 0,
}


@dataclass
class RebuildState:
    """Tracks in-flight async rebuild tasks for a single (namespace, collection) pair.

    Fields:
    - task: The asyncio.Task running the community rebuild
    - pending: True if another GC fired while this rebuild was running (needs re-enqueue)
    - completed: True after the task completes (used to clear communities_invalidated on next GC pass)
    """

    task: asyncio.Task[Any]
    pending: bool = False
    completed: bool = False


class MaintenanceLoop:
    """Drives scheduled per-collection maintenance passes.

    Owns the ``.maintenance-state.json`` file. Implements four configurable
    policies per pass: three run per-non-excluded-collection — FTS optimize
    (``_run_fts_optimize``), orphan chunk cleanup (``_run_orphan_cleanup``),
    expired-chunk pruning (``_run_expired_chunk_pruning``), and graph GC
    (``_run_graph_gc``) — plus one pass-level policy: failed-ingest retry
    (``_run_failed_ingest_retry``).
    """

    def __init__(
        self,
        job_store: "JobStore",
        search_store: "SearchStore",
        config: "MaintenanceConfig",
        data_dir: Path,
        graph_store: "GraphStore | None" = None,
        graph_config: "GraphConfig | None" = None,
        enrichment_client: "LLMEnrichmentClientProtocol | None" = None,
    ) -> None:
        self._job_store = job_store
        self._search_store = search_store
        self._config = config
        self._graph_store = graph_store
        self._graph_config = graph_config
        self._enrichment_client = enrichment_client
        self._state_file: Path = data_dir / ".maintenance-state.json"
        # Manual trigger signal: POST /maintenance/trigger sets this event.
        self._trigger_event: asyncio.Event = asyncio.Event()
        # Rebuild state tracking: maps (namespace, collection) → RebuildState
        self._rebuild_state: dict[tuple[str, str], RebuildState] = {}
        # E2f BE-5: synonym enrichment state tracking (separate from _rebuild_state).
        # Maps (namespace, collection) → RebuildState for in-flight synonym enrichment tasks.
        self._synonym_state: dict[tuple[str, str], RebuildState] = {}
        # E2f BE-5: collections pending community rebuild after synonym enrichment.
        # Producer: schedule_synonym_enrichment / _run_synonym_enrichment add to this set.
        # Consumer: _drain_communities_pending_rebuild (called in _run_one_pass) removes from it.
        self._communities_pending_rebuild: set[tuple[str, str]] = set()
        # E2g BE-7: PageRank recompute state tracking (separate from _rebuild_state/
        # _synonym_state). Maps (namespace, collection) → RebuildState for in-flight
        # PageRank recompute tasks.
        self._pagerank_state: dict[tuple[str, str], RebuildState] = {}

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
            return copy.deepcopy(_EMPTY_STATE)
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "MaintenanceLoop: state file %s unreadable (%s); treating as empty",
                self._state_file,
                exc,
            )
            return copy.deepcopy(_EMPTY_STATE)
        if not isinstance(raw, dict):
            logger.warning(
                "MaintenanceLoop: state file %s has unexpected format; treating as empty",
                self._state_file,
            )
            return copy.deepcopy(_EMPTY_STATE)
        # Ensure required top-level keys are present (tolerate partial/old files).
        return {
            "last_run_at": raw.get("last_run_at"),
            "next_run_at": raw.get("next_run_at"),
            "collection_health": raw.get("collection_health", {}),
            "retry_counts": raw.get("retry_counts", {}),
            "last_expired_pruned_at": raw.get("last_expired_pruned_at"),
            "last_graph_gc_at": raw.get("last_graph_gc_at"),
            "stale_mention_count": raw.get("stale_mention_count", 0),
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

    async def _run_expired_chunk_pruning(self, collection: str, namespace: str) -> None:
        """Prune expired chunks for a single collection (BE-7, E2a).

        Checks ``config.maintenance.prune_expired_chunks`` first; returns
        immediately when the policy is disabled.

        On success, logs WARNING with the count and doc_ids of deleted chunks
        (only when at least one chunk was pruned), then sets
        ``expired_chunks_removed_last_run`` in the current collection health dict.
        This legacy field now records fully-pruned document IDs returned by
        ``SearchStore.prune_expired_chunks``; live expired-chunk counts are exposed
        separately by status via ``SearchStore.count_expired_chunks``.
        """
        if not self._config.prune_expired_chunks:
            return

        pruned_doc_ids = await self._search_store.prune_expired_chunks(collection, namespace)
        n = len(pruned_doc_ids)

        if n > 0:
            preview_doc_ids = pruned_doc_ids[:20]
            omitted_doc_ids = max(0, n - len(preview_doc_ids))
            logger.warning(
                "MaintenanceLoop: pruned %d expired document(s) from %s/%s — "
                "doc_ids_preview: %s omitted_doc_ids: %d",
                n,
                namespace,
                collection,
                preview_doc_ids,
                omitted_doc_ids,
            )

        if self._graph_store is not None:
            for doc_id in dict.fromkeys(pruned_doc_ids):
                try:
                    await self._graph_store.delete_defref_graph_by_doc(
                        collection,
                        doc_id,
                        namespace,
                        delete_doc_owned_code_symbols=True,
                    )
                except Exception as exc:
                    logger.warning(
                        "MaintenanceLoop: def/ref graph cleanup failed for %s/%s doc_id=%s: %s",
                        namespace,
                        collection,
                        doc_id,
                        exc,
                    )

        if hasattr(self, "_current_health"):
            self._current_health["expired_chunks_removed_last_run"] = n

    async def _run_graph_gc(self, collection: str, namespace: str) -> tuple[int, Any]:
        """Graph garbage collection for a single collection (BE-7).

        Algorithm:
        1. Check if graph is disabled (graph_store=None or config.graph.enabled=False) → skip
        2. Fetch live chunk IDs via store.list_chunks_raw(collection, namespace)
           On exception: WARNING logged, abort collection GC, return early
        3. Call graph_store.count_stale_mentions(collection, live_chunk_ids, namespace)
        4. Call graph_store.prune_stale_mentions(collection, live_chunk_ids, namespace)
        5. Call graph_store.delete_orphan_nodes_and_edges(collection, namespace) → GcPassResult
        6. If communities_invalidated=True:
           - Check _rebuild_state[(namespace, collection)]:
             - If task exists and not done: set pending=True, don't spawn new task
             - If task done or no task: spawn new async rebuild task with done-callback
           - Update per-collection health entry: communities_invalidated=True

        Returns: (stale_count, GcPassResult) tuple where stale_count is the count
                 measured BEFORE pruning. Returns (0, None) if skipped.
        """
        # Early exit if graph is disabled
        if self._graph_store is None:
            return (0, None)

        # Check if graph.enabled = false in config (realistic path when store is live)
        # This is a defense-in-depth check; route handlers should have already guarded this
        try:
            from archon_search.config import SearchConfig
            # We don't have direct access to the full config here, so we check a simpler marker
            # that would be set by tests or initialization
            if not getattr(self, "_config_graph_enabled", True):
                return (0, None)
        except Exception:
            pass  # Proceed on any config lookup error

        # Phase 1: Fetch live chunk IDs from the search store
        live_chunk_ids: set[str] = set()
        try:
            async for chunk in self._search_store.list_chunks_raw(collection, namespace):
                chunk_id: str = chunk.get("chunk_id", "")
                if chunk_id:
                    live_chunk_ids.add(chunk_id)
        except Exception as exc:
            logger.warning(
                "MaintenanceLoop: list_chunks_raw failed for %s/%s, skipping graph GC: %s",
                namespace,
                collection,
                exc,
            )
            return (0, None)

        # Convert to frozenset for GraphStore API
        live_chunk_ids_frozen = frozenset(live_chunk_ids)

        # Phase 2: Count stale mentions (read-only)
        stale_count = await self._graph_store.count_stale_mentions(collection, live_chunk_ids_frozen, ns=namespace)

        # Phase 3: Prune stale mentions
        pruned_count = await self._graph_store.prune_stale_mentions(collection, live_chunk_ids_frozen, ns=namespace)

        # Phase 4: Delete orphan nodes and edges
        gc_result = await self._graph_store.delete_orphan_nodes_and_edges(collection, ns=namespace)

        # Phase 5: Handle community invalidation and rebuild
        rebuild_key = (namespace, collection)
        if gc_result.communities_invalidated:
            # Check if rebuild already in flight
            if rebuild_key in self._rebuild_state:
                existing_state = self._rebuild_state[rebuild_key]
                if not existing_state.task.done():
                    # Task still running; set pending flag for re-enqueue
                    existing_state.pending = True
                else:
                    # Task completed; clear the entry and spawn a new one
                    del self._rebuild_state[rebuild_key]
                    self._spawn_rebuild_task(namespace, collection)
            else:
                # No existing rebuild; spawn a new task
                self._spawn_rebuild_task(namespace, collection)
        else:
            # No new orphans this pass. Consume any completed rebuild state entry so
            # the health update logic can reset communities_invalidated to False.
            # (completed=True means the rebuild finished; the flag may be cleared now.)
            if rebuild_key in self._rebuild_state:
                existing_state = self._rebuild_state[rebuild_key]
                if existing_state.completed:
                    del self._rebuild_state[rebuild_key]

        return (stale_count, gc_result)

    def _spawn_rebuild_task(self, namespace: str, collection: str) -> None:
        """Spawn an async community rebuild task for a collection.

        The task runs _rebuild_communities_async with CPU priority degradation.
        On completion, the done-callback checks the pending flag and either:
        - Clears the state entry (completed=True for next pass to consume)
        - Re-enqueues a new rebuild (if pending=True)
        """
        rebuild_key = (namespace, collection)
        task = asyncio.create_task(self._rebuild_communities_async(namespace, collection))

        # Create RebuildState to track this task
        rebuild_state = RebuildState(task=task, pending=False, completed=False)
        self._rebuild_state[rebuild_key] = rebuild_state

        # Attach done-callback
        def _on_rebuild_done(t: asyncio.Task[Any]) -> None:
            try:
                # If the task raised an exception, log it
                exc = t.exception()
                if exc is not None:
                    logger.error(
                        "MaintenanceLoop: community rebuild failed for %s/%s: %s",
                        namespace,
                        collection,
                        exc,
                    )
            except asyncio.CancelledError:
                pass  # Task was cancelled, no action needed

            # Check if a subsequent GC set pending=True (needs re-enqueue)
            if rebuild_key in self._rebuild_state:
                current_state = self._rebuild_state[rebuild_key]
                if current_state.pending:
                    # Re-enqueue a new rebuild task
                    current_state.pending = False
                    self._spawn_rebuild_task(namespace, collection)
                else:
                    # No pending rebuild; just mark completed for next GC pass to consume
                    current_state.completed = True

        task.add_done_callback(_on_rebuild_done)

    async def _rebuild_communities_async(self, namespace: str, collection: str) -> None:
        """Async community rebuild worker. Runs with CPU priority degradation (if enabled).

        Algorithm:
        1. Get CPU priority setting from config (if available) or default to "normal"
        2. On Linux only: attempt to set process priority
           - "low" (10), "normal" (0), "high" (-5)
           - Capture original nice value, restore in finally
           - Catch PermissionError/OSError, log WARNING, continue anyway
        3. Construct or import CommunityBuilder
        4. Call builder.build(collection, ns=namespace)
        5. Log completion
        """
        from archon_search.community_builder import CommunityBuilder

        # Get CPU priority setting
        cpu_priority = getattr(self._config, "gc_rebuild_cpu_priority", "normal")
        nice_values = {"low": 10, "normal": 0, "high": -5}
        target_nice = nice_values.get(cpu_priority, 0)

        original_nice = 0

        # Set CPU priority on Linux
        if sys.platform == "linux":
            try:
                # Capture original nice level before changing
                original_nice = os.getpriority(os.PRIO_PROCESS, 0)
            except (OSError, AttributeError) as exc:
                logger.warning(
                    "MaintenanceLoop: could not get original CPU priority: %s",
                    exc,
                )
                original_nice = 0

            try:
                os.setpriority(os.PRIO_PROCESS, 0, target_nice)
            except (OSError, AttributeError) as exc:
                logger.warning(
                    "MaintenanceLoop: could not set CPU priority to %d for community rebuild %s/%s: %s",
                    target_nice,
                    namespace,
                    collection,
                    exc,
                )

        try:
            # Build communities
            builder = CommunityBuilder(
                graph_store=self._graph_store,
                config=self._graph_config,  # GraphConfig
                search_store=self._search_store,
                enrichment_client=self._enrichment_client,
            )
            await builder.build(collection, ns=namespace)
            logger.info(
                "MaintenanceLoop: community rebuild completed for %s/%s",
                namespace,
                collection,
            )
        finally:
            # Restore CPU priority
            if sys.platform == "linux":
                try:
                    os.setpriority(os.PRIO_PROCESS, 0, original_nice)
                except (OSError, AttributeError) as exc:
                    logger.warning(
                        "MaintenanceLoop: could not restore CPU priority to %d: %s",
                        original_nice,
                        exc,
                    )

    # ------------------------------------------------------------------
    # E2f BE-5: Synonym enrichment — scheduling, debounce, and run
    # ------------------------------------------------------------------

    def schedule_synonym_enrichment(self, collection: str, ns: str) -> None:
        """Schedule synonym enrichment for a (namespace, collection) pair.

        Called by ``SearchPipeline.on_synonym_edges_written`` callback after
        each ingest when ``config.graph.enrichment_auto = True``.

        Debounce (S12): if an enrichment task is already in-flight for this
        (ns, collection), set ``pending = True`` and return without spawning a
        new task.  The done-callback on the in-flight task will re-enqueue if
        pending is set.  This prevents duplicate concurrent enrichment tasks.

        After ``_run_synonym_enrichment`` writes synonym edges, ``(ns, collection)``
        is added to ``_communities_pending_rebuild`` so that the next maintenance
        pass triggers a community rebuild (S3/S4).

        ``pipeline.py`` calls this method via ``Callable`` only — no import of
        ``MaintenanceLoop``.
        """
        key = (ns, collection)
        if key in self._synonym_state:
            existing = self._synonym_state[key]
            if not existing.task.done():
                # Task still in-flight — set pending flag for re-enqueue, return.
                existing.pending = True
                return
            # Task completed; clear the entry and spawn a fresh one below.
            del self._synonym_state[key]

        task = asyncio.create_task(
            self._run_synonym_enrichment(collection, ns)
        )
        state = RebuildState(task=task, pending=False, completed=False)
        self._synonym_state[key] = state

        def _on_enrichment_done(t: asyncio.Task[Any]) -> None:
            try:
                exc = t.exception()
                if exc is not None:
                    logger.error(
                        "MaintenanceLoop: synonym enrichment failed for %s/%s: %s",
                        ns, collection, exc,
                    )
            except asyncio.CancelledError:
                pass

            if key in self._synonym_state:
                current = self._synonym_state[key]
                if current.pending:
                    current.pending = False
                    self.schedule_synonym_enrichment(collection, ns)
                else:
                    # Enrichment done, no re-enqueue pending — clear the state entry.
                    # Entry is only needed for debounce; once done, remove it to
                    # prevent unbounded growth of _synonym_state.
                    del self._synonym_state[key]

        task.add_done_callback(_on_enrichment_done)

    async def _run_synonym_enrichment(self, collection: str, ns: str) -> None:
        """Run synonym detection for a collection and write synonym_of edges.

        Algorithm:
        1. Check prerequisites (graph_store present, config available).
        2. Call AliasLoader.load(collection, ns) → (alias_edges, skip_pairs).
        3. Instantiate SynonymDetector with the graph_store and a stub embedder.
           (Name embeddings are pre-stored on GraphNode.name_embedding; the embedder
           is kept for forward-compatibility but is not called during detect().)
        4. Call detector.detect(collection, ns=ns, skip_pairs=skip_pairs) → list[GraphEdge].
        5. If any edges found (alias + ANN combined): call
           graph_store.write_graph(collection, [], all_edges, ns=ns),
           then add (ns, collection) to _communities_pending_rebuild.

        Errors are propagated to the caller (schedule_synonym_enrichment's done-callback
        logs them as ERROR).
        """
        if self._graph_store is None or self._graph_config is None:
            return

        from archon_search.alias_loader import AliasLoader  # noqa: PLC0415
        from archon_search.config import SearchConfig  # noqa: PLC0415
        from archon_search.embedder import Embedder  # noqa: PLC0415
        from archon_search.synonym_detector import SynonymDetector  # noqa: PLC0415

        # Step 1: Load manual alias edges and produce skip_pairs to exclude from ANN.
        # S319 race: this method runs as a background task scheduled by the
        # post-ingest callback.  When two documents contain the two alias
        # entities and are ingested concurrently, the background task may
        # execute before the second graph write completes — find_nodes_by_name
        # returns zero nodes for entities not yet persisted.  A single retry
        # after a grace period gives concurrent writes time to commit.
        alias_loader = AliasLoader(config=self._graph_config, graph_store=self._graph_store)
        try:
            alias_edges, skip_pairs = await alias_loader.load(collection, ns)
            if not alias_edges and self._graph_config.alias_file:
                await asyncio.sleep(0.5)
                alias_edges, skip_pairs = await alias_loader.load(collection, ns)
        except Exception:  # noqa: BLE001
            logger.warning(
                "MaintenanceLoop: AliasLoader.load() failed for %s/%s; proceeding with ANN only",
                ns, collection, exc_info=True,
            )
            alias_edges, skip_pairs = [], set()

        # Build a minimal SearchConfig shell carrying only the graph config.
        # SynonymDetector.detect() uses only config.graph.synonym_threshold,
        # so the other fields remain at defaults.
        cfg = SearchConfig()
        cfg.graph = self._graph_config

        # SynonymDetector requires an Embedder at __init__ (forward-compatibility).
        # detect() uses stored name_embedding only and never calls encode().
        class _NullEmbedderBackend:
            model_name: str = "null-embedder"
            is_warm: bool = False

            def encode(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
                raise NotImplementedError("SynonymDetector does not call embed() — stored name_embedding only")

        embedder = Embedder(_NullEmbedderBackend())

        detector = SynonymDetector(
            graph_store=self._graph_store,
            embedder=embedder,
            config=cfg,
        )
        # Pass skip_pairs so alias pairs are not duplicated as ANN edges.
        ann_edges = await detector.detect(collection, ns=ns, skip_pairs=skip_pairs)

        all_edges = alias_edges + ann_edges
        if all_edges:
            await self._graph_store.write_graph(
                collection, [], all_edges, ns=ns
            )
            logger.info(
                "MaintenanceLoop: wrote %d synonym_of edges (%d manual, %d ANN) for %s/%s",
                len(all_edges), len(alias_edges), len(ann_edges), ns, collection,
            )
            # Trigger community rebuild only when synonym edges were written.
            # Spurious rebuilds on every ingest are wasteful — Leiden is expensive.
            self._communities_pending_rebuild.add((ns, collection))

    def _drain_communities_pending_rebuild(self) -> None:
        """Drain ``_communities_pending_rebuild`` by spawning a community rebuild task
        for each pending (ns, collection) pair.

        Called once per maintenance pass in ``_run_one_pass``.  Producer is
        ``schedule_synonym_enrichment`` / ``_run_synonym_enrichment``; this method is
        the sole consumer.  The producer ONLY adds; this method ONLY removes.
        """
        if not self._communities_pending_rebuild:
            return

        # Snapshot and clear atomically before spawning to avoid re-processing
        # entries added by concurrent enrichment tasks during the loop.
        pending = set(self._communities_pending_rebuild)
        self._communities_pending_rebuild -= pending

        for ns, collection in pending:
            self._spawn_rebuild_task(ns, collection)
            logger.debug(
                "MaintenanceLoop: triggered community rebuild for %s/%s "
                "after synonym enrichment",
                ns, collection,
            )

    # ------------------------------------------------------------------
    # E2g BE-7: PageRank recompute — scheduling, debounce, and run
    # ------------------------------------------------------------------

    def schedule_pagerank_recompute(self, collection: str, ns: str) -> None:
        """Schedule a PageRank recompute for a (namespace, collection) pair.

        Called by ``SearchPipeline.on_defref_edges_written`` after any ingest
        that writes new code-symbol nodes/edges via the E2g BE-3 def/ref write
        (never for prose-only ingests that only wrote E1a co-occurrence edges).

        Debounce: mirrors ``schedule_synonym_enrichment`` exactly — if a
        recompute task is already in-flight for this (ns, collection), set
        ``pending = True`` and return without spawning a new task. The
        done-callback on the in-flight task re-enqueues if pending is set.

        ``pipeline.py`` calls this method via ``Callable`` only — no import of
        ``MaintenanceLoop``.
        """
        key = (ns, collection)
        if key in self._pagerank_state:
            existing = self._pagerank_state[key]
            if not existing.task.done():
                existing.pending = True
                return
            del self._pagerank_state[key]

        task = asyncio.create_task(
            self._run_pagerank_recompute(collection, ns)
        )
        state = RebuildState(task=task, pending=False, completed=False)
        self._pagerank_state[key] = state

        def _on_pagerank_done(t: asyncio.Task[Any]) -> None:
            try:
                exc = t.exception()
                if exc is not None:
                    logger.error(
                        "MaintenanceLoop: PageRank recompute failed for %s/%s: %s",
                        ns, collection, exc,
                    )
            except asyncio.CancelledError:
                pass

            if key in self._pagerank_state:
                current = self._pagerank_state[key]
                if current.pending:
                    current.pending = False
                    self.schedule_pagerank_recompute(collection, ns)
                else:
                    del self._pagerank_state[key]

        task.add_done_callback(_on_pagerank_done)

    async def _run_pagerank_recompute(self, collection: str, ns: str) -> None:
        """Compute and persist PageRank scores for a collection (E2g BE-7).

        No-op when ``graph_store`` is absent. Errors propagate to the caller
        (``schedule_pagerank_recompute``'s done-callback logs them as ERROR).
        """
        if self._graph_store is None:
            return

        from archon_search.pagerank_builder import PageRankBuilder  # noqa: PLC0415

        builder = PageRankBuilder(self._graph_store)
        await builder.build(collection, ns)

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
                health[col_key] = dict(_EMPTY_HEALTH_ENTRY)
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

        # Accumulate stale mention counts from per-collection GC runs.
        per_collection_stale_counts: list[int] = []

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

            try:
                await self._run_expired_chunk_pruning(col, ns)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "MaintenanceLoop: _run_expired_chunk_pruning failed for %s: %s", key, exc
                )
                col_health["last_error"] = str(exc)

            try:
                gc_stale_count, _gc_result = await self._run_graph_gc(col, ns)
                per_collection_stale_counts.append(gc_stale_count)
                # Update communities_invalidated from gc_result if nodes were removed
                if _gc_result is not None and getattr(_gc_result, "communities_invalidated", False):
                    col_health["communities_invalidated"] = True
                else:
                    # Reset communities_invalidated when no new orphans were found.
                    # Two cases:
                    # 1. No rebuild in flight (key absent) — clear immediately.
                    # 2. Rebuild completed since last pass (completed=True) — consume the flag,
                    #    remove the entry, and clear communities_invalidated.
                    rebuild_key = (ns, col)
                    existing = self._rebuild_state.get(rebuild_key)
                    if existing is None:
                        col_health["communities_invalidated"] = False
                    elif existing.completed:
                        # Rebuild finished; consume the completed entry and reset the flag.
                        del self._rebuild_state[rebuild_key]
                        col_health["communities_invalidated"] = False
                    # else: rebuild still in flight — keep communities_invalidated as-is
                    #       (the state file's value from Phase 5 of the previous pass remains).
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "MaintenanceLoop: _run_graph_gc failed for %s: %s", key, exc
                )
                col_health["last_error"] = str(exc)

            health[key] = col_health

        # After the per-collection loop, record the timestamp of the prune pass when
        # the policy is enabled; preserve the previous value when disabled.
        if self._config.prune_expired_chunks:
            last_expired_pruned_at: str | None = now_str
        else:
            last_expired_pruned_at = state.get("last_expired_pruned_at")

        # Record last_graph_gc_at when graph GC ran
        last_graph_gc_at: str | None = state.get("last_graph_gc_at")
        if self._graph_store is not None:
            last_graph_gc_at = now_str

        # Aggregate stale mention counts from per-collection GC runs.
        # The spec requires SUM — not last-collection-wins, average, or max.
        stale_mention_count = sum(per_collection_stale_counts)

        # E2f BE-5: drain _communities_pending_rebuild — spawn community rebuild tasks
        # for any (ns, collection) pair added by synonym enrichment since the last pass.
        # This is separate from the GC-triggered rebuild path (_run_graph_gc) and runs
        # once per pass regardless of which collections had enrichment.
        try:
            self._drain_communities_pending_rebuild()
        except Exception as exc:  # noqa: BLE001
            logger.error("MaintenanceLoop: _drain_communities_pending_rebuild failed: %s", exc)

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
            "last_expired_pruned_at": last_expired_pruned_at,
            "last_graph_gc_at": last_graph_gc_at,
            "stale_mention_count": stale_mention_count,
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

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from archon_search._durable_io import atomic_write_json

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

    Owns the ``.maintenance-state.json`` file. Does not perform the
    maintenance work itself — concrete policies are implemented in
    later tasks (BE-5 FTS, BE-6 orphan cleanup, BE-8 retry).
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
    # Per-pass policies (stubs — implemented by BE-5, BE-6, BE-8)
    # ------------------------------------------------------------------

    async def _run_fts_optimize(self, collection: str, namespace: str) -> None:
        """FTS optimize for a single collection. Implemented by BE-5."""

    async def _run_orphan_cleanup(self, collection: str, namespace: str) -> None:
        """Orphan chunk cleanup for a single collection. Implemented by BE-6."""

    async def _run_failed_ingest_retry(self) -> None:
        """Pass-level failed-ingest retry. Implemented by BE-8."""

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

            # Collect O(1) metadata values.
            meta_chunk_count = 0
            try:
                meta = await self._search_store.get_collection_meta(col, ns)
                if meta is not None:
                    meta_chunk_count = meta.chunk_count
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "MaintenanceLoop: get_collection_meta failed for %s: %s", key, exc
                )

            col_health["meta_chunk_count"] = meta_chunk_count

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
        try:
            await self._run_failed_ingest_retry()
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

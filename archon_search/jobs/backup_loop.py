"""BackupLoop — in-process scheduled backup orchestrator.

The ``BackupLoop`` runs two cooperative coroutines under ``asyncio.gather``:

* ``_trigger_loop`` — enumerates collections per namespace and enqueues
  ``source="backup"`` export jobs through ``JobStore.create_export``. It honours
  ``BackupConfig.interval_hours`` (``0`` disables periodic ticks but keeps the
  completion loop active to drain any pre-existing in-flight jobs), the
  ``exclude`` patterns, and a two-stage dedup check (in-flight + already queued).
* ``_completion_loop`` — polls in-flight jobs; on ``DONE`` it updates the
  ``.backup-state.json`` last-backup-at map and triggers rotation; on
  ``FAILED``/``CANCELLED`` it logs and drops the job from the tracker.

State is persisted in ``data_dir / ".backup-state.json"`` as a JSON object of
``"{namespace}/{collection}" -> ISO-8601 timestamp``. Writes are atomic
(temp file + ``os.replace``).

See ``Documentation/Backlog/D2-scheduled-backup-plan.md`` Task 3.1 for the
contract and acceptance criteria.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from archon_search._durable_io import atomic_write_json
from archon_search.jobs.model import JobStatus

if TYPE_CHECKING:
    from archon_search.config import BackupConfig
    from archon_search.jobs.store import JobStore
    from archon_search.store import SearchStore
    from archon_search.types import ExportJob, ImportJob

logger = logging.getLogger(__name__)

# Polling interval for the completion loop. Independent of the trigger interval:
# backups take long enough that minute-resolution drain is plenty.
_BACKUP_COMPLETION_POLL_SECONDS: int = 60
_SECONDS_PER_HOUR: int = 3600
# Archive timestamp format — second-precision, filesystem-safe, lexicographically
# sortable so a plain ``sorted()`` orders archives oldest-to-newest.
_ARCHIVE_TIMESTAMP_FORMAT: str = "%Y%m%dT%H%M%SZ"


class BackupLoop:
    """Drives scheduled per-collection backups.

    Owns the ``.backup-state.json`` file and the in-flight job tracker. Does not
    perform the export itself — that work is delegated to the regular export
    worker via ``JobStore.create_export(..., source="backup")``.
    """

    def __init__(
        self,
        job_store: "JobStore",
        search_store: "SearchStore",
        config: "BackupConfig",
        data_dir: Path,
    ) -> None:
        self._job_store = job_store
        self._search_store = search_store
        self._config = config
        self._state_file: Path = data_dir / ".backup-state.json"
        # job_id -> (namespace, collection)
        self._in_flight: dict[str, tuple[str, str]] = {}
        self._last_tick_at: str | None = None

    # ------------------------------------------------------------------
    # In-flight tracking
    # ------------------------------------------------------------------

    def is_collection_in_flight(self, ns: str, col: str) -> bool:
        target = (ns, col)
        return any(v == target for v in self._in_flight.values())

    def track(self, job_id: str, ns: str, col: str) -> None:
        self._in_flight[job_id] = (ns, col)

    # ------------------------------------------------------------------
    # Exclusion
    # ------------------------------------------------------------------

    def _is_excluded(self, ns: str, col: str) -> bool:
        """Match against ``config.exclude`` patterns.

        Patterns are either bare ``"{col}"`` (matches the collection in any
        namespace) or qualified ``"{ns}/{col}"`` (matches only that namespace).
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

    def _load_state(self) -> dict[str, str]:
        """Read the last-backup-at map. Returns ``{}`` on missing or corrupt file."""
        if not self._state_file.exists():
            return {}
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("BackupLoop: state file %s unreadable (%s); treating as empty",
                           self._state_file, exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        # Coerce to str→str; drop any malformed entries silently.
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, (str, int, float))}

    def _save_state(self, state: dict[str, str]) -> None:
        """Atomic durable write of the state map via the shared helper."""
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._state_file, state)

    # ------------------------------------------------------------------
    # Rotation
    # ------------------------------------------------------------------

    def _rotate(self, ns: str, col: str) -> None:
        """Delete all but the ``keep`` most recent archives for ``(ns, col)``.

        ``keep == 0`` disables rotation entirely.
        """
        keep = self._config.keep
        if keep == 0:
            return
        ns_dir = Path(self._config.output_dir) / ns
        if not ns_dir.exists():
            return
        # Archive name format: ``{col}.backup.{YYYYmmddTHHMMSSZ}.tar.gz`` —
        # lexicographic sort = chronological sort.
        archives = sorted(ns_dir.glob(f"{col}.backup.*.tar.gz"))
        to_delete = archives[:-keep] if len(archives) > keep else []
        for path in to_delete:
            try:
                path.unlink()
                logger.info("Rotation: deleted backup archive %s", path)
            except OSError as exc:
                logger.warning("Rotation: failed to delete %s: %s", path, exc)

    # ------------------------------------------------------------------
    # Trigger loop
    # ------------------------------------------------------------------

    async def _run_one_tick(self) -> None:
        """Single pass: enumerate collections, dedup, enqueue backup jobs.

        Logs and swallows any exception so the surrounding loop keeps running.
        Updates ``_last_tick_at`` even when the tick fails so observability
        reflects that the scheduler is alive.
        """
        self._last_tick_at = datetime.now(timezone.utc).isoformat()
        try:
            collections = await self._search_store.list_collections()
        except Exception as exc:  # noqa: BLE001 — survive any backend hiccup
            logger.error("BackupLoop: list_collections failed during tick: %s", exc)
            return

        try:
            queued = self._job_store.list_queued_bulk()
        except Exception as exc:  # noqa: BLE001
            logger.error("BackupLoop: list_queued_bulk failed during tick: %s", exc)
            return
        # Build a set of (ns, col) for already-queued backup jobs — O(1) dedup
        # without re-scanning the queue per collection.
        queued_backup_keys: set[tuple[str, str]] = {
            (j.namespace, j.collection)
            for j in queued
            if getattr(j, "source", "user") == "backup"
        }

        ts = datetime.now(timezone.utc).strftime(_ARCHIVE_TIMESTAMP_FORMAT)
        for info in collections:
            ns = info.namespace
            col = info.name
            if self._is_excluded(ns, col):
                continue
            if self.is_collection_in_flight(ns, col):
                continue
            if (ns, col) in queued_backup_keys:
                continue
            archive_path = str(Path(self._config.output_dir) / ns / f"{col}.backup.{ts}.tar.gz")
            tmp_path = f"{archive_path}.tmp"
            try:
                job = self._job_store.create_export(
                    col, archive_path, tmp_path, namespace=ns, source="backup"
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("BackupLoop: failed to enqueue backup for %s/%s: %s", ns, col, exc)
                continue
            self.track(job.job_id, ns, col)
            logger.info("BackupLoop: enqueued backup job %s for %s/%s", job.job_id, ns, col)

    def _startup_overdue_check(self) -> bool:
        """Return True if any persisted collection is overdue (interval elapsed).

        Used at startup so a restarted server doesn't wait a full interval before
        catching up. Always False when ``interval_hours == 0``.
        """
        if self._config.interval_hours <= 0:
            return False
        state = self._load_state()
        if not state:
            return False
        threshold = timedelta(hours=self._config.interval_hours)
        now = datetime.now(timezone.utc)
        for _, ts_str in state.items():
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            if now - ts >= threshold:
                return True
        return False

    async def _trigger_loop(self) -> None:
        """Periodic trigger loop. Exits immediately when ``interval_hours == 0``.

        With ``interval_hours > 0``: fires an immediate tick on startup if any
        persisted collection is overdue, then ticks every ``interval_hours``.
        """
        if self._config.interval_hours <= 0:
            return
        if self._startup_overdue_check():
            await self._run_one_tick()
        sleep_seconds = self._config.interval_hours * _SECONDS_PER_HOUR
        while True:
            await asyncio.sleep(sleep_seconds)
            await self._run_one_tick()

    # ------------------------------------------------------------------
    # Completion loop
    # ------------------------------------------------------------------

    def _drain_completed(self) -> None:
        """Sync helper: inspect each in-flight job, react to terminal state.

        Extracted so the completion loop is trivially testable without
        spinning up asyncio.
        """
        # Snapshot to allow mutation during iteration.
        for job_id, (ns, col) in list(self._in_flight.items()):
            job: ExportJob | ImportJob | None = self._job_store.get(job_id)  # type: ignore[assignment]
            if job is None:
                # Job evicted or never persisted — drop the tracker entry.
                self._in_flight.pop(job_id, None)
                continue
            status = job.status
            if status == JobStatus.DONE:
                state = self._load_state()
                state[f"{ns}/{col}"] = job.updated_at
                self._save_state(state)
                self._rotate(ns, col)
                output = getattr(job, "output_path", "") or getattr(job, "archive_path", "")
                logger.info("Backup completed for %s/%s; archive: %s", ns, col, output)
                self._in_flight.pop(job_id, None)
            elif status == JobStatus.FAILED:
                logger.error("Backup failed for %s/%s: %s", ns, col, job.error)
                self._in_flight.pop(job_id, None)
            elif status == JobStatus.CANCELLED:
                logger.info("Backup cancelled for %s/%s", ns, col)
                self._in_flight.pop(job_id, None)
            # PENDING / QUEUED / RUNNING / CANCELLING — leave in tracker.

    async def _completion_loop(self) -> None:
        while True:
            try:
                self._drain_completed()
            except Exception as exc:  # noqa: BLE001
                logger.error("BackupLoop: completion drain failed: %s", exc)
            await asyncio.sleep(_BACKUP_COMPLETION_POLL_SECONDS)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run trigger + completion loops concurrently."""
        await asyncio.gather(self._trigger_loop(), self._completion_loop())

"""Bulk job scheduler — promotes QUEUED export/import jobs to RUNNING."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from archon_search.jobs.store import JobStore
from archon_search.types import ExportJob, ImportJob, JobStatus, MigrationJob

logger = logging.getLogger(__name__)

_SCHEDULER_TICK_SECONDS: int = 5


class JobScheduler:
    """5-second tick scheduler for bulk (export/import) jobs.

    On each tick it promotes up to ``max_concurrent`` QUEUED bulk jobs to
    RUNNING and calls ``dispatch_fn`` for each promoted job. The caller is
    responsible for creating an ``asyncio.Task`` inside ``dispatch_fn`` and
    registering it via ``register_task()`` so the scheduler can track active
    concurrency.
    """

    def __init__(
        self,
        store: JobStore,
        max_concurrent: int,
        dispatch_fn: Callable[[ExportJob | ImportJob | MigrationJob], None],
    ) -> None:
        self._store = store
        self._max_concurrent = max_concurrent
        # ``dispatch_fn`` is a public, reassignable attribute so the FastAPI
        # lifespan can install the real export/import dispatch closure once
        # ``app.state`` (search_store, pipeline, embedder_cache) is ready.
        # ``_tick()`` reads it via attribute access, not a closed-over local.
        self.dispatch_fn = dispatch_fn
        self._active: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def active_count(self) -> int:
        """Number of tasks currently tracked (including completed ones not yet GC'd)."""
        return len(self._active)

    def register_task(self, task: asyncio.Task) -> None:
        """Register a task created by dispatch_fn; it is auto-removed when done."""
        self._active.add(task)
        task.add_done_callback(self._active.discard)

    async def run(self) -> None:
        """Infinite tick loop; exits cleanly on CancelledError."""
        try:
            while True:
                await asyncio.sleep(_SCHEDULER_TICK_SECONDS)
                self._tick()
        except asyncio.CancelledError:
            logger.debug("JobScheduler: cancelled, stopping tick loop")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _active_count_running(self) -> int:
        """Count of non-done tasks (i.e. tasks still running)."""
        return sum(1 for t in self._active if not t.done())

    def _tick(self) -> None:
        """Single scheduler tick: promote QUEUED jobs up to max_concurrent."""
        queued = self._store.list_queued_bulk()
        slots = max(0, self._max_concurrent - self._active_count_running)
        for job in queued[:slots]:
            promoted = self._store.transition(job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
            if promoted is None:
                # Another tick or concurrent caller beat us — skip this job.
                continue
            try:
                self.dispatch_fn(promoted)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "JobScheduler: dispatch_fn raised for job %s — marking FAILED: %s",
                    job.job_id,
                    exc,
                )
                try:
                    self._store.update(
                        job.job_id,
                        status=JobStatus.FAILED,
                        error=f"dispatch_failed: {exc}",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "JobScheduler: failed to mark job %s as FAILED after dispatch error",
                        job.job_id,
                    )

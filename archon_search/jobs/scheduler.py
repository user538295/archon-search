"""Bulk job scheduler — promotes QUEUED export/import jobs to RUNNING."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from archon_search.jobs.store import JobStore
from archon_search.types import ExportJob, ImportJob, JobStatus, MigrationJob

logger = logging.getLogger(__name__)

_SCHEDULER_TICK_SECONDS: int = 5
#: Wire-facing job ``error`` (served by GET /jobs) when ``dispatch_fn`` raises; the exception text can
#: embed server paths and library internals, so it is logged only.
DISPATCH_FAILED_DETAIL = "dispatch failed; see the server log for details"


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
        # Job ids whose QUEUED -> RUNNING claim failed on their latest attempt: only the first failure of
        # such a streak logs a traceback, so a persistently failing disk does not flood the log every tick.
        self._claim_failures: set[str] = set()

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
        self._claim_failures.intersection_update(job.job_id for job in queued)  # forget jobs no longer QUEUED
        slots = max(0, self._max_concurrent - self._active_count_running)
        for job in queued[:slots]:
            try:
                promoted = self._store.transition(job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
            except Exception:  # noqa: BLE001 — the claim did not happen; the job stays QUEUED for a later tick
                self._log_claim_failure(job.job_id)
                continue
            self._claim_failures.discard(job.job_id)
            if promoted is None:
                # Another tick or concurrent caller beat us — skip this job.
                continue
            try:
                self.dispatch_fn(promoted)
            except Exception:  # noqa: BLE001
                logger.exception("JobScheduler: dispatch_fn raised for job %s — marking FAILED", job.job_id)
                try:
                    self._store.update(
                        job.job_id,
                        status=JobStatus.FAILED,
                        error=DISPATCH_FAILED_DETAIL,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "JobScheduler: failed to mark job %s as FAILED after dispatch error",
                        job.job_id,
                    )

    def _log_claim_failure(self, job_id: str) -> None:
        """Log a failed claim: with its traceback on the first failure of a streak, as one line after that."""
        if job_id in self._claim_failures:
            logger.warning("JobScheduler: persisting job %s as RUNNING failed again — still QUEUED", job_id)
            return
        self._claim_failures.add(job_id)
        logger.exception("JobScheduler: failed to persist job %s as RUNNING — leaving it QUEUED", job_id)

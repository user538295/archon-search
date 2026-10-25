"""Unit tests for JobScheduler — Task 3.1."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from archon_search.jobs.scheduler import (
    _SCHEDULER_TICK_SECONDS,
    DISPATCH_FAILED_DETAIL,
    JobScheduler,
)
from archon_search.jobs.store import JobStore
from archon_search.types import ExportJob, ImportJob, JobStatus
from tests._job_store_faults import (
    JOB_WRITE_FAILURES,
    failing_job_writes,
    persisted_statuses,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Upper bound for run() to dispatch a job once its writes succeed again (normally a few 10 ms ticks).
_RUN_DISPATCH_TIMEOUT_SECONDS = 30.0


def _make_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def _noop_dispatch(job: ExportJob | ImportJob) -> None:
    """Dispatch closure that does nothing (simulates deferred worker)."""


def _make_export_job(store: JobStore, collection: str = "col") -> ExportJob:
    return store.create_export(
        collection=collection,
        output_path="/tmp/out.tar.gz",
        tmp_path="/tmp/out.jsonl.tmp",
    )


def _make_import_job(store: JobStore, collection: str = "col") -> ImportJob:
    return store.create_import(
        collection=collection,
        archive_path="/tmp/archive.tar.gz",
        force_overwrite=False,
        ignore_schema_version=False,
        on_error="fail",
    )


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

def test_scheduler_tick_seconds_is_int() -> None:
    assert isinstance(_SCHEDULER_TICK_SECONDS, int)
    assert _SCHEDULER_TICK_SECONDS > 0


# ---------------------------------------------------------------------------
# test_tick_promotes_queued_to_running
# ---------------------------------------------------------------------------

def test_tick_promotes_queued_to_running(tmp_path: Path) -> None:
    """Two QUEUED bulk jobs, max_concurrent=1, first tick promotes exactly one."""
    store = _make_store(tmp_path)
    job1 = _make_export_job(store, "col1")
    job2 = _make_import_job(store, "col2")

    dispatched: list[ExportJob | ImportJob] = []

    def dispatch(job: ExportJob | ImportJob) -> None:
        dispatched.append(job)

    scheduler = JobScheduler(store=store, max_concurrent=1, dispatch_fn=dispatch)
    scheduler._tick()

    # Exactly one job promoted to RUNNING
    assert len(dispatched) == 1
    promoted_id = dispatched[0].job_id
    assert store.get(promoted_id).status == JobStatus.RUNNING  # type: ignore[union-attr]

    # The other job remains QUEUED
    other_id = job2.job_id if promoted_id == job1.job_id else job1.job_id
    assert store.get(other_id).status == JobStatus.QUEUED  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# test_tick_respects_max_concurrent
# ---------------------------------------------------------------------------

def test_tick_respects_max_concurrent(tmp_path: Path) -> None:
    """max_concurrent=2, three QUEUED jobs, first tick promotes exactly two."""
    store = _make_store(tmp_path)
    _make_export_job(store, "col1")
    _make_import_job(store, "col2")
    _make_export_job(store, "col3")

    dispatched: list[ExportJob | ImportJob] = []

    def dispatch(job: ExportJob | ImportJob) -> None:
        dispatched.append(job)

    scheduler = JobScheduler(store=store, max_concurrent=2, dispatch_fn=dispatch)
    scheduler._tick()

    assert len(dispatched) == 2


# ---------------------------------------------------------------------------
# test_tick_does_nothing_when_slots_full
# ---------------------------------------------------------------------------

def test_tick_does_nothing_when_slots_full(tmp_path: Path) -> None:
    """active count == max_concurrent; no promotion."""
    store = _make_store(tmp_path)
    _make_export_job(store, "col1")

    dispatched: list[ExportJob | ImportJob] = []

    def dispatch(job: ExportJob | ImportJob) -> None:
        dispatched.append(job)

    scheduler = JobScheduler(store=store, max_concurrent=1, dispatch_fn=dispatch)

    # Simulate one active task
    mock_task: asyncio.Task = MagicMock(spec=asyncio.Task)
    mock_task.done.return_value = False
    scheduler.register_task(mock_task)

    scheduler._tick()

    assert len(dispatched) == 0


# ---------------------------------------------------------------------------
# test_tick_fifo_ordering
# ---------------------------------------------------------------------------

def test_tick_fifo_ordering(tmp_path: Path) -> None:
    """Promotes older job first (by created_at)."""
    import time

    store = _make_store(tmp_path)
    job1 = _make_export_job(store, "col1")
    time.sleep(0.01)  # ensure different timestamps
    _make_export_job(store, "col2")

    dispatched: list[ExportJob | ImportJob] = []

    def dispatch(job: ExportJob | ImportJob) -> None:
        dispatched.append(job)

    scheduler = JobScheduler(store=store, max_concurrent=1, dispatch_fn=dispatch)
    scheduler._tick()

    assert len(dispatched) == 1
    assert dispatched[0].job_id == job1.job_id


# ---------------------------------------------------------------------------
# test_cancelled_error_exits_run
# ---------------------------------------------------------------------------

def test_cancelled_error_exits_run(tmp_path: Path) -> None:
    """run() handles asyncio.CancelledError without raising."""
    store = _make_store(tmp_path)
    scheduler = JobScheduler(store=store, max_concurrent=1, dispatch_fn=_noop_dispatch)

    async def _run_and_cancel() -> None:
        task = asyncio.create_task(scheduler.run())
        await asyncio.sleep(0)  # let coroutine start
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pytest.fail("run() propagated CancelledError — it should swallow it")

    asyncio.run(_run_and_cancel())


# ---------------------------------------------------------------------------
# test_active_count_decrements_on_task_done
# ---------------------------------------------------------------------------

def test_active_count_decrements_on_task_done(tmp_path: Path) -> None:
    """Task completion decrements active count."""
    store = _make_store(tmp_path)
    scheduler = JobScheduler(store=store, max_concurrent=2, dispatch_fn=_noop_dispatch)

    async def _noop_coro() -> None:
        return

    async def _run() -> None:
        task = asyncio.create_task(_noop_coro())
        scheduler.register_task(task)
        assert scheduler.active_count == 1
        await task  # let it complete
        # The done-callback fires synchronously on task completion when awaited in the
        # same event loop iteration; give one more yield to let callbacks run.
        await asyncio.sleep(0)
        assert scheduler.active_count == 0

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# test_dispatch_failure_transitions_job_to_failed
# ---------------------------------------------------------------------------

def test_dispatch_failure_transitions_job_to_failed(tmp_path: Path) -> None:
    """If dispatch_fn raises, the job is marked FAILED with the sanitized error and the scheduler continues."""
    store = _make_store(tmp_path)
    job = _make_export_job(store, "col1")

    call_count = 0

    def failing_dispatch(j: ExportJob | ImportJob) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("worker_unavailable /secret/path")

    scheduler = JobScheduler(store=store, max_concurrent=1, dispatch_fn=failing_dispatch)
    scheduler._tick()  # should not raise

    assert call_count == 1
    failed = store.get(job.job_id)
    assert failed is not None
    assert failed.status == JobStatus.FAILED
    # The wire-facing job error never carries the exception text (it can embed internals).
    error = failed.error or ""
    assert error == DISPATCH_FAILED_DETAIL
    assert "worker_unavailable" not in error


# ---------------------------------------------------------------------------
# QUEUED -> RUNNING claim whose persist fails
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("write_error", JOB_WRITE_FAILURES)
def test_tick_transition_write_failure_does_not_dispatch_and_keeps_job_queued(
    tmp_path: Path, write_error: Exception
) -> None:
    """A failed QUEUED->RUNNING persist (any exception, not only OSError) means the claim did not happen: the
    tick must not raise, must not dispatch, and must leave the job QUEUED (memory == disk) so a later tick can
    claim it.

    The failing write raises after reaching disk (RUNNING in the file); the corrective rewrite then restores
    QUEUED there — so the on-disk assertion is not vacuously satisfied by an untouched file."""
    store = _make_store(tmp_path)
    job = _make_export_job(store, "col1")
    dispatched: list[ExportJob | ImportJob] = []
    scheduler = JobScheduler(store=store, max_concurrent=1, dispatch_fn=dispatched.append)

    with failing_job_writes(count=1, exc=write_error, fail_after_write=1) as writes:
        scheduler._tick()  # must not raise

    assert writes.statuses_of(job.job_id) == [JobStatus.RUNNING.value, JobStatus.QUEUED.value]

    assert dispatched == []
    in_memory = store.get(job.job_id)
    assert in_memory is not None
    assert in_memory.status == JobStatus.QUEUED
    assert persisted_statuses(tmp_path / "jobs.json")[job.job_id] == JobStatus.QUEUED.value


def _claim_failure_records(caplog: pytest.LogCaptureFixture, job_id: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "archon_search.jobs.scheduler" and job_id in r.getMessage()]


def test_repeated_claim_failure_logs_traceback_once_per_streak(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A job whose claim keeps failing must not flood the log with a traceback every tick: the first failure
    of a streak logs the traceback, later ones one WARNING line. Across ALL loggers (the JobStore's failed
    corrective rewrite included) the two failing ticks emit exactly one traceback-bearing record."""
    store = _make_store(tmp_path)
    job = _make_export_job(store, "col1")
    dispatched: list[ExportJob | ImportJob] = []
    scheduler = JobScheduler(store=store, max_concurrent=1, dispatch_fn=dispatched.append)
    caplog.set_level(logging.WARNING)

    with failing_job_writes():
        scheduler._tick()
        scheduler._tick()

    records = _claim_failure_records(caplog, job.job_id)
    assert len(records) == 2
    assert records[0].levelno == logging.ERROR
    assert records[0].exc_info is not None
    assert records[1].levelno == logging.WARNING
    assert records[1].exc_info is None
    store_rewrite_warnings = [r for r in caplog.records if r.name == "archon_search.jobs.store"]
    assert len(store_rewrite_warnings) == 2  # one per failing tick: the store did log, just without a traceback
    assert [r for r in caplog.records if r.exc_info] == [records[0]]

    scheduler._tick()  # writes succeed again: the claim succeeds
    assert [j.job_id for j in dispatched] == [job.job_id]


def test_claim_failure_after_a_successful_claim_logs_traceback_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A new failure streak (after a successful claim) logs its first failure with the traceback again."""
    store = _make_store(tmp_path)
    first = _make_export_job(store, "col1")
    dispatched: list[ExportJob | ImportJob] = []
    scheduler = JobScheduler(store=store, max_concurrent=1, dispatch_fn=dispatched.append)
    caplog.set_level(logging.WARNING, logger="archon_search.jobs.scheduler")

    with failing_job_writes():
        scheduler._tick()
    scheduler._tick()
    store.update(first.job_id, status=JobStatus.QUEUED)  # re-queued (as a resume would)
    caplog.clear()
    with failing_job_writes():
        scheduler._tick()

    (record,) = _claim_failure_records(caplog, first.job_id)
    assert record.exc_info is not None


def test_run_survives_transition_write_failure_and_dispatches_on_a_later_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failed claim write must neither kill the run() loop (nothing restarts it — every QUEUED bulk job
    would then wait forever) nor strand the job: once writes succeed again, a later tick dispatches it."""
    import archon_search.jobs.scheduler as scheduler_module

    store = _make_store(tmp_path)
    job = _make_export_job(store, "col1")
    dispatched: list[ExportJob | ImportJob] = []
    monkeypatch.setattr(scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.01)

    async def _run() -> asyncio.Task:
        dispatched_event = asyncio.Event()

        def _dispatch(j: ExportJob | ImportJob) -> None:
            dispatched.append(j)
            dispatched_event.set()

        scheduler = JobScheduler(store=store, max_concurrent=1, dispatch_fn=_dispatch)
        task = asyncio.create_task(scheduler.run())
        # Generous cap: a contended CI worker may be slow; the wait ends as soon as the dispatch happens. If
        # run() died instead, the wait times out and the assertions below report the loop's exception.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(dispatched_event.wait(), timeout=_RUN_DISPATCH_TIMEOUT_SECONDS)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return task

    with failing_job_writes(count=1) as writes:
        task = asyncio.run(_run())

    # The failed claim (RUNNING attempted), its corrective rewrite (QUEUED restored), a later tick's claim.
    assert writes.statuses_of(job.job_id) == [
        JobStatus.RUNNING.value,
        JobStatus.QUEUED.value,
        JobStatus.RUNNING.value,
    ]
    assert task.cancelled() or task.exception() is None, f"run() loop died: {task.exception()!r}"
    assert [j.job_id for j in dispatched] == [job.job_id]


def test_failed_claim_does_not_stop_the_tick_from_claiming_the_next_job(tmp_path: Path) -> None:
    """A failed claim skips only that job: the same tick still claims and dispatches the next QUEUED job."""
    store = _make_store(tmp_path)
    first = _make_export_job(store, "col1")
    second = _make_export_job(store, "col2")
    dispatched: list[ExportJob | ImportJob] = []
    scheduler = JobScheduler(store=store, max_concurrent=2, dispatch_fn=dispatched.append)

    with failing_job_writes(count=1):  # only the first claim's write fails
        scheduler._tick()

    assert [j.job_id for j in dispatched] == [second.job_id]
    assert store.get(first.job_id).status == JobStatus.QUEUED  # type: ignore[union-attr]


def test_claim_failure_streak_is_forgotten_once_the_job_leaves_queued(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A job that leaves QUEUED without being claimed (e.g. marked FAILED elsewhere) is pruned from the
    streak on the next tick: if it is later re-queued, its next claim failure starts a new streak and logs
    the traceback again instead of the one-line repeat."""
    store = _make_store(tmp_path)
    job = _make_export_job(store, "col1")
    scheduler = JobScheduler(store=store, max_concurrent=1, dispatch_fn=_noop_dispatch)
    caplog.set_level(logging.WARNING, logger="archon_search.jobs.scheduler")

    with failing_job_writes():
        scheduler._tick()
    store.update(job.job_id, status=JobStatus.FAILED)
    scheduler._tick()  # the job is no longer QUEUED: this tick prunes it from the streak
    store.update(job.job_id, status=JobStatus.QUEUED)
    caplog.clear()
    with failing_job_writes():
        scheduler._tick()

    (record,) = _claim_failure_records(caplog, job.job_id)
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None

"""Unit tests for JobScheduler — Task 3.1."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from archon_search.jobs.scheduler import JobScheduler, _SCHEDULER_TICK_SECONDS
from archon_search.jobs.store import JobStore
from archon_search.types import ExportJob, ImportJob, JobStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    """If dispatch_fn raises, the job is marked FAILED and the scheduler continues."""
    store = _make_store(tmp_path)
    job = _make_export_job(store, "col1")

    call_count = 0

    def failing_dispatch(j: ExportJob | ImportJob) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("worker_unavailable")

    scheduler = JobScheduler(store=store, max_concurrent=1, dispatch_fn=failing_dispatch)
    scheduler._tick()  # should not raise

    assert call_count == 1
    assert store.get(job.job_id).status == JobStatus.FAILED  # type: ignore[union-attr]
    assert "dispatch_failed" in (store.get(job.job_id).error or "")  # type: ignore[union-attr]

"""BE-34 (C10, S85): how a describe job ends when its task is cancelled, and that a job already over is left alone."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from archon_search.description_lifecycle import DescribeSettlement
from archon_search.jobs.store import JobStore
from archon_search.server.routes_collections import (
    DESCRIBE_SHUTTING_DOWN_DETAIL,
    _describe_task,
    _spawn_describe_task,
)
from archon_search.server.routes_jobs import INTERNAL_ERROR_DETAIL
from archon_search.types import JobStatus

_SECRET = "text of a chunk the exception echoes"


class _NeverCalledLifecycle:
    async def generate_now(self, collection: str, namespace: str) -> DescribeSettlement:
        raise AssertionError("the task was cancelled before its first step")


class _BrokenLifecycle:
    async def generate_now(self, collection: str, namespace: str) -> DescribeSettlement:
        raise RuntimeError(_SECRET)


async def _spawn_and_cancel_before_the_first_step(store: JobStore, job_id: str) -> SimpleNamespace:
    """Spawn the job's task the way the route does and cancel it before the loop ever ran it."""
    app_state = SimpleNamespace(_background_tasks=set())
    _spawn_describe_task(app_state, store.get(job_id), store, _NeverCalledLifecycle())  # type: ignore[arg-type]
    [task] = app_state._background_tasks
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)  # done-callbacks run on the next loop iteration
    assert task.cancelled()
    return app_state


def test_spawned_task_is_tracked_and_cancelled_before_its_first_step_still_ends_the_job_failed(tmp_path: Path) -> None:
    """The coroutine body never ran, so only the done-callback wired by the spawn can end the job (else it stays
    RUNNING until a restart); the finished task leaves the tracking set."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create_description("docs")

    app_state = asyncio.run(_spawn_and_cancel_before_the_first_step(store, job.job_id))

    ended = store.get(job.job_id)
    assert (ended.status, ended.error) == (JobStatus.FAILED, DESCRIBE_SHUTTING_DOWN_DETAIL)
    assert app_state._background_tasks == set()


def test_a_job_that_already_settled_is_not_rewritten_by_the_cancellation_callback(tmp_path: Path) -> None:
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create_description("docs")
    result = {"outcome": "generated", "description_status": "ok"}
    store.update(job.job_id, status=JobStatus.DONE, result=result)

    asyncio.run(_spawn_and_cancel_before_the_first_step(store, job.job_id))

    settled = store.get(job.job_id)
    assert (settled.status, settled.result, settled.error) == (JobStatus.DONE, result, None)


def test_a_task_that_breaks_ends_the_job_failed_and_logs_the_error_type_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """S76: the unexpected error is sanitized on the wire and in the log (its message may echo document text)."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create_description("docs")

    with caplog.at_level(logging.DEBUG):
        asyncio.run(_describe_task(job, store, _BrokenLifecycle()))  # type: ignore[arg-type]

    ended = store.get(job.job_id)
    assert (ended.status, ended.error) == (JobStatus.FAILED, INTERNAL_ERROR_DETAIL)
    assert "RuntimeError" in caplog.text
    assert _SECRET not in caplog.text

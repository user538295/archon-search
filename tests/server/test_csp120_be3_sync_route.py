"""Tests for POST /sync endpoint — CSP120 BE-3."""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable, Coroutine, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.server import routes_sync
from archon_search.server.routes_jobs import INTERNAL_ERROR_DETAIL, JOB_SUPERSEDED_DETAIL, _SYNC_JOB_NOT_CANCELLABLE_DETAIL
from archon_search.server.routes_sync import _SYNC_FAILED_DETAIL
from archon_search.types import JobStatus
from tests._job_store_faults import (
    JOB_WRITE_FAILURES,
    assert_error_logged_with_traceback,
    failing_job_writes,
    persisted_statuses,
    recording_job_writes,
)
from tests.integration.conftest import JOBS_FILE_NAME, make_real_app

pytestmark = pytest.mark.integration

#: Bound on any wait in these tests: a regression shows up as a timeout, never as a hang.
_WAIT_TIMEOUT_SECONDS = 5.0


def _clean_sync_stub() -> AsyncMock:
    """A ``collection_sync.sync`` stand-in that finishes at once with an empty, error-free result."""
    return AsyncMock(return_value=MagicMock(added=[], removed=[], unchanged=[], errors=[], skipped=[], updated=[]))


# ---------------------------------------------------------------------------
# Unit tests (stub app via make_real_app + mock collection_sync)
# ---------------------------------------------------------------------------


def test_post_sync_returns_202_running(tmp_path, monkeypatch):
    """POST /sync returns 202 with JobResponse status=RUNNING, and the job's first persisted status is
    RUNNING: it is created in one write, never QUEUED (nothing schedules a SyncJob)."""
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Mock sync so it doesn't actually run filesystem operations
        client.app.state.collection_sync.sync = _clean_sync_stub()

        with recording_job_writes() as writes:
            resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
            assert resp.status_code == 202
            body = resp.json()
            assert body["status"] == "RUNNING"
            _poll_job_until_terminal(client, body["job_id"], api_key)

        statuses = writes.statuses_of(body["job_id"])
        assert statuses[:1] == [JobStatus.RUNNING.value], f"first persisted status must be RUNNING; got {statuses!r}"
        assert JobStatus.QUEUED.value not in statuses, f"a SyncJob must never be persisted QUEUED; got {statuses!r}"


def test_post_sync_409_on_concurrent_submit(tmp_path, monkeypatch):
    """POST /sync returns 409 when sync_lock is already held."""
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Acquire the lock externally (simulating a concurrent sync in progress)
        lock = client.app.state.sync_lock

        async def _hold_lock():
            await lock.acquire()

        client.portal.call(_hold_lock)
        try:
            resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
            assert resp.status_code == 409
            assert "sync already in progress" in resp.json()["detail"]
        finally:
            # On the event-loop thread, like the acquire: asyncio.Lock is not thread-safe.
            client.portal.call(lock.release)


def test_post_sync_requires_bearer_auth(tmp_path, monkeypatch):
    """POST /sync returns 401 without a valid Bearer token."""
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        resp = client.post("/sync")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Integration tests (real async behaviour)
# ---------------------------------------------------------------------------


def _poll_job_until_terminal(client: TestClient, job_id: str, api_key: str, timeout: float = _WAIT_TIMEOUT_SECONDS) -> dict:
    """Poll GET /jobs/{id} until a terminal status is reached."""
    deadline = time.monotonic() + timeout
    terminal = {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers={"Authorization": f"Bearer {api_key}"})
        assert r.status_code == 200
        if r.json()["status"] in terminal:
            return r.json()
        time.sleep(0.05)
    pytest.fail(f"Job {job_id} did not reach a terminal status within {timeout}s")


def test_post_sync_dispatches_to_collection_sync_only(tmp_path, monkeypatch):
    """POST /sync calls SearchCollectionSync.sync once; MaintenanceLoop is not invoked.

    S15: sync only runs SearchCollectionSync.sync(), not FTS optimize / orphan cleanup /
    graph GC.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        sync_mock = _clean_sync_stub()
        client.app.state.collection_sync.sync = sync_mock
        monkeypatch.setattr(cfg, "pinned_collections", ["pinned-a"])
        monkeypatch.setattr(cfg, "collections", ["col-b", "col-c"])

        resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        _poll_job_until_terminal(client, job_id, api_key)

        # assert sync called once — S15 proof (not MaintenanceLoop)
        assert sync_mock.await_count == 1, f"Expected sync called once, got {sync_mock.await_count}"
        # The collections passed: pinned_collections, then collections.
        sync_mock.assert_awaited_once_with(["pinned-a", "col-b", "col-c"])


def test_post_sync_task_failed_releases_lock_and_second_submit_succeeds(tmp_path, monkeypatch):
    """If sync() raises, job transitions to FAILED and sync_lock is released.

    S23: a subsequent POST /sync must return 202, not 409.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        fail_mock = AsyncMock(side_effect=OSError("disk full"))
        client.app.state.collection_sync.sync = fail_mock

        resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        job = _poll_job_until_terminal(client, job_id, api_key)
        assert job["status"] == "FAILED"

        # Lock must be released — second submit should succeed (202, not 409)
        client.app.state.collection_sync.sync = _clean_sync_stub()

        resp2 = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
        assert resp2.status_code == 202


def test_post_sync_job_result_contains_all_sync_result_fields(tmp_path, monkeypatch):
    """Job result on DONE contains all 6 SyncResult fields (S7).

    Uses a real sync over a tmp_path collection (empty — no configured collections).
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Real sync — no collections configured, so it runs cleanly and DONE quickly
        resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        job = _poll_job_until_terminal(client, job_id, api_key)
        assert job["status"] == "DONE"
        assert job.get("kind") == "sync", f"Expected kind='sync', got {job.get('kind')!r}"

        result = job["result"]
        assert result is not None
        for field in ("added", "removed", "unchanged", "errors", "skipped", "updated"):
            assert field in result, f"Missing field: {field}"
            assert isinstance(result[field], list), f"Expected list for {field}"


# ---------------------------------------------------------------------------
# A failed job write inside POST /sync used to leave sync_lock held (every later
# POST /sync answered 409 until a restart), and a failed task spawn left the job
# RUNNING with no task behind it; both must answer the sanitized 500 with the
# lock released.
# ---------------------------------------------------------------------------

_SANITIZED_500 = {"detail": INTERNAL_ERROR_DETAIL}
_ROUTES_SYNC_LOGGER = "archon_search.server.routes_sync"
#: ``_persist_job_status`` (the best-effort FAILED write) logs under ``routes_jobs``.
_ROUTES_JOBS_LOGGER = "archon_search.server.routes_jobs"
_SPAWN_FAILED_JOB = (JobStatus.FAILED.value, _SYNC_FAILED_DETAIL)


def _known_job_ids(client: TestClient) -> set[str]:
    return {j.job_id for j in client.app.state.job_store.list()}


def _new_jobs_in_memory(client: TestClient, known_ids: set[str]) -> list[tuple[str, str | None]]:
    """``(status, error)`` of every job not in *known_ids*, as the job store holds it."""
    return sorted((j.status.value, j.error) for j in client.app.state.job_store.list() if j.job_id not in known_ids)


def _new_jobs_on_disk(jobs_path: Path, known_ids: set[str]) -> list[tuple[str, str | None]]:
    """``(status, error)`` of every job not in *known_ids*, as written in the jobs file.

    Raw JSON on purpose: loading a JobStore would rewrite a RUNNING row to FAILED ``process_restart``.
    A missing file reads as no jobs, so a regression shows up in the comparison, not as a FileNotFoundError.
    """
    items = json.loads(jobs_path.read_text()) if jobs_path.exists() else []
    return sorted((i["status"], i["error"]) for i in items if i["job_id"] not in known_ids)


def _observe_failed_sync(client: TestClient, resp: Any, known_ids: set[str], jobs_path: Path) -> dict:
    """Everything a failed POST /sync must get right, gathered so one assert shows every deviation."""
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    return {
        "status_code": resp.status_code,
        "body": body,
        "lock_held": client.app.state.sync_lock.locked(),
        "new_jobs_in_memory": _new_jobs_in_memory(client, known_ids),
        "new_jobs_on_disk": _new_jobs_on_disk(jobs_path, known_ids),
    }


def _expected_failed_sync(*, in_memory: list[tuple[str, str | None]], on_disk: list[tuple[str, str | None]]) -> dict:
    return {
        "status_code": 500,
        "body": _SANITIZED_500,
        "lock_held": False,
        "new_jobs_in_memory": in_memory,
        "new_jobs_on_disk": on_disk,
    }


def _follow_up_sync_status(client: TestClient, api_key: str) -> int:
    """POST /sync again with a clean sync stub: a 202 (not 409) proves the failed request released sync_lock."""
    client.app.state.collection_sync.sync = _clean_sync_stub()
    return client.post("/sync", headers={"Authorization": f"Bearer {api_key}"}).status_code


def _is_sync_task(coro: Any) -> bool:
    return getattr(coro, "cr_code", None) is routes_sync._sync_task.__code__


@contextmanager
def _failing_sync_task_spawn(on_spawn: Callable[[], object] = lambda: None) -> Iterator[list[Coroutine]]:
    """Make ``asyncio.create_task`` raise for the ``_sync_task`` coroutine only; other tasks spawn normally.

    Calls *on_spawn* just before raising. Yields the rejected coroutines, left as the route leaves them,
    so a test can check the route closed them. The patch is undone when the block exits.
    """
    real_create_task = asyncio.create_task
    rejected: list[Coroutine] = []

    def _create_task(coro: Any, *args: Any, **kwargs: Any) -> asyncio.Task:
        if not _is_sync_task(coro):
            return real_create_task(coro, *args, **kwargs)
        rejected.append(coro)
        on_spawn()
        raise RuntimeError("simulated spawn failure")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(routes_sync.asyncio, "create_task", _create_task)
        yield rejected


# The oserror case passes its status, body, lock and job asserts on the old code too (it caught OSError on
# the create); a revert fails it only via the log assert (the old handler logged nothing).
@pytest.mark.parametrize("exc", JOB_WRITE_FAILURES)
def test_post_sync_failed_job_create_releases_lock_and_leaves_no_job(tmp_path, monkeypatch, caplog, exc):
    """A failed persist of the RUNNING job (any exception) answers the sanitized 500, releases sync_lock
    and leaves no job behind."""
    with make_real_app(tmp_path, monkeypatch, raise_server_exceptions=False) as (client, _cfg, api_key):
        known = _known_job_ids(client)
        with failing_job_writes(count=1, exc=exc):
            resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})

        assert _observe_failed_sync(client, resp, known, tmp_path / JOBS_FILE_NAME) == _expected_failed_sync(in_memory=[], on_disk=[])
        assert _follow_up_sync_status(client, api_key) == 202
    assert_error_logged_with_traceback(caplog, _ROUTES_SYNC_LOGGER)


def test_post_sync_spawn_failure_answers_500_with_job_failed_and_lock_released(tmp_path, monkeypatch, caplog):
    """When spawning _sync_task fails, the job already saved RUNNING is persisted FAILED ("sync failed"),
    the un-awaited coroutine is closed, and the sanitized 500 is returned with sync_lock released."""
    with make_real_app(tmp_path, monkeypatch, raise_server_exceptions=False) as (client, _cfg, api_key):
        known = _known_job_ids(client)
        with _failing_sync_task_spawn() as rejected:
            resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})

        expected = _expected_failed_sync(in_memory=[_SPAWN_FAILED_JOB], on_disk=[_SPAWN_FAILED_JOB])
        assert _observe_failed_sync(client, resp, known, tmp_path / JOBS_FILE_NAME) == expected
        assert [inspect.getcoroutinestate(coro) for coro in rejected] == [inspect.CORO_CLOSED]
        assert _follow_up_sync_status(client, api_key) == 202
    assert_error_logged_with_traceback(caplog, _ROUTES_SYNC_LOGGER)


@pytest.mark.parametrize("exc", JOB_WRITE_FAILURES)
def test_post_sync_spawn_failure_with_failed_status_write_still_releases_lock(tmp_path, monkeypatch, caplog, exc):
    """The FAILED write after a spawn failure is best-effort: when it fails too, the sanitized 500 is still
    returned with sync_lock released. Memory holds FAILED; the file keeps the RUNNING row."""
    with make_real_app(tmp_path, monkeypatch, raise_server_exceptions=False) as (client, _cfg, api_key):
        known = _known_job_ids(client)
        with (
            ExitStack() as writes_fail_from_spawn,
            _failing_sync_task_spawn(lambda: writes_fail_from_spawn.enter_context(failing_job_writes(exc=exc))),
        ):
            resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})

        expected = _expected_failed_sync(in_memory=[_SPAWN_FAILED_JOB], on_disk=[(JobStatus.RUNNING.value, None)])
        assert _observe_failed_sync(client, resp, known, tmp_path / JOBS_FILE_NAME) == expected
        (failed_job_id,) = _known_job_ids(client) - known
        assert _follow_up_sync_status(client, api_key) == 202
        # The follow-up's first successful job write persists memory, this FAILED row included.
        assert persisted_statuses(tmp_path / JOBS_FILE_NAME)[failed_job_id] == JobStatus.FAILED.value
    assert_error_logged_with_traceback(caplog, _ROUTES_SYNC_LOGGER)
    assert_error_logged_with_traceback(caplog, _ROUTES_JOBS_LOGGER)


# ---------------------------------------------------------------------------
# sync_lock handover over HTTP (the pure-asyncio ``_try_acquire`` unit tests live in
# test_routes_sync_try_acquire.py): asyncio.Lock hands a released lock to a queued
# waiter (the lifespan's startup sync waits for it via ``async with``), so right
# after such a release ``locked()`` is False yet ``acquire()`` queues. POST /sync
# used to wait there for the whole startup sync, then run a redundant sync; it
# must answer 409.
# ---------------------------------------------------------------------------


def test_post_sync_answers_409_over_http_while_the_lock_is_handed_to_a_queued_waiter(tmp_path, monkeypatch):
    """Over HTTP: the lock is held on the event loop with a task queued for it, then released (handed to that
    waiter) inside the request right before the route's try-acquire, in the same loop pass. A TestClient
    request cannot hit that window by timing: once released, the waiter takes the lock on the loop's next pass.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        lock = client.app.state.sync_lock
        waiter_acquired = asyncio.Event()
        let_waiter_finish = asyncio.Event()

        async def _waiter() -> None:
            async with lock:
                waiter_acquired.set()
                await let_waiter_finish.wait()

        async def _hold_lock_and_queue_waiter() -> asyncio.Task:
            await lock.acquire()
            waiter = asyncio.create_task(_waiter())
            await asyncio.sleep(0)  # the waiter runs up to its queued acquire()
            return waiter

        real_try_acquire = routes_sync._try_acquire

        async def _release_then_try_acquire(sync_lock: asyncio.Lock) -> bool:
            sync_lock.release()  # hands the lock to the queued waiter, which has not run yet
            assert not sync_lock.locked(), "test setup is wrong — the handed-over lock must read free"
            # Bounded: a try-acquire that queues behind the waiter would otherwise hang the request.
            return await asyncio.wait_for(real_try_acquire(sync_lock), _WAIT_TIMEOUT_SECONDS)

        waiter = client.portal.call(_hold_lock_and_queue_waiter)
        try:
            monkeypatch.setattr(routes_sync, "_try_acquire", _release_then_try_acquire)
            known = _known_job_ids(client)
            with recording_job_writes() as writes:
                resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})

            assert (resp.status_code, resp.json()) == (409, {"detail": "sync already in progress"})
            assert _known_job_ids(client) == known, "a refused request must create no job"
            assert writes.attempts == 0, "a refused request must not write the job store"
            client.portal.call(asyncio.wait_for, waiter_acquired.wait(), _WAIT_TIMEOUT_SECONDS)
            assert lock.locked(), "the queued waiter must hold the lock after the refused request"
        finally:
            # On the event-loop thread, like the acquire: asyncio.Event and asyncio.Lock are not thread-safe.
            client.portal.call(let_waiter_finish.set)
            client.portal.call(asyncio.wait_for, waiter, _WAIT_TIMEOUT_SECONDS)
        assert not lock.locked()


# ---------------------------------------------------------------------------
# Older servers created SyncJobs QUEUED, then moved them to RUNNING; a row left
# QUEUED (or PENDING) never runs — nothing schedules a SyncJob — and is never
# evicted (not terminal). POST /sync supersedes them, in every namespace.
# ---------------------------------------------------------------------------


def _persisted_jobs(jobs_path: Path) -> dict[str, tuple[str, str | None]]:
    """Job id -> ``(status, error)`` as written in the jobs file (raw JSON, see ``_new_jobs_on_disk``)."""
    return {item["job_id"]: (item["status"], item["error"]) for item in json.loads(jobs_path.read_text())}


def _add_legacy_sync_job(store: Any, status: JobStatus, namespace: str = "default") -> str:
    """A SyncJob as an older server could leave it: *status* (QUEUED or PENDING), never run."""
    legacy = store.create_sync(namespace=namespace)
    store.update(legacy.job_id, status=status)
    return legacy.job_id


def _blocking_sync_stub(let_sync_finish: asyncio.Event, result: Any) -> Callable[[list[str]], Coroutine]:
    """A ``collection_sync.sync`` stand-in that returns *result* once *let_sync_finish* is set (on the loop)."""

    async def _sync(collections: list[str]) -> Any:
        await let_sync_finish.wait()
        return result

    return _sync


def test_post_sync_marks_legacy_queued_and_pending_sync_jobs_failed_superseded(tmp_path, monkeypatch):
    """Only QUEUED/PENDING SyncJobs are marked FAILED, in every namespace; a RUNNING SyncJob (the startup
    sync is created RUNNING before it waits for the lock) and other job kinds are left alone."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        store = client.app.state.job_store
        client.app.state.collection_sync.sync = _clean_sync_stub()
        legacy_ids = [
            _add_legacy_sync_job(store, JobStatus.QUEUED),
            _add_legacy_sync_job(store, JobStatus.PENDING),
            _add_legacy_sync_job(store, JobStatus.QUEUED, namespace="other-ns"),
        ]
        running_sync = store.create_sync(namespace="default")
        pending_ingest = store.create(collection="docs")

        resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 202
        _poll_job_until_terminal(client, resp.json()["job_id"], api_key)

        superseded = (JobStatus.FAILED.value, JOB_SUPERSEDED_DETAIL)
        persisted = _persisted_jobs(tmp_path / JOBS_FILE_NAME)
        for legacy_id in legacy_ids:
            legacy = store.get(legacy_id)
            assert (legacy.status.value, legacy.error) == superseded
            assert persisted[legacy_id] == superseded
        assert store.get(running_sync.job_id).status == JobStatus.RUNNING
        assert store.get(pending_ingest.job_id).status == JobStatus.PENDING


@pytest.mark.parametrize("exc", JOB_WRITE_FAILURES)
def test_post_sync_failed_supersede_write_still_starts_the_sync(tmp_path, monkeypatch, caplog, exc):
    """The supersede write is best-effort: when it fails, POST /sync still answers 202, the spawned task holds
    sync_lock until the sync ends, and the legacy job is FAILED in memory (the next write persists it)."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        store = client.app.state.job_store
        legacy_id = _add_legacy_sync_job(store, JobStatus.QUEUED)
        let_sync_finish = asyncio.Event()
        clean_result = _clean_sync_stub().return_value
        client.app.state.collection_sync.sync = _blocking_sync_stub(let_sync_finish, clean_result)

        with failing_job_writes(count=1, exc=exc) as writes:
            resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
        try:
            assert resp.status_code == 202
            assert writes.statuses_of(legacy_id)[:1] == [JobStatus.FAILED.value], "the failed write must be the supersede"
            legacy = store.get(legacy_id)
            assert (legacy.status, legacy.error) == (JobStatus.FAILED, JOB_SUPERSEDED_DETAIL)
            assert client.app.state.sync_lock.locked(), "the running sync task must hold sync_lock"
        finally:
            client.portal.call(let_sync_finish.set)
        assert _poll_job_until_terminal(client, resp.json()["job_id"], api_key)["status"] == JobStatus.DONE.value
        assert not client.app.state.sync_lock.locked(), "the finished sync task must release sync_lock"
        assert persisted_statuses(tmp_path / JOBS_FILE_NAME)[legacy_id] == JobStatus.FAILED.value
    assert_error_logged_with_traceback(caplog, _ROUTES_JOBS_LOGGER)


# ---------------------------------------------------------------------------
# DELETE /jobs/{id} on a SyncJob: nothing can stop a running sync, so moving the
# job to CANCELLING was a lie (the sync ran on) and CANCELLING is a crash status
# that arms the crash-loop guard on the next boot. Any non-terminal SyncJob —
# manual, startup, or legacy QUEUED/PENDING — answers 409 and is left unchanged.
# ---------------------------------------------------------------------------

_SYNC_NOT_CANCELLABLE = {"detail": _SYNC_JOB_NOT_CANCELLABLE_DETAIL}


def test_delete_running_manual_sync_job_answers_409_and_the_sync_still_ends_done(tmp_path, monkeypatch):
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        let_sync_finish = asyncio.Event()
        sync_result = MagicMock(added=["a.md"], removed=[], unchanged=[], errors=[], skipped=[], updated=[])
        client.app.state.collection_sync.sync = _blocking_sync_stub(let_sync_finish, sync_result)

        resp = client.post("/sync", headers=headers)
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        try:
            with recording_job_writes() as writes:
                delete = client.delete(f"/jobs/{job_id}", headers=headers)
            assert (delete.status_code, delete.json()) == (409, _SYNC_NOT_CANCELLABLE)
            assert writes.statuses_of(job_id) == [], "a refused DELETE must not write the sync job"
            assert client.get(f"/jobs/{job_id}", headers=headers).json()["status"] == JobStatus.RUNNING.value
        finally:
            client.portal.call(let_sync_finish.set)

        job = _poll_job_until_terminal(client, job_id, api_key)
        assert (job["status"], job["result"]["added"]) == (JobStatus.DONE.value, ["a.md"])


@pytest.mark.parametrize("legacy_status", [JobStatus.QUEUED, JobStatus.PENDING], ids=lambda s: s.value)
def test_delete_legacy_sync_job_answers_409_and_leaves_its_status(tmp_path, monkeypatch, legacy_status):
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        legacy_id = _add_legacy_sync_job(client.app.state.job_store, legacy_status)

        delete = client.delete(f"/jobs/{legacy_id}", headers={"Authorization": f"Bearer {api_key}"})

        assert (delete.status_code, delete.json()) == (409, _SYNC_NOT_CANCELLABLE)
        assert client.app.state.job_store.get(legacy_id).status == legacy_status
        assert persisted_statuses(tmp_path / JOBS_FILE_NAME)[legacy_id] == legacy_status.value


def test_delete_running_sync_job_of_another_namespace_answers_404_not_409(tmp_path, monkeypatch):
    """The namespace check runs before the sync check: another namespace's RUNNING SyncJob reads as missing
    (404), so a 409 cannot leak that the id exists, and the job is left unchanged."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        store = client.app.state.job_store
        foreign = store.create_sync(namespace="other-ns")
        assert foreign.status == JobStatus.RUNNING, "test setup is wrong — the foreign sync must be non-terminal"

        with recording_job_writes() as writes:
            delete = client.delete(f"/jobs/{foreign.job_id}", headers={"Authorization": f"Bearer {api_key}"})

        assert (delete.status_code, delete.json()) == (404, {"detail": "Job not found"})
        assert writes.attempts == 0, "a refused DELETE must not write the job store"
        assert store.get(foreign.job_id).status == JobStatus.RUNNING
        assert persisted_statuses(tmp_path / JOBS_FILE_NAME)[foreign.job_id] == JobStatus.RUNNING.value


def test_delete_terminal_sync_job_stays_idempotent_200(tmp_path, monkeypatch):
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        client.app.state.collection_sync.sync = _clean_sync_stub()
        job_id = client.post("/sync", headers=headers).json()["job_id"]
        assert _poll_job_until_terminal(client, job_id, api_key)["status"] == JobStatus.DONE.value

        delete = client.delete(f"/jobs/{job_id}", headers=headers)

        assert (delete.status_code, delete.json()["status"]) == (200, JobStatus.DONE.value)

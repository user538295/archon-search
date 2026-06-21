"""Tests for synchronous 503 StoreBusy propagation on POST /ingest and POST /collections/.

Implements Task 2c.2 of Documentation/Backlog/A5-ingest-hardening-plan.md.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.model import JobStatus
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_KEY = os.environ.get("ARCHON_SEARCH_API_KEY", "0" * 64)


def _make_app_and_client(tmp_path: Path):  # type: ignore[no-untyped-def]
    """Create an app+TestClient with a mock search_store that has a real asyncio.Lock."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    jobs = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, jobs)

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.update_collection_meta = AsyncMock()
    mock_store.delete_collection_meta = AsyncMock()
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    # Real per-collection lock map so we can pre-hold locks in tests.
    _lock_map: dict[str, asyncio.Lock] = {}

    def lock_for(collection: str) -> asyncio.Lock:
        if collection not in _lock_map:
            _lock_map[collection] = asyncio.Lock()
        return _lock_map[collection]

    mock_store.lock_for = lock_for
    app.state.search_store = mock_store

    client = TestClient(app, headers={"Authorization": f"Bearer {TEST_KEY}"})
    return app, client, mock_store, jobs


# ---------------------------------------------------------------------------
# POST /ingest 503 contract
# ---------------------------------------------------------------------------


def test_post_ingest_returns_503_when_lock_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the per-collection lock is held, POST /ingest returns 503 + Retry-After."""
    app, client, mock_store, jobs = _make_app_and_client(tmp_path)

    # Force asyncio.wait_for to raise TimeoutError immediately, simulating a held lock.
    async def _raise_timeout(coro: object, timeout: object = None) -> None:
        import inspect
        if inspect.iscoroutine(coro):
            coro.close()  # prevent ResourceWarning
        raise asyncio.TimeoutError

    monkeypatch.setattr("archon_search.server._ingest_lock.asyncio.wait_for", _raise_timeout)

    response = client.post("/ingest", json={"collection": "test-col"})

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "30"
    body = response.json()
    assert body.get("error") == "store_busy"


def test_post_ingest_succeeds_when_lock_free(tmp_path: Path) -> None:
    """Happy path: POST /ingest returns 202 when the lock is free."""
    app, client, mock_store, jobs = _make_app_and_client(tmp_path)

    response = client.post("/ingest", json={"collection": "free-col"})
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data


def test_post_ingest_releases_lock_after_background_task(tmp_path: Path) -> None:
    """After the background task completes, the lock is free for a subsequent ingest."""
    app, client, mock_store, jobs = _make_app_and_client(tmp_path)

    # First ingest — lock acquired then released by wrapper.
    r1 = client.post("/ingest", json={"collection": "shared-col"})
    assert r1.status_code == 202

    # Lock must be free now.
    lock = mock_store.lock_for("shared-col")
    assert not lock.locked(), "Lock must be released after background task completes"

    # Second ingest into the same collection must not time out.
    r2 = client.post("/ingest", json={"collection": "shared-col"})
    assert r2.status_code == 202


def test_post_ingest_releases_lock_on_task_cancellation(tmp_path: Path) -> None:
    """Lock is released even when the background task is cancelled via DELETE /jobs/{id}."""
    app, client, mock_store, jobs = _make_app_and_client(tmp_path)

    # Inject a pipeline function that blocks until cancelled so we can test cancellation.
    cancelled_event = asyncio.Event()

    async def blocking_pipeline(job_id: str, store: JobStore, body, *, namespace: str = "default", locked_by_caller: bool = False) -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled_event.set()
            raise

    app.state.ingest_pipeline = blocking_pipeline

    r1 = client.post("/ingest", json={"collection": "cancel-col"})
    assert r1.status_code == 202
    job_id = r1.json()["job_id"]

    # Cancel the job.
    r_del = client.delete(f"/jobs/{job_id}")
    assert r_del.status_code in (200, 202)

    # The TestClient portal has already driven the cancellation to completion
    # before returning; no additional settling is needed.

    # Lock must be free regardless of task cancellation.
    lock = mock_store.lock_for("cancel-col")
    assert not lock.locked(), "Lock must be released after task cancellation"


# ---------------------------------------------------------------------------
# POST /collections/ 503 contract
# ---------------------------------------------------------------------------


def test_post_collections_returns_503_when_lock_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the per-collection lock is held, POST /collections/ returns 503."""
    src = tmp_path / "docs"
    src.mkdir()

    app, client, mock_store, jobs = _make_app_and_client(tmp_path)

    # Force asyncio.wait_for to raise TimeoutError immediately, simulating a held lock.
    async def _raise_timeout(coro: object, timeout: object = None) -> None:
        import inspect
        if inspect.iscoroutine(coro):
            coro.close()  # prevent ResourceWarning
        raise asyncio.TimeoutError

    monkeypatch.setattr("archon_search.server._ingest_lock.asyncio.wait_for", _raise_timeout)

    response = client.post("/collections/", json={"path": str(src)})

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "30"
    body = response.json()
    assert body.get("error") == "store_busy"


def test_post_collections_503_leaves_no_orphaned_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 503 on POST /collections/ must leave config, meta, and jobs completely untouched."""
    src = tmp_path / "docs"
    src.mkdir()

    app, client, mock_store, jobs = _make_app_and_client(tmp_path)

    # Record initial job count before the request.
    initial_job_count = len(jobs._jobs) if hasattr(jobs, "_jobs") else 0

    # Force asyncio.wait_for to raise TimeoutError immediately (pre-acquire path).
    async def _raise_timeout(coro: object, timeout: object = None) -> None:
        import inspect
        if inspect.iscoroutine(coro):
            coro.close()  # prevent ResourceWarning
        raise asyncio.TimeoutError

    monkeypatch.setattr("archon_search.server._ingest_lock.asyncio.wait_for", _raise_timeout)

    response = client.post("/collections/", json={"path": str(src)})

    assert response.status_code == 503
    body = response.json()
    assert body.get("error") == "store_busy"

    # Config must NOT contain the path — no orphaned config entry.
    resolved = str(src.resolve())
    assert resolved not in app.state.config.collections, (
        "503 must not leave the collection path in config.collections"
    )

    # Stub meta was written then rolled back via delete_collection_meta.
    mock_store.update_collection_meta.assert_called_once()
    mock_store.delete_collection_meta.assert_called_once()

    # No new job must have been created.
    final_job_count = len(jobs._jobs) if hasattr(jobs, "_jobs") else 0
    assert final_job_count == initial_job_count, (
        "503 must not create a new job (orphaned job_id)"
    )


def test_post_ingest_503_on_A_does_not_block_B(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held lock on collection A must not prevent collection B from being ingested."""
    app, client, mock_store, jobs = _make_app_and_client(tmp_path)

    # Reduce the timeout so the contended case fails fast without a 30 s wait.
    monkeypatch.setattr("archon_search.constants.INGEST_LOCK_TIMEOUT_S", 0.05)
    # _ingest_lock.py reads _constants.INGEST_LOCK_TIMEOUT_S at call time via the
    # module alias; patch it there too so the running call sees the new value.
    import archon_search.server._ingest_lock as _ingest_lock_mod
    import archon_search.constants as _constants_mod
    monkeypatch.setattr(_ingest_lock_mod, "_constants", _constants_mod)

    # Pre-acquire the lock for collection "A" so the request handler times out.
    # We need the lock to be held BEFORE the TestClient request runs; since the
    # TestClient drives its own event loop we acquire it via asyncio.run() — the
    # lock object itself is not loop-bound, only its internal state matters.
    lock_a = mock_store.lock_for("col-a")
    asyncio.run(lock_a.acquire())  # lock_a is now held; TestClient's wait_for will time out

    # POST /ingest for collection A → 503 (lock held).
    r_a = client.post("/ingest", json={"collection": "col-a"})
    assert r_a.status_code == 503, f"Expected 503 for locked col-a, got {r_a.status_code}"
    assert r_a.json().get("error") == "store_busy"

    # POST /ingest for collection B → 202 (different lock, uncontended).
    r_b = client.post("/ingest", json={"collection": "col-b"})
    assert r_b.status_code == 202, f"Expected 202 for free col-b, got {r_b.status_code}"
    assert "job_id" in r_b.json()

    # Release the pre-held lock to avoid ResourceWarning / state leakage.
    if lock_a.locked():
        lock_a.release()

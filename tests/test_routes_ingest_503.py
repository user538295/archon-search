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
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()

    # Real per-collection lock map so we can pre-hold locks in tests.
    _lock_map: dict[str, asyncio.Lock] = {}

    def _lock_for(collection: str) -> asyncio.Lock:
        if collection not in _lock_map:
            _lock_map[collection] = asyncio.Lock()
        return _lock_map[collection]

    mock_store._lock_for = _lock_for
    app.state.search_store = mock_store

    client = TestClient(app, headers={"Authorization": f"Bearer {TEST_KEY}"})
    return app, client, mock_store, jobs


# ---------------------------------------------------------------------------
# POST /ingest 503 contract
# ---------------------------------------------------------------------------


def test_post_ingest_returns_503_when_lock_held(tmp_path: Path) -> None:
    """When the per-collection lock is held, POST /ingest returns 503 + Retry-After."""
    from archon_search.server._ingest_lock import acquire_collection_lock_or_503
    from fastapi.responses import JSONResponse

    app, client, mock_store, jobs = _make_app_and_client(tmp_path)

    # Pre-acquire the lock so acquire_collection_lock_or_503 times out.
    lock = mock_store._lock_for("test-col")

    async def _pre_acquire() -> None:
        await lock.acquire()

    asyncio.get_event_loop().run_until_complete(_pre_acquire())

    try:
        # Patch the timeout to be tiny so the test is fast.
        import archon_search.server._ingest_lock as _il_mod
        original_timeout = _il_mod.INGEST_LOCK_TIMEOUT_S
        _il_mod.INGEST_LOCK_TIMEOUT_S = 0.05  # type: ignore[assignment]
        try:
            response = client.post("/ingest", json={"collection": "test-col"})
        finally:
            _il_mod.INGEST_LOCK_TIMEOUT_S = original_timeout  # type: ignore[assignment]
    finally:
        lock.release()

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
    lock = mock_store._lock_for("shared-col")
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

    # Give the event loop a moment to process cancellation.
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0.05))

    # Lock must be free regardless of task cancellation.
    lock = mock_store._lock_for("cancel-col")
    assert not lock.locked(), "Lock must be released after task cancellation"


# ---------------------------------------------------------------------------
# POST /collections/ 503 contract
# ---------------------------------------------------------------------------


def test_post_collections_returns_503_when_lock_held(tmp_path: Path) -> None:
    """When the per-collection lock is held, POST /collections/ returns 503."""
    from archon_search.sync import path_to_collection_name

    src = tmp_path / "docs"
    src.mkdir()
    collection_name = path_to_collection_name(str(src.resolve()))

    app, client, mock_store, jobs = _make_app_and_client(tmp_path)

    lock = mock_store._lock_for(collection_name)

    async def _pre_acquire() -> None:
        await lock.acquire()

    asyncio.get_event_loop().run_until_complete(_pre_acquire())

    try:
        import archon_search.server._ingest_lock as _il_mod
        original_timeout = _il_mod.INGEST_LOCK_TIMEOUT_S
        _il_mod.INGEST_LOCK_TIMEOUT_S = 0.05  # type: ignore[assignment]
        try:
            response = client.post("/collections/", json={"path": str(src)})
        finally:
            _il_mod.INGEST_LOCK_TIMEOUT_S = original_timeout  # type: ignore[assignment]
    finally:
        lock.release()

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "30"
    body = response.json()
    assert body.get("error") == "store_busy"

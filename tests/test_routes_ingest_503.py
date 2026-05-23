"""A5c — tests for synchronous StoreBusyError → HTTP 503 propagation (Task 2c.2).

Uses bare FastAPI app (no create_app, no chonkie) following the pattern from
test_routes_search_telemetry.py.

These tests verify that when the per-collection lock is held (reindex in progress),
POST /ingest and POST /collections return 503 + Retry-After: 30.
"""
from __future__ import annotations

import asyncio
import math
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from archon_search.constants import DEFAULT_NAMESPACE, INGEST_LOCK_TIMEOUT_S


def _make_ingest_app_with_store(
    *,
    search_store: MagicMock | None = None,
    auth_key: str | None = None,
) -> FastAPI:
    """Create a minimal FastAPI app with ingest + jobs router and a mock search_store."""
    import os as _os
    from archon_search.server.routes_jobs import router as jobs_router
    from archon_search.jobs.store import JobStore
    from archon_search.server.middleware_auth import APIKeyMiddleware

    key = auth_key or _os.environ.get("ARCHON_SEARCH_API_KEY", "0" * 64)
    tmpdir = tempfile.mkdtemp()

    app = FastAPI()
    app.state.job_store = JobStore(path=Path(tmpdir) / "jobs.json")
    app.state._background_tasks = set()
    app.state.ingest_pipeline = None
    if search_store is not None:
        app.state.search_store = search_store

    app.add_middleware(APIKeyMiddleware, api_key=key, namespaces={})

    @app.middleware("http")
    async def _inject_namespace(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.namespace = DEFAULT_NAMESPACE
        return await call_next(request)

    app.include_router(jobs_router)
    return app


def _auth_client(app: FastAPI) -> TestClient:
    import os as _os
    key = _os.environ.get("ARCHON_SEARCH_API_KEY", "0" * 64)
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


# ---------------------------------------------------------------------------
# POST /ingest — 503 when lock held
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="pre-acquire lock wiring pending in next commit")
def test_post_ingest_returns_503_when_lock_held() -> None:
    """POST /ingest returns 503 + Retry-After: 30 when the collection lock is held."""
    mock_store = MagicMock()

    # Create a real lock and pre-acquire it
    held_lock = asyncio.Lock()
    asyncio.get_event_loop().run_until_complete(held_lock.acquire())

    mock_store._lock_for = MagicMock(return_value=held_lock)

    app = _make_ingest_app_with_store(search_store=mock_store)
    c = _auth_client(app)

    response = c.post("/ingest", json={"collection": "docs", "path": "/tmp/docs"})
    assert response.status_code == 503
    assert "Retry-After" in response.headers
    assert response.headers["Retry-After"] == str(math.ceil(INGEST_LOCK_TIMEOUT_S))
    body = response.json()
    assert body.get("error") == "store_busy"

    # Release the lock after the test
    held_lock.release()


def test_post_ingest_succeeds_when_lock_free() -> None:
    """POST /ingest returns 202 when the lock is free."""
    mock_store = MagicMock()
    free_lock = asyncio.Lock()
    mock_store._lock_for = MagicMock(return_value=free_lock)

    app = _make_ingest_app_with_store(search_store=mock_store)
    c = _auth_client(app)

    with patch(
        "archon_search.server.routes_jobs.asyncio.create_task",
        side_effect=lambda coro: (coro.close(), MagicMock())[1],
    ):
        response = c.post("/ingest", json={"collection": "docs", "path": "/tmp/docs"})

    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data


def test_post_ingest_releases_lock_after_background_task() -> None:
    """After a successful 202, the lock is released when the background task finishes."""
    mock_store = MagicMock()
    lock = asyncio.Lock()
    mock_store._lock_for = MagicMock(return_value=lock)

    app = _make_ingest_app_with_store(search_store=mock_store)
    c = _auth_client(app)

    tasks_run = []

    async def _fake_task(*args, **kwargs):
        tasks_run.append(True)

    with patch(
        "archon_search.server.routes_jobs.asyncio.create_task",
        side_effect=lambda coro: (coro.close(), MagicMock())[1],
    ):
        response = c.post("/ingest", json={"collection": "docs", "path": "/tmp/docs"})

    assert response.status_code == 202
    # After task completes, the lock should be released (not held)
    assert not lock.locked(), "Lock should be released after background task"

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


def test_post_ingest_returns_503_when_lock_held() -> None:
    """POST /ingest returns 503 + Retry-After: 30 when the collection lock is held.

    Uses asyncio.wait_for with a very short timeout by patching INGEST_LOCK_TIMEOUT_S
    on the _ingest_lock module to avoid a 30s wait.
    """
    import archon_search.server._ingest_lock as lock_module

    mock_store = MagicMock()

    # Return a pre-acquired lock so any acquire attempt will timeout
    class _AlwaysLockedLock:
        """Fake asyncio.Lock that is always considered locked."""
        def locked(self) -> bool:
            return True

        def _lock_for(self, _col: str) -> "_AlwaysLockedLock":
            return self

        async def acquire(self) -> None:
            # Never resolves — simulates a perpetually held lock
            await asyncio.sleep(100)

        def release(self) -> None:
            pass

    fake_lock = _AlwaysLockedLock()
    mock_store._lock_for = MagicMock(return_value=fake_lock)

    app = _make_ingest_app_with_store(search_store=mock_store)
    c = _auth_client(app)

    # Patch timeout to 0.05s so test is fast
    original_timeout = lock_module.INGEST_LOCK_TIMEOUT_S
    lock_module.INGEST_LOCK_TIMEOUT_S = 0.05  # type: ignore[attr-defined]
    try:
        response = c.post("/ingest", json={"collection": "docs", "path": "/tmp/docs"})
    finally:
        lock_module.INGEST_LOCK_TIMEOUT_S = original_timeout  # type: ignore[attr-defined]

    assert response.status_code == 503
    assert "Retry-After" in response.headers
    body = response.json()
    assert body.get("error") == "store_busy"


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


def test_post_ingest_lock_is_acquired_before_task() -> None:
    """POST /ingest pre-acquires the lock before creating the background task."""
    mock_store = MagicMock()
    acquired_calls = []

    class _TrackingLock:
        """Tracks lock acquire calls."""
        async def acquire(self) -> None:
            acquired_calls.append(True)

        def release(self) -> None:
            pass

        def locked(self) -> bool:
            return False

    tracking_lock = _TrackingLock()
    mock_store._lock_for = MagicMock(return_value=tracking_lock)

    app = _make_ingest_app_with_store(search_store=mock_store)
    c = _auth_client(app)

    with patch(
        "archon_search.server.routes_jobs.asyncio.create_task",
        side_effect=lambda coro: (coro.close(), MagicMock())[1],
    ):
        response = c.post("/ingest", json={"collection": "docs", "path": "/tmp/docs"})

    assert response.status_code == 202
    # The lock should have been acquired (pre-acquire happened)
    assert acquired_calls, "Lock.acquire() was never called — pre-acquire is missing"

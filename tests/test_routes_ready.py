"""Tests for GET /ready endpoint — Task 5.2 (B2)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    return TestClient(app)


@pytest.fixture()
def client_ping_true(tmp_path: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    app.state.search_store.ping = AsyncMock(return_value=True)
    return TestClient(app)


@pytest.fixture()
def client_ping_false(tmp_path: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    app.state.search_store.ping = AsyncMock(return_value=False)
    return TestClient(app)


def test_ready_returns_200_when_storage_ok(client_ping_true: TestClient) -> None:
    response = client_ping_true.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["checks"]["storage"] == "ok"


def test_ready_returns_503_when_storage_fails(client_ping_false: TestClient) -> None:
    response = client_ping_false.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["checks"]["storage"] == "fail"


def test_ready_reachable_without_bearer_token(client_ping_true: TestClient) -> None:
    """GET /ready must not require authentication."""
    response = client_ping_true.get("/ready")
    assert response.status_code != 401


def test_ready_body_schema_is_bounded(client_ping_true: TestClient) -> None:
    """Response body must contain only 'ready' and 'checks' — no extra fields."""
    response = client_ping_true.get("/ready")
    body = response.json()
    assert set(body.keys()) == {"ready", "checks"}
    assert set(body["checks"].keys()) == {"storage", "models"}


def test_ready_503_body_is_readiness_response_not_error_detail(
    client_ping_false: TestClient,
) -> None:
    """503 body must be ReadinessResponse shape, not ErrorDetail."""
    response = client_ping_false.get("/ready")
    body = response.json()
    assert "ready" in body
    assert "checks" in body
    assert "detail" not in body


def test_ready_appears_in_openapi_without_bearer_auth(client: TestClient) -> None:
    """GET /ready must appear in OpenAPI without BearerAuth security annotation."""
    openapi = client.get("/openapi.json").json()
    paths = openapi.get("paths", {})
    assert "/ready" in paths, "/ready must appear in OpenAPI paths"
    ready_ops = paths["/ready"]
    for method, operation in ready_ops.items():
        if isinstance(operation, dict):
            security = operation.get("security", None)
            assert security is None or security == [], (
                f"/ready {method} must have no security annotation, got {security}"
            )
    # Both 200 and 503 response shapes documented
    get_op = ready_ops.get("get", {})
    responses = get_op.get("responses", {})
    assert "200" in responses
    assert "503" in responses


def test_watcher_manager_slot_is_none_by_default(tmp_path: Path) -> None:
    """app.state.watcher_manager must be None after create_app."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    assert app.state.watcher_manager is None


def test_ready_does_not_call_collect_readiness() -> None:
    """GET /ready must not import or use collect_readiness — it must stay minimal."""
    import archon_search.server.routes_ready as mod
    from pathlib import Path as _Path

    source = _Path(mod.__file__).read_text()
    assert "collect_readiness" not in source


@pytest.mark.integration
def test_ready_returns_503_after_store_disconnect(tmp_path: Path) -> None:
    """After disconnect, GET /ready must return 503."""
    import asyncio

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    store = app.state.search_store

    async def setup() -> None:
        await store.connect()
        await store.disconnect()

    # asyncio.run (not get_event_loop().run_until_complete): the latter raises
    # "no current event loop" in MainThread when a prior async test closed the loop.
    asyncio.run(setup())
    client = TestClient(app)
    response = client.get("/ready")
    assert response.status_code == 503

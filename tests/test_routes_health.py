"""Tests for GET /health endpoint ."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    return TestClient(app)


def _make_client_with_mcp_config(
    tmp_path: Path,
    *,
    mcp_enabled: bool = True,
    host: str = "127.0.0.1",
    port: int = 8765,
    bound: bool = True,
) -> TestClient:
    """Build a TestClient with a specific MCP enabled/disabled configuration.

    ``bound`` controls ``app.state.mcp_bound`` — set True to simulate a
    successful MCP mount (the route returns a non-null bindAddress), or False
    to simulate a failed mount (bindAddress is None). Mirrors the BE-8 helper
    ``_make_client_with_mcp_config`` in tests/test_routes_status.py — sets the
    state directly without opening the lifespan so the bound/not-bound branches
    are exercised deterministically. ``/health`` is unauthenticated.
    """
    from archon_search.config import McpConfig

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.host = host
    config.port = port
    config.mcp = McpConfig(enabled=mcp_enabled)
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    app.state.mcp_bound = bound
    return TestClient(app)


@pytest.mark.parametrize("mcp_enabled", [True, False])
def test_health_does_not_disclose_the_mcp_bind_address(tmp_path: Path, mcp_enabled: bool) -> None:
    """GET /health is unauthenticated: the MCP bind address lives in the authenticated GET /status."""
    client = _make_client_with_mcp_config(
        tmp_path, mcp_enabled=mcp_enabled, host="10.1.2.3", port=8765, bound=True
    )
    response = client.get("/health")
    assert "mcp" not in response.json()
    assert "10.1.2.3" not in response.text


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_health_has_status_running(client: TestClient) -> None:
    response = client.get("/health")
    assert response.json()["status"] == "running"


def test_health_is_status_only_and_carries_no_version(client: TestClient) -> None:
    """The unauthenticated liveness probe does not disclose the version (``GET /status`` does, with a key)."""
    response = client.get("/health")
    assert response.json() == {"status": "running"}

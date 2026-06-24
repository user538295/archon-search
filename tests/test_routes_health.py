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


def test_health_includes_mcp_bind_when_enabled(tmp_path: Path) -> None:
    """GET /health includes a non-null ``mcp.bindAddress`` when MCP is enabled
    and the mount succeeded (D9 BE-9)."""
    client = _make_client_with_mcp_config(
        tmp_path, mcp_enabled=True, host="127.0.0.1", port=8765, bound=True
    )
    data = client.get("/health").json()
    assert data["mcp"] is not None
    assert data["mcp"]["enabled"] is True
    assert data["mcp"]["bindAddress"] == "127.0.0.1:8765/mcp"


def test_health_omits_mcp_when_disabled(tmp_path: Path) -> None:
    """GET /health returns ``mcp: null`` when MCP is disabled (D9 BE-9)."""
    client = _make_client_with_mcp_config(tmp_path, mcp_enabled=False)
    data = client.get("/health").json()
    assert "mcp" in data
    assert data["mcp"] is None


def test_health_mcp_enabled_but_not_bound(tmp_path: Path) -> None:
    """GET /health reports ``enabled=True`` but ``bindAddress=null`` when MCP is
    enabled yet the mount has not succeeded (mount-failed / pre-bind state, D9 BE-9)."""
    client = _make_client_with_mcp_config(tmp_path, mcp_enabled=True, bound=False)
    data = client.get("/health").json()
    assert data["mcp"] is not None
    assert data["mcp"]["enabled"] is True
    assert data["mcp"]["bindAddress"] is None


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_health_has_status_running(client: TestClient) -> None:
    response = client.get("/health")
    assert response.json()["status"] == "running"


def test_health_has_version(client: TestClient) -> None:
    response = client.get("/health")
    data = response.json()
    assert "version" in data
    assert isinstance(data["version"], str)
    assert len(data["version"]) > 0

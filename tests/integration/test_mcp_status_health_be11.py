"""D9 / BE-11 — Slice 3 integration tests: status/health MCP fields.

Covers (completes S7):
- test_status_mcp_field_present — make_real_app(mcp_enabled=True) → GET /status
  response has mcp.bindAddress non-null.
- test_status_mcp_field_absent_when_disabled — make_real_app(mcp_enabled=False) →
  GET /status response mcp is null.
- test_health_mcp_field — same for GET /health (both enabled and disabled).

These are full-stack integration tests through the real create_app() lifespan:
when mcp_enabled=True the lifespan actually mounts the MCP sub-app and sets
app.state.mcp_bound=True, so bindAddress reflects the configured host:port/mcp.
This complements the BE-8/BE-9 unit tests in tests/test_routes_status.py and
tests/test_routes_health.py, which set app.state.mcp_bound directly on a bare
TestClient without a lifespan.
"""
from __future__ import annotations

import pytest

from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


def test_status_mcp_field_present(tmp_path, monkeypatch) -> None:
    """GET /status returns a non-null mcp.bindAddress when MCP is mounted.

    make_real_app(mcp_enabled=True) runs the real lifespan which mounts the MCP
    sub-app and sets app.state.mcp_bound=True, so bindAddress is the configured
    host:port/mcp.  Default config host/port are 127.0.0.1:8765.
    """
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, cfg, api_key):
        resp = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "mcp" in body, "mcp key must be present in the /status response"
        mcp = body["mcp"]
        assert mcp is not None, "mcp field must be non-null when mcp.enabled=True"
        assert mcp["bindAddress"] is not None, "MCP mount failed during lifespan"
        assert mcp["enabled"] is True
        assert mcp["bindAddress"] == f"{cfg.host}:{cfg.port}/mcp", (
            f"bindAddress must reflect host:port/mcp, got {mcp['bindAddress']!r}"
        )


def test_status_mcp_field_absent_when_disabled(tmp_path, monkeypatch) -> None:
    """GET /status returns mcp=null when mcp.enabled=False."""
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=False) as (client, _cfg, api_key):
        resp = client.get("/status", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "mcp" in body, "mcp key must be present (and null) in the /status response"
        assert body["mcp"] is None, "mcp field must be null when mcp.enabled=False"


def test_health_mcp_field(tmp_path, monkeypatch) -> None:
    """GET /health includes mcp.bindAddress when enabled, and mcp=null when disabled.

    /health is unauthenticated, so no Authorization header is sent.  The two
    sub-cases use distinct data dirs (``tmp_path/enabled`` vs ``tmp_path/disabled``)
    so the second ``make_real_app`` never reopens the first's LanceDB store.
    """
    with make_real_app(tmp_path / "enabled", monkeypatch, mcp_enabled=True) as (
        client,
        cfg,
        _api_key,
    ):
        resp = client.get("/health")
        assert resp.status_code == 200, f"GET /health failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "mcp" in body, "mcp key must be present in the /health response"
        mcp = body["mcp"]
        assert mcp is not None, "mcp field must be non-null when mcp.enabled=True"
        assert mcp["bindAddress"] is not None, "MCP mount failed during lifespan"
        assert mcp["enabled"] is True
        assert mcp["bindAddress"] == f"{cfg.host}:{cfg.port}/mcp"

    with make_real_app(tmp_path / "disabled", monkeypatch, mcp_enabled=False) as (
        client,
        _cfg,
        _api_key,
    ):
        resp = client.get("/health")
        assert resp.status_code == 200, f"GET /health failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "mcp" in body, "mcp key must be present (and null) in the /health response"
        assert body["mcp"] is None, "mcp field must be null when mcp.enabled=False"

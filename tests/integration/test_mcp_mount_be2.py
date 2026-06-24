"""D9 / BE-2 — Mount the MCP HTTP app in create_app()'s lifespan + enable gate.

Covers:
- test_mcp_disabled_skips_mount (unit) — mcp.enabled=False → create_mcp_http_app()
  is never called and no /mcp route is registered.
- test_mcp_endpoint_responds_when_enabled (integration) — with mcp.enabled=True the
  /mcp mount exists (the request is handled by the MCP sub-app, not a 404 miss).
- test_rest_endpoint_unaffected (integration) — REST GET /health still works after mount.
- test_mcp_disabled_no_mount (integration) — mcp.enabled=False → /mcp returns 404.
- test_mcp_mounted_rejects_unauthenticated (integration) — POST /mcp without a bearer
  token returns 401 (APIKeyMiddleware fires on the mounted app).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.xdist_group("mcp")


# ---------------------------------------------------------------------------
# Unit — enable gate (no real FastMCP needed; create_mcp_http_app is mocked)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_mcp_disabled_skips_mount(tmp_path, monkeypatch) -> None:
    """mcp.enabled=False → create_mcp_http_app() is never called; no /mcp route."""
    with patch("archon_search.server.mcp.create_mcp_http_app") as mock_factory:
        with make_real_app(tmp_path, monkeypatch, mcp_enabled=False) as (client, _cfg, _key):
            # Factory must never be invoked when MCP is disabled.
            assert mock_factory.call_count == 0
            # No mount route registered on the FastAPI app.
            mounted = [
                r for r in client.app.routes if getattr(r, "path", "") == "/mcp"
            ]
            assert mounted == []


# ---------------------------------------------------------------------------
# Integration — real mount via FastMCP
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_mcp_endpoint_responds_when_enabled(tmp_path, monkeypatch) -> None:
    """With mcp.enabled=True, the /mcp mount is live and the FastMCP session
    manager task group is initialized — proven by a full JSON-RPC `initialize`
    handshake returning 200 (not 404, not a 4xx auth/transport rejection, and not
    a 500 from an uninitialized task group). See ADR 09, Proof 2.
    """
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "be2-test", "version": "1.0"},
                },
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert resp.status_code == 200, resp.text
        assert "mcp-session-id" in resp.headers


@pytest.mark.integration
def test_rest_endpoint_unaffected(tmp_path, monkeypatch) -> None:
    """REST GET /health still responds correctly after the MCP mount."""
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, _key):
        resp = client.get("/health")
        assert resp.status_code == 200


@pytest.mark.integration
def test_mcp_disabled_no_mount(tmp_path, monkeypatch) -> None:
    """mcp.enabled=False → /mcp returns 404 (no mount registered)."""
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=False) as (client, _cfg, api_key):
        resp = client.get("/mcp", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 404


@pytest.mark.integration
def test_mcp_mounted_rejects_unauthenticated(tmp_path, monkeypatch) -> None:
    """POST /mcp on the mounted app without a bearer token → 401.

    Verifies APIKeyMiddleware fires on the /mcp path after mount (not 404/500).
    """
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, _key):
        resp = client.post("/mcp", json={})
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.integration
def test_mcp_failure_does_not_block_rest(tmp_path, monkeypatch) -> None:
    """create_mcp_http_app() raising must NOT block REST startup.

    Exercises the `except Exception` fallback in create_app()'s lifespan: REST
    serves normally and /mcp is never mounted (404) when MCP construction fails.
    """
    with patch(
        "archon_search.server.mcp.create_mcp_http_app",
        side_effect=RuntimeError("boom"),
    ):
        with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
            assert client.get("/health").status_code == 200
            resp = client.get("/mcp", headers={"Authorization": f"Bearer {api_key}"})
            assert resp.status_code == 404

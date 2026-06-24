"""Tests for APIKeyMiddleware on the FastMCP HTTP transport ."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from archon_search.embedder_cache import EmbedderCache

pytestmark = pytest.mark.xdist_group("mcp")

VALID_KEY = "ab" * 32  # 64-char hex

# create_mcp_http_app() builds the endpoint at the sub-app root ("/") via
# FastMCP 3.4.x http_app(path="/") — so the JSON-RPC endpoint of the standalone
# (non-mounted) app is "/", not "/mcp". Auth-rejection tests still target "/mcp":
# APIKeyMiddleware fires before routing, so any non-exempt path returns 401.
# (fastmcp is a declared dependency; no sys.modules aliasing of a stub needed.)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline() -> MagicMock:
    from archon_search.pipeline import SearchPipelineResult

    pipeline = MagicMock()
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))
    return pipeline


def _make_starlette_client(valid_key: str = VALID_KEY) -> TestClient:
    """Build a TestClient against create_mcp_http_app() with a known key."""
    pipeline = _make_pipeline()
    with patch("archon_search.server.mcp.load_or_generate_key", return_value=(valid_key, "test")):
        from archon_search.server import mcp as mcp_module

        starlette_app = mcp_module.create_mcp_http_app(
            pipeline, "default", embedder_cache=EmbedderCache(max_size=3)
        )
    return TestClient(starlette_app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_mcp_http_rejects_unauthenticated_connection() -> None:
    """HTTP request to /mcp without Bearer token returns 401."""
    client = _make_starlette_client()
    response = client.post("/mcp", json={})
    assert response.status_code == 401


def test_mcp_http_accepts_valid_token() -> None:
    """HTTP request to the MCP endpoint with valid token proceeds (not 401)."""
    client = _make_starlette_client()
    response = client.post("/", headers={"Authorization": f"Bearer {VALID_KEY}"}, json={})
    # Not 401 — middleware passed; MCP may return other status for malformed payload
    assert response.status_code != 401


def test_mcp_health_exempt_from_auth() -> None:
    """GET /health on MCP app returns 200 without a token."""
    client = _make_starlette_client()
    response = client.get("/health")
    assert response.status_code == 200


def test_mcp_wrong_token_returns_401() -> None:
    """HTTP request to /mcp with wrong token returns 401."""
    client = _make_starlette_client(valid_key=VALID_KEY)
    wrong_key = "cc" * 32  # different valid-format key
    response = client.post("/mcp", headers={"Authorization": f"Bearer {wrong_key}"}, json={})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# X-Request-ID on MCP HTTP app — Task 2.2 (B1)
# ---------------------------------------------------------------------------

import re as _re
_REQUEST_ID_RE = _re.compile(r"^[A-Za-z0-9._-]{1,128}$")

_MCP_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.0.1"},
    },
}
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def test_mcp_request_id_on_valid_message() -> None:
    """MCP responses carry X-Request-ID header on a valid authenticated request."""
    pipeline = _make_pipeline()
    with patch("archon_search.server.mcp.load_or_generate_key", return_value=(VALID_KEY, "test")):
        from archon_search.server import mcp as mcp_module
        starlette_app = mcp_module.create_mcp_http_app(
            pipeline, "default", embedder_cache=EmbedderCache(max_size=3)
        )
    with TestClient(starlette_app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/",
            json=_MCP_INIT,
            headers={**_MCP_HEADERS, "Authorization": f"Bearer {VALID_KEY}"},
        )
    assert _REQUEST_ID_RE.match(resp.headers.get("x-request-id", "")), (
        f"Expected X-Request-ID header matching charset, got headers: {dict(resp.headers)}"
    )


def test_request_id_present_when_timings_disabled() -> None:
    """X-Request-ID header is present even when stage_timings_enabled=False."""
    from archon_search.config import ObservabilityConfig, SearchConfig

    pipeline = _make_pipeline()
    cfg = SearchConfig()
    cfg.observability = ObservabilityConfig(stage_timings_enabled=False)

    with patch("archon_search.server.mcp.load_or_generate_key", return_value=(VALID_KEY, "test")):
        from archon_search.server import mcp as mcp_module
        starlette_app = mcp_module.create_mcp_http_app(
            pipeline, "default", config=cfg, embedder_cache=EmbedderCache(max_size=3)
        )

    with TestClient(starlette_app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/",
            json=_MCP_INIT,
            headers={**_MCP_HEADERS, "Authorization": f"Bearer {VALID_KEY}"},
        )
    assert _REQUEST_ID_RE.match(resp.headers.get("x-request-id", "")), (
        f"X-Request-ID must be present when timings disabled, got: {dict(resp.headers)}"
    )

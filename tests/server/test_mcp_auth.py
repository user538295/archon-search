"""Tests for APIKeyMiddleware on the FastMCP HTTP transport (FEAT-045 Task 4.3)."""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

VALID_KEY = "ab" * 32  # 64-char hex

# Ensure fastmcp resolves to the real mcp.server.fastmcp so mcp.py can be imported
# and so we get a real Starlette HTTP app back (not a stub).
if "fastmcp" not in sys.modules:
    import mcp.server.fastmcp as _real_fastmcp
    sys.modules["fastmcp"] = _real_fastmcp  # type: ignore[assignment]


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

        starlette_app = mcp_module.create_mcp_http_app(pipeline, "default")
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
    """HTTP request to /mcp with valid token proceeds (not 401)."""
    client = _make_starlette_client()
    response = client.post("/mcp", headers={"Authorization": f"Bearer {VALID_KEY}"}, json={})
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

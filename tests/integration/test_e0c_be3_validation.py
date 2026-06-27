"""E0c / BE-3 — Config-wired fanout and top_k validation.

Tests that:
- SearchRequest.top_k no longer has a static le=100 Pydantic bound
- Fanout check reads config.max_fanout (not the removed _FANOUT_VALIDATION_LIMIT constant)
- top_k check reads config.top_k_max (not a static Field bound)
- Same checks apply to /explain and MCP explain tool

Scenarios covered: S8, S9, S11, S12, S14
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Unit-level: Pydantic no longer enforces le=100 on SearchRequest.top_k
# ---------------------------------------------------------------------------


def test_search_request_top_k_has_no_static_upper_bound() -> None:
    """SearchRequest(top_k=500) must parse cleanly — bound moved out of Field."""
    from archon_search.server.routes_search import SearchRequest

    req = SearchRequest(top_k=500, collection="c", query="q")
    assert req.top_k == 500


def test_explain_request_top_k_has_no_static_upper_bound() -> None:
    """ExplainRequest(top_k=500) must parse cleanly — le=100 bound removed from Field."""
    from archon_search.server.routes_explain import ExplainRequest

    req = ExplainRequest(top_k=500, collection="c", query="q")
    assert req.top_k == 500


# ---------------------------------------------------------------------------
# REST /search fanout validation from config.max_fanout
# ---------------------------------------------------------------------------


def test_fanout_respected_from_config_at_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """config max_fanout=3, POST /search with 3 collections → 200 (at the limit)."""
    with make_real_app(tmp_path, monkeypatch, max_fanout=3) as (client, _cfg, api_key):
        resp = client.post(
            "/search",
            json={"collections": ["a", "b", "c"], "query": "hello"},
            headers=_auth(api_key),
        )
        # Collections don't exist → 404, but not 422 fanout error. 200 or 404 are both fine.
        assert resp.status_code == 404, f"Expected 404 (past validation, collection not found), got {resp.status_code}: {resp.text}"


def test_fanout_exceeded_returns_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """config max_fanout=3, POST /search with 4 collections → 422 with correct message."""
    with make_real_app(tmp_path, monkeypatch, max_fanout=3) as (client, _cfg, api_key):
        resp = client.post(
            "/search",
            json={"collections": ["a", "b", "c", "d"], "query": "hello"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422
        assert "collections length exceeds maximum of 3" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# REST /search top_k validation from config.top_k_max
# ---------------------------------------------------------------------------


def test_top_k_at_max_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """config top_k_max=200, POST /search with top_k=200 → not 422."""
    with make_real_app(tmp_path, monkeypatch, top_k_max=200) as (client, _cfg, api_key):
        resp = client.post(
            "/search",
            json={"collection": "any", "query": "hello", "top_k": 200},
            headers=_auth(api_key),
        )
        # Collection doesn't exist → 404, not 422 top_k error
        assert resp.status_code == 404, f"Expected 404 (past validation, collection not found), got {resp.status_code}: {resp.text}"


def test_top_k_exceeded_returns_422_with_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """top_k=201 with top_k_max=200 → 422 with exact message."""
    with make_real_app(tmp_path, monkeypatch, top_k_max=200) as (client, _cfg, api_key):
        resp = client.post(
            "/search",
            json={"collection": "any", "query": "hello", "top_k": 201},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422
        assert "top_k 201 exceeds operator-configured maximum of 200" in resp.json()["detail"]


def test_top_k_default_100_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default top_k_max=100 config, top_k=100 → not rejected (S14)."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        # No top_k_max override — uses default 100
        assert _cfg.top_k_max == 100
        resp = client.post(
            "/search",
            json={"collection": "any", "query": "hello", "top_k": 100},
            headers=_auth(api_key),
        )
        # Collection doesn't exist → 404, but NOT 422 top_k error
        assert resp.status_code == 404, f"Expected 404 (default cap 100 should allow top_k=100), got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# REST /explain fanout and top_k validation
# ---------------------------------------------------------------------------


def test_explain_top_k_exceeded_returns_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """config top_k_max=200, POST /explain with top_k=201 → 422."""
    with make_real_app(tmp_path, monkeypatch, top_k_max=200) as (client, _cfg, api_key):
        resp = client.post(
            "/explain",
            json={"collection": "any", "query": "hello", "top_k": 201},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422
        assert "top_k 201 exceeds operator-configured maximum of 200" in resp.json()["detail"]


def test_explain_fanout_exceeded_returns_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """config max_fanout=3, POST /explain with 4 collections → 422."""
    with make_real_app(tmp_path, monkeypatch, max_fanout=3) as (client, _cfg, api_key):
        resp = client.post(
            "/explain",
            json={"collections": ["a", "b", "c", "d"], "query": "hello"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422
        assert "collections length exceeds maximum of 3" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# MCP search and explain fanout / top_k validation
# ---------------------------------------------------------------------------

def _mcp_headers(token: str, session_id: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return headers


def _mcp_initialize(client, token: str) -> str:
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "be3-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "MCP initialize must return mcp-session-id header"
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_mcp_headers(token, session_id),
    )
    return session_id


def _mcp_call_tool(client, token: str, session_id: str, tool_name: str, arguments: dict) -> dict:
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        headers=_mcp_headers(token, session_id),
    )
    assert resp.status_code == 200, f"MCP tools/call ({tool_name}) failed: {resp.status_code} {resp.text[:300]}"
    data_lines = [line[5:].strip() for line in resp.text.split("\n") if line.startswith("data:")]
    assert data_lines, f"No data: lines in SSE response for {tool_name}: {resp.text[:300]!r}"
    return json.loads(data_lines[-1])


def _get_tool_payload(result: dict, tool_name: str) -> dict:
    """Extract the parsed JSON payload from an MCP tool SSE response."""
    rpc_result = result.get("result", {})
    content = rpc_result.get("content", [])
    assert content, f"Tool '{tool_name}' returned empty content: {rpc_result!r}"
    return json.loads(content[0].get("text", "{}"))


@pytest.mark.xdist_group("mcp")
def test_mcp_search_fanout_exceeded_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP search with 4 collections when max_fanout=3 → error response (not exception)."""
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, max_fanout=3) as (client, _cfg, api_key):
        session_id = _mcp_initialize(client, api_key)
        result = _mcp_call_tool(
            client, api_key, session_id, "search",
            {"collections": ["a", "b", "c", "d"], "query": "hello"},
        )
        payload = _get_tool_payload(result, "search")
        assert "error" in payload
        assert "3" in payload["error"]  # max_fanout value in message


@pytest.mark.xdist_group("mcp")
def test_mcp_explain_top_k_exceeded_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP explain with top_k=201 when top_k_max=200 → error response."""
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, top_k_max=200) as (client, _cfg, api_key):
        session_id = _mcp_initialize(client, api_key)
        result = _mcp_call_tool(
            client, api_key, session_id, "explain",
            {"collection": "any", "query": "hello", "top_k": 201},
        )
        payload = _get_tool_payload(result, "explain")
        assert "error" in payload
        assert "201" in payload["error"]
        assert "200" in payload["error"]


@pytest.mark.xdist_group("mcp")
def test_mcp_explain_fanout_exceeded_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP explain with 4 collections when max_fanout=3 → error response."""
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, max_fanout=3) as (client, _cfg, api_key):
        session_id = _mcp_initialize(client, api_key)
        result = _mcp_call_tool(
            client, api_key, session_id, "explain",
            {"collections": ["a", "b", "c", "d"], "query": "hello"},
        )
        payload = _get_tool_payload(result, "explain")
        assert "error" in payload
        assert "3" in payload["error"]  # max_fanout value in message

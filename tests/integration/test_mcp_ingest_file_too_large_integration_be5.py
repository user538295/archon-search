"""BE-5 integration — real MCP app + oversized file → error result with code field.

Test:
- test_mcp_ingest_file_too_large_integration
    Real MCP app with max_file_mb=1, a file > 1 MB → MCP tool result has
    status="error", code="file_too_large", actionable message (S4 integration half).
"""
from __future__ import annotations

import json

import pytest

from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# MCP JSON-RPC helpers (mirrored from existing roundtrip tests)
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
    """Send MCP initialize + notifications/initialized; return session_id."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "be5-test", "version": "1.0"},
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
    """Call an MCP tool; return the parsed SSE result payload."""
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
    assert resp.status_code == 200, (
        f"MCP tools/call ({tool_name}) failed: {resp.status_code} {resp.text[:300]}"
    )
    data_lines = [
        line[5:].strip()
        for line in resp.text.split("\n")
        if line.startswith("data:")
    ]
    assert data_lines, f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
    return json.loads(data_lines[-1])


def _extract_tool_text(result: dict, tool_name: str):
    """Extract and parse the JSON text from an MCP tool response."""
    rpc_result = result.get("result")
    assert rpc_result is not None, (
        f"Tool '{tool_name}' RPC result missing 'result' key: {result!r}"
    )
    content = rpc_result.get("content", [])
    assert content, f"Tool '{tool_name}' returned empty content list: {rpc_result!r}"
    text = content[0].get("text", "")
    assert text, f"Tool '{tool_name}' returned empty text: {content!r}"
    return json.loads(text)


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


def test_mcp_ingest_file_too_large_integration(tmp_path, monkeypatch) -> None:
    """Real MCP app: ingest_file with a file > max_file_mb → result has code='file_too_large'.

    Scenario S4 (integration half).
    """
    # Write a file that exceeds the 1 MB limit (1 MB + 1 byte)
    oversized_file = tmp_path / "big.txt"
    oversized_file.write_bytes(b"x" * (1024 * 1024 + 1))

    toml_content = "[ingest]\nmax_file_mb = 1\n"

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, toml_content=toml_content) as (
        client,
        _cfg,
        api_key,
    ):
        session_id = _mcp_initialize(client, api_key)

        ingest_result = _mcp_call_tool(
            client, api_key, session_id,
            "ingest_file",
            {"path": str(oversized_file), "collection": "test-col"},
        )
        parsed = _extract_tool_text(ingest_result, "ingest_file")

        assert isinstance(parsed, dict), f"Expected dict, got {type(parsed)}: {parsed!r}"
        assert parsed.get("status") == "error", f"Expected status='error': {parsed!r}"
        assert parsed.get("code") == "file_too_large", (
            f"Expected code='file_too_large': {parsed!r}"
        )
        assert "error" in parsed, f"'error' key missing: {parsed!r}"
        assert "[ingest].max_file_mb" in parsed["error"], (
            f"Actionable message not in error field: {parsed['error']!r}"
        )
        assert parsed.get("chunks_created") == 0, (
            f"Expected chunks_created=0 for oversized file: {parsed!r}"
        )

"""T-2: E2e test — MCP ingest_file returns file_too_large code.

Plan: Documentation/Backlog/e0d-pdf-large-file-support-team-plan.md Task T-2.

Tests:
- test_e2e_mcp_ingest_file_too_large
    MCP TestClient ingest_file with oversized path → status="error",
    code="file_too_large"; message is actionable.

Scenario S4 (e2e).
"""
from __future__ import annotations

import json

import pytest

from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]

ONE_MB = 1 * 1024 * 1024


# ---------------------------------------------------------------------------
# MCP JSON-RPC helpers
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
                "clientInfo": {"name": "t2-e2e-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, (
        f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    )
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
    assert data_lines, (
        f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
    )
    return json.loads(data_lines[-1])


def _extract_tool_text(result: dict, tool_name: str):
    """Extract and parse the JSON text from an MCP tool response.

    Note: does NOT assert isError=False because error responses from the
    ingest_file tool are represented as application-level errors (the tool
    returns a well-formed result dict with status='error'), not as
    envelope-level exceptions.
    """
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
# T-2 e2e test: MCP ingest_file file_too_large (S4)
# ---------------------------------------------------------------------------


def test_e2e_mcp_ingest_file_too_large(tmp_path, monkeypatch) -> None:
    """MCP TestClient ingest_file with oversized path → status="error",
    code="file_too_large"; message is actionable.

    Completes: S4

    Uses a real file (1 MB + 1 byte) with max_file_mb=1 configured.
    The pipeline guard in pipeline.ingest_file() catches the oversize before
    any parsing, returning an error IngestResult that the MCP ingest_file tool
    serializes with the code field.

    Verifies:
    - status == "error"
    - code == "file_too_large"
    - error message contains file size, limit, and "[ingest].max_file_mb"
    - chunks_created == 0
    - No envelope-level exception (isError not set or False)
    """
    oversized_file = tmp_path / "large.pdf"
    oversized_file.write_bytes(b"x" * (ONE_MB + 1))

    toml_content = "[ingest]\nmax_file_mb = 1\n"

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True, toml_content=toml_content) as (
        client,
        _cfg,
        api_key,
    ):
        assert _cfg.ingest.max_file_mb == 1, (
            f"Expected max_file_mb=1 from toml, got {_cfg.ingest.max_file_mb}"
        )

        session_id = _mcp_initialize(client, api_key)

        raw = _mcp_call_tool(
            client, api_key, session_id,
            "ingest_file",
            {"path": str(oversized_file), "collection": "test-collection"},
        )

        # The tool must not produce an envelope-level exception
        rpc_result = raw.get("result", {})
        assert not rpc_result.get("isError"), (
            f"ingest_file must not raise an unhandled exception for file_too_large; "
            f"got isError=True: {rpc_result!r}"
        )

        parsed = _extract_tool_text(raw, "ingest_file")

        assert isinstance(parsed, dict), (
            f"Expected dict result from ingest_file, got {type(parsed)}: {parsed!r}"
        )
        assert parsed.get("status") == "error", (
            f"Expected status='error' for oversized file, got: {parsed!r}"
        )
        assert parsed.get("code") == "file_too_large", (
            f"Expected code='file_too_large' for oversized file, got: {parsed!r}"
        )
        assert "error" in parsed, f"'error' key missing from result: {parsed!r}"

        error_msg = parsed["error"]
        # Message must name the file size and the limit.
        # File is ONE_MB + 1 bytes; math.ceil(1_048_577 / 1_048_576) = 2, so "2 MB".
        # The configured limit is max_file_mb=1, so "1 MB".
        assert "[ingest].max_file_mb" in error_msg, (
            f"Actionable config key '[ingest].max_file_mb' missing from error message: {error_msg!r}"
        )
        assert "2 MB" in error_msg, (
            f"File size '2 MB' (math.ceil of ONE_MB+1) not mentioned in error message: {error_msg!r}"
        )
        assert "1 MB" in error_msg, (
            f"Limit '1 MB' not mentioned in error message: {error_msg!r}"
        )
        assert parsed.get("chunks_created") == 0, (
            f"Expected chunks_created=0 for rejected file, got: {parsed!r}"
        )

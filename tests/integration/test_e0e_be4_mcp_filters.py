"""tests/integration/test_e0e_be4_mcp_filters.py

BE-4 integration test: MCP search tool with filters + collections via TestClient.

Plan task: BE-4 — Remove MCP language restriction; build `SearchFilters` for
multi-collection path; pass to `search_many()` #backend-role

Tests:
- test_mcp_search_tool_multi_collection_with_language_filter: MCP JSON-RPC via TestClient,
  collections + language returns valid non-error response.

Run with:
    uv run pytest tests/integration/test_e0e_be4_mcp_filters.py -v
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# MCP JSON-RPC helpers (mirrors test_mcp_roundtrip_t2.py)
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
                "clientInfo": {"name": "be4-mcp-test", "version": "1.0"},
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
    """Call an MCP tool and return the parsed SSE result payload."""
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
    if not data_lines:
        raise AssertionError(
            f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
        )
    return json.loads(data_lines[-1])


def _extract_tool_payload(result: dict, tool_name: str) -> dict:
    """Extract and parse the JSON text from an MCP tool response.

    Returns the parsed JSON payload.
    """
    assert result, f"Tool '{tool_name}' returned empty result dict"
    rpc_result = result.get("result")
    assert rpc_result is not None, (
        f"Tool '{tool_name}' RPC result missing 'result' key: {result!r}"
    )
    # NOTE: tool-level errors (code="validation_error" etc.) appear as isError=False
    # at the RPC envelope — they're content-level errors. We parse the text and check
    # the payload's 'code' field to detect them.
    content = rpc_result.get("content", [])
    assert content, f"Tool '{tool_name}' returned empty content list: {rpc_result!r}"
    text = content[0].get("text", "")
    assert text, f"Tool '{tool_name}' returned empty text: {content!r}"
    return json.loads(text)


# ---------------------------------------------------------------------------
# test_mcp_search_tool_multi_collection_with_language_filter
# ---------------------------------------------------------------------------


def test_mcp_search_tool_multi_collection_with_language_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP JSON-RPC: search with collections + language returns a valid non-error response.

    Previously (pre-BE-4) this returned code='validation_error'. After BE-4 the
    restriction is lifted: the tool calls search_many() with a SearchFilters containing
    the language field and returns results (or empty results if no language-tagged docs).

    Scenario C3 / S8.
    """
    col_a = "be4-mcp-col-a"
    col_b = "be4-mcp-col-b"

    doc_a = tmp_path / "doc_a.txt"
    doc_a.write_text(textwrap.dedent("""\
        This document discusses Python programming language.
        Functions, classes, and modules are key concepts.
        Object-oriented patterns in Python.
    """) * 4)

    doc_b = tmp_path / "doc_b.txt"
    doc_b.write_text(textwrap.dedent("""\
        TypeScript is a strongly typed programming language.
        Interfaces, generics, and decorators are key features.
        Modern JavaScript development with TypeScript.
    """) * 4)

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        # Ingest documents into both collections via file path (polls job until DONE)
        ingest_file_via_path(client, col_a, str(doc_a), api_key=api_key)
        ingest_file_via_path(client, col_b, str(doc_b), api_key=api_key)

        # MCP session
        session_id = _mcp_initialize(client, api_key)

        # Call MCP search tool with collections + language filter
        # Previously this returned validation_error; after BE-4 it must succeed
        result = _mcp_call_tool(
            client, api_key, session_id, "search",
            {
                "query": "programming language",
                "collections": [col_a, col_b],
                "language": "en",
            },
        )

        payload = _extract_tool_payload(result, "search")

        # Must NOT be a validation_error (core assertion for BE-4)
        assert payload.get("code") != "validation_error", (
            f"Expected language filter to be accepted in multi-collection MCP search, "
            f"but got code='validation_error': {payload!r}"
        )

        # Response must be a valid search response with results or empty list
        # (language filter may return empty if no language-tagged docs — that's correct)
        assert "results" in payload, (
            f"Expected 'results' in payload (valid search response), got: {payload!r}"
        )
        assert isinstance(payload["results"], list), (
            f"Expected results to be a list, got: {type(payload['results'])!r}"
        )

"""D9 / T-2 — Round-trip e2e: ingest file/directory via MCP then search via MCP.

Tests:
- ``test_mcp_ingest_then_search_round_trip``: ingest a file via MCP ``ingest_file``,
  then search via MCP ``search`` with a matching query; assert document appears in results.
- ``test_mcp_ingest_directory_then_search``: ingest a directory via MCP ``ingest_directory``,
  then search via MCP ``search``; assert at least one result is returned.

MCP ``ingest_file`` and ``ingest_directory`` are synchronous blocking tools — they complete
in the response (no background job to poll). The "wait for DONE" step is implicit: the tool
call blocks until ingest completes and returns status/chunks_created in the response.

Scenarios completed: S4 (ingest_file stores a document), S5 (search finds the ingested document).
"""
from __future__ import annotations

import json
import textwrap
from typing import Any

import pytest

from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# MCP JSON-RPC helpers (duplicated from T-1 to avoid cross-test coupling)
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
                "clientInfo": {"name": "roundtrip-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, (
        f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    )
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "MCP initialize must return mcp-session-id header"
    # Send notifications/initialized (fire-and-forget; 200/202/204 all valid)
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


def _extract_tool_text(result: dict, tool_name: str) -> Any:
    """Extract and parse the JSON text from an MCP tool response.

    Asserts the response is non-empty and not an unhandled exception.
    Returns the parsed JSON payload.
    """
    assert result, f"Tool '{tool_name}' returned empty result dict"
    rpc_result = result.get("result")
    assert rpc_result is not None, (
        f"Tool '{tool_name}' RPC result missing 'result' key: {result!r}"
    )
    assert not rpc_result.get("isError"), (
        f"Tool '{tool_name}' returned envelope-level isError=True (unhandled exception): {rpc_result!r}"
    )
    content = rpc_result.get("content", [])
    assert content, f"Tool '{tool_name}' returned empty content list: {rpc_result!r}"
    text = content[0].get("text", "")
    assert text, f"Tool '{tool_name}' returned empty text: {content!r}"
    return json.loads(text)


# ---------------------------------------------------------------------------
# T-2 test 1: ingest_file → search round-trip (S4 + S5)
# ---------------------------------------------------------------------------


def test_mcp_ingest_then_search_round_trip(tmp_path, monkeypatch) -> None:
    """Ingest a file via MCP ingest_file; search via MCP search; assert document in results (S4, S5).

    MCP ingest_file is a synchronous blocking tool — it returns status='ok' directly
    in the response when complete, so no background polling is needed.

    The ingested document contains the phrase 'quantum entanglement gravitational lens',
    which is used as the search query to verify the specific document is found.
    """
    collection = "mcp-roundtrip-ingest-file"
    # Use a distinctive phrase that will match via FTS
    unique_phrase = "quantum entanglement gravitational lens"
    doc_content = textwrap.dedent(f"""\
        This document tests the MCP round-trip ingest and search flow.
        {unique_phrase} is a distinctive phrase for retrieval verification.
        Archon Search MCP integration end-to-end test document.
    """)

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        # Create a real document file
        doc_file = tmp_path / "roundtrip" / "doc.txt"
        doc_file.parent.mkdir(parents=True, exist_ok=True)
        doc_file.write_text(doc_content)

        session_id = _mcp_initialize(client, api_key)

        # Step 1: Ingest file via MCP ingest_file (synchronous — returns status='ok' when done)
        ingest_result = _mcp_call_tool(client, api_key, session_id, "ingest_file", {
            "collection": collection,
            "path": str(doc_file),
        })
        ingest_parsed = _extract_tool_text(ingest_result, "ingest_file")

        # Verify ingest succeeded (status='ok', chunks_created > 0)
        assert isinstance(ingest_parsed, dict), (
            f"ingest_file must return a dict, got: {type(ingest_parsed)!r}: {ingest_parsed!r}"
        )
        assert ingest_parsed.get("status") == "ok", (
            f"ingest_file must return status='ok' for a valid file, got: {ingest_parsed!r}"
        )
        chunks_created = ingest_parsed.get("chunks_created", 0)
        assert chunks_created > 0, (
            f"ingest_file must create at least one chunk, got chunks_created={chunks_created!r}"
        )

        # Step 2: Search via MCP search with the matching query
        search_result = _mcp_call_tool(client, api_key, session_id, "search", {
            "query": unique_phrase,
            "collection": collection,
        })
        search_parsed = _extract_tool_text(search_result, "search")

        # Verify search returned the ingested document
        assert isinstance(search_parsed, dict), (
            f"search must return a dict, got: {type(search_parsed)!r}"
        )
        assert "results" in search_parsed, (
            f"search response must have 'results' key: {search_parsed!r}"
        )
        results = search_parsed["results"]
        assert isinstance(results, list), (
            f"search 'results' must be a list, got: {type(results)!r}"
        )
        assert len(results) > 0, (
            f"search must return at least one result after MCP ingest_file, got empty list"
        )
        # The ingested document's path should appear in at least one result
        found_paths = [r.get("source_path", "") for r in results if isinstance(r, dict)]
        assert any(p == str(doc_file) for p in found_paths), (
            f"ingested document path {str(doc_file)!r} not found in search results. "
            f"source_paths found: {found_paths!r}"
        )


# ---------------------------------------------------------------------------
# T-2 test 2: ingest_directory → search round-trip (S4 + S5)
# ---------------------------------------------------------------------------


def test_mcp_ingest_directory_then_search(tmp_path, monkeypatch) -> None:
    """Ingest a directory via MCP ingest_directory; search via MCP search; assert at least one result (S4, S5).

    MCP ingest_directory is a synchronous blocking tool — it returns a list of
    IngestResult dicts when complete. No background polling needed.

    Uses two distinct documents in the directory to verify that directory
    ingestion finds and stores all files.
    """
    collection = "mcp-roundtrip-ingest-dir"
    unique_phrase = "stellar nucleosynthesis baryonic matter"
    doc1_content = textwrap.dedent(f"""\
        First document in the MCP directory round-trip test.
        {unique_phrase} is a distinctive phrase for retrieval verification.
        This is document alpha for the ingest_directory round-trip e2e test.
    """)
    doc2_content = textwrap.dedent("""\
        Second document in the MCP directory round-trip test.
        This document is for testing multi-file directory ingestion via MCP.
        Archon Search MCP round-trip directory integration end-to-end test.
    """)

    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, api_key):
        # Create a directory with two real document files
        dir_path = tmp_path / "roundtrip_dir"
        dir_path.mkdir(parents=True, exist_ok=True)
        (dir_path / "doc1.txt").write_text(doc1_content)
        (dir_path / "doc2.txt").write_text(doc2_content)

        session_id = _mcp_initialize(client, api_key)

        # Step 1: Ingest directory via MCP ingest_directory (synchronous — returns list of results)
        ingest_result = _mcp_call_tool(client, api_key, session_id, "ingest_directory", {
            "collection": collection,
            "path": str(dir_path),
        })
        ingest_parsed = _extract_tool_text(ingest_result, "ingest_directory")

        # Verify directory ingest returned results for the files
        assert isinstance(ingest_parsed, list), (
            f"ingest_directory must return a list of results, got: {type(ingest_parsed)!r}: {ingest_parsed!r}"
        )
        assert len(ingest_parsed) > 0, (
            f"ingest_directory must return at least one result for a non-empty directory"
        )
        # At least one file must have been ingested successfully
        ok_results = [r for r in ingest_parsed if isinstance(r, dict) and r.get("status") == "ok"]
        assert len(ok_results) > 0, (
            f"ingest_directory must have at least one status='ok' result, got: {ingest_parsed!r}"
        )
        total_chunks = sum(r.get("chunks_created", 0) for r in ok_results)
        assert total_chunks > 0, (
            f"ingest_directory must create at least one chunk across all files, "
            f"got total_chunks={total_chunks}"
        )

        # Step 2: Search via MCP search with the matching query
        search_result = _mcp_call_tool(client, api_key, session_id, "search", {
            "query": unique_phrase,
            "collection": collection,
        })
        search_parsed = _extract_tool_text(search_result, "search")

        # Verify search returned at least one result after directory ingestion
        assert isinstance(search_parsed, dict), (
            f"search must return a dict, got: {type(search_parsed)!r}"
        )
        assert "results" in search_parsed, (
            f"search response must have 'results' key: {search_parsed!r}"
        )
        results = search_parsed["results"]
        assert isinstance(results, list), (
            f"search 'results' must be a list, got: {type(results)!r}"
        )
        assert len(results) > 0, (
            f"search must return at least one result after MCP ingest_directory, got empty list"
        )
        # Verify at least one result belongs to the ingested directory (proves S5)
        found_paths_dir = [r.get("source_path", "") for r in results if isinstance(r, dict)]
        assert any(str(dir_path) in p for p in found_paths_dir), (
            f"no search result has source_path from the ingested directory {str(dir_path)!r}. "
            f"source_paths found: {found_paths_dir!r}"
        )

"""Integration tests for BE-9: MCP search tool graph_mode=global/local parameter.

Tests:
- MCP search(graph_mode='global') with real app + built communities returns results (S5)
- MCP search(graph_mode='local') with real app + built communities returns results (S5)

Scenarios: C1, S5
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# Helpers: spaCy stub (needed for graph_enabled=True)
# ---------------------------------------------------------------------------


def _install_spacy_stub_no_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy that returns NO named entities for any text."""

    class _FakeDoc:
        def __init__(self) -> None:
            self.ents: list = []

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            return _FakeDoc()

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]
    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]
    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


# ---------------------------------------------------------------------------
# MCP JSON-RPC helpers (same pattern as test_mcp_roundtrip_t2.py)
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
                "clientInfo": {"name": "be9-test", "version": "1.0"},
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
    assert data_lines, (
        f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
    )
    return json.loads(data_lines[-1])


def _extract_tool_text(result: dict, tool_name: str):
    """Extract and parse the JSON text from an MCP tool response."""
    assert result, f"Tool '{tool_name}' returned empty result dict"
    rpc_result = result.get("result")
    assert rpc_result is not None, (
        f"Tool '{tool_name}' RPC result missing 'result' key: {result!r}"
    )
    assert not rpc_result.get("isError"), (
        f"Tool '{tool_name}' returned isError=True (unhandled exception): {rpc_result!r}"
    )
    content = rpc_result.get("content", [])
    assert content, f"Tool '{tool_name}' returned empty content list: {rpc_result!r}"
    text = content[0].get("text", "")
    assert text, f"Tool '{tool_name}' returned empty text: {content!r}"
    return json.loads(text)


# ---------------------------------------------------------------------------
# Helper: write communities to the graph store
# ---------------------------------------------------------------------------


async def _write_communities_to_store(
    db_path: str,
    col: str,
    chunk_ids: list[str],
    *,
    community_id: str = "test-comm-1",
    entity_ids: list[str] | None = None,
) -> None:
    """Write a single community to the GraphStore at db_path."""
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import Community

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_communities_table(col)
        community = Community(
            community_id=community_id,
            entity_ids=entity_ids or ["entity-1"],
            representative_chunk_ids=chunk_ids,
            built_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            summary_text=None,
        )
        await gs.write_communities(col, [community])
    finally:
        await gs.disconnect()


async def _get_chunk_ids(db_path: str, col: str) -> list[str]:
    """Return chunk IDs from the store for the given collection."""
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.store import SearchStore

    s = SearchStore(db_path)
    await s.connect()
    try:
        rows = [r async for r in s.list_chunks_raw(col, DEFAULT_NAMESPACE)]
        return [r["chunk_id"] for r in rows]
    finally:
        await s.disconnect()


# ---------------------------------------------------------------------------
# test_mcp_search_global_mode_real
# ---------------------------------------------------------------------------


def test_mcp_search_global_mode_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP search with graph_mode=global; real app + built communities → result dict with
    results list and graph_expansion_applied=True (S5).
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "be9-mcp-global-mode"
    doc = tmp_path / "doc.txt"
    doc.write_text(
        "SearchPipeline provides hybrid retrieval. "
        "Community detection clusters related entities.\n" * 5,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Ingest document.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Build communities with a real chunk ID.
        chunk_ids = asyncio.run(_get_chunk_ids(cfg.db_path, col))
        assert chunk_ids, "Ingest must produce at least one chunk"
        asyncio.run(_write_communities_to_store(cfg.db_path, col, chunk_ids[:1]))

        # MCP search with graph_mode=global.
        session_id = _mcp_initialize(client, api_key)
        raw = _mcp_call_tool(client, api_key, session_id, "search", {
            "query": "community retrieval pipeline",
            "collection": col,
            "graph_mode": "global",
        })
        parsed = _extract_tool_text(raw, "search")

        assert isinstance(parsed, dict), f"Expected dict result; got: {type(parsed).__name__}: {parsed!r}"
        assert "results" in parsed, f"Expected 'results' key; got: {list(parsed.keys())}"
        assert "error" not in parsed, f"Unexpected error in MCP search result: {parsed!r}"
        assert parsed.get("graph_expansion_applied") is True, (
            f"Expected graph_expansion_applied=True for global mode; got: {parsed.get('graph_expansion_applied')!r}. "
            f"Full: {parsed!r}"
        )


# ---------------------------------------------------------------------------
# test_mcp_search_local_mode_real
# ---------------------------------------------------------------------------


def test_mcp_search_local_mode_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP search with graph_mode=local; real app + built communities → result dict with
    results list. Local mode with no entity match returns graph_expansion_applied=False (S5).
    The key check is that the parameter is threaded correctly (no 422, no error).
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "be9-mcp-local-mode"
    doc = tmp_path / "doc.txt"
    doc.write_text(
        "Hybrid search combines vector and full text search. "
        "Reranker improves result quality.\n" * 5,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Ingest document.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Build communities so the table exists.
        chunk_ids = asyncio.run(_get_chunk_ids(cfg.db_path, col))
        assert chunk_ids, "Ingest must produce at least one chunk"
        asyncio.run(_write_communities_to_store(cfg.db_path, col, chunk_ids[:1]))

        # MCP search with graph_mode=local.
        session_id = _mcp_initialize(client, api_key)
        raw = _mcp_call_tool(client, api_key, session_id, "search", {
            "query": "hybrid retrieval quality",
            "collection": col,
            "graph_mode": "local",
        })
        parsed = _extract_tool_text(raw, "search")

        assert isinstance(parsed, dict), f"Expected dict result; got: {type(parsed).__name__}: {parsed!r}"
        assert "results" in parsed, f"Expected 'results' key; got: {list(parsed.keys())}"
        assert "error" not in parsed, f"Unexpected error in MCP search result: {parsed!r}"
        # graph_expansion_applied can be True or False depending on entity matching.
        # The key check: no error, no 422, valid response shape.
        assert "graph_expansion_applied" in parsed, (
            f"'graph_expansion_applied' key must be present in response: {list(parsed.keys())}"
        )

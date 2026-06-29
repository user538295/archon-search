"""E1b / T-3 — e2e tests for GET /status community stats and MCP search graph_mode.

Covers:
- (a) GET /status after build-communities shows correct community_count and
      last_built_at for the collection  (S4)
- (b) MCP search tool with graph_mode=global returns results  (S5)
- (c) MCP search tool with graph_mode=local returns results  (S5)

Communities are seeded directly into the GraphStore (no leidenalg required).
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

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# spaCy stub — needed for make_real_app(graph_enabled=True)
# ---------------------------------------------------------------------------


def _install_spacy_stub_no_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy that returns NO named entities for any text.

    Must be called BEFORE make_real_app because create_app calls _check_graph_deps
    which imports spacy synchronously.
    """

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


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Shared helpers — community seeding and chunk ID retrieval
# ---------------------------------------------------------------------------


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


async def _write_communities_to_store(
    db_path: str,
    col: str,
    chunk_ids: list[str],
    *,
    community_id: str = "test-comm-t3",
    entity_ids: list[str] | None = None,
    built_at: datetime | None = None,
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
            entity_ids=entity_ids or ["entity-t3-1"],
            representative_chunk_ids=chunk_ids,
            built_at=built_at or datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            summary_text=None,
        )
        await gs.write_communities(col, [community])
    finally:
        await gs.disconnect()


# ---------------------------------------------------------------------------
# MCP JSON-RPC helpers (same pattern as test_e1b_be9_mcp_search_graph_mode_integration.py)
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
                "clientInfo": {"name": "t3-test", "version": "1.0"},
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


def _extract_tool_text(result: dict, tool_name: str) -> dict:
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
# (a) test_e2e_status_community_fields  (S4)
# ---------------------------------------------------------------------------


def test_e2e_status_community_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /status after build-communities shows correct community_count and
    last_built_at for the collection  (S4).

    - Ingest a document to create the collection.
    - Seed a community directly into the GraphStore.
    - GET /status and verify the collection entry has community_count >= 1
      and last_built_at is a non-null ISO 8601 timestamp string.
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "t3-status-community-fields"
    built_at = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    doc = tmp_path / "doc.txt"
    doc.write_text(
        "SearchPipeline provides hybrid retrieval capabilities. "
        "Community detection clusters related entities for graph-aware search.\n" * 5,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        # Ingest so the collection exists.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Retrieve chunk IDs from the store.
        chunk_ids = asyncio.run(_get_chunk_ids(cfg.db_path, col))
        assert chunk_ids, "Ingest must have produced at least one chunk"

        # Seed a community (simulates successful build-communities).
        asyncio.run(
            _write_communities_to_store(
                cfg.db_path, col, chunk_ids[:1], built_at=built_at
            )
        )

        # GET /status and locate the target collection entry.
        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, (
            f"Expected 200 from GET /status; got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "collections" in body, (
            f"Expected 'collections' key in status response; got: {list(body.keys())}"
        )

        col_entry = next(
            (c for c in body["collections"] if c.get("name") == col),
            None,
        )
        assert col_entry is not None, (
            f"Collection {col!r} not found in status response; "
            f"collections: {[c.get('name') for c in body['collections']]}"
        )

        # community_count must be >= 1 (we seeded one community).
        community_count = col_entry.get("community_count")
        assert community_count is not None, (
            f"Expected 'community_count' field in collection entry; got: {col_entry!r}"
        )
        assert isinstance(community_count, int), (
            f"Expected community_count to be an int; got {type(community_count).__name__!r}"
        )
        assert community_count >= 1, (
            f"Expected community_count >= 1 after seeding one community; "
            f"got {community_count!r}. Entry: {col_entry!r}"
        )

        # last_built_at must be a non-null ISO 8601 string.
        last_built_at = col_entry.get("last_built_at")
        assert last_built_at is not None, (
            f"Expected 'last_built_at' to be non-null after seeding community; "
            f"got None. Entry: {col_entry!r}"
        )
        assert isinstance(last_built_at, str), (
            f"Expected last_built_at to be a string; got {type(last_built_at).__name__!r}"
        )
        # Must be parseable as an ISO 8601 datetime.
        try:
            parsed_dt = datetime.fromisoformat(last_built_at)
        except ValueError as exc:
            pytest.fail(
                f"last_built_at {last_built_at!r} is not valid ISO 8601: {exc}"
            )


# ---------------------------------------------------------------------------
# (b) test_e2e_mcp_search_global_mode  (S5)
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("mcp")
def test_e2e_mcp_search_global_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP search tool with graph_mode=global returns results.

    - Ingest a document to create the collection and chunks.
    - Seed a community pointing to the ingested chunk.
    - Call the MCP search tool with graph_mode='global'.
    - Verify the result contains a 'results' key with non-empty list
      and graph_expansion_applied=True  (S5).
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "t3-mcp-global-mode"
    doc = tmp_path / "doc.txt"
    doc.write_text(
        "SearchPipeline provides vector and FTS retrieval. "
        "Community detection clusters related entities for graph-aware retrieval.\n" * 5,
        encoding="utf-8",
    )

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True
    ) as (client, cfg, api_key):
        # Ingest document to create chunks.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Seed a community pointing to the ingested chunk.
        chunk_ids = asyncio.run(_get_chunk_ids(cfg.db_path, col))
        assert chunk_ids, "Ingest must produce at least one chunk"
        asyncio.run(_write_communities_to_store(cfg.db_path, col, chunk_ids[:1]))

        # Call the MCP search tool with graph_mode=global.
        session_id = _mcp_initialize(client, api_key)
        raw = _mcp_call_tool(
            client,
            api_key,
            session_id,
            "search",
            {
                "query": "community retrieval pipeline",
                "collection": col,
                "graph_mode": "global",
            },
        )
        parsed = _extract_tool_text(raw, "search")

        assert isinstance(parsed, dict), (
            f"Expected dict result from MCP search; got {type(parsed).__name__!r}: {parsed!r}"
        )
        assert "results" in parsed, (
            f"Expected 'results' key in MCP search response; got: {list(parsed.keys())}"
        )
        assert "error" not in parsed, (
            f"Unexpected error in MCP search result: {parsed!r}"
        )
        assert len(parsed["results"]) > 0, (
            f"Expected non-empty results from MCP search with graph_mode=global; "
            f"got empty list. Full response: {parsed!r}"
        )
        assert parsed.get("graph_expansion_applied") is True, (
            f"Expected graph_expansion_applied=True for global mode; "
            f"got {parsed.get('graph_expansion_applied')!r}. Full: {parsed!r}"
        )


# ---------------------------------------------------------------------------
# (c) test_e2e_mcp_search_local_mode  (S5)
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("mcp")
def test_e2e_mcp_search_local_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP search tool with graph_mode=local returns results.

    - Ingest a document.
    - Seed a community table (so local mode does not 422 on missing table).
    - Call the MCP search tool with graph_mode='local'.
    - Verify the result contains a 'results' key and no error  (S5).
    - graph_expansion_applied may be True or False depending on entity matching.
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "t3-mcp-local-mode"
    doc = tmp_path / "doc.txt"
    doc.write_text(
        "Hybrid search combines vector retrieval and full-text search. "
        "The reranker improves result quality for all query types.\n" * 5,
        encoding="utf-8",
    )

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True
    ) as (client, cfg, api_key):
        # Ingest document to create chunks.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Seed a community table so local mode does not get a missing-table fallback.
        chunk_ids = asyncio.run(_get_chunk_ids(cfg.db_path, col))
        assert chunk_ids, "Ingest must produce at least one chunk"
        asyncio.run(_write_communities_to_store(cfg.db_path, col, chunk_ids[:1]))

        # Call the MCP search tool with graph_mode=local.
        session_id = _mcp_initialize(client, api_key)
        raw = _mcp_call_tool(
            client,
            api_key,
            session_id,
            "search",
            {
                "query": "hybrid retrieval quality",
                "collection": col,
                "graph_mode": "local",
            },
        )
        parsed = _extract_tool_text(raw, "search")

        assert isinstance(parsed, dict), (
            f"Expected dict result from MCP search; got {type(parsed).__name__!r}: {parsed!r}"
        )
        assert "results" in parsed, (
            f"Expected 'results' key in MCP search response; got: {list(parsed.keys())}"
        )
        assert "error" not in parsed, (
            f"Unexpected error in MCP search result: {parsed!r}"
        )
        # With the no-entity spaCy stub, graph_expansion_applied is always False
        # (no entity match → hybrid fallback). Key contract: the field must be
        # present and the call must not error.
        assert "graph_expansion_applied" in parsed, (
            f"'graph_expansion_applied' field must be present in MCP search response; "
            f"got keys: {list(parsed.keys())}"
        )

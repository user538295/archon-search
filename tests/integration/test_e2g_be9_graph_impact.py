"""Route + MCP integration tests for E2g BE-9: graph_impact on REST and MCP.

Tests verify:
- REST GET /graph/{collection}/impact/{symbol} guards graph.enabled=false → 422
- MCP graph_impact tool returns McpErrorResponse (not a raised exception) when
  graph.enabled=false
- A real request returns the grouped (direct/indirect per side), capped,
  PageRank-ordered response with no shape transform relative to
  GraphStore.compute_impact's ImpactResult
- The MCP tool's real response matches the REST route's shape for the same query
- file_path actually reaches GraphStore.compute_impact on both surfaces
  (ambiguous-symbol fixture)
- Omitting depth/direction on both surfaces applies depth=2, direction="both"
  defaults before calling compute_impact
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from archon_search.graph_store import GraphStore
from archon_search.graph_types import (
    DEFAULT_IMPACT_DEPTH,
    EntityType,
    GraphEdge,
    GraphNode,
    ImpactDirection,
    RelationshipType,
    make_code_symbol_qualified_name,
    make_stable_edge_id,
    make_stable_entity_id,
)
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a stub spaCy module that returns NO named entities.

    Must be called BEFORE make_real_app(graph_enabled=True) because create_app
    calls _check_graph_deps which imports spaCy synchronously.
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


def _symbol(
    name: str,
    source_path: str | None = None,
    pagerank_score: float | None = None,
) -> GraphNode:
    """Build a code_symbol GraphNode. ``collection_name`` is descriptive metadata
    only — GraphStore per-collection node lookups (find_nodes_by_name) key off the
    ``collection`` argument passed to write_graph/ensure_graph_tables, not this
    field — so a fixed placeholder is safe across the different collections these
    tests write into."""
    qualified = make_code_symbol_qualified_name(name, source_path)
    return GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, qualified),
        entity_name=name,
        entity_type=EntityType.code_symbol,
        source_doc_id="doc-abc",
        collection_name="impact-test",
        pagerank_score=pagerank_score,
        source_path=source_path,
    )


def _edge(
    src: GraphNode,
    tgt: GraphNode,
    rel: RelationshipType = RelationshipType.calls,
    extraction_method: str | None = "extracted",
) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(src.id, tgt.id, rel.value),
        source_node_id=src.id,
        target_node_id=tgt.id,
        relationship_type=rel,
        source_doc_id="doc-abc",
        extraction_method=extraction_method,
    )


async def _seed_impact_graph(
    db_path: str,
    collection: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    ns: str = "default",
) -> None:
    """Write nodes/edges directly into GraphStore, via a fresh connection.

    Mirrors ``_seed_node_with_mentions`` in test_routes_graph_salience.py: safe
    to call via asyncio.run() from the main test thread while TestClient runs
    in a background thread with its own event loop.
    """
    store = GraphStore(db_path)
    await store.connect()
    try:
        await store.ensure_graph_tables(collection, ns=ns)
        await store.write_graph(collection, nodes, edges, ns=ns)
    finally:
        await store.disconnect()


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
                "clientInfo": {"name": "be9-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    session_id = resp.headers.get("mcp-session-id")
    assert session_id is not None, "MCP initialize did not return mcp-session-id header"

    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        headers=_mcp_headers(token, session_id),
    )
    assert resp.status_code in (200, 202), (
        f"MCP notifications/initialized failed: {resp.status_code} {resp.text[:300]}"
    )
    return session_id


def _mcp_tool_call(client, token: str, session_id: str, tool_name: str, arguments: dict) -> dict:
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        headers=_mcp_headers(token, session_id),
    )
    assert resp.status_code == 200, (
        f"MCP tools/call {tool_name} failed: {resp.status_code} {resp.text[:300]}"
    )
    data_lines = [
        line[5:].strip() for line in resp.text.split("\n") if line.startswith("data:")
    ]
    assert data_lines, f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
    body = json.loads(data_lines[-1])
    assert body.get("jsonrpc") == "2.0"

    rpc_result = body.get("result", {})
    content = rpc_result.get("content", [])
    assert content, f"Tool '{tool_name}' returned empty content list: {rpc_result!r}"
    text = content[0].get("text", "")
    assert text, f"Tool '{tool_name}' returned empty text: {content!r}"
    return json.loads(text)


# ---------------------------------------------------------------------------
# Guard tests
# ---------------------------------------------------------------------------


def test_graphImpactRoute_guardsGraphDisabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /graph/{collection}/impact/{symbol} → 422 when graph.enabled=false."""
    with make_real_app(tmp_path, monkeypatch, graph_enabled=False) as (client, cfg, api_key):
        response = client.get("/graph/any-collection/impact/foo", headers=_auth(api_key))

        assert response.status_code == 422, (
            f"Expected 422 when graph disabled, got {response.status_code}: {response.text}"
        )
        assert "graph inspection requires [graph] enabled=true" in response.json()["detail"]


def test_graphImpactMcpTool_returnsErrorResponse_whenGraphDisabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP graph_impact tool returns McpErrorResponse (not a raised exception) when
    graph.enabled=false."""
    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=False, mcp_enabled=True
    ) as (client, cfg, api_key):
        session_id = _mcp_initialize(client, api_key)

        result = _mcp_tool_call(
            client, api_key, session_id, "graph_impact",
            {"collection": "any-collection", "symbol": "foo"},
        )

        assert isinstance(result, dict), f"Expected dict, got: {type(result).__name__}: {result!r}"
        assert "error" in result, f"Expected 'error' key in result: {result!r}"
        assert result.get("code") == "graph_disabled", (
            f"Expected code='graph_disabled'; got: {result!r}"
        )


# ---------------------------------------------------------------------------
# Real request — grouped, capped, PageRank-ordered result; no shape transform
# ---------------------------------------------------------------------------


def test_graphImpactRoute_realRequest_returnsGroupedResult(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real HTTP request returns the grouped direct/indirect result, PageRank-ordered."""
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-impact.txt"
        doc_file.write_text("root symbol content for impact analysis", encoding="utf-8")
        ingest_file_via_path(client, "impact-col", str(doc_file), api_key=api_key)

        root = _symbol("root")
        caller_low = _symbol("caller_low", pagerank_score=0.2)
        caller_high = _symbol("caller_high", pagerank_score=0.9)
        callee = _symbol("callee")
        nodes = [root, caller_low, caller_high, callee]
        edges = [_edge(caller_low, root), _edge(caller_high, root), _edge(root, callee)]
        asyncio.run(_seed_impact_graph(cfg.db_path, "impact-col", nodes, edges))

        response = client.get(
            "/graph/impact-col/impact/root?direction=both&depth=2", headers=_auth(api_key)
        )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        data = response.json()

        assert data["symbol"] == "root"
        assert set(data.keys()) == {"symbol", "callers", "callees", "depth_used"}, (
            f"Response must mirror ImpactResult 1:1, no extra/missing top-level fields: {data.keys()}"
        )

        for group in (data["callers"], data["callees"]):
            assert set(group.keys()) == {"direct", "indirect", "truncated", "omitted_count"}, (
                f"Group must mirror ImpactGroup 1:1: {group.keys()}"
            )
            for edge_entry in (*group["direct"], *group["indirect"]):
                assert set(edge_entry.keys()) == {
                    "entity_id", "entity_name", "relationship_type", "extraction_method", "depth",
                }

        caller_names = [e["entity_name"] for e in data["callers"]["direct"]]
        assert caller_names == ["caller_high", "caller_low"], (
            f"Expected PageRank-descending order; got: {caller_names}"
        )
        callee_names = [e["entity_name"] for e in data["callees"]["direct"]]
        assert callee_names == ["callee"]

        assert data["callers"]["truncated"] is False
        assert data["callers"]["omitted_count"] == 0
        assert data["callees"]["truncated"] is False
        assert data["callees"]["omitted_count"] == 0


# ---------------------------------------------------------------------------
# MCP tool real response matches REST route's shape
# ---------------------------------------------------------------------------


def test_graphImpactMcpTool_realRequest_matchesRestShape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP graph_impact's real response matches REST's for the same query."""
    _install_spacy_stub(monkeypatch)

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True
    ) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-match.txt"
        doc_file.write_text("root symbol content for shape matching", encoding="utf-8")
        ingest_file_via_path(client, "match-col", str(doc_file), api_key=api_key)

        root = _symbol("root")
        caller = _symbol("caller", pagerank_score=0.5)
        callee = _symbol("callee", pagerank_score=0.5)
        nodes = [root, caller, callee]
        edges = [_edge(caller, root), _edge(root, callee)]
        asyncio.run(_seed_impact_graph(cfg.db_path, "match-col", nodes, edges))

        rest_response = client.get(
            "/graph/match-col/impact/root?direction=both&depth=2", headers=_auth(api_key)
        )
        assert rest_response.status_code == 200
        rest_data = rest_response.json()

        session_id = _mcp_initialize(client, api_key)
        mcp_result = _mcp_tool_call(
            client, api_key, session_id, "graph_impact",
            {"collection": "match-col", "symbol": "root", "depth": 2, "direction": "both"},
        )

        assert mcp_result == rest_data, (
            f"MCP result must structurally match the REST response:\n"
            f"MCP: {mcp_result!r}\nREST: {rest_data!r}"
        )


# ---------------------------------------------------------------------------
# file_path threading — ambiguous symbol fixture
# ---------------------------------------------------------------------------


def test_graphImpactRoute_filePathParam_reachesComputeImpact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """file_path on both REST and MCP surfaces actually reaches compute_impact
    (proven via an ambiguous same-named symbol fixture, not dropped silently)."""
    _install_spacy_stub(monkeypatch)

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True
    ) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-ambig.txt"
        doc_file.write_text("ambiguous symbol content", encoding="utf-8")
        ingest_file_via_path(client, "ambig-col", str(doc_file), api_key=api_key)

        run_a = _symbol("run", source_path="a.py", pagerank_score=0.1)
        run_b = _symbol("run", source_path="b.py", pagerank_score=0.9)
        caller_of_a = _symbol("caller_of_a")
        caller_of_b = _symbol("caller_of_b")
        nodes = [run_a, run_b, caller_of_a, caller_of_b]
        edges = [_edge(caller_of_a, run_a), _edge(caller_of_b, run_b)]
        asyncio.run(_seed_impact_graph(cfg.db_path, "ambig-col", nodes, edges))

        # REST: file_path="a.py" resolves to run_a → caller_of_a
        resp_file_a = client.get(
            "/graph/ambig-col/impact/run?direction=callers&file_path=a.py",
            headers=_auth(api_key),
        )
        assert resp_file_a.status_code == 200
        assert [e["entity_name"] for e in resp_file_a.json()["callers"]["direct"]] == [
            "caller_of_a"
        ]

        # REST: no file_path falls back to highest PageRank (run_b) → caller_of_b
        resp_no_file = client.get(
            "/graph/ambig-col/impact/run?direction=callers", headers=_auth(api_key)
        )
        assert resp_no_file.status_code == 200
        assert [e["entity_name"] for e in resp_no_file.json()["callers"]["direct"]] == [
            "caller_of_b"
        ]

        # MCP: same fixture, same file_path threading
        session_id = _mcp_initialize(client, api_key)
        mcp_file_a = _mcp_tool_call(
            client, api_key, session_id, "graph_impact",
            {"collection": "ambig-col", "symbol": "run", "direction": "callers", "file_path": "a.py"},
        )
        assert [e["entity_name"] for e in mcp_file_a["callers"]["direct"]] == ["caller_of_a"]

        mcp_no_file = _mcp_tool_call(
            client, api_key, session_id, "graph_impact",
            {"collection": "ambig-col", "symbol": "run", "direction": "callers"},
        )
        assert [e["entity_name"] for e in mcp_no_file["callers"]["direct"]] == ["caller_of_b"]


# ---------------------------------------------------------------------------
# Omitted depth/direction → defaults (depth=2, direction="both")
# ---------------------------------------------------------------------------


def test_graphImpactRoute_omittedDepthDirection_appliesDefaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting depth/direction on both REST and MCP results in compute_impact being
    called with depth=2, direction='both'."""
    _install_spacy_stub(monkeypatch)

    captured_calls: list[dict] = []
    original_compute_impact = GraphStore.compute_impact

    async def _spy_compute_impact(self, collection, symbol, depth, direction, extraction_method_filter, file_path, ns):
        captured_calls.append({"depth": depth, "direction": direction})
        return await original_compute_impact(
            self, collection, symbol, depth, direction, extraction_method_filter, file_path, ns
        )

    monkeypatch.setattr(GraphStore, "compute_impact", _spy_compute_impact)

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True
    ) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-defaults.txt"
        doc_file.write_text("defaults test content", encoding="utf-8")
        ingest_file_via_path(client, "defaults-col", str(doc_file), api_key=api_key)

        # REST: omit depth/direction entirely
        response = client.get(
            "/graph/defaults-col/impact/foo", headers=_auth(api_key)
        )
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        assert captured_calls, "compute_impact was never called via REST"
        assert captured_calls[-1]["depth"] == DEFAULT_IMPACT_DEPTH
        assert captured_calls[-1]["direction"] == ImpactDirection.both

        # MCP: omit depth/direction entirely
        session_id = _mcp_initialize(client, api_key)
        _mcp_tool_call(
            client, api_key, session_id, "graph_impact",
            {"collection": "defaults-col", "symbol": "foo"},
        )
        assert captured_calls[-1]["depth"] == DEFAULT_IMPACT_DEPTH
        assert captured_calls[-1]["direction"] == ImpactDirection.both


# ---------------------------------------------------------------------------
# Invalid direction value
# ---------------------------------------------------------------------------


def test_graphImpactRoute_invalidDirection_returns422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REST returns 422 with an 'invalid direction' message for an unrecognized direction."""
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-baddir.txt"
        doc_file.write_text("bad direction content", encoding="utf-8")
        ingest_file_via_path(client, "baddir-col", str(doc_file), api_key=api_key)

        response = client.get(
            "/graph/baddir-col/impact/foo?direction=sideways", headers=_auth(api_key)
        )

        assert response.status_code == 422, (
            f"Expected 422 for invalid direction, got {response.status_code}: {response.text}"
        )
        assert "invalid direction" in response.json()["detail"]


def test_graphImpactMcpTool_invalidDirection_returnsValidationError(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP returns McpErrorResponse with code='validation_error' for an unrecognized direction."""
    _install_spacy_stub(monkeypatch)

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True
    ) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-baddir-mcp.txt"
        doc_file.write_text("bad direction content", encoding="utf-8")
        ingest_file_via_path(client, "baddir-mcp-col", str(doc_file), api_key=api_key)

        session_id = _mcp_initialize(client, api_key)
        result = _mcp_tool_call(
            client, api_key, session_id, "graph_impact",
            {"collection": "baddir-mcp-col", "symbol": "foo", "direction": "sideways"},
        )

        assert result.get("code") == "validation_error", f"Expected validation_error; got: {result!r}"


# ---------------------------------------------------------------------------
# Collection not found (404)
# ---------------------------------------------------------------------------


def test_graphImpactRoute_collectionNotFound_returns404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REST returns 404 with detail 'collection not found' for a nonexistent collection."""
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        response = client.get(
            "/graph/does-not-exist-col/impact/foo", headers=_auth(api_key)
        )

        assert response.status_code == 404, (
            f"Expected 404, got {response.status_code}: {response.text}"
        )
        assert response.json()["detail"] == "collection not found"


def test_graphImpactMcpTool_collectionNotFound_returnsNotFound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP returns McpErrorResponse with code='not_found' for a nonexistent collection."""
    _install_spacy_stub(monkeypatch)

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True
    ) as (client, cfg, api_key):
        session_id = _mcp_initialize(client, api_key)
        result = _mcp_tool_call(
            client, api_key, session_id, "graph_impact",
            {"collection": "does-not-exist-col", "symbol": "foo"},
        )

        assert result.get("code") == "not_found", f"Expected not_found; got: {result!r}"


# ---------------------------------------------------------------------------
# depth lower-bound validation (depth < 1)
# ---------------------------------------------------------------------------


def test_graphImpactRoute_depthZero_returns422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REST returns 422 for depth=0 rather than silently returning an empty 200."""
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-depth0.txt"
        doc_file.write_text("depth zero content", encoding="utf-8")
        ingest_file_via_path(client, "depth0-col", str(doc_file), api_key=api_key)

        response = client.get(
            "/graph/depth0-col/impact/foo?depth=0", headers=_auth(api_key)
        )

        assert response.status_code == 422, (
            f"Expected 422 for depth=0, got {response.status_code}: {response.text}"
        )


def test_graphImpactMcpTool_depthZero_returnsValidationError(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP returns McpErrorResponse with code='validation_error' for depth=0."""
    _install_spacy_stub(monkeypatch)

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True
    ) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-depth0-mcp.txt"
        doc_file.write_text("depth zero content", encoding="utf-8")
        ingest_file_via_path(client, "depth0-mcp-col", str(doc_file), api_key=api_key)

        session_id = _mcp_initialize(client, api_key)
        result = _mcp_tool_call(
            client, api_key, session_id, "graph_impact",
            {"collection": "depth0-mcp-col", "symbol": "foo", "depth": 0},
        )

        assert result.get("code") == "validation_error", f"Expected validation_error; got: {result!r}"


# ---------------------------------------------------------------------------
# extraction_method_filter passthrough
# ---------------------------------------------------------------------------


def test_graphImpactRoute_extractionMethodFilter_reachesComputeImpact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """extraction_method_filter='extracted' on REST reaches compute_impact and excludes
    'inferred'-tagged edges (proven via one extracted edge and one inferred edge from the
    same root to two different callees)."""
    _install_spacy_stub(monkeypatch)

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-extmethod.txt"
        doc_file.write_text("extraction method filter content", encoding="utf-8")
        ingest_file_via_path(client, "extmethod-col", str(doc_file), api_key=api_key)

        root = _symbol("root")
        callee_extracted = _symbol("callee_extracted")
        callee_inferred = _symbol("callee_inferred")
        nodes = [root, callee_extracted, callee_inferred]
        edges = [
            _edge(root, callee_extracted, extraction_method="extracted"),
            _edge(root, callee_inferred, extraction_method="inferred"),
        ]
        asyncio.run(_seed_impact_graph(cfg.db_path, "extmethod-col", nodes, edges))

        response = client.get(
            "/graph/extmethod-col/impact/root?direction=callees&extraction_method_filter=extracted",
            headers=_auth(api_key),
        )
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        callee_names = [e["entity_name"] for e in response.json()["callees"]["direct"]]
        assert callee_names == ["callee_extracted"], (
            f"extraction_method_filter did not reach compute_impact: {callee_names}"
        )


def test_graphImpactMcpTool_extractionMethodFilter_reachesComputeImpact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """extraction_method_filter='extracted' on MCP reaches compute_impact and excludes
    'inferred'-tagged edges (same fixture shape as the REST equivalent)."""
    _install_spacy_stub(monkeypatch)

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True
    ) as (client, cfg, api_key):
        doc_file = tmp_path / "doc-extmethod-mcp.txt"
        doc_file.write_text("extraction method filter content", encoding="utf-8")
        ingest_file_via_path(client, "extmethod-mcp-col", str(doc_file), api_key=api_key)

        root = _symbol("root")
        callee_extracted = _symbol("callee_extracted")
        callee_inferred = _symbol("callee_inferred")
        nodes = [root, callee_extracted, callee_inferred]
        edges = [
            _edge(root, callee_extracted, extraction_method="extracted"),
            _edge(root, callee_inferred, extraction_method="inferred"),
        ]
        asyncio.run(_seed_impact_graph(cfg.db_path, "extmethod-mcp-col", nodes, edges))

        session_id = _mcp_initialize(client, api_key)
        result = _mcp_tool_call(
            client, api_key, session_id, "graph_impact",
            {
                "collection": "extmethod-mcp-col",
                "symbol": "root",
                "direction": "callees",
                "extraction_method_filter": "extracted",
            },
        )
        callee_names = [e["entity_name"] for e in result["callees"]["direct"]]
        assert callee_names == ["callee_extracted"], (
            f"extraction_method_filter did not reach compute_impact via MCP: {callee_names}"
        )

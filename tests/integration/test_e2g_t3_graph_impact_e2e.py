"""Tester-role e2e test for E2g T-3: ``graph_impact`` end-to-end via real MCP and
real HTTP, re-verifying scenarios S5, S6, S7, S13.

Distinct from BE-9's own integration tests (``test_e2g_be9_graph_impact.py``),
which prove REST/MCP shape parity for a *simple* (non-hub) graph. This test:

- Ingests a small fixture file through the real ingest pipeline into a real
  collection before seeding the graph store directly. The shared
  ``install_spacy_stub`` recognizes only "Alice"/"Bob"/"Google"
  (``tests/integration/conftest.py``); the fixture text below contains none
  of those names, so ingest extracts zero graph nodes as a side effect of
  avoiding the stub's keywords, not because the stub is inert. Ingest's only
  purpose here is to materialize a real collection (`get_collection_meta`
  must resolve it) so the REST route and MCP tool don't 404, matching how a
  real deployment resolves `collection`. It does not exercise the extraction
  pipeline; the graph itself (nodes/edges below) is seeded directly via
  ``GraphStore.write_graph``.
- Seeds a **hub symbol** whose caller and callee counts exceed
  ``MAX_IMPACT_GROUP_SIZE`` (``archon_search/graph_types.py``), so
  ``omitted_count > 0`` on both sides (S6 — capped per group with an explicit
  omitted-count, never silently partial).
- Seeds a separate small chain graph (grandcaller -> caller -> hub) to
  exercise depth-2 ``indirect`` traversal, since the truncation scenario's
  direct-hop callers alone already fill ``MAX_IMPACT_GROUP_SIZE`` and would
  never surface any ``indirect`` entries in the final (capped) result.
- Seeds a root symbol with one "extracted" and one "inferred" caller edge to
  exercise ``extraction_method_filter`` (S7).
- Queries ``graph_impact`` for each scenario over real HTTP and separately
  over real MCP (mounted on the same ASGI app / TestClient), asserting both
  responses are byte-for-byte identical where compared (S5, S13).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from archon_search.graph_store import GraphStore
from archon_search.graph_types import (
    MAX_IMPACT_GROUP_SIZE,
    EntityType,
    GraphEdge,
    GraphNode,
    RelationshipType,
    make_code_symbol_qualified_name,
    make_stable_edge_id,
    make_stable_entity_id,
)
from tests.integration.conftest import (
    ingest_file_via_path,
    install_spacy_stub,
    make_real_app,
    mcp_initialize,
    mcp_tool_call,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]


# ---------------------------------------------------------------------------
# Local helpers (fixture-specific — not shared with BE-9's own fixtures)
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _symbol(
    name: str,
    source_path: str | None = None,
    pagerank_score: float | None = None,
) -> GraphNode:
    qualified = make_code_symbol_qualified_name(name, source_path)
    return GraphNode(
        id=make_stable_entity_id(EntityType.code_symbol.value, qualified),
        entity_name=name,
        entity_type=EntityType.code_symbol,
        source_doc_id="doc-abc",
        collection_name="impact-e2e",
        pagerank_score=pagerank_score,
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

    Safe to call via asyncio.run() from the main test thread while TestClient
    runs in a background thread with its own event loop.
    """
    store = GraphStore(db_path)
    await store.connect()
    try:
        await store.ensure_graph_tables(collection, ns=ns)
        await store.write_graph(collection, nodes, edges, ns=ns)
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# S6: capped per group with explicit omitted_count on BOTH callers and
# callees; depth_used asserted explicitly.
# ---------------------------------------------------------------------------


def test_e2e_graphImpact_truncatesBothGroups_withOmittedCount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S6: a hub symbol with both caller and callee counts exceeding
    MAX_IMPACT_GROUP_SIZE is capped on both sides, with explicit
    truncated/omitted_count fields — never a silently partial answer. Also
    asserts depth_used and HTTP/MCP parity (S5/S13) for this scenario."""
    install_spacy_stub(monkeypatch)

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True
    ) as (client, cfg, api_key):
        collection = "hub-impact-e2e"
        doc_file = tmp_path / "hub_module.txt"
        doc_file.write_text("plain fixture content, no named entities", encoding="utf-8")
        ingest_file_via_path(client, collection, str(doc_file), api_key=api_key)

        hub = _symbol("hub")
        num_callers = MAX_IMPACT_GROUP_SIZE + 10
        num_callees = MAX_IMPACT_GROUP_SIZE + 5
        callers = [_symbol(f"caller_{i}", pagerank_score=float(i)) for i in range(num_callers)]
        callees = [_symbol(f"callee_{i}", pagerank_score=float(i)) for i in range(num_callees)]
        nodes = [hub, *callers, *callees]
        edges = [_edge(c, hub) for c in callers] + [_edge(hub, c) for c in callees]
        asyncio.run(_seed_impact_graph(cfg.db_path, collection, nodes, edges))

        rest_response = client.get(
            f"/graph/{collection}/impact/hub?direction=both&depth=2",
            headers=_auth(api_key),
        )
        assert rest_response.status_code == 200, (
            f"Expected 200, got {rest_response.status_code}: {rest_response.text}"
        )
        rest_data = rest_response.json()

        session_id = mcp_initialize(client, api_key)
        mcp_result = mcp_tool_call(
            client, api_key, session_id, "graph_impact",
            {"collection": collection, "symbol": "hub", "depth": 2, "direction": "both"},
        )

        assert mcp_result == rest_data, (
            f"MCP result must agree with the REST response for the hub symbol:\n"
            f"MCP: {mcp_result!r}\nREST: {rest_data!r}"
        )

        assert rest_data["callers"]["truncated"] is True
        assert rest_data["callers"]["omitted_count"] == num_callers - MAX_IMPACT_GROUP_SIZE
        assert len(rest_data["callers"]["direct"]) == MAX_IMPACT_GROUP_SIZE

        assert rest_data["callees"]["truncated"] is True
        assert rest_data["callees"]["omitted_count"] == num_callees - MAX_IMPACT_GROUP_SIZE
        assert len(rest_data["callees"]["direct"]) == MAX_IMPACT_GROUP_SIZE

        # Highest-PageRank entries are kept, in descending order (S13).
        expected_caller_names = [
            f"caller_{i}"
            for i in range(num_callers - 1, num_callers - 1 - MAX_IMPACT_GROUP_SIZE, -1)
        ]
        assert [e["entity_name"] for e in rest_data["callers"]["direct"]] == expected_caller_names
        expected_callee_names = [
            f"callee_{i}"
            for i in range(num_callees - 1, num_callees - 1 - MAX_IMPACT_GROUP_SIZE, -1)
        ]
        assert [e["entity_name"] for e in rest_data["callees"]["direct"]] == expected_callee_names

        # Only hop 1 is ever populated here (no edges into callers or out of
        # callees were seeded), so depth_used is 1 regardless of the
        # requested depth=2.
        assert rest_data["depth_used"] == 1


# ---------------------------------------------------------------------------
# S5/S13 + depth-2 indirect traversal: a small, un-truncated chain graph so
# indirect entries actually survive into the final (uncapped) result.
# ---------------------------------------------------------------------------


def test_e2e_graphImpact_depthTwoIndirectRipple_parityAndOrdering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S5/S13: HTTP and MCP agree, PageRank-descending order holds for both
    direct and indirect entries, and a genuine 2-hop chain
    (grandcaller -> caller -> hub) surfaces in the callers group's
    ``indirect`` list with the correct ``depth_used``."""
    install_spacy_stub(monkeypatch)

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True
    ) as (client, cfg, api_key):
        collection = "chain-impact-e2e"
        doc_file = tmp_path / "chain_module.txt"
        doc_file.write_text("plain fixture content, no named entities", encoding="utf-8")
        ingest_file_via_path(client, collection, str(doc_file), api_key=api_key)

        hub = _symbol("chainhub")
        caller_a = _symbol("caller_a", pagerank_score=0.9)
        caller_b = _symbol("caller_b", pagerank_score=0.4)
        grandcaller_a = _symbol("grandcaller_a", pagerank_score=0.8)
        grandcaller_b = _symbol("grandcaller_b", pagerank_score=0.2)
        callee = _symbol("callee", pagerank_score=0.5)
        nodes = [hub, caller_a, caller_b, grandcaller_a, grandcaller_b, callee]
        edges = [
            _edge(caller_a, hub),
            _edge(caller_b, hub),
            _edge(grandcaller_a, caller_a),
            _edge(grandcaller_b, caller_b),
            _edge(hub, callee),
        ]
        asyncio.run(_seed_impact_graph(cfg.db_path, collection, nodes, edges))

        rest_response = client.get(
            f"/graph/{collection}/impact/chainhub?direction=both&depth=2",
            headers=_auth(api_key),
        )
        assert rest_response.status_code == 200, (
            f"Expected 200, got {rest_response.status_code}: {rest_response.text}"
        )
        rest_data = rest_response.json()

        session_id = mcp_initialize(client, api_key)
        mcp_result = mcp_tool_call(
            client, api_key, session_id, "graph_impact",
            {"collection": collection, "symbol": "chainhub", "depth": 2, "direction": "both"},
        )

        assert mcp_result == rest_data, (
            f"MCP result must agree with the REST response for the chain graph:\n"
            f"MCP: {mcp_result!r}\nREST: {rest_data!r}"
        )

        assert rest_data["callers"]["truncated"] is False
        assert rest_data["callers"]["omitted_count"] == 0
        assert [e["entity_name"] for e in rest_data["callers"]["direct"]] == [
            "caller_a", "caller_b",
        ]
        assert [e["entity_name"] for e in rest_data["callers"]["indirect"]] == [
            "grandcaller_a", "grandcaller_b",
        ]
        assert all(e["depth"] == 2 for e in rest_data["callers"]["indirect"])

        assert rest_data["callees"]["truncated"] is False
        assert [e["entity_name"] for e in rest_data["callees"]["direct"]] == ["callee"]

        # Both directions reached hop 2 (callers via the grandcaller chain;
        # depth_used is the max across whichever groups were requested).
        assert rest_data["depth_used"] == 2


# ---------------------------------------------------------------------------
# S7: extraction_method_filter excludes non-matching edges on both surfaces.
# ---------------------------------------------------------------------------


def test_e2e_graphImpact_extractionMethodFilter_excludesInferredEdges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S7: extraction_method_filter='extracted' on both REST and MCP excludes
    a caller reached only via an 'inferred' edge, and the excluded edge does
    not affect omitted_count for the extracted-only view."""
    install_spacy_stub(monkeypatch)

    with make_real_app(
        tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True
    ) as (client, cfg, api_key):
        collection = "extfilter-impact-e2e"
        doc_file = tmp_path / "extfilter_module.txt"
        doc_file.write_text("plain fixture content, no named entities", encoding="utf-8")
        ingest_file_via_path(client, collection, str(doc_file), api_key=api_key)

        hub = _symbol("hub")
        caller_extracted = _symbol("caller_extracted", pagerank_score=0.9)
        caller_inferred = _symbol("caller_inferred", pagerank_score=0.5)
        nodes = [hub, caller_extracted, caller_inferred]
        edges = [
            _edge(caller_extracted, hub, extraction_method="extracted"),
            _edge(caller_inferred, hub, extraction_method="inferred"),
        ]
        asyncio.run(_seed_impact_graph(cfg.db_path, collection, nodes, edges))

        # Positive control: unfiltered, both callers are reachable — proves
        # the inferred-edge caller genuinely exists in the traversal before
        # asserting the filter excludes it below.
        unfiltered_response = client.get(
            f"/graph/{collection}/impact/hub?direction=callers",
            headers=_auth(api_key),
        )
        assert unfiltered_response.status_code == 200
        unfiltered_names = {
            e["entity_name"] for e in unfiltered_response.json()["callers"]["direct"]
        }
        assert unfiltered_names == {"caller_extracted", "caller_inferred"}, (
            f"expected both callers reachable without a filter; got: {unfiltered_names}"
        )

        rest_response = client.get(
            f"/graph/{collection}/impact/hub"
            "?direction=callers&extraction_method_filter=extracted",
            headers=_auth(api_key),
        )
        assert rest_response.status_code == 200, (
            f"Expected 200, got {rest_response.status_code}: {rest_response.text}"
        )
        rest_data = rest_response.json()
        rest_caller_names = [e["entity_name"] for e in rest_data["callers"]["direct"]]
        assert rest_caller_names == ["caller_extracted"], (
            f"extraction_method_filter did not exclude the inferred-edge caller via REST: "
            f"{rest_caller_names}"
        )
        assert rest_data["callers"]["truncated"] is False
        assert rest_data["callers"]["omitted_count"] == 0

        session_id = mcp_initialize(client, api_key)
        mcp_result = mcp_tool_call(
            client, api_key, session_id, "graph_impact",
            {
                "collection": collection,
                "symbol": "hub",
                "direction": "callers",
                "extraction_method_filter": "extracted",
            },
        )
        mcp_caller_names = [e["entity_name"] for e in mcp_result["callers"]["direct"]]
        assert mcp_caller_names == ["caller_extracted"], (
            f"extraction_method_filter did not exclude the inferred-edge caller via MCP: "
            f"{mcp_caller_names}"
        )
        assert mcp_result["callers"]["truncated"] is False
        assert mcp_result["callers"]["omitted_count"] == 0

        assert mcp_result == rest_data, (
            f"MCP result must agree with the REST response for the filtered query:\n"
            f"MCP: {mcp_result!r}\nREST: {rest_data!r}"
        )

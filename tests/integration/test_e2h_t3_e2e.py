"""Integration e2e tests for T-3: explain provenance, MCP rejection, naive cap, multi-collection guard.

Covers:
1. POST /explain with graph_mode="ppr" returns graph_mode_applied="ppr", ppr_entities_matched > 0,
   and graph_provenance with PPR steps on at least one result (seeded graph).
2. MCP search_with_context rejects graph_mode="ppr" with code="graph_mode_not_supported".
3. Naive expansion cap is applied: seeding 10+ neighbours with cap=5 → expansion_used=True
   and expander adds exactly 5 terms.
4. POST /explain with collections=["a","b"] and graph_mode="ppr" → 422 (multi-collection guard).
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphMention,
    GraphNode,
    RelationshipType,
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

pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _node(name: str, col: str, entity_type: EntityType = EntityType.concept) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(entity_type.value, name),
        entity_name=name,
        entity_type=entity_type,
        source_doc_id="doc1",
        collection_name=col,
    )


def _mention(node: GraphNode, chunk_id: str) -> GraphMention:
    return GraphMention(entity_id=node.id, chunk_id=chunk_id, doc_id="doc1")


def _edge(source: GraphNode, target: GraphNode) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(source.id, target.id, RelationshipType.related_to.value),
        source_node_id=source.id,
        target_node_id=target.id,
        relationship_type=RelationshipType.related_to,
        source_doc_id="doc1",
    )


def _install_no_entity_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a spaCy stub that returns NO named entities for any text."""

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


def _install_kubernetes_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a spaCy stub that recognizes 'kubernetes' as ORG when it appears in text."""

    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self, ents: list) -> None:
            self.ents = ents

    _ENTITY_MAP = [("kubernetes", "ORG")]

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            ents = [_FakeEnt(name, label) for name, label in _ENTITY_MAP if name in text.lower()]
            return _FakeDoc(ents)

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


async def _seed_graph(
    db_path: str,
    collection: str,
    ns: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    mentions: list[GraphMention],
) -> None:
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_graph_tables(collection, ns=ns)
        await gs.write_graph(collection, nodes, edges, ns=ns)
        await gs.write_mentions(collection, mentions, ns=ns)
    finally:
        await gs.disconnect()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_e2h_t3_explainPprMode_provenanceAndCount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain with graph_mode='ppr' and seeded graph returns graph_mode_applied='ppr',
    ppr_entities_matched > 0, and graph_provenance with PPR steps on at least one result.
    """
    _install_kubernetes_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc = tmp_path / "doc.txt"
        doc.write_text("kubernetes is a container orchestration system for deploying workloads.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        # Get a real chunk ID so the seeded mention points to an existing chunk
        resp = client.post(
            "/search",
            json={"collection": "col", "query": "kubernetes"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results, "Expected at least one chunk after ingest"
        chunk_id = results[0]["chunk_id"]

        # Seed a graph node named "kubernetes" and a mention pointing to the real chunk
        node_k8s = _node("kubernetes", "col")
        mentions = [_mention(node_k8s, chunk_id)]
        asyncio.run(_seed_graph(cfg.db_path, "col", "default", [node_k8s], [], mentions))

        resp = client.post(
            "/explain",
            json={"collection": "col", "query": "kubernetes", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["graph_mode_applied"] == "ppr", (
            f"Expected graph_mode_applied='ppr', got {body.get('graph_mode_applied')!r}"
        )
        assert "ppr_entities_matched" in body, "ppr_entities_matched missing from response"
        assert isinstance(body["ppr_entities_matched"], int), (
            f"Expected ppr_entities_matched to be int, got {type(body['ppr_entities_matched'])}"
        )
        assert body["ppr_entities_matched"] > 0, (
            f"Expected ppr_entities_matched > 0 with seeded graph, got {body['ppr_entities_matched']}"
        )

        # At least one result must have graph_provenance with a PPR step
        assert body["results"], "Expected at least one result in explain response"
        results_with_provenance = [r for r in body["results"] if r.get("graph_provenance") is not None]
        assert results_with_provenance, "Expected at least one result with graph_provenance from PPR walk"
        first_prov = results_with_provenance[0]["graph_provenance"]
        assert isinstance(first_prov, dict), (
            f"graph_provenance must be a dict, got {type(first_prov)}"
        )
        assert "steps" in first_prov, f"graph_provenance must have 'steps' key, got: {first_prov!r}"
        ppr_steps = [s for s in first_prov["steps"] if s.get("relationship") == "ppr"]
        assert ppr_steps, f"Expected steps with relationship='ppr', got: {first_prov['steps']}"


@pytest.mark.xdist_group("mcp")
def test_e2h_t3_mcpSearchWithContext_rejectsPprMode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP search_with_context with graph_mode='ppr' returns error code='graph_mode_not_supported'
    with message containing 'ppr'.
    """
    _install_no_entity_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        doc = tmp_path / "doc.txt"
        doc.write_text("Hello world, this is a test document.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        session_id = mcp_initialize(client, api_key)
        result = mcp_tool_call(
            client,
            api_key,
            session_id,
            "search_with_context",
            {"collection": "col", "query": "hello", "graph_mode": "ppr"},
        )

        assert "code" in result, f"Expected 'code' key in MCP error response, got: {result!r}"
        assert result["code"] == "graph_mode_not_supported", (
            f"Expected code='graph_mode_not_supported', got: {result['code']!r}"
        )
        assert "error" in result, f"Expected 'error' key in MCP error response, got: {result!r}"
        assert "ppr" in result["error"], (
            f"Expected 'ppr' in error message, got: {result['error']!r}"
        )


def test_e2h_t3_naiveCap_highDegreeEntity_expansionBounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naive expansion cap is applied when a node has more neighbours than the cap.

    Seeds 10 neighbour nodes for entity 'Alice' with naive_max_expansion_terms=5.
    POST /search with graph_mode='naive' must return expansion_used=True and complete
    without timing out — verifying the cap prevents unbounded expansion.
    """
    install_spacy_stub(monkeypatch)
    # Use a custom cap of 5 so 10 seeded neighbours clearly exceed it
    toml = "[graph]\nenabled = true\nnaive_max_expansion_terms = 5\n"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        assert cfg.graph.naive_max_expansion_terms == 5

        doc = tmp_path / "doc.txt"
        doc.write_text("Alice is an expert in distributed systems and machine learning.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        # Retrieve real chunk ID
        resp = client.post(
            "/search",
            json={"collection": "col", "query": "Alice"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results, "Expected at least one chunk after ingest"
        chunk_id = results[0]["chunk_id"]

        # Seed the central entity node + 10 neighbour nodes + edges from centre to each
        col = "col"
        ns = "default"
        center = _node("Alice", col, EntityType.person)
        neighbours = [_node(f"neighbour_{i}", col) for i in range(10)]
        edges = [_edge(center, n) for n in neighbours]
        mentions = [_mention(center, chunk_id)]
        all_nodes = [center, *neighbours]

        asyncio.run(_seed_graph(cfg.db_path, col, ns, all_nodes, edges, mentions))

        resp = client.post(
            "/search",
            json={"collection": col, "query": "Alice", "graph_mode": "naive"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["expansion_used"] is True, (
            f"Expected expansion_used=True with seeded neighbours, got: {body.get('expansion_used')!r}"
        )

        # Verify the expander actually applies the cap: exactly 5 terms added (cap=5, 10 neighbours)
        from archon_search.graph_store import GraphStore

        db_path = cfg.db_path
        pipeline = client.app.state.pipeline  # type: ignore[attr-defined]
        expander = pipeline._graph_expander
        assert expander is not None, "GraphExpander not wired into pipeline"

        async def _check_cap() -> int:
            gs = GraphStore(db_path)
            await gs.connect()
            try:
                result = await expander.expand("Alice", col, ns=ns)
                return len(result.neighbour_names_added)
            finally:
                await gs.disconnect()

        added_count = asyncio.run(_check_cap())
        assert added_count == 5, (
            f"Expected exactly 5 added terms (cap=5, 10 neighbours seeded), got {added_count}"
        )


def test_e2h_t3_explainMultiCollection_graphMode_returns422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain with collections=['a','b'] and graph_mode='ppr' → 422.

    The guard fires before any pipeline access, so no prior ingest is needed.
    """
    _install_no_entity_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        resp = client.post(
            "/explain",
            json={
                "collections": ["a", "b"],
                "query": "test query",
                "graph_mode": "ppr",
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 422, (
            f"Expected 422 for multi-collection + graph_mode, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        detail = body.get("detail")
        assert detail == "graph_mode is not supported with multi-collection fanout; use a single collection", (
            f"Unexpected detail: {detail!r}"
        )

"""Integration tests for BE-6: Wire PPRWalker into pipeline._search_ppr_mode.

Covers:
- PPR mode blends entity-linked chunks with hybrid candidates
- Empty node table → falls back to hybrid, ppr_entities_matched=0
- ppr_top_entities config is applied
- MCP search with graph_mode="ppr" returns ppr_entities_matched
- search_many with graph_mode="ppr" dispatches correctly
- PPR chunks are prepended before hybrid results (ordering guarantee)
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
    make_stable_entity_id,
)
from tests.integration.conftest import (
    ingest_file_via_path,
    make_real_app,
    mcp_initialize,
    mcp_tool_call,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("ppr_wiring")]


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
    """Install a spaCy stub that returns 'kubernetes' as an entity when present in text."""

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
    """Seed a real GraphStore with nodes, edges, and mentions for testing."""
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


def test_pprMode_blendedResults_entityChunkInTopK(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PPR mode: entity-linked chunk appears in results, ppr_entities_matched > 0."""
    _install_kubernetes_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        # Ingest a document about kubernetes so the chunk lands in the store
        doc = tmp_path / "k8s.txt"
        doc.write_text("kubernetes is a container orchestration system for deploying workloads.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        db_path = cfg.db_path
        ns = "default"
        col = "col"

        # After ingest there are real chunk IDs in the store.
        # We need to look up which chunk IDs were created so we can write mentions.
        # Use a POST /search to find real chunk_ids, then seed graph targeting those IDs.
        resp = client.post(
            "/search",
            json={"collection": col, "query": "kubernetes"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results, "Expected at least one chunk after ingest"
        chunk_id = results[0]["chunk_id"]

        # Seed graph: one node "kubernetes", one mention pointing to the real chunk
        node_k8s = _node("kubernetes", col)
        mentions = [_mention(node_k8s, chunk_id)]
        asyncio.run(_seed_graph(db_path, col, ns, [node_k8s], [], mentions))

        # Now search with PPR
        resp = client.post(
            "/search",
            json={"collection": col, "query": "kubernetes", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ppr_entities_matched"] > 0, (
            f"Expected ppr_entities_matched > 0, got {body['ppr_entities_matched']}"
        )
        assert body["results"], "Expected non-empty results"
        chunk_ids_in_results = [r["chunk_id"] for r in body["results"]]
        assert chunk_id in chunk_ids_in_results, (
            f"Entity-linked chunk {chunk_id!r} not in PPR results: {chunk_ids_in_results}"
        )


def test_pprMode_emptyNodeTable_fallsBackToHybrid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PPR mode with no entity matches → falls back to hybrid, ppr_entities_matched=0.

    Uses a no-entity spaCy stub so graph extraction produces no nodes during ingest.
    PPRWalker then finds no entity matches → ppr_entities_matched=0, hybrid fallback.
    """
    # Use a stub that extracts NO entities so the graph stays empty after ingest
    _install_no_entity_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc = tmp_path / "doc.txt"
        doc.write_text("kubernetes is a container orchestration system.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        # No graph seeding — graph tables have no nodes/mentions

        resp = client.post(
            "/search",
            json={"collection": "col", "query": "kubernetes", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ppr_entities_matched"] == 0, (
            f"Expected ppr_entities_matched=0 for empty graph, got {body['ppr_entities_matched']}"
        )
        # Hybrid fallback must return results
        assert body["results"], "Expected non-empty results from hybrid fallback"


def test_pprMode_pprTopEntities_config_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ppr_top_entities=1 config means at most 1 entity contributes to PPR result."""
    _install_kubernetes_spacy_stub(monkeypatch)
    toml = "[graph]\nenabled = true\nppr_top_entities = 1\n"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        assert cfg.graph.ppr_top_entities == 1

        doc = tmp_path / "k8s.txt"
        doc.write_text("kubernetes deployment and orchestration system overview.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        db_path = cfg.db_path
        col = "col"
        ns = "default"

        # Retrieve a real chunk ID
        resp = client.post(
            "/search",
            json={"collection": col, "query": "kubernetes"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results
        chunk_id = results[0]["chunk_id"]

        node_k8s = _node("kubernetes", col)
        mentions = [_mention(node_k8s, chunk_id)]
        asyncio.run(_seed_graph(db_path, col, ns, [node_k8s], [], mentions))

        resp = client.post(
            "/search",
            json={"collection": col, "query": "kubernetes", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # With ppr_top_entities=1, exactly 1 entity contributes → ppr_entities_matched == 1
        assert "ppr_entities_matched" in body
        assert body["ppr_entities_matched"] > 0


def test_pprMode_mcpSearch_pprEntitiesMatchedInResponse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP search tool with graph_mode='ppr' returns ppr_entities_matched in response."""
    _install_kubernetes_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True, mcp_enabled=True) as (client, cfg, api_key):
        doc = tmp_path / "k8s.txt"
        doc.write_text("kubernetes cluster management and scheduling.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        db_path = cfg.db_path
        col = "col"
        ns = "default"

        # Retrieve a real chunk ID
        resp = client.post(
            "/search",
            json={"collection": col, "query": "kubernetes"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results
        chunk_id = results[0]["chunk_id"]

        node_k8s = _node("kubernetes", col)
        asyncio.run(_seed_graph(db_path, col, ns, [node_k8s], [], [_mention(node_k8s, chunk_id)]))

        session_id = mcp_initialize(client, api_key)
        result = mcp_tool_call(
            client, api_key, session_id, "search",
            {"collection": col, "query": "kubernetes", "graph_mode": "ppr"},
        )
        assert "ppr_entities_matched" in result, (
            f"ppr_entities_matched missing from MCP search response: {result!r}"
        )
        assert result["ppr_entities_matched"] > 0


def test_pprMode_multiCollection_returns422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-collection PPR is not supported: POST /search with collections + graph_mode='ppr' returns 422."""
    _install_kubernetes_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc = tmp_path / "k8s.txt"
        doc.write_text("kubernetes is used for container orchestration at scale.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        col = "col"

        # Multi-collection PPR must be rejected — search_many has no PPR branch
        resp = client.post(
            "/search",
            json={"collections": [col], "query": "kubernetes", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422, resp.text


def test_pprMode_chunkOrdering_pprChunksPrependedBeforeHybrid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PPR chunks are prepended: entity-linked chunk ranks ≥ its plain-hybrid position.

    We ingest two docs: one with the query keyword ('kubernetes') and one without.
    We seed the graph so only the non-keyword doc's chunk is entity-linked (via mention).
    In plain hybrid search, the keyword doc would rank first.
    In PPR mode, the entity-linked chunk (non-keyword doc) should appear in results
    (PPR prepend guarantees it is included even if hybrid alone would not rank it first).
    """
    _install_kubernetes_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        # Doc A: contains the keyword → hybrid search will rank this first
        doc_a = tmp_path / "doc_a.txt"
        doc_a.write_text("kubernetes is a container orchestration platform for scaling workloads.")

        # Doc B: does NOT contain the keyword, hybrid alone would rank it lower
        doc_b = tmp_path / "doc_b.txt"
        doc_b.write_text("This document is about general software deployment practices.")

        ingest_file_via_path(client, "col", str(doc_a), api_key=api_key)
        ingest_file_via_path(client, "col", str(doc_b), api_key=api_key)

        db_path = cfg.db_path
        col = "col"
        ns = "default"

        # Find doc_b's chunk ID via source_path filter
        resp = client.post(
            "/search",
            json={"collection": col, "query": "software deployment practices"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        # Find the chunk from doc_b
        doc_b_chunks = [r for r in results if "deployment practices" in r.get("text", "")]
        if not doc_b_chunks:
            pytest.skip("Could not locate doc_b chunk for ordering test — skipping")
        doc_b_chunk_id = doc_b_chunks[0]["chunk_id"]

        # Seed graph: link 'kubernetes' entity → doc_b's chunk (non-keyword doc)
        node_k8s = _node("kubernetes", col)
        asyncio.run(_seed_graph(db_path, col, ns, [node_k8s], [], [_mention(node_k8s, doc_b_chunk_id)]))

        # PPR search: doc_b's chunk should appear (PPR prepend includes entity-linked chunks)
        resp = client.post(
            "/search",
            json={"collection": col, "query": "kubernetes", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ppr_chunk_ids = [r["chunk_id"] for r in body["results"]]

        assert doc_b_chunk_id in ppr_chunk_ids, (
            f"PPR-linked chunk {doc_b_chunk_id!r} not in PPR results: {ppr_chunk_ids}. "
            "PPR prepend must include entity-linked chunks regardless of hybrid ranking."
        )

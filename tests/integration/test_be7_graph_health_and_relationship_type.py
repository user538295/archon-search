"""BE-7 integration tests — GraphCollectionStats health fields and relationship_type passthrough.

Tests:
  - test_health_metrics_reflect_synonym_edges_in_status_endpoint
    Mixed fixture: 1 synonym_of edge + 1 related_to edge.
    GET /status shows synonym_edge_count == 1 and synonym_link_rate == 0.5 exactly.
  - test_count_synonym_edges_filter_discriminates_edge_types
    Seed 1 synonym_of + 1 related_to edge; assert count_synonym_edges == 1 (not 2).
  - test_graph_inspection_shows_relationship_type_on_edges
    GET /graph/{collection} edge responses include relationship_type from the edges table.
  - test_cross_collection_inspection_preserves_synonym_relationship_type
    GET /graph/cross-collection returns synonym edges with relationship_type='synonym_of'.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a stub spaCy module that returns NO named entities.

    Must be called BEFORE make_real_app(graph_enabled=True).
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


async def _seed_graph_with_nodes_and_synonyms(
    db_path: str,
    collection: str,
    ns: str = "default",
) -> None:
    """Write two nodes connected by a synonym edge into GraphStore.

    Opens a fresh connection independent from the running app's connection.
    """
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphEdge,
        GraphMention,
        GraphNode,
        RelationshipType,
        make_stable_edge_id,
        make_stable_entity_id,
    )

    node_a_id = make_stable_entity_id("concept", "Kubernetes")
    node_b_id = make_stable_entity_id("concept", "K8s")
    edge_id = make_stable_edge_id(node_a_id, node_b_id, RelationshipType.synonym_of.value)

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_graph_tables(collection, ns=ns)
        nodes = [
            GraphNode(
                id=node_a_id,
                entity_name="Kubernetes",
                entity_type=EntityType.concept,
                source_doc_id="seed-doc",
                collection_name=collection,
            ),
            GraphNode(
                id=node_b_id,
                entity_name="K8s",
                entity_type=EntityType.concept,
                source_doc_id="seed-doc",
                collection_name=collection,
            ),
        ]
        edges = [
            GraphEdge(
                id=edge_id,
                source_node_id=node_a_id,
                target_node_id=node_b_id,
                relationship_type=RelationshipType.synonym_of,
                source_doc_id="seed-doc",
                extraction_method="manual",
            )
        ]
        await gs.write_graph(collection, nodes, edges, ns=ns)
        # Also write a mention so the collection metadata is visible
        mentions = [
            GraphMention(entity_id=node_a_id, chunk_id="chunk-0001", doc_id="seed-doc"),
            GraphMention(entity_id=node_b_id, chunk_id="chunk-0001", doc_id="seed-doc"),
        ]
        await gs.write_mentions(collection, mentions, ns=ns)
    finally:
        await gs.disconnect()


async def _seed_graph_with_mixed_edge_types(
    db_path: str,
    collection: str,
    ns: str = "default",
) -> None:
    """Write three nodes: one synonym_of edge AND one related_to edge.

    Topology:
    - node_a (Kubernetes) --synonym_of--> node_b (K8s)
    - node_a (Kubernetes) --related_to--> node_c (Docker)

    This gives:  edge_count=2, synonym_edge_count=1, synonym_link_rate=0.5 exactly.
    """
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphEdge,
        GraphMention,
        GraphNode,
        RelationshipType,
        make_stable_edge_id,
        make_stable_entity_id,
    )

    node_a_id = make_stable_entity_id("concept", "Kubernetes")
    node_b_id = make_stable_entity_id("concept", "K8s")
    node_c_id = make_stable_entity_id("concept", "Docker")
    synonym_edge_id = make_stable_edge_id(node_a_id, node_b_id, RelationshipType.synonym_of.value)
    related_edge_id = make_stable_edge_id(node_a_id, node_c_id, RelationshipType.related_to.value)

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_graph_tables(collection, ns=ns)
        nodes = [
            GraphNode(
                id=node_a_id,
                entity_name="Kubernetes",
                entity_type=EntityType.concept,
                source_doc_id="seed-doc",
                collection_name=collection,
            ),
            GraphNode(
                id=node_b_id,
                entity_name="K8s",
                entity_type=EntityType.concept,
                source_doc_id="seed-doc",
                collection_name=collection,
            ),
            GraphNode(
                id=node_c_id,
                entity_name="Docker",
                entity_type=EntityType.concept,
                source_doc_id="seed-doc",
                collection_name=collection,
            ),
        ]
        edges = [
            GraphEdge(
                id=synonym_edge_id,
                source_node_id=node_a_id,
                target_node_id=node_b_id,
                relationship_type=RelationshipType.synonym_of,
                source_doc_id="seed-doc",
                extraction_method="manual",
            ),
            GraphEdge(
                id=related_edge_id,
                source_node_id=node_a_id,
                target_node_id=node_c_id,
                relationship_type=RelationshipType.related_to,
                source_doc_id="seed-doc",
                extraction_method="manual",
            ),
        ]
        await gs.write_graph(collection, nodes, edges, ns=ns)
        mentions = [
            GraphMention(entity_id=node_a_id, chunk_id="chunk-0001", doc_id="seed-doc"),
        ]
        await gs.write_mentions(collection, mentions, ns=ns)
    finally:
        await gs.disconnect()


async def _seed_collection_meta(
    db_path: str, collection: str, ns: str = "default"
) -> None:
    """Create a collection table and meta row with the given namespace."""
    import hashlib
    from datetime import datetime, timezone

    from archon_search._types import ChunkRecord, normalize_iso_utc
    from archon_search.collection_meta import CollectionMeta
    from archon_search.store import SearchStore

    _STUB_EMBEDDING_DIM = 384
    store = SearchStore(db_path)
    await store.connect()
    try:
        await store.ensure_collection(collection, _STUB_EMBEDDING_DIM)
        doc_id = hashlib.sha256(collection.encode()).hexdigest()
        chunks = [
            ChunkRecord(
                doc_id=doc_id,
                chunk_id=f"{doc_id}-000000",
                text="Kubernetes and K8s are synonyms",
                vector=[0.0] * _STUB_EMBEDDING_DIM,
                source_path=f"/fake/{collection}.txt",
                indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
            )
        ]
        await store.ingest_chunks(collection, chunks)
        meta = CollectionMeta(
            name=collection,
            active_embedding_model="stub-model",
            doc_count=1,
            chunk_count=1,
            namespace=ns,
        )
        await store.update_collection_meta(meta)
    finally:
        await store.disconnect()


def test_health_metrics_reflect_synonym_edges_in_status_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed fixture (1 synonym_of + 1 related_to edge) → exact health metric values.

    Covers C4 / S6 — GraphCollectionStats health fields in status response.

    With 2 edges total and 1 synonym_of edge:
    - synonym_edge_count == 1  (filter discriminates — would be 2 if filter dropped)
    - synonym_link_rate == 0.5 (1/2 — formula: synonym_edge_count / edge_count)
    """
    _install_spacy_stub(monkeypatch)
    col = "be7-health-metrics"

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        db_path = cfg.db_path

        # Seed collection meta and mixed graph data (1 synonym_of + 1 related_to edge)
        asyncio.run(_seed_collection_meta(db_path, col))
        asyncio.run(_seed_graph_with_mixed_edge_types(db_path, col))

        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "graph" in data and data["graph"] is not None
        graph = data["graph"]

        col_entries = [c for c in graph["collections"] if c["collection"] == col]
        assert len(col_entries) == 1, (
            f"Expected 1 entry for collection {col!r}, got: {graph['collections']}"
        )
        stats = col_entries[0]

        # Verify health metric fields are present
        assert "synonym_edge_count" in stats
        assert "singleton_node_pct" in stats
        assert "synonym_link_rate" in stats
        assert "connected_component_count" not in stats

        # Exact counts: 1 synonym_of edge out of 2 total edges
        assert stats["synonym_edge_count"] == 1, (
            f"Expected synonym_edge_count == 1 (filter must discriminate), got: {stats['synonym_edge_count']}"
        )

        # Exact rate: 1 synonym_of / 2 total edges = 0.5
        assert stats["synonym_link_rate"] == pytest.approx(0.5), (
            f"Expected synonym_link_rate == 0.5 (1/2 edges are synonyms), got: {stats['synonym_link_rate']}"
        )


def test_count_synonym_edges_filter_discriminates_edge_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """count_synonym_edges returns 1 when graph has 1 synonym_of + 1 related_to edge.

    Proves the _where_eq('relationship_type', 'synonym_of') predicate in
    GraphStore.count_synonym_edges discriminates between edge types.
    If the filter were dropped, count would be 2 (total edges), not 1.
    """
    _install_spacy_stub(monkeypatch)
    col = "be7-filter-discrimination"

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        db_path = cfg.db_path

        asyncio.run(_seed_collection_meta(db_path, col))
        asyncio.run(_seed_graph_with_mixed_edge_types(db_path, col))

        # Call count_synonym_edges directly via GraphStore
        from archon_search.graph_store import GraphStore

        async def _get_counts() -> tuple[int, int]:
            gs = GraphStore(db_path)
            await gs.connect()
            try:
                synonym_count = await gs.count_synonym_edges(col, ns="default")
                edge_count = await gs.edge_count(col, ns="default")
                return synonym_count, edge_count
            finally:
                await gs.disconnect()

        synonym_count, edge_count = asyncio.run(_get_counts())

        assert edge_count == 2, (
            f"Expected total edge_count == 2 (1 synonym_of + 1 related_to), got: {edge_count}"
        )
        assert synonym_count == 1, (
            f"Expected count_synonym_edges == 1 (only synonym_of edges), got: {synonym_count}. "
            f"If this is 2, the filter predicate is not applied."
        )


def test_graph_inspection_shows_relationship_type_on_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /graph/{collection} edge responses include relationship_type from the edges table.

    Covers S7 — GraphEdgeResponse.relationship_type populated on single-collection route.
    """
    _install_spacy_stub(monkeypatch)
    col = "be7-edge-relationship-type"

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        db_path = cfg.db_path

        asyncio.run(_seed_collection_meta(db_path, col))
        asyncio.run(_seed_graph_with_nodes_and_synonyms(db_path, col))

        resp = client.get(f"/graph/{col}", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /graph/{col} failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "edges" in data
        edges = data["edges"]
        assert len(edges) > 0, "Expected at least one edge in the graph inspection response"

        # All edges must have a relationship_type field
        for edge in edges:
            assert "relationship_type" in edge, (
                f"Edge {edge.get('edge_id')} is missing relationship_type field"
            )

        # Find the synonym edge
        synonym_edges = [e for e in edges if e["relationship_type"] == "synonym_of"]
        assert len(synonym_edges) > 0, (
            f"Expected at least one synonym_of edge, got: {[e['relationship_type'] for e in edges]}"
        )


def test_cross_collection_inspection_preserves_synonym_relationship_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /graph/cross-collection returns synonym edges with relationship_type='synonym_of'.

    Covers S7 — GraphEdgeResponse.relationship_type populated on cross-collection route.
    """
    _install_spacy_stub(monkeypatch)
    col_a = "be7-cross-col-a"
    col_b = "be7-cross-col-b"

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        db_path = cfg.db_path

        asyncio.run(_seed_collection_meta(db_path, col_a))
        asyncio.run(_seed_collection_meta(db_path, col_b))
        asyncio.run(_seed_graph_with_nodes_and_synonyms(db_path, col_a))
        asyncio.run(_seed_graph_with_nodes_and_synonyms(db_path, col_b))

        resp = client.get(
            f"/graph/cross-collection?collections={col_a},{col_b}",
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"GET /graph/cross-collection failed: {resp.status_code} {resp.text}"
        )
        data = resp.json()

        assert "edges" in data
        edges = data["edges"]
        assert len(edges) > 0, "Expected at least one edge in cross-collection response"

        for edge in edges:
            assert "relationship_type" in edge, (
                f"Edge {edge.get('edge_id')} is missing relationship_type field"
            )

        synonym_edges = [e for e in edges if e["relationship_type"] == "synonym_of"]
        assert len(synonym_edges) > 0, (
            f"Expected at least one synonym_of edge in cross-collection response, "
            f"got: {[e['relationship_type'] for e in edges]}"
        )

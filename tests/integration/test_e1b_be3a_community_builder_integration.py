"""Integration test for CommunityBuilder with real leidenalg — E1b BE-3a.

Skips gracefully when leidenalg is not installed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

leidenalg = pytest.importorskip("leidenalg", reason="leidenalg not installed; skipping BE-3a integration test")

pytestmark = pytest.mark.integration

from archon_search.config import GraphConfig
from archon_search.graph_store import GraphStore
from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)


def _make_node(name: str, collection: str = "test-col") -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id("concept", name),
        entity_name=name,
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name=collection,
    )


def _make_edge(src: GraphNode, tgt: GraphNode) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(src.id, tgt.id, "related_to"),
        source_node_id=src.id,
        target_node_id=tgt.id,
        relationship_type=RelationshipType.related_to,
        source_doc_id="doc-1",
    )


@pytest.mark.asyncio
async def test_max_community_size_split_real_leiden(tmp_path: Path):
    """Real Leiden run on a triangle graph; all output communities must be <= max_community_size=2."""
    from archon_search.community_builder import CommunityBuilder

    col = "test-col"

    # Triangle: A—B—C—A (all 3 connected; Leiden should put them in 1 community
    # which then gets split because max_community_size=2)
    node_a = _make_node("Alpha")
    node_b = _make_node("Beta")
    node_c = _make_node("Gamma")
    nodes = [node_a, node_b, node_c]
    edges = [
        _make_edge(node_a, node_b),
        _make_edge(node_b, node_c),
        _make_edge(node_c, node_a),
    ]

    store = GraphStore(tmp_path / "graph_db")
    await store.connect()
    await store.ensure_graph_tables(col, ns="default")
    await store.write_graph(col, nodes, edges, ns="default")

    config = GraphConfig(
        enabled=True,
        leiden_resolution=1.0,
        max_community_size=2,
    )
    builder = CommunityBuilder(store, config)
    communities = await builder.build(col, ns="default")
    await store.disconnect()

    assert len(communities) >= 1
    for c in communities:
        assert len(c.entity_ids) <= 2, (
            f"Community {c.community_id} has {len(c.entity_ids)} entities, exceeds max=2"
        )
    # All 3 entity IDs must be covered
    all_ids = {eid for c in communities for eid in c.entity_ids}
    expected_ids = {n.id for n in nodes}
    assert all_ids == expected_ids

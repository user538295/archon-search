"""Unit tests for graph_inspector.py (E2b graph inspection use case)."""

from __future__ import annotations

import logging
import math

import pytest

from archon_search.graph_inspector import (
    CrossCollectionGraphView,
    CollectionGraphView,
    GraphEdgeInspection,
    GraphNodeInspection,
    _MENTIONS_SCAN_CEILING,
    _truncate_graph,
    inspect_collection,
    inspect_cross_collection,
    to_graphml,
)
from archon_search.graph_types import GraphEdge, GraphMention, GraphNode, EntityType, RelationshipType
from tests.conftest import MockGraphStore


# ============================================================================
# Truncation helper tests
# ============================================================================


def test_truncate_graph_node_sort_order():
    """Truncation sorts nodes by (chunk_count desc, entity_id asc)."""
    nodes = [
        GraphNodeInspection("id-c", "C", chunk_count=2, salience=0.1),
        GraphNodeInspection("id-a", "A", chunk_count=3, salience=0.3),  # highest chunk_count
        GraphNodeInspection("id-b", "B", chunk_count=3, salience=0.2),  # same as id-a
    ]
    edges = []

    out_nodes, _, _ = _truncate_graph(nodes, edges, max_nodes=2, max_edges=1000)

    # Should be sorted (chunk_count desc, entity_id asc)
    # id-a and id-b both have chunk_count=3, so sorted by id: id-a < id-b
    assert len(out_nodes) == 2
    assert out_nodes[0].entity_id == "id-a"
    assert out_nodes[1].entity_id == "id-b"


def test_truncate_graph_node_cap_fires():
    """When node count exceeds max_nodes, truncated=True."""
    nodes = [
        GraphNodeInspection("id-1", "N1", chunk_count=5, salience=0.5),
        GraphNodeInspection("id-2", "N2", chunk_count=4, salience=0.4),
        GraphNodeInspection("id-3", "N3", chunk_count=3, salience=0.3),
        GraphNodeInspection("id-4", "N4", chunk_count=2, salience=0.2),
    ]
    edges = []

    out_nodes, _, truncated = _truncate_graph(nodes, edges, max_nodes=2, max_edges=1000)

    assert len(out_nodes) == 2
    assert truncated is True


def test_truncate_graph_edge_filtering_removes_dangling_edges():
    """Edges where one endpoint was truncated are excluded."""
    nodes = [
        GraphNodeInspection("id-a", "A", chunk_count=10, salience=1.0),
        GraphNodeInspection("id-b", "B", chunk_count=8, salience=0.8),
        GraphNodeInspection("id-c", "C", chunk_count=6, salience=0.6),
        GraphNodeInspection("id-d", "D", chunk_count=4, salience=0.4),
        GraphNodeInspection("id-e", "E", chunk_count=2, salience=0.2),
    ]
    edges = [
        GraphEdgeInspection("edge-ab", "id-a", "id-b", weight=5, source_chunk_ids=[]),
        GraphEdgeInspection("edge-ac", "id-a", "id-c", weight=4, source_chunk_ids=[]),
        GraphEdgeInspection("edge-de", "id-d", "id-e", weight=3, source_chunk_ids=[]),  # both truncated
    ]

    out_nodes, out_edges, truncated = _truncate_graph(nodes, edges, max_nodes=3, max_edges=1000)

    assert len(out_nodes) == 3
    assert {n.entity_id for n in out_nodes} == {"id-a", "id-b", "id-c"}
    # Only edge-ab and edge-ac should survive (both endpoints in survivor set)
    assert len(out_edges) == 2
    assert {e.edge_id for e in out_edges} == {"edge-ab", "edge-ac"}
    assert truncated is True


def test_truncate_graph_edge_sort_order():
    """Surviving edges are sorted by (weight desc, edge_id asc)."""
    nodes = [
        GraphNodeInspection("id-a", "A", chunk_count=10, salience=1.0),
        GraphNodeInspection("id-b", "B", chunk_count=8, salience=0.8),
        GraphNodeInspection("id-c", "C", chunk_count=6, salience=0.6),
    ]
    edges = [
        GraphEdgeInspection("edge-2", "id-a", "id-b", weight=2, source_chunk_ids=[]),
        GraphEdgeInspection("edge-1", "id-a", "id-c", weight=3, source_chunk_ids=[]),  # highest weight
        GraphEdgeInspection("edge-3", "id-b", "id-c", weight=3, source_chunk_ids=[]),  # same weight as edge-1
    ]

    _, out_edges, _ = _truncate_graph(nodes, edges, max_nodes=3, max_edges=1000)

    # Sorted by (weight desc, edge_id asc)
    # edge-1 and edge-3 both have weight=3, so sorted by id: edge-1 < edge-3
    assert len(out_edges) == 3
    assert out_edges[0].edge_id == "edge-1"  # weight=3, id="edge-1"
    assert out_edges[1].edge_id == "edge-3"  # weight=3, id="edge-3"
    assert out_edges[2].edge_id == "edge-2"  # weight=2


def test_truncate_graph_edge_cap_fires():
    """When edge count exceeds max_edges, truncated=True."""
    nodes = [
        GraphNodeInspection("id-a", "A", chunk_count=10, salience=1.0),
        GraphNodeInspection("id-b", "B", chunk_count=8, salience=0.8),
    ]
    edges = [
        GraphEdgeInspection("e1", "id-a", "id-b", weight=5, source_chunk_ids=[]),
        GraphEdgeInspection("e2", "id-b", "id-a", weight=4, source_chunk_ids=[]),
    ]

    _, out_edges, truncated = _truncate_graph(nodes, edges, max_nodes=2, max_edges=1)

    assert len(out_edges) == 1
    assert truncated is True


def test_truncate_graph_no_truncation():
    """When counts are within limits, truncated=False."""
    nodes = [
        GraphNodeInspection("id-a", "A", chunk_count=5, salience=0.5),
        GraphNodeInspection("id-b", "B", chunk_count=3, salience=0.3),
    ]
    edges = [GraphEdgeInspection("e1", "id-a", "id-b", weight=2, source_chunk_ids=[])]

    _, _, truncated = _truncate_graph(nodes, edges, max_nodes=10, max_edges=10)

    assert truncated is False


def test_truncate_graph_empty_input():
    """Truncation handles empty node/edge lists."""
    nodes: list[GraphNodeInspection] = []
    edges: list[GraphEdgeInspection] = []

    out_nodes, out_edges, truncated = _truncate_graph(nodes, edges, max_nodes=5, max_edges=5)

    assert out_nodes == []
    assert out_edges == []
    assert truncated is False


# ============================================================================
# inspect_collection tests
# ============================================================================


@pytest.mark.asyncio
async def test_inspect_derives_chunk_count_from_mentions(mock_graph_store: MockGraphStore):
    """Two mentions for an entity → chunk_count=2."""
    entity_id = "test-entity-id"
    node = GraphNode(
        id=entity_id,
        entity_name="TestEntity",
        entity_type=EntityType.concept,
        source_doc_id="doc1",
        collection_name="test",
    )
    mock_graph_store.nodes["test"] = [node]
    mock_graph_store.mentions["test"] = [
        GraphMention(entity_id=entity_id, chunk_id="chunk-1", doc_id="doc1"),
        GraphMention(entity_id=entity_id, chunk_id="chunk-2", doc_id="doc1"),
    ]

    view = await inspect_collection(
        mock_graph_store, "test", total_chunk_count=10, max_nodes=1000, max_edges=1000
    )

    assert len(view.nodes) == 1
    assert view.nodes[0].chunk_count == 2


@pytest.mark.asyncio
async def test_inspect_salience_formula(mock_graph_store: MockGraphStore):
    """Salience = min(chunk_count / total_chunk_count, 1.0)."""
    entity_id = "test-entity"
    node = GraphNode(
        id=entity_id,
        entity_name="TestEntity",
        entity_type=EntityType.concept,
        source_doc_id="doc1",
        collection_name="test",
    )
    mock_graph_store.nodes["test"] = [node]
    mock_graph_store.mentions["test"] = [
        GraphMention(entity_id=entity_id, chunk_id=f"chunk-{i}", doc_id="doc1") for i in range(5)
    ]

    # Case 1: Normal case
    view = await inspect_collection(
        mock_graph_store, "test", total_chunk_count=10, max_nodes=1000, max_edges=1000
    )
    assert view.nodes[0].salience == 0.5

    # Case 2: Clamping (chunk_count > total_chunk_count)
    view = await inspect_collection(
        mock_graph_store, "test", total_chunk_count=3, max_nodes=1000, max_edges=1000
    )
    assert view.nodes[0].salience == 1.0

    # Case 3: Zero denominator
    view = await inspect_collection(
        mock_graph_store, "test", total_chunk_count=0, max_nodes=1000, max_edges=1000
    )
    assert view.nodes[0].salience == 0.0


@pytest.mark.asyncio
async def test_inspect_weight_is_cooccurrence_count(mock_graph_store: MockGraphStore):
    """Edge weight = distinct chunks where both endpoints mentioned."""
    entity_a = "entity-a"
    entity_b = "entity-b"

    node_a = GraphNode(
        id=entity_a, entity_name="A", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="test"
    )
    node_b = GraphNode(
        id=entity_b, entity_name="B", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="test"
    )

    edge = GraphEdge(
        id="edge-id",
        source_node_id=entity_a,
        target_node_id=entity_b,
        relationship_type=RelationshipType.related_to,
        source_doc_id="doc1",
    )

    mock_graph_store.nodes["test"] = [node_a, node_b]
    mock_graph_store.edges["test"] = [edge]
    mock_graph_store.mentions["test"] = [
        # Both A and B mentioned in chunk-1 and chunk-2
        GraphMention(entity_id=entity_a, chunk_id="chunk-1", doc_id="doc1"),
        GraphMention(entity_id=entity_b, chunk_id="chunk-1", doc_id="doc1"),
        GraphMention(entity_id=entity_a, chunk_id="chunk-2", doc_id="doc1"),
        GraphMention(entity_id=entity_b, chunk_id="chunk-2", doc_id="doc1"),
        # A mentioned alone in chunk-3
        GraphMention(entity_id=entity_a, chunk_id="chunk-3", doc_id="doc1"),
    ]

    view = await inspect_collection(
        mock_graph_store, "test", total_chunk_count=10, max_nodes=1000, max_edges=1000
    )

    assert len(view.edges) == 1
    assert view.edges[0].weight == 2  # co-occur in chunk-1 and chunk-2


@pytest.mark.asyncio
async def test_inspect_source_chunk_ids_capped_at_20(mock_graph_store: MockGraphStore):
    """source_chunk_ids has len <= 20; sorted lexicographically."""
    entity_a = "a"
    entity_b = "b"

    node_a = GraphNode(
        id=entity_a, entity_name="A", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="test"
    )
    node_b = GraphNode(
        id=entity_b, entity_name="B", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="test"
    )

    edge = GraphEdge(
        id="edge",
        source_node_id=entity_a,
        target_node_id=entity_b,
        relationship_type=RelationshipType.related_to,
        source_doc_id="doc1",
    )

    mock_graph_store.nodes["test"] = [node_a, node_b]
    mock_graph_store.edges["test"] = [edge]
    # Create 30 co-occurrence mentions (A and B mentioned together)
    mock_graph_store.mentions["test"] = [
        GraphMention(entity_id=entity_a, chunk_id=f"chunk-{i:02d}", doc_id="doc1")
        for i in range(30)
    ] + [GraphMention(entity_id=entity_b, chunk_id=f"chunk-{i:02d}", doc_id="doc1") for i in range(30)]

    view = await inspect_collection(
        mock_graph_store, "test", total_chunk_count=100, max_nodes=1000, max_edges=1000
    )

    assert len(view.edges[0].source_chunk_ids) == 20
    # Verify lexicographic sort
    assert view.edges[0].source_chunk_ids == sorted(view.edges[0].source_chunk_ids)


@pytest.mark.asyncio
async def test_inspect_empty_tables_returns_empty_view(mock_graph_store: MockGraphStore):
    """Absent/empty graph tables → empty view, not error."""
    view = await inspect_collection(
        mock_graph_store, "test", total_chunk_count=10, max_nodes=1000, max_edges=1000
    )

    assert view.nodes == []
    assert view.edges == []
    assert view.truncated is False
    assert view.node_count == 0
    assert view.edge_count == 0


@pytest.mark.asyncio
async def test_inspect_pre_e2b_nodes_read_as_zero(mock_graph_store: MockGraphStore):
    """Nodes without mentions → chunk_count=0, salience=0.0; edges with no co-occurrence → weight=0."""
    entity_a = "a"
    entity_b = "b"

    node_a = GraphNode(
        id=entity_a, entity_name="A", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="test"
    )
    node_b = GraphNode(
        id=entity_b, entity_name="B", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="test"
    )

    edge = GraphEdge(
        id="edge",
        source_node_id=entity_a,
        target_node_id=entity_b,
        relationship_type=RelationshipType.related_to,
        source_doc_id="doc1",
    )

    mock_graph_store.nodes["test"] = [node_a, node_b]
    mock_graph_store.edges["test"] = [edge]
    # No mentions at all (pre-E2b)
    mock_graph_store.mentions["test"] = []

    view = await inspect_collection(
        mock_graph_store, "test", total_chunk_count=10, max_nodes=1000, max_edges=1000
    )

    assert all(n.chunk_count == 0 for n in view.nodes)
    assert all(n.salience == 0.0 for n in view.nodes)
    assert all(e.weight == 0 for e in view.edges)
    assert all(e.source_chunk_ids == [] for e in view.edges)


@pytest.mark.asyncio
async def test_inspect_chunk_count_deduplicates_entity_chunk_pairs(mock_graph_store: MockGraphStore):
    """Duplicate (entity_id, chunk_id) pairs → chunk_count = unique pairs."""
    entity_id = "entity"
    node = GraphNode(
        id=entity_id, entity_name="Entity", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="test"
    )

    mock_graph_store.nodes["test"] = [node]
    # Same entity, same chunk, mentioned twice (extractor produced it twice)
    mock_graph_store.mentions["test"] = [
        GraphMention(entity_id=entity_id, chunk_id="chunk-1", doc_id="doc1"),
        GraphMention(entity_id=entity_id, chunk_id="chunk-1", doc_id="doc1"),  # duplicate
        GraphMention(entity_id=entity_id, chunk_id="chunk-2", doc_id="doc1"),
    ]

    view = await inspect_collection(
        mock_graph_store, "test", total_chunk_count=10, max_nodes=1000, max_edges=1000
    )

    # chunk_count should be 2 (chunk-1 and chunk-2), not 3
    assert view.nodes[0].chunk_count == 2


@pytest.mark.asyncio
async def test_inspect_node_count_is_pretruncation_total(mock_graph_store: MockGraphStore):
    """node_count reflects pre-truncation total."""
    nodes_data = [
        GraphNode(
            id=f"id-{i}",
            entity_name=f"N{i}",
            entity_type=EntityType.concept,
            source_doc_id="doc1",
            collection_name="test",
        )
        for i in range(5)
    ]
    mock_graph_store.nodes["test"] = nodes_data
    # Add mentions to ensure non-zero chunk_count
    for node in nodes_data:
        mock_graph_store.mentions.setdefault("test", []).append(GraphMention(entity_id=node.id, chunk_id=f"chunk-{node.id}", doc_id="doc1"))

    view = await inspect_collection(
        mock_graph_store, "test", total_chunk_count=10, max_nodes=3, max_edges=1000
    )

    assert view.node_count == 5  # pre-truncation
    assert len(view.nodes) == 3  # post-truncation
    assert view.truncated is True


@pytest.mark.asyncio
async def test_inspect_edge_count_is_post_node_filter_pre_edge_cap(mock_graph_store: MockGraphStore):
    """edge_count = edges where BOTH endpoints survive; before edge cap."""
    nodes_data = [
        GraphNode(
            id=f"id-{i}",
            entity_name=f"N{i}",
            entity_type=EntityType.concept,
            source_doc_id="doc1",
            collection_name="test",
        )
        for i in range(5)
    ]
    edges_data = [
        GraphEdge(
            id="e1", source_node_id="id-0", target_node_id="id-1", relationship_type=RelationshipType.related_to,
            source_doc_id="doc1"
        ),
        GraphEdge(
            id="e2", source_node_id="id-1", target_node_id="id-2", relationship_type=RelationshipType.related_to,
            source_doc_id="doc1"
        ),
        GraphEdge(
            id="e3", source_node_id="id-3", target_node_id="id-4", relationship_type=RelationshipType.related_to,
            source_doc_id="doc1"
        ),
        # 8 more edges (all will be filtered out by node cap)
        GraphEdge(
            id="e4", source_node_id="id-0", target_node_id="id-3", relationship_type=RelationshipType.related_to,
            source_doc_id="doc1"
        ),
    ]

    mock_graph_store.nodes["test"] = nodes_data
    mock_graph_store.edges["test"] = edges_data
    # Add mentions for each node
    for i in range(5):
        mock_graph_store.mentions.setdefault("test", []).append(
            GraphMention(entity_id=f"id-{i}", chunk_id=f"chunk-{i}", doc_id="doc1")
        )

    view = await inspect_collection(
        mock_graph_store, "test", total_chunk_count=10, max_nodes=3, max_edges=2
    )

    # With max_nodes=3, survivors are: id-0, id-1, id-2 (sorted by chunk_count desc, then id asc)
    # e1: id-0 → id-1 ✓ (both survive)
    # e2: id-1 → id-2 ✓ (both survive)
    # e3: id-3 → id-4 ✗ (neither survive)
    # e4: id-0 → id-3 ✗ (id-3 doesn't survive)
    # So edge_count = 2, len(edges) = 2
    assert view.edge_count == 2
    assert len(view.edges) == 2
    assert view.truncated is True


@pytest.mark.asyncio
async def test_inspect_sets_truncated_when_mentions_exceed_ceiling(mock_graph_store: MockGraphStore):
    """When mention scan hits ceiling, truncated=True regardless of node/edge counts."""
    node = GraphNode(
        id="id", entity_name="N", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="test"
    )
    mock_graph_store.nodes["test"] = [node]

    # Simulate hitting the ceiling by setting enough mentions
    # (mock_graph_store will return up to _MENTIONS_SCAN_CEILING)
    mock_graph_store.mentions["test"] = [
        GraphMention(entity_id="id", chunk_id=f"chunk-{i}", doc_id="doc1") for i in range(_MENTIONS_SCAN_CEILING)
    ]

    view = await inspect_collection(
        mock_graph_store, "test", total_chunk_count=10, max_nodes=10, max_edges=10
    )

    # Even though node/edge counts don't exceed limits, truncated should be True
    # because mention ceiling was hit
    assert view.truncated is True


# ============================================================================
# inspect_cross_collection tests
# ============================================================================


@pytest.mark.asyncio
async def test_cross_collection_node_dedup_sums_chunk_counts(mock_graph_store: MockGraphStore):
    """Same entity_id in 2 collections → merged node with chunk_count = sum."""
    entity_id = "shared-entity"

    # Collection A
    node_a = GraphNode(
        id=entity_id, entity_name="SharedEntity", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="a"
    )
    mock_graph_store.nodes["a"] = [node_a]
    mock_graph_store.mentions["a"] = [
        GraphMention(entity_id=entity_id, chunk_id="a-chunk-1", doc_id="doc1"),
        GraphMention(entity_id=entity_id, chunk_id="a-chunk-2", doc_id="doc1"),
    ]

    # Collection B
    node_b = GraphNode(
        id=entity_id, entity_name="SharedEntity", entity_type=EntityType.concept, source_doc_id="doc2", collection_name="b"
    )
    mock_graph_store.nodes["b"] = [node_b]
    mock_graph_store.mentions["b"] = [
        GraphMention(entity_id=entity_id, chunk_id="b-chunk-1", doc_id="doc2"),
        GraphMention(entity_id=entity_id, chunk_id="b-chunk-2", doc_id="doc2"),
        GraphMention(entity_id=entity_id, chunk_id="b-chunk-3", doc_id="doc2"),
    ]

    view = await inspect_cross_collection(
        mock_graph_store,
        ["a", "b"],
        {"a": 10, "b": 10},
        max_nodes=1000,
        max_edges=1000,
    )

    assert len(view.nodes) == 1
    assert view.nodes[0].chunk_count == 5  # 2 + 3


@pytest.mark.asyncio
async def test_cross_collection_salience_weighted_avg(mock_graph_store: MockGraphStore):
    """Salience is weighted average across collections."""
    entity_id = "entity"

    # Collection A: chunk_count=4, salience_a=0.4 (4/10)
    node_a = GraphNode(
        id=entity_id, entity_name="Entity", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="a"
    )
    mock_graph_store.nodes["a"] = [node_a]
    mock_graph_store.mentions["a"] = [
        GraphMention(entity_id=entity_id, chunk_id=f"chunk-{i}", doc_id="doc1") for i in range(4)
    ]

    # Collection B: chunk_count=2, salience_b=0.4 (2/5)
    node_b = GraphNode(
        id=entity_id, entity_name="Entity", entity_type=EntityType.concept, source_doc_id="doc2", collection_name="b"
    )
    mock_graph_store.nodes["b"] = [node_b]
    mock_graph_store.mentions["b"] = [
        GraphMention(entity_id=entity_id, chunk_id=f"chunk-{i}", doc_id="doc2") for i in range(2)
    ]

    view = await inspect_cross_collection(
        mock_graph_store,
        ["a", "b"],
        {"a": 10, "b": 5},
        max_nodes=1000,
        max_edges=1000,
    )

    # Weighted avg: (4*0.4 + 2*0.4) / (4 + 2) = (1.6 + 0.8) / 6 = 2.4 / 6 = 0.4
    assert view.nodes[0].chunk_count == 6
    assert view.nodes[0].salience == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_cross_collection_edge_dedup_sums_weights(mock_graph_store: MockGraphStore):
    """Same edge_id in 2 collections → merged edge with weight = sum."""
    edge_id = "shared-edge"
    entity_a = "a"
    entity_b = "b"

    # Collection 1
    node_a1 = GraphNode(
        id=entity_a, entity_name="A", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="col1"
    )
    node_b1 = GraphNode(
        id=entity_b, entity_name="B", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="col1"
    )
    edge1 = GraphEdge(
        id=edge_id, source_node_id=entity_a, target_node_id=entity_b, relationship_type=RelationshipType.related_to,
        source_doc_id="doc1"
    )

    mock_graph_store.nodes["col1"] = [node_a1, node_b1]
    mock_graph_store.edges["col1"] = [edge1]
    mock_graph_store.mentions["col1"] = [
        GraphMention(entity_id=entity_a, chunk_id="chunk-1", doc_id="doc1"),
        GraphMention(entity_id=entity_b, chunk_id="chunk-1", doc_id="doc1"),
    ]

    # Collection 2
    node_a2 = GraphNode(
        id=entity_a, entity_name="A", entity_type=EntityType.concept, source_doc_id="doc2", collection_name="col2"
    )
    node_b2 = GraphNode(
        id=entity_b, entity_name="B", entity_type=EntityType.concept, source_doc_id="doc2", collection_name="col2"
    )
    edge2 = GraphEdge(
        id=edge_id, source_node_id=entity_a, target_node_id=entity_b, relationship_type=RelationshipType.related_to,
        source_doc_id="doc2"
    )

    mock_graph_store.nodes["col2"] = [node_a2, node_b2]
    mock_graph_store.edges["col2"] = [edge2]
    mock_graph_store.mentions["col2"] = [
        GraphMention(entity_id=entity_a, chunk_id="chunk-1", doc_id="doc2"),
        GraphMention(entity_id=entity_b, chunk_id="chunk-1", doc_id="doc2"),
        GraphMention(entity_id=entity_a, chunk_id="chunk-2", doc_id="doc2"),
        GraphMention(entity_id=entity_b, chunk_id="chunk-2", doc_id="doc2"),
    ]

    view = await inspect_cross_collection(
        mock_graph_store,
        ["col1", "col2"],
        {"col1": 10, "col2": 10},
        max_nodes=1000,
        max_edges=1000,
    )

    assert len(view.edges) == 1
    assert view.edges[0].weight == 3  # 1 + 2


@pytest.mark.asyncio
async def test_cross_collection_source_chunk_ids_unioned_and_capped(mock_graph_store: MockGraphStore):
    """source_chunk_ids are unioned and capped at 20."""
    entity_a = "a"
    entity_b = "b"
    edge_id = "edge"

    # Collection 1: A and B co-occur in chunks 0-9
    node_a1 = GraphNode(
        id=entity_a, entity_name="A", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="c1"
    )
    node_b1 = GraphNode(
        id=entity_b, entity_name="B", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="c1"
    )
    edge1 = GraphEdge(
        id=edge_id, source_node_id=entity_a, target_node_id=entity_b, relationship_type=RelationshipType.related_to,
        source_doc_id="doc1"
    )

    mock_graph_store.nodes["c1"] = [node_a1, node_b1]
    mock_graph_store.edges["c1"] = [edge1]
    mock_graph_store.mentions["c1"] = (
        [GraphMention(entity_id=entity_a, chunk_id=f"chunk-{i}", doc_id="doc1") for i in range(10)]
        + [GraphMention(entity_id=entity_b, chunk_id=f"chunk-{i}", doc_id="doc1") for i in range(10)]
    )

    # Collection 2: A and B co-occur in chunks 10-19
    node_a2 = GraphNode(
        id=entity_a, entity_name="A", entity_type=EntityType.concept, source_doc_id="doc2", collection_name="c2"
    )
    node_b2 = GraphNode(
        id=entity_b, entity_name="B", entity_type=EntityType.concept, source_doc_id="doc2", collection_name="c2"
    )
    edge2 = GraphEdge(
        id=edge_id, source_node_id=entity_a, target_node_id=entity_b, relationship_type=RelationshipType.related_to,
        source_doc_id="doc2"
    )

    mock_graph_store.nodes["c2"] = [node_a2, node_b2]
    mock_graph_store.edges["c2"] = [edge2]
    mock_graph_store.mentions["c2"] = (
        [GraphMention(entity_id=entity_a, chunk_id=f"chunk-{i}", doc_id="doc2") for i in range(10, 20)]
        + [GraphMention(entity_id=entity_b, chunk_id=f"chunk-{i}", doc_id="doc2") for i in range(10, 20)]
    )

    view = await inspect_cross_collection(
        mock_graph_store,
        ["c1", "c2"],
        {"c1": 100, "c2": 100},
        max_nodes=1000,
        max_edges=1000,
    )

    # Union of chunks: chunk-0 to chunk-19 (20 unique chunks)
    # Should be capped at 20
    assert len(view.edges[0].source_chunk_ids) == 20
    # Verify sorted
    assert view.edges[0].source_chunk_ids == sorted(view.edges[0].source_chunk_ids)


@pytest.mark.asyncio
async def test_cross_collection_one_empty_collection_contributes_zero(mock_graph_store: MockGraphStore):
    """One collection with absent tables → result contains only the non-empty collection's data."""
    entity_id = "entity"

    # Collection A has data
    node_a = GraphNode(
        id=entity_id, entity_name="Entity", entity_type=EntityType.concept, source_doc_id="doc1", collection_name="a"
    )
    mock_graph_store.nodes["a"] = [node_a]
    mock_graph_store.mentions["a"] = [GraphMention(entity_id=entity_id, chunk_id="chunk-1", doc_id="doc1")]

    # Collection B has no data (tables absent)
    # mock_graph_store.nodes["b"] = []
    # mock_graph_store.edges["b"] = []
    # mock_graph_store.mentions["b"] = []

    view = await inspect_cross_collection(
        mock_graph_store,
        ["a", "b"],
        {"a": 10, "b": 10},
        max_nodes=1000,
        max_edges=1000,
    )

    # Should contain only data from collection A
    assert view.node_count == 1
    assert len(view.nodes) == 1


@pytest.mark.asyncio
async def test_cross_collection_truncation_fires_after_merge(mock_graph_store: MockGraphStore):
    """Truncation fires on merged (post-dedup) graph."""
    # Create 6 distinct nodes across 2 collections (3 each)
    for i in range(3):
        node = GraphNode(
            id=f"id-a-{i}",
            entity_name=f"A{i}",
            entity_type=EntityType.concept,
            source_doc_id="doc1",
            collection_name="a",
        )
        mock_graph_store.nodes.setdefault("a", []).append(node)
        mock_graph_store.mentions.setdefault("a", []).append(
            GraphMention(entity_id=f"id-a-{i}", chunk_id=f"chunk-{i}", doc_id="doc1")
        )

    for i in range(3):
        node = GraphNode(
            id=f"id-b-{i}",
            entity_name=f"B{i}",
            entity_type=EntityType.concept,
            source_doc_id="doc2",
            collection_name="b",
        )
        mock_graph_store.nodes.setdefault("b", []).append(node)
        mock_graph_store.mentions.setdefault("b", []).append(
            GraphMention(entity_id=f"id-b-{i}", chunk_id=f"chunk-{i}", doc_id="doc2")
        )

    view = await inspect_cross_collection(
        mock_graph_store,
        ["a", "b"],
        {"a": 10, "b": 10},
        max_nodes=4,
        max_edges=1000,
    )

    # Merged has 6 nodes; cap at 4; should be truncated
    assert view.node_count == 6
    assert len(view.nodes) == 4
    assert view.truncated is True


# ============================================================================
# GraphML export tests (BE-8)
# ============================================================================


def test_to_graphml_produces_valid_xml():
    """to_graphml() produces valid XML with graphml root element."""
    import xml.etree.ElementTree as ET

    nodes = [
        GraphNodeInspection("entity-1", "Entity One", chunk_count=5, salience=0.5),
        GraphNodeInspection("entity-2", "Entity Two", chunk_count=3, salience=0.3),
    ]
    edges = [
        GraphEdgeInspection(
            "edge-1", "entity-1", "entity-2", weight=2, source_chunk_ids=["chunk-a", "chunk-b"]
        )
    ]
    view = CollectionGraphView(
        nodes=nodes, edges=edges, node_count=2, edge_count=1, truncated=False
    )

    graphml_bytes = to_graphml(view)

    # Parse and verify root tag is graphml
    root = ET.fromstring(graphml_bytes)
    assert root.tag.endswith("graphml") or root.tag == "graphml"
    # Verify it's valid bytes and can be decoded
    assert isinstance(graphml_bytes, bytes)
    assert len(graphml_bytes) > 0


def test_to_graphml_includes_truncated_attribute():
    """to_graphml() includes truncated flag as graph-level <data> element."""
    import xml.etree.ElementTree as ET

    nodes = [GraphNodeInspection("entity-1", "Entity One", chunk_count=5, salience=0.5)]
    edges = []

    # Test with truncated=True
    view_truncated = CollectionGraphView(
        nodes=nodes, edges=edges, node_count=1, edge_count=0, truncated=True
    )
    graphml_bytes = to_graphml(view_truncated)

    root = ET.fromstring(graphml_bytes)
    # Extract namespace from the root element
    namespace = None
    if '}' in root.tag:
        namespace = root.tag.split('}')[0] + '}'

    # Find the graph element and its truncated data child
    if namespace:
        graph_elem = root.find(f".//{namespace}graph")
        data_elem = graph_elem.find(f"{namespace}data[@key='truncated']") if graph_elem is not None else None
    else:
        graph_elem = root.find(".//graph")
        data_elem = graph_elem.find("data[@key='truncated']") if graph_elem is not None else None

    assert data_elem is not None
    assert data_elem.text == "true"

    # Test with truncated=False
    view_not_truncated = CollectionGraphView(
        nodes=nodes, edges=edges, node_count=1, edge_count=0, truncated=False
    )
    graphml_bytes = to_graphml(view_not_truncated)

    root = ET.fromstring(graphml_bytes)
    # Extract namespace again for the new root
    namespace = None
    if '}' in root.tag:
        namespace = root.tag.split('}')[0] + '}'

    # Find the graph element and its truncated data child
    if namespace:
        graph_elem = root.find(f".//{namespace}graph")
        data_elem = graph_elem.find(f"{namespace}data[@key='truncated']") if graph_elem is not None else None
    else:
        graph_elem = root.find(".//graph")
        data_elem = graph_elem.find("data[@key='truncated']") if graph_elem is not None else None

    assert data_elem is not None
    assert data_elem.text == "false"


def test_graphml_networkx_import_error_yields_clear_message():
    """to_graphml() raises ImportError with actionable message when networkx missing."""
    import sys
    from unittest.mock import patch

    nodes = [GraphNodeInspection("entity-1", "Entity One", chunk_count=5, salience=0.5)]
    edges = []
    view = CollectionGraphView(
        nodes=nodes, edges=edges, node_count=1, edge_count=0, truncated=False
    )

    # Mock the networkx import to fail
    with patch.dict(sys.modules, {"networkx": None}):
        with pytest.raises(ImportError) as exc_info:
            to_graphml(view)
        assert "GraphML export requires networkx" in str(exc_info.value)
        assert "archon-search[graph]" in str(exc_info.value)


# ============================================================================
# Integration tests (real GraphStore with LanceDB)
# ============================================================================


@pytest.mark.asyncio
async def test_inspect_cross_collection_real_store(tmp_path):
    """Integration test: cross-collection inspection with real GraphStore and LanceDB.

    Writes data to 2 collections with a shared entity, verifies that
    inspect_cross_collection correctly merges the entity (summed chunk_count).
    """
    from archon_search.graph_store import GraphStore

    # Create a GraphStore instance with the tmp_path
    db_path = str(tmp_path / "test.db")
    graph_store = GraphStore(db_path)

    # Connect to the database
    await graph_store.connect()

    # Define collections
    col_a = "collection-a"
    col_b = "collection-b"

    # Ensure graph tables exist for both collections
    await graph_store.ensure_graph_tables(col_a)
    await graph_store.ensure_graph_tables(col_b)

    # Create nodes for collection A
    nodes_a = [
        GraphNode(
            id="entity-1",  # Shared entity across collections
            entity_name="Entity One",
            entity_type=EntityType.concept,
            source_doc_id="doc-a-1",
            collection_name=col_a,
        ),
        GraphNode(
            id="entity-a-2",  # Unique to collection A
            entity_name="Entity A2",
            entity_type=EntityType.concept,
            source_doc_id="doc-a-2",
            collection_name=col_a,
        ),
    ]

    # Create nodes for collection B
    nodes_b = [
        GraphNode(
            id="entity-1",  # Shared entity across collections
            entity_name="Entity One",
            entity_type=EntityType.concept,
            source_doc_id="doc-b-1",
            collection_name=col_b,
        ),
        GraphNode(
            id="entity-b-2",  # Unique to collection B
            entity_name="Entity B2",
            entity_type=EntityType.concept,
            source_doc_id="doc-b-2",
            collection_name=col_b,
        ),
    ]

    # Create edges for collection A
    edges_a = [
        GraphEdge(
            id="edge-1a",
            source_node_id="entity-1",
            target_node_id="entity-a-2",
            relationship_type=RelationshipType.related_to,
            source_doc_id="doc-a-1",
        ),
    ]

    # Create edges for collection B
    edges_b = [
        GraphEdge(
            id="edge-1b",
            source_node_id="entity-1",
            target_node_id="entity-b-2",
            relationship_type=RelationshipType.related_to,
            source_doc_id="doc-b-1",
        ),
    ]

    # Create mentions for collection A (entity-1 in 2 chunks, entity-a-2 in 1 chunk)
    mentions_a = [
        GraphMention(entity_id="entity-1", chunk_id="chunk-a-1", doc_id="doc-a-1"),
        GraphMention(entity_id="entity-1", chunk_id="chunk-a-2", doc_id="doc-a-1"),
        GraphMention(entity_id="entity-a-2", chunk_id="chunk-a-3", doc_id="doc-a-2"),
    ]

    # Create mentions for collection B (entity-1 in 3 chunks, entity-b-2 in 1 chunk)
    mentions_b = [
        GraphMention(entity_id="entity-1", chunk_id="chunk-b-1", doc_id="doc-b-1"),
        GraphMention(entity_id="entity-1", chunk_id="chunk-b-2", doc_id="doc-b-1"),
        GraphMention(entity_id="entity-1", chunk_id="chunk-b-3", doc_id="doc-b-1"),
        GraphMention(entity_id="entity-b-2", chunk_id="chunk-b-4", doc_id="doc-b-2"),
    ]

    # Write data to collection A
    await graph_store.write_graph(col_a, nodes_a, edges_a)
    await graph_store.write_mentions(col_a, mentions_a)

    # Write data to collection B
    await graph_store.write_graph(col_b, nodes_b, edges_b)
    await graph_store.write_mentions(col_b, mentions_b)

    # Call inspect_cross_collection
    total_chunk_counts = {
        col_a: 10,  # Denominator for salience in collection A
        col_b: 10,  # Denominator for salience in collection B
    }
    view = await inspect_cross_collection(
        graph_store,
        [col_a, col_b],
        total_chunk_counts,
        max_nodes=1000,
        max_edges=1000,
    )

    # Verify merged nodes
    # entity-1 should be merged with chunk_count = 2 + 3 = 5
    assert view.node_count == 3  # Total nodes before dedup: 2 + 2
    assert len(view.nodes) == 3  # All 3 merged nodes

    # Find the merged entity-1
    entity_1_merged = None
    for node in view.nodes:
        if node.entity_id == "entity-1":
            entity_1_merged = node
            break

    assert entity_1_merged is not None
    assert entity_1_merged.chunk_count == 5  # 2 from A + 3 from B

    # Other entities should have their counts unchanged
    entity_a2 = next((n for n in view.nodes if n.entity_id == "entity-a-2"), None)
    assert entity_a2 is not None
    assert entity_a2.chunk_count == 1

    entity_b2 = next((n for n in view.nodes if n.entity_id == "entity-b-2"), None)
    assert entity_b2 is not None
    assert entity_b2.chunk_count == 1

    # Verify edges are present
    assert len(view.edges) == 2  # edge-1a and edge-1b (different IDs, no merge)


# ============================================================================
# TF-IDF salience tests (BE-2 / E2c)
# ============================================================================


def _make_node(entity_id: str, entity_name: str, collection: str = "test") -> "GraphNode":
    return GraphNode(
        id=entity_id,
        entity_name=entity_name,
        entity_type=EntityType.concept,
        source_doc_id="doc1",
        collection_name=collection,
    )


def _make_mention(entity_id: str, chunk_id: str) -> "GraphMention":
    return GraphMention(entity_id=entity_id, chunk_id=chunk_id, doc_id="doc1")


@pytest.mark.asyncio
async def test_inspect_collection_tfidf_domain_specific_outranks_ubiquitous(mock_graph_store: MockGraphStore):
    """Domain-specific entity (low df) ranks above ubiquitous entity (high df) in tfidf mode.

    unique-entity: chunk_count=3, TF=0.3, IDF=log(4/1)≈1.386, salience≈0.416
    common-entity: chunk_count=6, TF=0.6, IDF=log(4/3)≈0.288, salience≈0.173
    Despite common-entity having higher chunk_count, unique-entity should rank first.
    """
    mock_graph_store.nodes["test"] = [
        _make_node("unique-entity", "Unique"),
        _make_node("common-entity", "Common"),
    ]
    mock_graph_store.mentions["test"] = (
        [_make_mention("unique-entity", f"u-chunk-{i}") for i in range(3)]
        + [_make_mention("common-entity", f"c-chunk-{i}") for i in range(6)]
    )

    entity_presence = {"unique-entity": 1, "common-entity": 3}

    view = await inspect_collection(
        mock_graph_store,
        "test",
        total_chunk_count=10,
        max_nodes=1000,
        max_edges=1000,
        salience_mode="tfidf",
        entity_presence=entity_presence,
        num_collections=3,
    )

    assert view.salience_mode == "tfidf"
    # unique-entity should rank first (higher TF-IDF salience despite lower chunk_count)
    assert view.nodes[0].entity_id == "unique-entity"
    assert view.nodes[1].entity_id == "common-entity"
    # Verify salience values are correct (TF-IDF, unbounded)
    import math
    expected_unique = (3 / 10) * math.log((3 + 1) / 1)
    expected_common = (6 / 10) * math.log((3 + 1) / 3)
    assert view.nodes[0].salience == pytest.approx(expected_unique, rel=1e-6)
    assert view.nodes[1].salience == pytest.approx(expected_common, rel=1e-6)


@pytest.mark.asyncio
async def test_inspect_collection_tfidf_single_namespace_collection_same_order(mock_graph_store: MockGraphStore):
    """With 1 namespace collection, TF-IDF rank order equals frequency rank order.

    When num_collections=1, IDF=log(2/df). All entities have df=1 (only one collection),
    so IDF=log(2) for all. Salience = TF * log(2), which preserves chunk_count ordering.
    """
    mock_graph_store.nodes["test"] = [
        _make_node("entity-a", "A"),
        _make_node("entity-b", "B"),
        _make_node("entity-c", "C"),
    ]
    mock_graph_store.mentions["test"] = (
        [_make_mention("entity-a", f"a-{i}") for i in range(5)]
        + [_make_mention("entity-b", f"b-{i}") for i in range(3)]
        + [_make_mention("entity-c", f"c-{i}") for i in range(1)]
    )

    entity_presence = {"entity-a": 1, "entity-b": 1, "entity-c": 1}

    view_freq = await inspect_collection(
        mock_graph_store,
        "test",
        total_chunk_count=20,
        max_nodes=1000,
        max_edges=1000,
        salience_mode="frequency",
    )
    view_tfidf = await inspect_collection(
        mock_graph_store,
        "test",
        total_chunk_count=20,
        max_nodes=1000,
        max_edges=1000,
        salience_mode="tfidf",
        entity_presence=entity_presence,
        num_collections=1,
    )

    # Both modes should produce the same entity ordering
    assert [n.entity_id for n in view_freq.nodes] == [n.entity_id for n in view_tfidf.nodes]
    assert view_tfidf.salience_mode == "tfidf"


@pytest.mark.asyncio
async def test_inspect_collection_tfidf_entity_in_all_collections_near_zero(mock_graph_store: MockGraphStore):
    """Entity present in all N collections has salience approaching 0 as N grows.

    IDF = log((N+1)/N) → 0 as N → ∞.
    """
    mock_graph_store.nodes["test"] = [_make_node("ubiquitous", "Ubiquitous")]
    mock_graph_store.mentions["test"] = [_make_mention("ubiquitous", f"chunk-{i}") for i in range(5)]

    import math

    for num_collections in (10, 100, 1000):
        entity_presence = {"ubiquitous": num_collections}  # in all collections
        view = await inspect_collection(
            mock_graph_store,
            "test",
            total_chunk_count=10,
            max_nodes=1000,
            max_edges=1000,
            salience_mode="tfidf",
            entity_presence=entity_presence,
            num_collections=num_collections,
        )
        idf = math.log((num_collections + 1) / num_collections)
        tf = 5 / 10
        expected_salience = tf * idf
        assert view.nodes[0].salience == pytest.approx(expected_salience, rel=1e-6)

    # Verify that salience decreases as N grows
    results = []
    for num_collections in (5, 50, 500):
        entity_presence = {"ubiquitous": num_collections}
        view = await inspect_collection(
            mock_graph_store,
            "test",
            total_chunk_count=10,
            max_nodes=1000,
            max_edges=1000,
            salience_mode="tfidf",
            entity_presence=entity_presence,
            num_collections=num_collections,
        )
        results.append(view.nodes[0].salience)

    assert results[0] > results[1] > results[2]


@pytest.mark.asyncio
async def test_inspect_collection_tfidf_truncation_uses_salience_not_chunk_count(mock_graph_store: MockGraphStore):
    """TF-IDF truncation sorts by salience, not chunk_count.

    In tfidf mode: node with lower chunk_count but higher salience survives the cap
    over a high-frequency but ubiquitous node.
    """
    # unique-entity: chunk_count=2, TF=2/20=0.1, IDF=log(4/1)=log(4)≈1.386, salience≈0.139
    # common-entity: chunk_count=8, TF=8/20=0.4, IDF=log(4/3)≈0.288, salience≈0.115
    # unique-entity has LOWER chunk_count but HIGHER tfidf salience
    mock_graph_store.nodes["test"] = [
        _make_node("unique-entity", "Unique"),
        _make_node("common-entity", "Common"),
    ]
    mock_graph_store.mentions["test"] = (
        [_make_mention("unique-entity", f"u-{i}") for i in range(2)]
        + [_make_mention("common-entity", f"c-{i}") for i in range(8)]
    )

    entity_presence = {"unique-entity": 1, "common-entity": 3}

    # With max_nodes=1 in tfidf mode, unique-entity should survive (higher salience)
    view_tfidf = await inspect_collection(
        mock_graph_store,
        "test",
        total_chunk_count=20,
        max_nodes=1,
        max_edges=1000,
        salience_mode="tfidf",
        entity_presence=entity_presence,
        num_collections=3,
    )
    assert view_tfidf.nodes[0].entity_id == "unique-entity"

    # With max_nodes=1 in frequency mode, common-entity should survive (higher chunk_count)
    view_freq = await inspect_collection(
        mock_graph_store,
        "test",
        total_chunk_count=20,
        max_nodes=1,
        max_edges=1000,
        salience_mode="frequency",
    )
    assert view_freq.nodes[0].entity_id == "common-entity"


@pytest.mark.asyncio
async def test_inspect_collection_tfidf_zero_chunks(mock_graph_store: MockGraphStore):
    """Collection with 0 total chunks → all salience=0.0 in tfidf mode."""
    mock_graph_store.nodes["test"] = [_make_node("entity-a", "A")]
    mock_graph_store.mentions["test"] = [_make_mention("entity-a", "chunk-1")]

    view = await inspect_collection(
        mock_graph_store,
        "test",
        total_chunk_count=0,
        max_nodes=1000,
        max_edges=1000,
        salience_mode="tfidf",
        entity_presence={"entity-a": 1},
        num_collections=3,
    )

    assert all(n.salience == 0.0 for n in view.nodes)
    assert view.salience_mode == "tfidf"


@pytest.mark.asyncio
async def test_inspect_collection_tfidf_pre_e2b_nodes(mock_graph_store: MockGraphStore):
    """Absent mentions table → chunk_count=0, salience=0.0, salience_mode echoed."""
    mock_graph_store.nodes["test"] = [
        _make_node("entity-a", "A"),
        _make_node("entity-b", "B"),
    ]
    # No mentions (pre-E2b state)
    mock_graph_store.mentions["test"] = []

    view = await inspect_collection(
        mock_graph_store,
        "test",
        total_chunk_count=10,
        max_nodes=1000,
        max_edges=1000,
        salience_mode="tfidf",
        entity_presence={"entity-a": 1, "entity-b": 1},
        num_collections=3,
    )

    assert all(n.chunk_count == 0 for n in view.nodes)
    assert all(n.salience == 0.0 for n in view.nodes)
    assert view.salience_mode == "tfidf"


@pytest.mark.asyncio
async def test_inspect_collection_frequency_unchanged(mock_graph_store: MockGraphStore):
    """Frequency mode (default) produces same results as before BE-2 (regression guard)."""
    mock_graph_store.nodes["test"] = [
        _make_node("entity-a", "A"),
        _make_node("entity-b", "B"),
    ]
    mock_graph_store.mentions["test"] = (
        [_make_mention("entity-a", f"a-{i}") for i in range(4)]
        + [_make_mention("entity-b", f"b-{i}") for i in range(2)]
    )

    # Default call (no new params) should work with salience_mode='frequency'
    view = await inspect_collection(
        mock_graph_store,
        "test",
        total_chunk_count=10,
        max_nodes=1000,
        max_edges=1000,
    )

    assert view.salience_mode == "frequency"
    # entity-a has higher chunk_count, should rank first
    assert view.nodes[0].entity_id == "entity-a"
    assert view.nodes[0].chunk_count == 4
    assert view.nodes[0].salience == pytest.approx(0.4)  # 4/10 clamped to [0.0, 1.0]
    assert view.nodes[1].entity_id == "entity-b"
    assert view.nodes[1].salience == pytest.approx(0.2)  # 2/10


@pytest.mark.asyncio
async def test_inspect_collection_tfidf_edge_count_consistent_with_node_set(mock_graph_store: MockGraphStore):
    """In tfidf mode, edge_count uses tfidf sort — the surviving node set differs from frequency.

    Three-node, two-edge fixture where frequency and tfidf select different top-2 nodes:
      node-a: chunk_count=10, df=3 (ubiquitous) → freq rank 1, tfidf rank 3
      node-b: chunk_count=5,  df=1 (unique)      → freq rank 2, tfidf rank 1
      node-c: chunk_count=3,  df=1 (unique)      → freq rank 3, tfidf rank 2

    With num_collections=3, total_chunk_count=20:
      tfidf-a = 0.5  * log(4/3) ≈ 0.144
      tfidf-b = 0.25 * log(4/1) ≈ 0.347
      tfidf-c = 0.15 * log(4/1) ≈ 0.208

    Frequency top-2: {node-a, node-b}
    TF-IDF top-2:    {node-b, node-c}

    Two edges: edge-ab (node-a → node-b) and edge-bc (node-b → node-c)
      TF-IDF mode:   node-a not in {b, c} → edge-ab drops, edge-bc survives → edge_count = 1
      Frequency mode: node-c not in {a, b} → edge-bc drops, edge-ab survives → edge_count = 1

    The test asserts nodes[0], edges[0], and edge_count for both modes.
    If edge_count incorrectly uses the frequency sort key in tfidf mode,
    it would return 0 instead of 1 (edge-bc filtered out), and the assertion catches the bug.
    """
    from archon_search.graph_types import GraphEdge, RelationshipType

    mock_graph_store.nodes["test"] = [
        _make_node("node-a", "A"),
        _make_node("node-b", "B"),
        _make_node("node-c", "C"),
    ]
    mock_graph_store.edges["test"] = [
        GraphEdge(
            id="edge-ab",
            source_node_id="node-a",
            target_node_id="node-b",
            relationship_type=RelationshipType.related_to,
            source_doc_id="doc1",
        ),
        GraphEdge(
            id="edge-bc",
            source_node_id="node-b",
            target_node_id="node-c",
            relationship_type=RelationshipType.related_to,
            source_doc_id="doc1",
        ),
    ]
    mock_graph_store.mentions["test"] = (
        [_make_mention("node-a", f"a-{i}") for i in range(10)]
        + [_make_mention("node-b", f"b-{i}") for i in range(5)]
        + [_make_mention("node-c", f"c-{i}") for i in range(3)]
    )

    entity_presence = {"node-a": 3, "node-b": 1, "node-c": 1}
    num_collections = 3
    total_chunk_count = 20

    # --- TF-IDF mode ---
    view_tfidf = await inspect_collection(
        mock_graph_store,
        "test",
        total_chunk_count=total_chunk_count,
        max_nodes=2,
        max_edges=1000,
        salience_mode="tfidf",
        entity_presence=entity_presence,
        num_collections=num_collections,
    )

    # node-b has highest tfidf salience
    assert view_tfidf.nodes[0].entity_id == "node-b"
    # node-a not in tfidf top-2; edge-ab (node-a → node-b) is dropped
    assert view_tfidf.edge_count == 1   # only edge-bc (node-b → node-c) survives
    assert len(view_tfidf.edges) == 1
    assert view_tfidf.edges[0].edge_id == "edge-bc"

    # --- Frequency mode (regression guard: different edge survives) ---
    view_freq = await inspect_collection(
        mock_graph_store,
        "test",
        total_chunk_count=total_chunk_count,
        max_nodes=2,
        max_edges=1000,
        salience_mode="frequency",
    )

    # node-a and node-b are top-2 by chunk_count; edge-ab survives, not edge-bc
    surviving_freq = {n.entity_id for n in view_freq.nodes}
    assert surviving_freq == {"node-a", "node-b"}
    assert view_freq.edge_count == 1
    assert view_freq.edges[0].edge_id == "edge-ab"


@pytest.mark.asyncio
async def test_inspect_collection_tfidf_equal_salience_tiebreak_entity_id(mock_graph_store: MockGraphStore):
    """Two entities with identical TF-IDF salience are ordered by entity_id ascending."""
    # entity-aaa and entity-zzz: same chunk_count, same df → same TF-IDF salience
    mock_graph_store.nodes["test"] = [
        _make_node("entity-zzz", "Z"),
        _make_node("entity-aaa", "A"),
    ]
    mock_graph_store.mentions["test"] = (
        [_make_mention("entity-zzz", f"z-{i}") for i in range(3)]
        + [_make_mention("entity-aaa", f"a-{i}") for i in range(3)]
    )

    entity_presence = {"entity-zzz": 1, "entity-aaa": 1}

    view = await inspect_collection(
        mock_graph_store,
        "test",
        total_chunk_count=10,
        max_nodes=1000,
        max_edges=1000,
        salience_mode="tfidf",
        entity_presence=entity_presence,
        num_collections=3,
    )

    # Both have same salience; entity-aaa < entity-zzz lexicographically → entity-aaa first
    assert view.nodes[0].entity_id == "entity-aaa"
    assert view.nodes[1].entity_id == "entity-zzz"
    assert view.nodes[0].salience == pytest.approx(view.nodes[1].salience)


@pytest.mark.asyncio
async def test_mcp_get_graph_still_returns_summary_after_signature_change(mock_graph_store: MockGraphStore):
    """Calling inspect_collection with default params works and returns expected shape.

    Verifies backward compatibility after BE-2 signature change:
    - salience_mode defaults to 'frequency'
    - CollectionGraphView still has nodes, edges, node_count, edge_count fields
      that mcp.py reads to build the summary dict.
    """
    mock_graph_store.nodes["test"] = [
        _make_node("entity-x", "X"),
        _make_node("entity-y", "Y"),
    ]
    mock_graph_store.mentions["test"] = [
        _make_mention("entity-x", "chunk-1"),
        _make_mention("entity-y", "chunk-2"),
    ]

    # Call with no new params — same as before BE-2
    view = await inspect_collection(
        mock_graph_store,
        "test",
        total_chunk_count=10,
        max_nodes=1000,
        max_edges=1000,
    )

    # Verify the fields mcp.py reads exist and are correct types
    assert isinstance(view, CollectionGraphView)
    assert view.salience_mode == "frequency"
    assert isinstance(view.nodes, list)
    assert isinstance(view.edges, list)
    assert isinstance(view.node_count, int)
    assert isinstance(view.edge_count, int)
    assert isinstance(view.truncated, bool)

    # Verify the exact dict structure mcp.py builds
    top_nodes = sorted(view.nodes, key=lambda n: (-n.salience, n.entity_id))[:20]
    summary_top_nodes = [
        {
            "entity_id": n.entity_id,
            "entity_name": n.entity_name,
            "chunk_count": n.chunk_count,
            "salience": n.salience,
        }
        for n in top_nodes
    ]
    assert len(summary_top_nodes) == 2
    assert all("salience" in entry for entry in summary_top_nodes)


@pytest.mark.asyncio
async def test_inspect_collection_tfidf_entity_presence_none_raises(mock_graph_store: MockGraphStore):
    """Calling inspect_collection(salience_mode='tfidf', entity_presence=None) raises ValueError."""
    mock_graph_store.nodes["test"] = [_make_node("entity-a", "A")]
    mock_graph_store.mentions["test"] = [_make_mention("entity-a", "chunk-1")]

    with pytest.raises(ValueError, match="entity_presence required for tfidf mode"):
        await inspect_collection(
            mock_graph_store,
            "test",
            total_chunk_count=10,
            max_nodes=1000,
            max_edges=1000,
            salience_mode="tfidf",
            entity_presence=None,
        )


@pytest.mark.asyncio
async def test_inspect_collection_tfidf_df_zero_in_presence_uses_df1(mock_graph_store: MockGraphStore):
    """entity_presence with explicit 0 value must not cause ZeroDivisionError; df=1 is used."""
    entity_id = "entity-a"
    mock_graph_store.nodes["test"] = [_make_node(entity_id, "A")]
    mock_graph_store.mentions["test"] = [_make_mention(entity_id, "chunk-1")]

    num_collections = 3
    # Explicit 0 in entity_presence — should NOT cause ZeroDivisionError
    view = await inspect_collection(
        mock_graph_store,
        "test",
        total_chunk_count=10,
        max_nodes=1000,
        max_edges=1000,
        salience_mode="tfidf",
        entity_presence={entity_id: 0},
        num_collections=num_collections,
    )

    # df=1 fallback must be used (max(..., 1) guards against the explicit 0)
    tf = 1 / 10
    expected_salience = tf * math.log((num_collections + 1) / 1)
    assert view.nodes[0].salience == pytest.approx(expected_salience, rel=1e-6)


@pytest.mark.asyncio
async def test_inspect_collection_tfidf_entity_not_in_presence_uses_df1_and_warns(
    mock_graph_store: MockGraphStore, caplog
):
    """Missing entity_presence key falls back to df=1 and emits exactly one aggregated WARNING.

    3 nodes total; only entity-x is present in entity_presence (entity-y and entity-z absent).
    The warning must say "2 of 3" — proving (a) aggregation (single warning, not one per entity)
    and (b) the correct argument order in the format string.
    """
    # 3 nodes: entity-x present in entity_presence; entity-y and entity-z absent
    mock_graph_store.nodes["test"] = [
        _make_node("entity-x", "X"),
        _make_node("entity-y", "Y"),
        _make_node("entity-z", "Z"),
    ]
    mock_graph_store.mentions["test"] = [
        _make_mention("entity-x", "chunk-1"),
        # entity-y: 5 distinct chunks
        *[_make_mention("entity-y", f"y-chunk-{i}") for i in range(5)],
        # entity-z: 3 distinct chunks
        *[_make_mention("entity-z", f"z-chunk-{i}") for i in range(3)],
    ]

    total = 20
    num_collections = 3
    with caplog.at_level(logging.WARNING, logger="archon_search.graph_inspector"):
        view = await inspect_collection(
            mock_graph_store,
            "test",
            total_chunk_count=total,
            max_nodes=1000,
            max_edges=1000,
            salience_mode="tfidf",
            entity_presence={"entity-x": 1},  # entity-y and entity-z are absent
            num_collections=num_collections,
        )

    # All three entities use df=1 fallback (entity-x explicitly set to 1; y and z default to 1)
    idf_fallback = math.log((num_collections + 1) / 1)
    node_map = {n.entity_id: n for n in view.nodes}
    assert node_map["entity-x"].salience == pytest.approx((1 / total) * idf_fallback, rel=1e-6)
    assert node_map["entity-y"].salience == pytest.approx((5 / total) * idf_fallback, rel=1e-6)
    assert node_map["entity-z"].salience == pytest.approx((3 / total) * idf_fallback, rel=1e-6)

    # Exactly one aggregated WARNING (not one per entity)
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1
    # "2 of 3" proves: 2 missing entities counted correctly, total 3 is node count not missing count
    assert "2 of 3" in warning_records[0].message


@pytest.mark.asyncio
@pytest.mark.integration
async def test_inspect_collection_tfidf_idf_formula(tmp_path):
    """Integration: verifies log((N+1)/df) IDF formula against hand-calculated values.

    Uses a real GraphStore with LanceDB to ensure end-to-end correctness.
    """
    import math
    from archon_search.graph_store import GraphStore

    db_path = str(tmp_path / "test.db")
    graph_store = GraphStore(db_path)
    await graph_store.connect()

    col = "tfidf-idf-test"
    await graph_store.ensure_graph_tables(col)

    # Create 3 entities with different document frequencies:
    #   entity-df1: unique (df=1)
    #   entity-df2: in 2 collections (df=2)
    #   entity-df3: in all 3 collections (df=3)
    from archon_search.graph_types import GraphNode, GraphMention, EntityType

    nodes = [
        GraphNode(id="entity-df1", entity_name="DF1", entity_type=EntityType.concept,
                  source_doc_id="doc1", collection_name=col),
        GraphNode(id="entity-df2", entity_name="DF2", entity_type=EntityType.concept,
                  source_doc_id="doc1", collection_name=col),
        GraphNode(id="entity-df3", entity_name="DF3", entity_type=EntityType.concept,
                  source_doc_id="doc1", collection_name=col),
    ]

    # Same TF for all: chunk_count=4, total=20 → TF=0.2
    mentions = (
        [GraphMention(entity_id="entity-df1", chunk_id=f"df1-{i}", doc_id="doc1") for i in range(4)]
        + [GraphMention(entity_id="entity-df2", chunk_id=f"df2-{i}", doc_id="doc1") for i in range(4)]
        + [GraphMention(entity_id="entity-df3", chunk_id=f"df3-{i}", doc_id="doc1") for i in range(4)]
    )

    await graph_store.write_graph(col, nodes, [])
    await graph_store.write_mentions(col, mentions)

    num_collections = 3
    entity_presence = {"entity-df1": 1, "entity-df2": 2, "entity-df3": 3}
    total_chunk_count = 20

    view = await inspect_collection(
        graph_store,
        col,
        total_chunk_count=total_chunk_count,
        max_nodes=1000,
        max_edges=1000,
        salience_mode="tfidf",
        entity_presence=entity_presence,
        num_collections=num_collections,
    )

    tf = 4 / 20  # TF = chunk_count / total_chunks

    # Verify each entity's salience matches the IDF formula
    node_map = {n.entity_id: n for n in view.nodes}

    # entity-df1: IDF = log((3+1)/1) = log(4)
    expected_df1 = tf * math.log((num_collections + 1) / 1)
    assert node_map["entity-df1"].salience == pytest.approx(expected_df1, rel=1e-6)

    # entity-df2: IDF = log((3+1)/2) = log(2)
    expected_df2 = tf * math.log((num_collections + 1) / 2)
    assert node_map["entity-df2"].salience == pytest.approx(expected_df2, rel=1e-6)

    # entity-df3: IDF = log((3+1)/3) = log(4/3)
    expected_df3 = tf * math.log((num_collections + 1) / 3)
    assert node_map["entity-df3"].salience == pytest.approx(expected_df3, rel=1e-6)

    # Ordering should be df1 > df2 > df3 (lower df → higher IDF → higher salience)
    assert node_map["entity-df1"].salience > node_map["entity-df2"].salience > node_map["entity-df3"].salience

    assert view.salience_mode == "tfidf"

"""Unit tests for graph_inspector.py (E2b graph inspection use case)."""

from __future__ import annotations

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

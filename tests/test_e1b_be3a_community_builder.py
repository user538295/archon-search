"""Unit tests for CommunityBuilder — E1b BE-3a.

Tests verify:
- build() raises ValueError when no graph nodes exist for the collection
- build() returns a single Community with WARNING when only 1 node exists
- _cluster_with_size_limit splits oversized communities via _run_leiden_partition_sync
- _split_oversized_communities respects _MAX_SPLIT_DEPTH (accepts oversized at depth limit)
- ImportError with install hint when leidenalg/igraph absent
- build() returns 10 singleton communities for 10 nodes with 0 edges
- GraphStore._arrow_to_edges static method converts an Arrow table to GraphEdge objects
"""
from __future__ import annotations

import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pyarrow as pa
import pytest

from archon_search.graph_store import GraphStore
from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_node(node_id: str, name: str | None = None) -> GraphNode:
    return GraphNode(
        id=node_id,
        entity_name=name or node_id,
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="test",
    )


def make_edge(src_id: str, tgt_id: str) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(src_id, tgt_id, "related_to"),
        source_node_id=src_id,
        target_node_id=tgt_id,
        relationship_type=RelationshipType.related_to,
        source_doc_id="doc-1",
    )


def make_mock_store(nodes: list[GraphNode], edges: list[GraphEdge]) -> MagicMock:
    store = MagicMock()
    store.get_all_nodes = AsyncMock(return_value=nodes)
    store.get_all_edges = AsyncMock(return_value=edges)
    store.write_communities = AsyncMock(return_value=None)
    return store


def make_graph_config(resolution: float = 1.0, max_size: int = 2):
    from archon_search.config import GraphConfig
    return GraphConfig(leiden_resolution=resolution, max_community_size=max_size)


# ---------------------------------------------------------------------------
# S6 — build() raises ValueError when no nodes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_communities_no_graph_nodes_raises():
    from archon_search.community_builder import CommunityBuilder

    store = make_mock_store(nodes=[], edges=[])
    config = make_graph_config()
    builder = CommunityBuilder(store, config)

    with pytest.raises(ValueError, match="entity graph"):
        await builder.build("my-col", ns="default")


# ---------------------------------------------------------------------------
# S7 — single node → 1 community, WARNING logged, leidenalg not called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_communities_single_entity_one_community(caplog):
    from archon_search.community_builder import CommunityBuilder

    one_node = make_node("node-a", "Alpha")
    store = make_mock_store(nodes=[one_node], edges=[])
    config = make_graph_config()
    builder = CommunityBuilder(store, config)

    with caplog.at_level(logging.WARNING, logger="archon_search.community_builder"):
        communities = await builder.build("test-col", ns="default")

    assert len(communities) == 1
    assert communities[0].entity_ids == ["node-a"]
    assert communities[0].representative_chunk_ids == []
    # A WARNING must have been emitted
    assert any(record.levelno >= logging.WARNING for record in caplog.records)


# ---------------------------------------------------------------------------
# S8 — _cluster_with_size_limit splits oversized community
# ---------------------------------------------------------------------------


def test_max_community_size_split():
    from archon_search.community_builder import _cluster_with_size_limit

    A, B, C = "node-a", "node-b", "node-c"
    nodes = [make_node(A), make_node(B), make_node(C)]
    edges = [make_edge(A, B), make_edge(B, C)]

    call_count = 0

    def mock_leiden(nodes_arg, edges_arg, resolution, seed=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Return a single oversized group
            return [[A, B, C]]
        else:
            # Return two groups on second call (subgraph has A, B, C)
            return [[A, B], [C]]

    with patch("archon_search.community_builder._run_leiden_partition_sync", side_effect=mock_leiden):
        result = _cluster_with_size_limit(nodes, edges, resolution=1.0, max_size=2)

    for group in result:
        assert len(group) <= 2, f"Group {group} exceeds max_size=2"
    # All original nodes must be present across all groups
    all_ids = {nid for group in result for nid in group}
    assert all_ids == {A, B, C}


# ---------------------------------------------------------------------------
# S8 — _split_oversized_communities respects _MAX_SPLIT_DEPTH
# ---------------------------------------------------------------------------


def test_max_community_size_split_depth_limit(caplog):
    from archon_search.community_builder import _split_oversized_communities, _MAX_SPLIT_DEPTH

    A, B, C = "node-a", "node-b", "node-c"
    nodes = [make_node(A), make_node(B), make_node(C)]
    nodes_by_id = {n.id: n for n in nodes}
    edges = [make_edge(A, B), make_edge(B, C)]

    # Even though max_size=2 and group has 3, at depth=_MAX_SPLIT_DEPTH it must accept
    with patch("archon_search.community_builder._run_leiden_partition_sync") as mock_leiden:
        with caplog.at_level(logging.WARNING, logger="archon_search.community_builder"):
            result = _split_oversized_communities(
                [[A, B, C]], nodes_by_id, edges, max_size=2, resolution=1.0, depth=_MAX_SPLIT_DEPTH
            )

    # Should return the group unchanged (accepted as-is)
    assert result == [[A, B, C]]
    # leidenalg must NOT have been called
    mock_leiden.assert_not_called()
    # WARNING must have been logged
    assert any(record.levelno >= logging.WARNING for record in caplog.records)


# ---------------------------------------------------------------------------
# S13 — ImportError with install hint when leidenalg absent
# ---------------------------------------------------------------------------


def test_leidenalg_absent_raises():
    from archon_search.community_builder import _run_leiden_partition_sync

    A, B = "node-a", "node-b"
    nodes = [make_node(A), make_node(B)]
    edges = [make_edge(A, B)]

    with patch.dict(sys.modules, {"leidenalg": None, "igraph": None}):
        with pytest.raises(ImportError, match="leidenalg"):
            _run_leiden_partition_sync(nodes, edges, resolution=1.0)


# ---------------------------------------------------------------------------
# S7-variant — build() with 10 nodes, 0 edges → 10 singleton communities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_communities_zero_edges_many_nodes():
    from archon_search.community_builder import CommunityBuilder

    nodes = [make_node(f"node-{i}", f"Entity{i}") for i in range(10)]
    store = make_mock_store(nodes=nodes, edges=[])
    config = make_graph_config(max_size=50)  # Large enough not to split
    builder = CommunityBuilder(store, config)

    # Patch Leiden to return 10 singleton groups
    singleton_groups = [[n.id] for n in nodes]

    with patch(
        "archon_search.community_builder._run_leiden_partition_sync",
        return_value=singleton_groups,
    ):
        communities = await builder.build("test-col", ns="default")

    assert len(communities) == 10
    # Each community has exactly 1 entity
    for c in communities:
        assert len(c.entity_ids) == 1


# ---------------------------------------------------------------------------
# GraphStore._arrow_to_edges static method
# ---------------------------------------------------------------------------


def test_arrow_to_edges_static():
    src_a = make_stable_entity_id("concept", "Alpha")
    src_b = make_stable_entity_id("concept", "Beta")
    edge_id_1 = make_stable_edge_id(src_a, src_b, "related_to")
    edge_id_2 = make_stable_edge_id(src_b, src_a, "uses")

    table = pa.table(
        {
            "id": [edge_id_1, edge_id_2],
            "source_node_id": [src_a, src_b],
            "target_node_id": [src_b, src_a],
            "relationship_type": ["related_to", "uses"],
            "source_doc_id": ["doc-1", "doc-2"],
            "extraction_method": pa.array([None, None], type=pa.utf8()),
        },
        schema=GraphStore._edges_schema(),
    )

    edges = GraphStore._arrow_to_edges(table)

    assert len(edges) == 2
    assert all(isinstance(e, GraphEdge) for e in edges)
    assert edges[0].id == edge_id_1
    assert edges[0].source_node_id == src_a
    assert edges[0].target_node_id == src_b
    assert edges[0].relationship_type == RelationshipType.related_to
    assert edges[0].source_doc_id == "doc-1"
    assert edges[1].relationship_type == RelationshipType.uses


# ---------------------------------------------------------------------------
# C1-T-9 — get_all_nodes / get_all_edges: table-absent and storage-error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_all_nodes_table_absent_returns_empty():
    from archon_search.graph_store import GraphStore

    store = MagicMock(spec=GraphStore)
    store._validate_collection = MagicMock()
    store._nodes_table_name = MagicMock(return_value="_archon_graph_test-col_nodes")
    store._load_all_from_table = AsyncMock(return_value=None)
    store._arrow_to_nodes = GraphStore._arrow_to_nodes

    # Call the real method, binding it to the mock
    result = await GraphStore.get_all_nodes(store, "test-col", ns="default")
    assert result == []


@pytest.mark.asyncio
async def test_get_all_edges_table_absent_returns_empty():
    from archon_search.graph_store import GraphStore

    store = MagicMock(spec=GraphStore)
    store._validate_collection = MagicMock()
    store._edges_table_name = MagicMock(return_value="_archon_graph_test-col_edges")
    store._load_all_from_table = AsyncMock(return_value=None)
    store._arrow_to_edges = GraphStore._arrow_to_edges

    result = await GraphStore.get_all_edges(store, "test-col", ns="default")
    assert result == []


@pytest.mark.asyncio
async def test_get_all_nodes_storage_error_raises_runtime_error():
    from archon_search.graph_store import GraphStore

    store = MagicMock(spec=GraphStore)
    store._validate_collection = MagicMock()
    store._nodes_table_name = MagicMock(return_value="_archon_graph_test-col_nodes")
    store._load_all_from_table = AsyncMock(
        side_effect=RuntimeError("Failed to load table '_archon_graph_test-col_nodes' for collection 'test-col': boom")
    )

    with pytest.raises(RuntimeError, match="Failed to load table"):
        await GraphStore.get_all_nodes(store, "test-col", ns="default")


# ---------------------------------------------------------------------------
# C1-T-12 — _split_oversized_communities: Leiden can't split → WARNING + return group
# ---------------------------------------------------------------------------


def test_split_oversized_cannot_split_single_group_returns_with_warning(caplog):
    from archon_search.community_builder import _split_oversized_communities

    A, B, C = "node-a", "node-b", "node-c"
    nodes = [make_node(A), make_node(B), make_node(C)]
    nodes_by_id = {n.id: n for n in nodes}
    edges = [make_edge(A, B), make_edge(B, C)]

    # Leiden returns a single group (still oversized) — cannot split further
    with patch(
        "archon_search.community_builder._run_leiden_partition_sync",
        return_value=[[A, B, C]],
    ):
        with caplog.at_level(logging.WARNING, logger="archon_search.community_builder"):
            result = _split_oversized_communities(
                [[A, B, C]], nodes_by_id, edges, max_size=2, resolution=1.0, depth=0
            )

    assert result == [[A, B, C]]
    assert any(record.levelno >= logging.WARNING for record in caplog.records)


# ---------------------------------------------------------------------------
# C1-T-5 — build() when Leiden returns empty groups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_leiden_returns_empty_groups():
    from archon_search.community_builder import CommunityBuilder

    nodes = [make_node(f"node-{i}") for i in range(3)]
    store = make_mock_store(nodes=nodes, edges=[])
    config = make_graph_config(max_size=10)
    builder = CommunityBuilder(store, config)

    with patch(
        "archon_search.community_builder._run_leiden_partition_sync",
        return_value=[],
    ):
        communities = await builder.build("test-col", ns="default")

    assert communities == []

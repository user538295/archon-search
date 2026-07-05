"""Integration tests for CommunityBuilder — E1b BE-3b (MMR + LLM summary stub).

Uses real GraphStore and SearchStore in tmp_path. Skips gracefully when
leidenalg is not installed.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

leidenalg = pytest.importorskip(
    "leidenalg", reason="leidenalg not installed; skipping BE-3b integration test"
)

pytestmark = pytest.mark.integration

from archon_search._types import ChunkRecord
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
from archon_search.store import SearchStore

_EMBEDDING_DIM = 3  # minimal dimension for tests


def _make_node(name: str, source_doc_id: str, collection: str = "test-col") -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id("concept", name),
        entity_name=name,
        entity_type=EntityType.concept,
        source_doc_id=source_doc_id,
        collection_name=collection,
    )


def _make_edge(src: GraphNode, tgt: GraphNode) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(src.id, tgt.id, "related_to"),
        source_node_id=src.id,
        target_node_id=tgt.id,
        relationship_type=RelationshipType.related_to,
        source_doc_id=src.source_doc_id,
    )


def _make_chunk(doc_id: str, chunk_idx: int = 0) -> ChunkRecord:
    chunk_id = f"{doc_id}-{chunk_idx:06d}"
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text=f"text for {doc_id} chunk {chunk_idx}",
        vector=[1.0, 0.0, 0.0],
        source_path=f"/fake/{doc_id}.txt",
        indexed_at="2026-01-01T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_representative_chunk_ids_populated(tmp_path: Path):
    """build() fills representative_chunk_ids with real chunk IDs from the store."""
    col = "test-col"
    doc_id_a = "doc-a" * 5 + "a" * 4  # 24 chars, valid doc_id-like string
    doc_id_b = "doc-b" * 5 + "b" * 4

    # Create two nodes pointing to different docs
    node_a = _make_node("Alpha", source_doc_id=doc_id_a, collection=col)
    node_b = _make_node("Beta", source_doc_id=doc_id_b, collection=col)
    nodes = [node_a, node_b]
    edges = [_make_edge(node_a, node_b)]

    # Set up GraphStore
    graph_store = GraphStore(tmp_path / "graph_db")
    await graph_store.connect()
    await graph_store.ensure_graph_tables(col, ns="default")
    await graph_store.write_graph(col, nodes, edges, ns="default")

    # Set up SearchStore with real chunks
    search_store = SearchStore(str(tmp_path / "search_db"))
    await search_store.connect()
    chunk_a = _make_chunk(doc_id_a, 0)
    chunk_b = _make_chunk(doc_id_b, 0)
    await search_store.ingest_chunks(col, [chunk_a])
    await search_store.ingest_chunks(col, [chunk_b])

    config = GraphConfig(
        enabled=True,
        community_summary_chunks=2,
    )
    from archon_search.community_builder import CommunityBuilder
    builder = CommunityBuilder(graph_store, config, search_store=search_store)

    communities = await builder.build(col)

    await graph_store.disconnect()
    await search_store.disconnect()

    assert len(communities) >= 1
    # All communities together must have at least one representative chunk ID
    all_rep_ids = [rid for c in communities for rid in c.representative_chunk_ids]
    assert len(all_rep_ids) > 0, "Expected at least one representative chunk ID"
    # Each ID must be a real chunk_id in the store
    real_chunk_ids = {chunk_a.chunk_id, chunk_b.chunk_id}
    for rid in all_rep_ids:
        assert rid in real_chunk_ids, f"representative_chunk_id {rid!r} not in store"


@pytest.mark.asyncio
async def test_llm_failure_still_writes_communities(tmp_path: Path):
    """When _generate_llm_summary raises, communities are still written with MMR reps."""
    col = "test-col"
    doc_id = "doc-llm" * 4 + "x" * 4

    node_a = _make_node("Alpha", source_doc_id=doc_id, collection=col)
    node_b = _make_node("Beta", source_doc_id=doc_id, collection=col)
    nodes = [node_a, node_b]
    edges = [_make_edge(node_a, node_b)]

    graph_store = GraphStore(tmp_path / "graph_db")
    await graph_store.connect()
    await graph_store.ensure_graph_tables(col, ns="default")
    await graph_store.write_graph(col, nodes, edges, ns="default")

    search_store = SearchStore(str(tmp_path / "search_db"))
    await search_store.connect()
    chunk = _make_chunk(doc_id, 0)
    await search_store.ingest_chunks(col, [chunk])

    config = GraphConfig(
        enabled=True,
        extraction_model="stub-model",
        community_summary_chunks=1,
    )
    from archon_search.community_builder import CommunityBuilder
    builder = CommunityBuilder(graph_store, config, search_store=search_store)

    with patch(
        "archon_search.community_builder._run_leiden_partition_sync",
        return_value=[[node_a.id, node_b.id]],
    ):
        communities = await builder.build(col)

    await graph_store.disconnect()
    await search_store.disconnect()

    assert len(communities) == 1
    assert communities[0].summary_text is None  # LLM stub raises → None
    assert communities[0].representative_chunk_ids != []  # MMR still ran

    # Verify communities were persisted
    graph_store2 = GraphStore(tmp_path / "graph_db")
    await graph_store2.connect()
    count, _ = await graph_store2.get_community_stats(col)
    await graph_store2.disconnect()
    assert count == 1


@pytest.mark.asyncio
async def test_build_idempotent(tmp_path: Path):
    """Calling build() twice replaces communities; count stays at 1, not 2."""
    col = "test-col"
    doc_id = "doc-idem" * 3 + "y" * 8

    node_a = _make_node("Alpha", source_doc_id=doc_id, collection=col)
    node_b = _make_node("Beta", source_doc_id=doc_id, collection=col)
    nodes = [node_a, node_b]
    edges = [_make_edge(node_a, node_b)]

    graph_store = GraphStore(tmp_path / "graph_db")
    await graph_store.connect()
    await graph_store.ensure_graph_tables(col, ns="default")
    await graph_store.write_graph(col, nodes, edges, ns="default")

    config = GraphConfig(enabled=True, community_summary_chunks=1)
    from archon_search.community_builder import CommunityBuilder
    builder = CommunityBuilder(graph_store, config)

    with patch(
        "archon_search.community_builder._run_leiden_partition_sync",
        return_value=[[node_a.id, node_b.id]],
    ):
        await builder.build(col)
        await builder.build(col)  # Second build replaces, not appends

    count, _ = await graph_store.get_community_stats(col, ns="default")
    await graph_store.disconnect()

    assert count == 1, f"Expected 1 community after idempotent build, got {count}"

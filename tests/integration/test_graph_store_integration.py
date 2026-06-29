"""Integration tests for GraphStore — E1a BE-3.

Uses real LanceDB in tmp_path. Tests verify:
- Full roundtrip: write nodes+edges, get_neighbours returns them, edge_count matches
- Table names use _archon_ prefix and are invisible to SearchStore.list_collections()
- Re-ingest of the same document does not duplicate edges
- Graph tables are NOT pruned when a document is deleted from the search store

Run with:
    uv run pytest tests/integration/test_graph_store_integration.py -v --no-cov
"""
from __future__ import annotations

import asyncio

import pytest

from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)

pytestmark = pytest.mark.integration

_EMBEDDING_DIM = 384


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(name: str, collection: str, doc_id: str = "doc-a", entity_type: EntityType = EntityType.concept) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(entity_type.value, name),
        entity_name=name,
        entity_type=entity_type,
        source_doc_id=doc_id,
        collection_name=collection,
    )


def _edge(src: GraphNode, tgt: GraphNode, doc_id: str = "doc-a") -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(src.id, tgt.id, RelationshipType.uses.value),
        source_node_id=src.id,
        target_node_id=tgt.id,
        relationship_type=RelationshipType.uses,
        source_doc_id=doc_id,
    )


# ---------------------------------------------------------------------------
# test_graph_store_roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_graph_store_roundtrip(tmp_path) -> None:
    """Write nodes+edges, get_neighbours returns expected, edge_count matches written count."""
    from archon_search.graph_store import GraphStore

    col = "myproject"
    node_a = _node("ServiceA", col)
    node_b = _node("ServiceB", col)
    edge_ab = _edge(node_a, node_b)

    async def _run() -> tuple[int, int, list[GraphNode]]:
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(col)
            await gs.write_graph(col, [node_a, node_b], [edge_ab])
            count = await gs.edge_count(col)
            ncount = await gs.node_count(col)
            neighbours = await gs.get_neighbours(col, [node_a.id])
            return count, ncount, neighbours
        finally:
            await gs.disconnect()

    count, ncount, neighbours = asyncio.run(_run())

    assert count == 1
    assert ncount == 2
    assert len(neighbours) == 1
    assert neighbours[0].entity_name == node_b.entity_name


# ---------------------------------------------------------------------------
# test_graph_table_names_use_archon_prefix
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_graph_table_names_use_archon_prefix(tmp_path) -> None:
    """Graph table names start with _archon_; list_collections() excludes them."""
    import lancedb

    from archon_search.graph_store import GraphStore
    from archon_search.store import SearchStore

    col = "myproject"

    async def _run() -> tuple[list[str], list[str]]:
        db_path = str(tmp_path / "db")
        gs = GraphStore(db_path)
        await gs.connect()
        try:
            await gs.ensure_graph_tables(col)
            # Get the raw LanceDB table names
            db = await lancedb.connect_async(db_path)
            raw_tables = await db.table_names()
        finally:
            await gs.disconnect()

        # Now check via SearchStore.list_collections (should see 0 user collections)
        store = SearchStore(db_path)
        await store.connect()
        try:
            collections = await store.list_collections()
        finally:
            await store.disconnect()

        return raw_tables, [c.name for c in collections]

    raw_tables, user_collections = asyncio.run(_run())

    # Both graph tables must start with _archon_
    graph_tables = [t for t in raw_tables if "graph" in t]
    assert len(graph_tables) == 2
    for t in graph_tables:
        assert t.startswith("_archon_"), f"Expected _archon_ prefix but got: {t}"

    # No _archon_graph_* tables should appear in user-visible collections
    for col_name in user_collections:
        assert "graph" not in col_name, f"Graph table leaked into user collections: {col_name}"


# ---------------------------------------------------------------------------
# test_reingest_same_document_edges_not_duplicated
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_reingest_same_document_edges_not_duplicated(tmp_path) -> None:
    """Ingesting the same document twice must not increase the edge count."""
    from archon_search.graph_store import GraphStore

    col = "myproject"
    node_a = _node("Alpha", col)
    node_b = _node("Beta", col)
    edge_ab = _edge(node_a, node_b)

    async def _run() -> tuple[int, int]:
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(col)
            # First ingest
            await gs.write_graph(col, [node_a, node_b], [edge_ab])
            count_after_first = await gs.edge_count(col)
            # Second ingest — same nodes + same edge (simulating re-ingest)
            await gs.write_graph(col, [node_a, node_b], [edge_ab])
            count_after_second = await gs.edge_count(col)
            return count_after_first, count_after_second
        finally:
            await gs.disconnect()

    first, second = asyncio.run(_run())

    assert first == 1
    assert second == 1, (
        f"Expected edge count to remain 1 after re-ingest, got {second}"
    )


# ---------------------------------------------------------------------------
# test_graph_tables_preserve_edges_after_document_deletion
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_graph_tables_preserve_edges_after_document_deletion(tmp_path) -> None:
    """Graph tables are NOT pruned when a search store document is deleted (E1a behaviour)."""
    import hashlib
    from datetime import datetime, timezone

    from archon_search._types import ChunkRecord, normalize_iso_utc
    from archon_search.graph_store import GraphStore
    from archon_search.store import SearchStore

    col = "myproject"
    source_path = "/data/doc1.txt"
    doc_id = hashlib.sha256(source_path.encode()).hexdigest()

    node_a = _node("Gamma", col, doc_id=doc_id)
    node_b = _node("Delta", col, doc_id=doc_id)
    edge_ab = _edge(node_a, node_b, doc_id=doc_id)

    async def _run() -> int:
        db_path = str(tmp_path / "db")

        # Set up search store and inject a chunk for doc1
        ss = SearchStore(db_path)
        await ss.connect()
        try:
            await ss.ensure_collection(col, _EMBEDDING_DIM)
            chunk = ChunkRecord(
                doc_id=doc_id,
                chunk_id=f"{doc_id}-000000",
                text="Some text from doc1",
                vector=[0.0] * _EMBEDDING_DIM,
                source_path=source_path,
                indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
                acl=None,
            )
            await ss.ingest_chunks(col, [chunk])
        finally:
            await ss.disconnect()

        # Write graph tables for doc1
        gs = GraphStore(db_path)
        await gs.connect()
        try:
            await gs.ensure_graph_tables(col)
            await gs.write_graph(col, [node_a, node_b], [edge_ab])
            count_before_delete = await gs.edge_count(col)
        finally:
            await gs.disconnect()

        # Delete doc1 from the search store
        ss2 = SearchStore(db_path)
        await ss2.connect()
        try:
            await ss2.delete_by_source_path(col, source_path)
        finally:
            await ss2.disconnect()

        # Graph tables must still contain the edge (not pruned by search store delete)
        gs2 = GraphStore(db_path)
        await gs2.connect()
        try:
            count_after_delete = await gs2.edge_count(col)
        finally:
            await gs2.disconnect()

        return count_before_delete, count_after_delete  # type: ignore[return-value]

    before, after = asyncio.run(_run())

    assert before == 1
    assert after == 1, (
        "Graph tables must NOT be pruned when a document is deleted from the search store "
        f"(E1a behaviour). Expected 1 edge after delete, got {after}"
    )


# ---------------------------------------------------------------------------
# test_graph_store_find_nodes_by_name_real_lancedb
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_graph_store_find_nodes_by_name_real_lancedb(tmp_path) -> None:
    """find_nodes_by_name uses lower() SQL against real LanceDB; verifies DataFusion supports it."""
    from archon_search.graph_store import GraphStore

    col = "searchcol"
    node = _node("AuthService", col, entity_type=EntityType.system)

    async def _run() -> tuple:
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(col)
            await gs.write_graph(col, [node], [])
            # Match with lowercase query
            results_lower = await gs.find_nodes_by_name(col, ["authservice"])
            # Match with uppercase query
            results_upper = await gs.find_nodes_by_name(col, ["AUTHSERVICE"])
            # No match
            results_miss = await gs.find_nodes_by_name(col, ["doesnotexist"])
            return results_lower, results_upper, results_miss
        finally:
            await gs.disconnect()

    lower_results, upper_results, miss_results = asyncio.run(_run())
    assert len(lower_results) == 1
    assert lower_results[0].entity_name == "AuthService"
    assert len(upper_results) == 1
    assert upper_results[0].entity_name == "AuthService"
    assert miss_results == []


@pytest.mark.integration
def test_graph_store_entity_subtype_none_roundtrip(tmp_path) -> None:
    """entity_subtype=None written to LanceDB must read back as None, not the string 'None'."""
    from archon_search.graph_store import GraphStore

    col = "nullcol"
    node_with_none = _node("ServiceA", col)  # entity_subtype defaults to None
    node_with_subtype = GraphNode(
        id=make_stable_entity_id("code_symbol", "process"),
        entity_name="process",
        entity_type=EntityType.code_symbol,
        source_doc_id="doc-a",
        collection_name=col,
        entity_subtype="method",
    )

    async def _run():
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(col)
            await gs.write_graph(col, [node_with_none, node_with_subtype], [])
            results = await gs.find_nodes_by_name(col, ["servicea", "process"])
            return results
        finally:
            await gs.disconnect()

    results = asyncio.run(_run())
    by_name = {r.entity_name: r for r in results}
    assert "ServiceA" in by_name
    assert by_name["ServiceA"].entity_subtype is None, (
        f"Expected entity_subtype=None, got {by_name['ServiceA'].entity_subtype!r}"
    )
    assert "process" in by_name
    assert by_name["process"].entity_subtype == "method"

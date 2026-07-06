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
            await gs.ensure_graph_tables(col, ns="default")
            await gs.write_graph(col, [node_a, node_b], [edge_ab], ns="default")
            count = await gs.edge_count(col, ns="default")
            ncount = await gs.node_count(col, ns="default")
            neighbours = await gs.get_neighbours(col, [node_a.id], ns="default")
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
            await gs.ensure_graph_tables(col, ns="default")
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

    # Graph tables (nodes, edges, mentions) must all start with _archon_
    graph_tables = [t for t in raw_tables if "graph" in t]
    assert len(graph_tables) == 3  # nodes, edges, mentions (E2b)
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
            await gs.ensure_graph_tables(col, ns="default")
            # First ingest
            await gs.write_graph(col, [node_a, node_b], [edge_ab], ns="default")
            count_after_first = await gs.edge_count(col, ns="default")
            # Second ingest — same nodes + same edge (simulating re-ingest)
            await gs.write_graph(col, [node_a, node_b], [edge_ab], ns="default")
            count_after_second = await gs.edge_count(col, ns="default")
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
            await gs.ensure_graph_tables(col, ns="default")
            await gs.write_graph(col, [node_a, node_b], [edge_ab], ns="default")
            count_before_delete = await gs.edge_count(col, ns="default")
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
            count_after_delete = await gs2.edge_count(col, ns="default")
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
            await gs.ensure_graph_tables(col, ns="default")
            await gs.write_graph(col, [node], [], ns="default")
            # Match with lowercase query
            results_lower = await gs.find_nodes_by_name(col, ["authservice"], ns="default")
            # Match with uppercase query
            results_upper = await gs.find_nodes_by_name(col, ["AUTHSERVICE"], ns="default")
            # No match
            results_miss = await gs.find_nodes_by_name(col, ["doesnotexist"], ns="default")
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
            await gs.ensure_graph_tables(col, ns="default")
            await gs.write_graph(col, [node_with_none, node_with_subtype], [], ns="default")
            results = await gs.find_nodes_by_name(col, ["servicea", "process"], ns="default")
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


# ---------------------------------------------------------------------------
# Mentions table (E2b — BE-3 integration tests)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_mentions_write_and_read_roundtrip(tmp_path) -> None:
    """Write 3 mentions, get_all returns 3; with real LanceDB in tmp_path."""
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import GraphMention

    col = "testcol"

    async def _run():
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(col, ns="default")
            # Write 3 mentions
            mentions = [
                GraphMention(entity_id="entity-a", chunk_id="chunk-1", doc_id="doc-1"),
                GraphMention(entity_id="entity-b", chunk_id="chunk-2", doc_id="doc-1"),
                GraphMention(entity_id="entity-c", chunk_id="chunk-3", doc_id="doc-1"),
            ]
            await gs.write_mentions(col, mentions, ns="default")
            # Read all mentions
            result = await gs.get_all_mentions(col, ns="default")
            return result
        finally:
            await gs.disconnect()

    result = asyncio.run(_run())
    assert len(result) == 3
    # Verify the mentions are correctly stored (order may vary)
    entity_ids = {m.entity_id for m in result}
    assert entity_ids == {"entity-a", "entity-b", "entity-c"}
    chunk_ids = {m.chunk_id for m in result}
    assert chunk_ids == {"chunk-1", "chunk-2", "chunk-3"}


@pytest.mark.integration
def test_mentions_delete_by_doc_then_write_is_idempotent(tmp_path) -> None:
    """Write, delete by doc_id, re-write; get_all returns same count not doubled."""
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import GraphMention

    col = "testcol"
    doc_id = "doc-123"

    async def _run():
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(col, ns="default")
            # First write
            mentions_1 = [
                GraphMention(entity_id="entity-x", chunk_id="chunk-x1", doc_id=doc_id),
                GraphMention(entity_id="entity-y", chunk_id="chunk-x2", doc_id=doc_id),
            ]
            await gs.write_mentions(col, mentions_1, ns="default")
            count_1 = len(await gs.get_all_mentions(col, ns="default"))

            # Delete by doc_id
            await gs.delete_mentions_by_doc(col, doc_id, ns="default")
            count_after_delete = len(await gs.get_all_mentions(col, ns="default"))

            # Re-write the same mentions
            await gs.write_mentions(col, mentions_1, ns="default")
            count_2 = len(await gs.get_all_mentions(col, ns="default"))

            return count_1, count_after_delete, count_2
        finally:
            await gs.disconnect()

    count_1, count_after_delete, count_2 = asyncio.run(_run())
    assert count_1 == 2
    assert count_after_delete == 0
    assert count_2 == 2, f"Expected 2 after re-write, got {count_2} (idempotency violated)"


# ---------------------------------------------------------------------------
# BE-2 — name_embedding round-trip integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_write_graph_stores_and_retrieves_name_embedding(tmp_path) -> None:
    """A GraphNode with a non-null embedding survives write_graph → get_all_nodes round-trip."""
    from archon_search.graph_store import GraphStore

    col = "embcol"
    embedding = [0.1, 0.2, 0.3, 0.4]
    node_with_emb = GraphNode(
        id=make_stable_entity_id("concept", "EmbeddedEntity"),
        entity_name="EmbeddedEntity",
        entity_type=EntityType.concept,
        source_doc_id="doc-emb",
        collection_name=col,
        name_embedding=embedding,
    )

    async def _run():
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(col, ns="default")
            await gs.write_graph(col, [node_with_emb], [], ns="default")
            nodes = await gs.get_all_nodes(col, ns="default")
            return nodes
        finally:
            await gs.disconnect()

    nodes = asyncio.run(_run())
    assert len(nodes) == 1
    assert nodes[0].entity_name == "EmbeddedEntity"
    assert nodes[0].name_embedding is not None, "name_embedding must be stored and retrieved"
    assert list(nodes[0].name_embedding) == pytest.approx(embedding, abs=1e-5), (
        f"Retrieved embedding {nodes[0].name_embedding} must match stored {embedding}"
    )


@pytest.mark.integration
def test_write_graph_preserves_existing_name_embedding_on_node_update(tmp_path) -> None:
    """Write a node with embedding, then write it again without embedding; original embedding preserved."""
    from archon_search.graph_store import GraphStore

    col = "preservecol"
    embedding = [0.5, 0.6, 0.7, 0.8]
    node_id = make_stable_entity_id("concept", "StableEntity")

    node_with_emb = GraphNode(
        id=node_id,
        entity_name="StableEntity",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name=col,
        name_embedding=embedding,
    )
    node_without_emb = GraphNode(
        id=node_id,
        entity_name="StableEntity",
        entity_type=EntityType.concept,
        source_doc_id="doc-2",  # different source doc (re-ingest)
        collection_name=col,
        name_embedding=None,   # no embedding in second write
    )

    async def _run():
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(col, ns="default")
            # First write: node with embedding
            await gs.write_graph(col, [node_with_emb], [], ns="default")
            # Second write: same node ID, no embedding — must NOT overwrite existing embedding
            await gs.write_graph(col, [node_without_emb], [], ns="default")
            nodes = await gs.get_all_nodes(col, ns="default")
            return nodes
        finally:
            await gs.disconnect()

    nodes = asyncio.run(_run())
    assert len(nodes) == 1
    assert nodes[0].name_embedding is not None, (
        "Existing name_embedding must be preserved when a subsequent write supplies name_embedding=None"
    )
    assert list(nodes[0].name_embedding) == pytest.approx(embedding, abs=1e-5), (
        f"Preserved embedding {nodes[0].name_embedding} must match original {embedding}"
    )

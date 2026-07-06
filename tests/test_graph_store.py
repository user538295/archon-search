"""Unit tests for GraphStore — E1a BE-3.

Tests verify:
- Table creation is idempotent
- Node upsert by stable ID prevents duplicates
- Edge upsert by stable ID prevents duplicates
- get_neighbours returns first-degree neighbours only
- edge_count returns 0 for empty / non-existent collection
- Invalid collection names raise ValueError before table creation
- No f-string SQL in graph_store.py
- find_nodes_by_name is case-insensitive
- find_nodes_by_name handles multi-word names
- check_and_warn_legacy_graph_tables warns on old-pattern tables (BE-1b)
- check_and_warn_legacy_graph_tables is silent when no legacy tables exist (BE-1b)
- check_and_warn_legacy_graph_tables returns [] and warns when list_tables raises (BE-1b)
- check_and_warn_legacy_graph_tables handles empty table list (BE-1b)
- check_and_warn_legacy_graph_tables does not flag ambiguous collection names (BE-1b)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

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


def _node(name: str, entity_type: EntityType = EntityType.concept) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(entity_type.value, name),
        entity_name=name,
        entity_type=entity_type,
        source_doc_id="doc-abc",
        collection_name="test-col",
    )


def _edge(src: GraphNode, tgt: GraphNode) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(src.id, tgt.id, RelationshipType.uses.value),
        source_node_id=src.id,
        target_node_id=tgt.id,
        relationship_type=RelationshipType.uses,
        source_doc_id="doc-abc",
    )


# ---------------------------------------------------------------------------
# Collection name validation
# ---------------------------------------------------------------------------


def test_graph_store_rejects_invalid_collection_name() -> None:
    """GraphStore must raise ValueError for invalid collection names before any DB call."""
    import asyncio

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db")

    async def _run() -> None:
        # Names starting with underscore, or with spaces — should both fail.
        with pytest.raises(ValueError, match="Invalid collection name"):
            await store.ensure_graph_tables("_bad_name", ns="default")

        with pytest.raises(ValueError, match="Invalid collection name"):
            await store.ensure_graph_tables("has space", ns="default")

        with pytest.raises(ValueError, match="Invalid collection name"):
            await store.ensure_graph_tables("", ns="default")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Idempotent table creation (unit — no real LanceDB)
# ---------------------------------------------------------------------------


def test_ensure_graph_tables_idempotent() -> None:
    """Calling ensure_graph_tables twice must not raise."""
    import asyncio

    from archon_search.graph_store import GraphStore

    # Use a real temp-like path; table creation is mocked.
    store = GraphStore("/tmp/fake-db-idem")

    mock_db = AsyncMock()
    mock_db.create_table = AsyncMock(return_value=AsyncMock())

    async def _run() -> None:
        store._db = mock_db
        await store.ensure_graph_tables("mycol", ns="default")
        await store.ensure_graph_tables("mycol", ns="default")
        # create_table called twice (once per table per call = 6 total for 3 tables: nodes, edges, mentions)
        assert mock_db.create_table.call_count == 6

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Upsert — nodes
# ---------------------------------------------------------------------------


def _make_empty_nodes_arrow_for_preservation():
    """Return an empty PyArrow table with id + name_embedding columns for the embedding-preservation query."""
    import pyarrow as pa

    return pa.table(
        {"id": [], "name_embedding": pa.array([], type=pa.list_(pa.float32()))},
        schema=pa.schema([pa.field("id", pa.utf8()), pa.field("name_embedding", pa.list_(pa.float32()), nullable=True)]),
    )


def _add_preservation_query_mock(mock_table) -> None:
    """Wire mock_table.query() and schema() for the embedding-preservation pre-read path.

    The pre-read now calls:
      1. ``await nodes_table.schema()`` — returns a schema with name_embedding column
      2. ``nodes_table.query().where().select().to_arrow()`` — returns an empty table
    """
    import pyarrow as pa

    # Mock schema() coroutine to return a schema that includes name_embedding
    pres_schema = pa.schema([
        pa.field("id", pa.utf8()),
        pa.field("name_embedding", pa.list_(pa.float32()), nullable=True),
    ])
    mock_table.schema = AsyncMock(return_value=pres_schema)

    pres_query = MagicMock()
    pres_query.where.return_value = pres_query
    pres_query.select.return_value = pres_query
    pres_query.to_arrow = AsyncMock(return_value=_make_empty_nodes_arrow_for_preservation())
    mock_table.query.return_value = pres_query


def test_write_graph_upserts_by_stable_id() -> None:
    """Re-writing the same node ID must not duplicate rows."""
    import asyncio

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-upsert-nodes")
    node_a = _node("AuthService", EntityType.system)

    # Build mock table that tracks merge_insert calls
    merge_builder = MagicMock()
    merge_builder.when_matched_update_all.return_value = merge_builder
    merge_builder.when_not_matched_insert_all.return_value = merge_builder
    merge_builder.execute = AsyncMock(return_value=None)

    # Use MagicMock for table (merge_insert is synchronous in real lancedb)
    mock_table = MagicMock()
    mock_table.merge_insert.return_value = merge_builder
    # Wire the embedding-preservation query chain (node has no embedding → triggers pre-read)
    _add_preservation_query_mock(mock_table)

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> None:
        store._db = mock_db
        await store.write_graph("test-col", [node_a], [], ns="default")
        await store.write_graph("test-col", [node_a], [], ns="default")
        # merge_insert called twice (once per write_graph call, on the nodes table)
        assert mock_table.merge_insert.call_count == 2
        # execute called twice (once per write_graph call for nodes)
        assert merge_builder.execute.call_count == 2

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Upsert — edges
# ---------------------------------------------------------------------------


def test_write_graph_upserts_edges_by_stable_id() -> None:
    """Writing the same edge twice must not increase the edge count."""
    import asyncio

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-upsert-edges")
    node_a = _node("A", EntityType.concept)
    node_b = _node("B", EntityType.concept)
    edge_ab = _edge(node_a, node_b)

    merge_builder = MagicMock()
    merge_builder.when_matched_update_all.return_value = merge_builder
    merge_builder.when_not_matched_insert_all.return_value = merge_builder
    merge_builder.execute = AsyncMock(return_value=None)

    # Use MagicMock for table (merge_insert is synchronous in real lancedb)
    mock_table = MagicMock()
    mock_table.merge_insert.return_value = merge_builder
    # Wire the embedding-preservation query chain (nodes have no embedding → triggers pre-read)
    _add_preservation_query_mock(mock_table)

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> None:
        store._db = mock_db
        await store.write_graph("test-col", [node_a, node_b], [edge_ab], ns="default")
        await store.write_graph("test-col", [node_a, node_b], [edge_ab], ns="default")
        # merge_insert is called for both nodes and edges tables — 4 calls total (2 per write)
        assert mock_table.merge_insert.call_count == 4

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# get_neighbours
# ---------------------------------------------------------------------------


def test_get_neighbours_returns_first_degree() -> None:
    """get_neighbours must return only direct neighbours of the given entity IDs."""
    import asyncio

    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-neighbours")

    node_a = _node("A", EntityType.concept)
    node_b = _node("B", EntityType.concept)
    node_c = _node("C", EntityType.concept)

    # Edge A→B (node_a is source, node_b is target)
    edge_ab = _edge(node_a, node_b)

    # Build arrow tables representing graph tables
    edge_schema = pa.schema([
        pa.field("id", pa.utf8()),
        pa.field("source_node_id", pa.utf8()),
        pa.field("target_node_id", pa.utf8()),
        pa.field("relationship_type", pa.utf8()),
        pa.field("source_doc_id", pa.utf8()),
    ])
    edges_arrow = pa.table(
        {
            "id": [edge_ab.id],
            "source_node_id": [edge_ab.source_node_id],
            "target_node_id": [edge_ab.target_node_id],
            "relationship_type": [edge_ab.relationship_type.value],
            "source_doc_id": [edge_ab.source_doc_id],
        },
        schema=edge_schema,
    )

    node_schema = pa.schema([
        pa.field("id", pa.utf8()),
        pa.field("entity_name", pa.utf8()),
        pa.field("entity_type", pa.utf8()),
        pa.field("source_doc_id", pa.utf8()),
        pa.field("collection_name", pa.utf8()),
        pa.field("entity_subtype", pa.utf8()),
    ])
    nodes_arrow = pa.table(
        {
            "id": [node_b.id],
            "entity_name": [node_b.entity_name],
            "entity_type": [node_b.entity_type.value],
            "source_doc_id": [node_b.source_doc_id],
            "collection_name": [node_b.collection_name],
            "entity_subtype": [None],
        },
        schema=node_schema,
    )

    # query() and where() are synchronous in real lancedb; only to_arrow() is async
    edges_query = MagicMock()
    edges_query.where.return_value = edges_query
    edges_query.to_arrow = AsyncMock(return_value=edges_arrow)

    nodes_query = MagicMock()
    nodes_query.where.return_value = nodes_query
    nodes_query.to_arrow = AsyncMock(return_value=nodes_arrow)

    edges_table = MagicMock()
    edges_table.query.return_value = edges_query

    nodes_table = MagicMock()
    nodes_table.query.return_value = nodes_query

    async def _open_table(name: str):
        if name.endswith("_edges"):
            return edges_table
        return nodes_table

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=_open_table)

    async def _run() -> list:
        store._db = mock_db
        return await store.get_neighbours("test-col", [node_a.id], ns="default")

    result = asyncio.run(_run())
    assert len(result) == 1
    assert result[0].entity_name == node_b.entity_name
    # node_c should NOT be in neighbours (no edge)


# ---------------------------------------------------------------------------
# edge_count
# ---------------------------------------------------------------------------


def test_edge_count_zero_before_ingest() -> None:
    """edge_count returns 0 when the edges table doesn't exist."""
    import asyncio

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-edge-count")

    mock_db = AsyncMock()
    # Simulate table-not-found: open_table raises FileNotFoundError (as LanceDB does)
    mock_db.open_table = AsyncMock(side_effect=FileNotFoundError("Table not found"))

    async def _run() -> int:
        store._db = mock_db
        return await store.edge_count("empty-col", ns="default")

    count = asyncio.run(_run())
    assert count == 0


# ---------------------------------------------------------------------------
# find_nodes_by_name — case-insensitive
# ---------------------------------------------------------------------------


def test_find_nodes_by_name_case_insensitive() -> None:
    """find_nodes_by_name must match regardless of case; unknown names return []."""
    import asyncio

    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-byname")

    node_schema = pa.schema([
        pa.field("id", pa.utf8()),
        pa.field("entity_name", pa.utf8()),
        pa.field("entity_type", pa.utf8()),
        pa.field("source_doc_id", pa.utf8()),
        pa.field("collection_name", pa.utf8()),
        pa.field("entity_subtype", pa.utf8()),
    ])
    nodes_arrow = pa.table(
        {
            "id": ["abc123"],
            "entity_name": ["AuthService"],
            "entity_type": [EntityType.system.value],
            "source_doc_id": ["doc-1"],
            "collection_name": ["col1"],
            "entity_subtype": [None],
        },
        schema=node_schema,
    )
    empty_arrow = pa.table(
        {
            "id": [],
            "entity_name": [],
            "entity_type": [],
            "source_doc_id": [],
            "collection_name": [],
            "entity_subtype": [],
        },
        schema=node_schema,
    )

    def _make_query(result: pa.Table) -> MagicMock:
        # query() and where() are synchronous; only to_arrow() is async
        q = MagicMock()
        q.where.return_value = q
        q.to_arrow = AsyncMock(return_value=result)
        return q

    mock_table_hit = MagicMock()
    mock_table_hit.query.return_value = _make_query(nodes_arrow)

    mock_table_miss = MagicMock()
    mock_table_miss.query.return_value = _make_query(empty_arrow)

    mock_db_hit = MagicMock()
    mock_db_hit.open_table = AsyncMock(return_value=mock_table_hit)

    mock_db_miss = MagicMock()
    mock_db_miss.open_table = AsyncMock(return_value=mock_table_miss)

    async def _run_hit() -> list:
        store._db = mock_db_hit
        return await store.find_nodes_by_name("col1", ["authservice"], ns="default")

    async def _run_miss() -> list:
        store._db = mock_db_miss
        return await store.find_nodes_by_name("col1", ["unknown_name"], ns="default")

    hit_results = asyncio.run(_run_hit())
    assert len(hit_results) == 1
    assert hit_results[0].entity_name == "AuthService"

    miss_results = asyncio.run(_run_miss())
    assert miss_results == []


def test_find_nodes_by_name_multi_word() -> None:
    """find_nodes_by_name with 'token validator' must match 'Token Validator' node."""
    import asyncio

    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-byname-multi")

    node_schema = pa.schema([
        pa.field("id", pa.utf8()),
        pa.field("entity_name", pa.utf8()),
        pa.field("entity_type", pa.utf8()),
        pa.field("source_doc_id", pa.utf8()),
        pa.field("collection_name", pa.utf8()),
        pa.field("entity_subtype", pa.utf8()),
    ])
    nodes_arrow = pa.table(
        {
            "id": ["def456"],
            "entity_name": ["Token Validator"],
            "entity_type": [EntityType.system.value],
            "source_doc_id": ["doc-2"],
            "collection_name": ["col1"],
            "entity_subtype": [None],
        },
        schema=node_schema,
    )

    # query() and where() are synchronous in real lancedb; only to_arrow() is async
    query = MagicMock()
    query.where.return_value = query
    query.to_arrow = AsyncMock(return_value=nodes_arrow)

    mock_table = MagicMock()
    mock_table.query.return_value = query

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> list:
        store._db = mock_db
        return await store.find_nodes_by_name("col1", ["token validator"], ns="default")

    results = asyncio.run(_run())
    assert len(results) == 1
    assert results[0].entity_name == "Token Validator"


# ---------------------------------------------------------------------------
# node_count
# ---------------------------------------------------------------------------


def test_node_count_zero_before_ingest() -> None:
    """node_count returns 0 when the nodes table doesn't exist."""
    import asyncio

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-node-count")

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=FileNotFoundError("Table not found"))

    async def _run() -> int:
        store._db = mock_db
        return await store.node_count("empty-col", ns="default")

    count = asyncio.run(_run())
    assert count == 0


# ---------------------------------------------------------------------------
# get_neighbours — edge cases
# ---------------------------------------------------------------------------


def test_get_neighbours_empty_entity_ids() -> None:
    """get_neighbours with empty entity_ids returns [] immediately without any DB call."""
    import asyncio

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-empty-ids")
    mock_db = AsyncMock()

    async def _run() -> list:
        store._db = mock_db
        return await store.get_neighbours("test-col", [], ns="default")

    result = asyncio.run(_run())
    assert result == []
    mock_db.open_table.assert_not_called()


def test_find_nodes_by_name_empty_names() -> None:
    """find_nodes_by_name with empty names list returns [] immediately without any DB call."""
    import asyncio

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-empty-names")
    mock_db = AsyncMock()

    async def _run() -> list:
        store._db = mock_db
        return await store.find_nodes_by_name("test-col", [], ns="default")

    result = asyncio.run(_run())
    assert result == []
    mock_db.open_table.assert_not_called()


def test_get_neighbours_multiple_entity_ids() -> None:
    """get_neighbours with multiple entity_ids deduplicates shared neighbours."""
    import asyncio

    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-multi-ids")
    col = "test-col"

    # A→B and C→B: querying [A, C] should return [B] once, not twice
    node_a = _node("A", EntityType.concept)
    node_b = _node("B", EntityType.concept)
    node_c = _node("C", EntityType.concept)
    edge_ab = _edge(node_a, node_b)

    # Rebuild edge_cb with node_c as source
    edge_cb = GraphEdge(
        id=make_stable_edge_id(node_c.id, node_b.id, RelationshipType.uses.value),
        source_node_id=node_c.id,
        target_node_id=node_b.id,
        relationship_type=RelationshipType.uses,
        source_doc_id="doc-abc",
    )

    edge_schema = pa.schema([
        pa.field("id", pa.utf8()),
        pa.field("source_node_id", pa.utf8()),
        pa.field("target_node_id", pa.utf8()),
        pa.field("relationship_type", pa.utf8()),
        pa.field("source_doc_id", pa.utf8()),
    ])
    edges_arrow = pa.table(
        {
            "id": [edge_ab.id, edge_cb.id],
            "source_node_id": [edge_ab.source_node_id, edge_cb.source_node_id],
            "target_node_id": [edge_ab.target_node_id, edge_cb.target_node_id],
            "relationship_type": [edge_ab.relationship_type.value, edge_cb.relationship_type.value],
            "source_doc_id": [edge_ab.source_doc_id, edge_cb.source_doc_id],
        },
        schema=edge_schema,
    )

    node_schema = pa.schema([
        pa.field("id", pa.utf8()),
        pa.field("entity_name", pa.utf8()),
        pa.field("entity_type", pa.utf8()),
        pa.field("source_doc_id", pa.utf8()),
        pa.field("collection_name", pa.utf8()),
        pa.field("entity_subtype", pa.utf8()),
    ])
    # Only B should appear as a neighbour (A and C are the query entities)
    nodes_arrow = pa.table(
        {
            "id": [node_b.id],
            "entity_name": [node_b.entity_name],
            "entity_type": [node_b.entity_type.value],
            "source_doc_id": [node_b.source_doc_id],
            "collection_name": [node_b.collection_name],
            "entity_subtype": [None],
        },
        schema=node_schema,
    )

    edges_query = MagicMock()
    edges_query.where.return_value = edges_query
    edges_query.to_arrow = AsyncMock(return_value=edges_arrow)

    nodes_query = MagicMock()
    nodes_query.where.return_value = nodes_query
    nodes_query.to_arrow = AsyncMock(return_value=nodes_arrow)

    edges_table = MagicMock()
    edges_table.query.return_value = edges_query

    nodes_table = MagicMock()
    nodes_table.query.return_value = nodes_query

    async def _open_table(name: str):
        if name.endswith("_edges"):
            return edges_table
        return nodes_table

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=_open_table)

    async def _run() -> list:
        store._db = mock_db
        return await store.get_neighbours(col, [node_a.id, node_c.id], ns="default")

    result = asyncio.run(_run())
    assert len(result) == 1, f"Expected 1 neighbour (B), got {len(result)}: {[r.entity_name for r in result]}"
    assert result[0].entity_name == node_b.entity_name


# ---------------------------------------------------------------------------
# get_edges_for_nodes
# ---------------------------------------------------------------------------


def test_get_edges_for_nodes_empty_entity_ids() -> None:
    """get_edges_for_nodes with empty entity_ids returns [] immediately without any DB call."""
    import asyncio

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-edges-empty")
    mock_db = AsyncMock()

    async def _run() -> list:
        store._db = mock_db
        return await store.get_edges_for_nodes("test-col", [], ns="default")

    result = asyncio.run(_run())
    assert result == []
    mock_db.open_table.assert_not_called()


def _build_edges_table_mock(edges: list) -> MagicMock:
    """Build a mock edges table returning *edges* as an Arrow table."""
    import pyarrow as pa

    edge_schema = pa.schema([
        pa.field("id", pa.utf8()),
        pa.field("source_node_id", pa.utf8()),
        pa.field("target_node_id", pa.utf8()),
        pa.field("relationship_type", pa.utf8()),
        pa.field("source_doc_id", pa.utf8()),
    ])
    arrow = pa.table(
        {
            "id": [e.id for e in edges],
            "source_node_id": [e.source_node_id for e in edges],
            "target_node_id": [e.target_node_id for e in edges],
            "relationship_type": [e.relationship_type.value for e in edges],
            "source_doc_id": [e.source_doc_id for e in edges],
        },
        schema=edge_schema,
    ) if edges else pa.table(
        {"id": [], "source_node_id": [], "target_node_id": [], "relationship_type": [], "source_doc_id": []},
        schema=edge_schema,
    )
    query = MagicMock()
    query.where.return_value = query
    query.to_arrow = AsyncMock(return_value=arrow)
    table = MagicMock()
    table.query.return_value = query
    return table


def test_get_edges_for_nodes_entity_as_source() -> None:
    """get_edges_for_nodes returns edges where the queried entity is the source."""
    import asyncio

    from archon_search.graph_store import GraphStore

    node_a = _node("A", EntityType.concept)
    node_b = _node("B", EntityType.concept)
    edge_ab = _edge(node_a, node_b)

    store = GraphStore("/tmp/fake-db-edges-source")
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=_build_edges_table_mock([edge_ab]))

    async def _run() -> list:
        store._db = mock_db
        return await store.get_edges_for_nodes("test-col", [node_a.id], ns="default")

    result = asyncio.run(_run())
    assert len(result) == 1
    assert result[0].id == edge_ab.id
    assert result[0].source_node_id == node_a.id
    assert result[0].target_node_id == node_b.id


def test_get_edges_for_nodes_entity_as_target() -> None:
    """get_edges_for_nodes returns edges where the queried entity is the target."""
    import asyncio

    from archon_search.graph_store import GraphStore

    node_a = _node("A", EntityType.concept)
    node_b = _node("B", EntityType.concept)
    edge_ab = _edge(node_a, node_b)

    store = GraphStore("/tmp/fake-db-edges-target")
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=_build_edges_table_mock([edge_ab]))

    async def _run() -> list:
        store._db = mock_db
        # Query by target node (B) — should still return the edge A→B
        return await store.get_edges_for_nodes("test-col", [node_b.id], ns="default")

    result = asyncio.run(_run())
    assert len(result) == 1
    assert result[0].id == edge_ab.id


def test_get_edges_for_nodes_entity_as_both_source_and_target() -> None:
    """get_edges_for_nodes returns edges in both directions for the same entity."""
    import asyncio

    from archon_search.graph_store import GraphStore

    node_a = _node("A", EntityType.concept)
    node_b = _node("B", EntityType.concept)
    node_c = _node("C", EntityType.concept)
    edge_ab = _edge(node_a, node_b)
    # Edge from C to A (A is target here)
    edge_ca = GraphEdge(
        id=make_stable_edge_id(node_c.id, node_a.id, RelationshipType.uses.value),
        source_node_id=node_c.id,
        target_node_id=node_a.id,
        relationship_type=RelationshipType.uses,
        source_doc_id="doc-abc",
    )

    store = GraphStore("/tmp/fake-db-edges-bidirectional")
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=_build_edges_table_mock([edge_ab, edge_ca]))

    async def _run() -> list:
        store._db = mock_db
        # A appears as source in A→B and as target in C→A
        return await store.get_edges_for_nodes("test-col", [node_a.id], ns="default")

    result = asyncio.run(_run())
    assert len(result) == 2
    returned_ids = {e.id for e in result}
    assert edge_ab.id in returned_ids
    assert edge_ca.id in returned_ids


def test_get_edges_for_nodes_no_matching_edges() -> None:
    """get_edges_for_nodes returns [] when no edges match the given entity_ids."""
    import asyncio

    from archon_search.graph_store import GraphStore

    node_x = _node("X", EntityType.concept)

    store = GraphStore("/tmp/fake-db-edges-nomatch")
    mock_db = AsyncMock()
    # Edges table returns empty result
    mock_db.open_table = AsyncMock(return_value=_build_edges_table_mock([]))

    async def _run() -> list:
        store._db = mock_db
        return await store.get_edges_for_nodes("test-col", [node_x.id], ns="default")

    result = asyncio.run(_run())
    assert result == []


# ---------------------------------------------------------------------------
# Mentions table (E2b — BE-3 unit tests)
# ---------------------------------------------------------------------------


def test_mentions_table_name_format() -> None:
    """Mentions table name follows _archon_graph_{ns}__{col}_mentions pattern."""
    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db")
    name = store._mentions_table_name("test-collection", "default")
    assert name == "_archon_graph_default__test-collection_mentions"


def test_mentions_schema_columns() -> None:
    """Mentions schema has entity_id, chunk_id, doc_id all as utf8."""
    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    schema = GraphStore._mentions_schema()
    assert isinstance(schema, pa.Schema)
    field_names = [f.name for f in schema]
    assert "entity_id" in field_names
    assert "chunk_id" in field_names
    assert "doc_id" in field_names
    # All fields must be utf8
    for f in schema:
        if f.name in ["entity_id", "chunk_id", "doc_id"]:
            assert f.type == pa.utf8(), f"Expected utf8 for {f.name}, got {f.type}"


def test_delete_mentions_uses_safe_predicate() -> None:
    """delete_mentions_by_doc uses _where_eq, never f-strings."""
    import asyncio

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-delete-mentions")

    # Mock table that tracks delete() call
    mock_table = AsyncMock()
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> str:
        store._db = mock_db
        await store.delete_mentions_by_doc("test-col", "doc-123", ns="default")
        # Capture the predicate passed to delete()
        return mock_table.delete.call_args[0][0]

    predicate = asyncio.run(_run())
    # Must use _where_eq pattern: doc_id = 'doc-123' (SQL-safe quoting)
    assert "doc_id" in predicate
    assert "=" in predicate
    # Must NOT contain an f-string with unquoted doc_id
    assert "{" not in predicate
    assert "f\"" not in predicate


# ---------------------------------------------------------------------------
# get_entity_presence_across_collections (BE-1)
# ---------------------------------------------------------------------------


def _build_nodes_arrow(nodes: list[GraphNode]):  # type: ignore[return]
    """Build a PyArrow table of node rows for use in mocks."""
    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    # Use the production schema rather than duplicating it — prevents silent drift
    # if _nodes_schema() gains or renames columns. See GraphStore._nodes_schema().
    node_schema = GraphStore._nodes_schema()
    if not nodes:
        return pa.table(
            {
                "id": [],
                "entity_name": [],
                "entity_type": [],
                "source_doc_id": [],
                "collection_name": [],
                "entity_subtype": [],
                "name_embedding": pa.array([], type=pa.list_(pa.float32())),
            },
            schema=node_schema,
        )
    return pa.table(
        {
            "id": [n.id for n in nodes],
            "entity_name": [n.entity_name for n in nodes],
            "entity_type": [n.entity_type.value for n in nodes],
            "source_doc_id": [n.source_doc_id for n in nodes],
            "collection_name": [n.collection_name for n in nodes],
            "entity_subtype": [n.entity_subtype for n in nodes],
            "name_embedding": pa.array(
                [n.name_embedding for n in nodes], type=pa.list_(pa.float32())
            ),
        },
        schema=node_schema,
    )


def _make_nodes_table_mock(nodes: list[GraphNode]) -> MagicMock:
    """Build a mock LanceDB table that returns *nodes* from query().to_arrow()."""
    arrow = _build_nodes_arrow(nodes)
    query = MagicMock()
    query.select.return_value = query  # support .select([...]) chaining
    query.to_arrow = AsyncMock(return_value=arrow)
    table = MagicMock()
    table.query.return_value = query
    return table


def test_get_entity_presence_across_collections_basic() -> None:
    """Entity in 2 of 3 collections → count=2; unique entity → count=1.

    - node_alpha appears in col1 and col2 → presence count = 2
    - node_beta appears only in col3 → presence count = 1
    """
    import asyncio

    from archon_search.graph_store import GraphStore

    node_alpha = GraphNode(
        id=make_stable_entity_id("concept", "Alpha"),
        entity_name="Alpha",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="col1",
    )
    node_beta = GraphNode(
        id=make_stable_entity_id("concept", "Beta"),
        entity_name="Beta",
        entity_type=EntityType.concept,
        source_doc_id="doc-3",
        collection_name="col3",
    )

    col1_table = _make_nodes_table_mock([node_alpha])
    col2_table = _make_nodes_table_mock([node_alpha])
    col3_table = _make_nodes_table_mock([node_beta])

    def _table_name_for(col: str) -> str:
        return "_archon_graph_default__" + col + "_nodes"

    async def _open_table(name: str):
        if name == _table_name_for("col1"):
            return col1_table
        if name == _table_name_for("col2"):
            return col2_table
        if name == _table_name_for("col3"):
            return col3_table
        raise FileNotFoundError(f"Table not found: {name}")

    store = GraphStore("/tmp/fake-db-presence-basic")
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=_open_table)

    async def _run() -> dict:
        store._db = mock_db
        return await store.get_entity_presence_across_collections(["col1", "col2", "col3"], ns="default")

    result = asyncio.run(_run())

    assert result[node_alpha.id] == 2, f"Alpha should appear in 2 collections, got {result.get(node_alpha.id)}"
    assert result[node_beta.id] == 1, f"Beta should appear in 1 collection, got {result.get(node_beta.id)}"
    assert len(result) == 2
    # Verify only the id column was fetched (efficiency: no GraphNode construction)
    col1_table.query.return_value.select.assert_called_once_with(["id"])


def test_get_entity_presence_empty_collections() -> None:
    """Empty collection_names list returns {} immediately without any DB call."""
    import asyncio

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-presence-empty")
    mock_db = AsyncMock()

    async def _run() -> dict:
        store._db = mock_db
        return await store.get_entity_presence_across_collections([], ns="default")

    result = asyncio.run(_run())

    assert result == {}
    mock_db.open_table.assert_not_called()


def test_get_entity_presence_absent_table_skipped() -> None:
    """Absent node table contributes 0 to entity counts; no exception is raised.

    - col1 has a node table with node_alpha
    - col2 has no node table (FileNotFoundError)
    - Result: {node_alpha.id: 1} — col2 is silently skipped
    """
    import asyncio

    from archon_search.graph_store import GraphStore

    node_alpha = GraphNode(
        id=make_stable_entity_id("concept", "Alpha"),
        entity_name="Alpha",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="col1",
    )

    col1_table = _make_nodes_table_mock([node_alpha])

    async def _open_table(name: str):
        if name == "_archon_graph_default__col1_nodes":
            return col1_table
        raise FileNotFoundError(f"Table not found: {name}")

    store = GraphStore("/tmp/fake-db-presence-absent")
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=_open_table)

    async def _run() -> dict:
        store._db = mock_db
        return await store.get_entity_presence_across_collections(["col1", "col2"], ns="default")

    result = asyncio.run(_run())

    assert result == {node_alpha.id: 1}, f"Expected only alpha with count=1, got {result}"


def test_get_entity_presence_dedup_within_collection() -> None:
    """Duplicate entity rows in one collection count as ONE collection, not many.

    Setup:
    - col1: node_alpha appears TWICE (two rows with same entity_id) — dedup must apply
    - col2: node_alpha appears ONCE (decoy to make total rows = 3)
    - Correct answer: alpha.count == 2 (distinct collections), not 3 (total rows)

    This test FAILS if the seen_in_collection dedup guard is removed.
    """
    import asyncio

    from archon_search.graph_store import GraphStore

    node_alpha = GraphNode(
        id=make_stable_entity_id("concept", "Alpha"),
        entity_name="Alpha",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="col1",
    )
    # Same entity_id, different source_doc_id — simulates a duplicate row in col1
    node_alpha_dup = GraphNode(
        id=node_alpha.id,  # SAME id — this is the duplicate
        entity_name="Alpha",
        entity_type=EntityType.concept,
        source_doc_id="doc-2",
        collection_name="col1",
    )

    # col1 has 2 rows for the same entity; col2 has 1 row for that entity
    col1_table = _make_nodes_table_mock([node_alpha, node_alpha_dup])
    col2_table = _make_nodes_table_mock([node_alpha])

    async def _open_table(name: str):
        if name == "_archon_graph_default__col1_nodes":
            return col1_table
        if name == "_archon_graph_default__col2_nodes":
            return col2_table
        raise FileNotFoundError(f"Table not found: {name}")

    store = GraphStore("/tmp/fake-db-presence-dedup")
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=_open_table)

    async def _run() -> dict:
        store._db = mock_db
        return await store.get_entity_presence_across_collections(["col1", "col2"], ns="default")

    result = asyncio.run(_run())

    # Total rows across both collections = 3; correct answer = 2 distinct collections
    assert result[node_alpha.id] == 2, (
        f"Alpha should be counted in 2 distinct collections (not 3 total rows), got {result.get(node_alpha.id)}"
    )
    assert len(result) == 1


def test_get_entity_presence_duplicate_collection_names_deduplicated() -> None:
    """Duplicate collection names are deduplicated; entity count is not inflated.

    Setup:
    - collection_names = ["col1", "col1"] — same collection passed twice
    - col1 has node_alpha
    - Correct answer: alpha.count == 1 (one distinct collection), not 2

    This test FAILS if the dict.fromkeys dedup guard is removed.
    """
    import asyncio

    from archon_search.graph_store import GraphStore

    node_alpha = GraphNode(
        id=make_stable_entity_id("concept", "Alpha"),
        entity_name="Alpha",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="col1",
    )

    col1_table = _make_nodes_table_mock([node_alpha])

    async def _open_table(name: str):
        if name == "_archon_graph_default__col1_nodes":
            return col1_table
        raise FileNotFoundError(f"Table not found: {name}")

    store = GraphStore("/tmp/fake-db-presence-dedup-colnames")
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=_open_table)

    async def _run() -> dict:
        store._db = mock_db
        return await store.get_entity_presence_across_collections(["col1", "col1"], ns="default")

    result = asyncio.run(_run())

    assert result[node_alpha.id] == 1, (
        f"Alpha should be counted in 1 distinct collection (not 2 due to duplicate name), "
        f"got {result.get(node_alpha.id)}"
    )
    assert len(result) == 1


def test_get_entity_presence_unreadable_table_skipped() -> None:
    """A RuntimeError from an unreadable table is caught; other collections proceed.

    Setup:
    - col1: valid node table with node_alpha
    - col2: open_table raises RuntimeError("simulated corruption")
    - Expected: result == {node_alpha.id: 1}, no exception raised
    """
    import asyncio

    from archon_search.graph_store import GraphStore

    node_alpha = GraphNode(
        id=make_stable_entity_id("concept", "Alpha"),
        entity_name="Alpha",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="col1",
    )

    col1_table = _make_nodes_table_mock([node_alpha])

    async def _open_table(name: str):
        if name == "_archon_graph_default__col1_nodes":
            return col1_table
        if name == "_archon_graph_default__col2_nodes":
            raise RuntimeError("simulated corruption")
        raise FileNotFoundError(f"Table not found: {name}")

    store = GraphStore("/tmp/fake-db-presence-corrupt")
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=_open_table)

    async def _run() -> dict:
        store._db = mock_db
        return await store.get_entity_presence_across_collections(["col1", "col2"], ns="default")

    result = asyncio.run(_run())

    assert result == {node_alpha.id: 1}, (
        f"col2 corruption should be skipped; expected {{alpha: 1}}, got {result}"
    )


def test_get_entity_presence_store_not_connected_raises() -> None:
    """A disconnected GraphStore raises RuntimeError, not silently returning {}.

    An empty presence map causes all entities to receive df=1 (max IDF boost),
    corrupting TF-IDF salience scores — so a disconnected store must fail loudly.
    """
    import asyncio

    import pytest

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-presence-not-connected")
    # _db is None by default — store is not connected

    async def _run() -> dict:
        return await store.get_entity_presence_across_collections(["col1"], ns="default")

    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(_run())


def test_get_entity_presence_to_arrow_failure_skips_collection() -> None:
    """Block 2 failure: to_arrow raises OSError → col2 skipped with WARNING; col1 counts.

    Setup:
    - col1: valid node table with node_alpha
    - col2: open_table succeeds but to_arrow raises OSError("corrupt page")
    - Expected: result == {node_alpha.id: 1}, WARNING logged for col2
    """
    import asyncio
    import io
    import logging as _logging

    from archon_search.graph_store import GraphStore

    node_alpha = GraphNode(
        id=make_stable_entity_id("concept", "Alpha"),
        entity_name="Alpha",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="col1",
    )

    col1_table = _make_nodes_table_mock([node_alpha])

    bad_query = MagicMock()
    bad_query.select.return_value = bad_query
    bad_query.to_arrow = AsyncMock(side_effect=OSError("corrupt page"))
    col2_table = MagicMock()
    col2_table.query.return_value = bad_query

    async def _open_table(name: str):
        if name == "_archon_graph_default__col1_nodes":
            return col1_table
        if name == "_archon_graph_default__col2_nodes":
            return col2_table
        raise FileNotFoundError(f"Table not found: {name}")

    store = GraphStore("/tmp/fake-db-presence-to-arrow-fail")
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=_open_table)

    async def _run() -> dict:
        store._db = mock_db
        return await store.get_entity_presence_across_collections(["col1", "col2"], ns="default")

    log_stream = io.StringIO()
    handler = _logging.StreamHandler(log_stream)
    handler.setLevel(_logging.WARNING)
    archon_logger = _logging.getLogger("archon_search")
    archon_logger.addHandler(handler)
    try:
        result = asyncio.run(_run())
    finally:
        archon_logger.removeHandler(handler)

    log_output = log_stream.getvalue()
    assert result == {node_alpha.id: 1}, (
        f"col2 to_arrow failure should be skipped; expected {{alpha: 1}}, got {result}"
    )
    assert "col2" in log_output or "corrupt page" in log_output, (
        f"Expected a WARNING log for col2 to_arrow failure, got: {log_output!r}"
    )


def test_get_entity_presence_open_table_unexpected_failure_skips_collection() -> None:
    """Block 1 unexpected failure: OSError from open_table → WARNING logged + col2 skipped.

    With Fix 1 applied, a non-FileNotFoundError/ValueError exception from open_table
    is caught, logged at WARNING, and re-raised as RuntimeError — which the upstream
    loop in get_entity_presence_across_collections catches and skips.

    Setup:
    - col1: valid node table with node_alpha
    - col2: open_table raises OSError("disk error") — neither FileNotFoundError nor ValueError
    - Expected: result == {node_alpha.id: 1}, WARNING logged for col2
    """
    import asyncio
    import io
    import logging as _logging

    from archon_search.graph_store import GraphStore

    node_alpha = GraphNode(
        id=make_stable_entity_id("concept", "Alpha"),
        entity_name="Alpha",
        entity_type=EntityType.concept,
        source_doc_id="doc-1",
        collection_name="col1",
    )

    col1_table = _make_nodes_table_mock([node_alpha])

    async def _open_table(name: str):
        if name == "_archon_graph_default__col1_nodes":
            return col1_table
        if name == "_archon_graph_default__col2_nodes":
            raise OSError("disk error")
        raise FileNotFoundError(f"Table not found: {name}")

    store = GraphStore("/tmp/fake-db-presence-open-fail")
    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=_open_table)

    async def _run() -> dict:
        store._db = mock_db
        return await store.get_entity_presence_across_collections(["col1", "col2"], ns="default")

    log_stream = io.StringIO()
    handler = _logging.StreamHandler(log_stream)
    handler.setLevel(_logging.WARNING)
    archon_logger = _logging.getLogger("archon_search")
    archon_logger.addHandler(handler)
    try:
        result = asyncio.run(_run())
    finally:
        archon_logger.removeHandler(handler)

    log_output = log_stream.getvalue()
    assert result == {node_alpha.id: 1}, (
        f"col2 open failure should be skipped; expected {{alpha: 1}}, got {result}"
    )
    assert "WARNING" in log_output or "col2" in log_output or "disk error" in log_output, (
        f"Expected a WARNING log for col2 open_table failure, got: {log_output!r}"
    )


# ---------------------------------------------------------------------------
# BE-1b — Legacy graph table startup warning
# ---------------------------------------------------------------------------


def test_startup_warns_on_legacy_graph_tables(caplog) -> None:
    """check_and_warn_legacy_graph_tables must emit a WARNING listing legacy tables.

    A legacy table is one whose name starts with ``_archon_graph_`` but does NOT
    match the E2d positive regex.  Two such tables are present in the mock DB;
    the function must log a WARNING that names both of them and includes
    instructions to delete them manually.
    """
    import asyncio
    import logging

    from archon_search.graph_store import check_and_warn_legacy_graph_tables

    # Simulate list_tables() returning a mix: four legacy + two new-pattern tables.
    list_tables_result = MagicMock()
    list_tables_result.tables = [
        "_archon_graph_mycol_nodes",
        "_archon_graph_mycol_edges",
        "_archon_graph_mycol_communities",
        "_archon_graph_mycol_mentions",
        "_archon_graph_default__mycol_nodes",
        "_archon_graph_default__mycol_edges",
        "some_other_table",
    ]
    mock_db = AsyncMock()
    mock_db.list_tables = AsyncMock(return_value=list_tables_result)

    with caplog.at_level(logging.WARNING, logger="archon_search"):
        legacy = asyncio.run(check_and_warn_legacy_graph_tables(mock_db))

    assert "_archon_graph_mycol_nodes" in legacy
    assert "_archon_graph_mycol_edges" in legacy
    assert "_archon_graph_mycol_communities" in legacy
    assert "_archon_graph_mycol_mentions" in legacy
    assert len(legacy) == 4

    assert any("legacy" in rec.message.lower() for rec in caplog.records), (
        f"Expected 'legacy' in WARNING log, got: {caplog.records!r}"
    )
    assert any("_archon_graph_mycol_nodes" in rec.message for rec in caplog.records), (
        f"Expected legacy table name in WARNING log, got: {caplog.records!r}"
    )
    assert any("_archon_graph_mycol_edges" in rec.message for rec in caplog.records), (
        f"Expected legacy table name in WARNING log, got: {caplog.records!r}"
    )
    # Must include actionable delete instructions
    assert any(
        "delete" in rec.message.lower() or "manual" in rec.message.lower()
        for rec in caplog.records
    ), f"Expected delete/manual instructions in WARNING log, got: {caplog.records!r}"
    import logging
    assert any(
        rec.levelno == logging.WARNING and "legacy" in rec.message.lower()
        for rec in caplog.records
    ), f"Expected WARNING-level 'legacy' record, got: {caplog.records!r}"


def test_startup_no_warn_when_no_legacy_tables(caplog) -> None:
    """check_and_warn_legacy_graph_tables must be silent when only new-pattern tables exist.

    New-pattern tables all match the E2d positive regex.  No WARNING must be
    emitted; the return value must be an empty list.
    """
    import asyncio
    import logging

    from archon_search.graph_store import check_and_warn_legacy_graph_tables

    list_tables_result = MagicMock()
    list_tables_result.tables = [
        "_archon_graph_default__col1_nodes",
        "_archon_graph_default__col1_edges",
        "_archon_graph_ns1__col2_communities",
        "some_unrelated_table",
    ]
    mock_db = AsyncMock()
    mock_db.list_tables = AsyncMock(return_value=list_tables_result)

    with caplog.at_level(logging.WARNING, logger="archon_search"):
        legacy = asyncio.run(check_and_warn_legacy_graph_tables(mock_db))

    assert legacy == [], f"Expected empty list when no legacy tables, got: {legacy}"
    archon_warnings = [r for r in caplog.records if r.name.startswith("archon_search")]
    assert not archon_warnings


def test_check_and_warn_legacy_graph_tables_exception(caplog) -> None:
    """check_and_warn_legacy_graph_tables must return [] and log WARNING when list_tables raises."""
    import asyncio
    import logging

    from archon_search.graph_store import check_and_warn_legacy_graph_tables

    mock_db = AsyncMock()
    mock_db.list_tables = AsyncMock(side_effect=RuntimeError("DB connection lost"))

    with caplog.at_level(logging.WARNING, logger="archon_search"):
        legacy = asyncio.run(check_and_warn_legacy_graph_tables(mock_db))

    assert legacy == [], f"Expected [] on exception, got: {legacy}"
    assert any("scan failed" in rec.message.lower() or "skipping" in rec.message.lower() for rec in caplog.records), (
        f"Expected scan-failed WARNING on exception, got: {caplog.records!r}"
    )
    import logging
    assert any(
        rec.levelno == logging.WARNING
        for rec in caplog.records
        if "scan failed" in rec.message.lower() or "skipping" in rec.message.lower()
    ), f"Expected WARNING-level scan-failed record, got: {caplog.records!r}"


def test_check_and_warn_legacy_graph_tables_empty(caplog) -> None:
    """check_and_warn_legacy_graph_tables returns [] and emits no WARNING for an empty table list."""
    import asyncio
    import logging

    from archon_search.graph_store import check_and_warn_legacy_graph_tables

    list_tables_result = MagicMock()
    list_tables_result.tables = []
    mock_db = AsyncMock()
    mock_db.list_tables = AsyncMock(return_value=list_tables_result)

    with caplog.at_level(logging.WARNING, logger="archon_search"):
        legacy = asyncio.run(check_and_warn_legacy_graph_tables(mock_db))

    assert legacy == []
    archon_warnings = [r for r in caplog.records if r.name.startswith("archon_search")]
    assert not archon_warnings


def test_check_and_warn_legacy_graph_tables_ambiguous_collection_name(caplog) -> None:
    """Known limitation: a table whose collection name contains __ is NOT flagged as legacy.

    A table named ``_archon_graph_foo__bar_nodes`` matches the E2d positive regex
    because the regex treats ``foo`` as the namespace and ``bar`` as the collection.
    This is an intentional false-negative — the heuristic prefers missing legacy
    tables over falsely warning about valid new-pattern tables.
    """
    import asyncio
    import logging

    from archon_search.graph_store import check_and_warn_legacy_graph_tables

    list_tables_result = MagicMock()
    list_tables_result.tables = [
        # This looks like a legacy table but has __ in the collection name portion;
        # the regex matches it as new-pattern (ns=foo, col=bar) — known limitation.
        "_archon_graph_foo__bar_nodes",
    ]
    mock_db = AsyncMock()
    mock_db.list_tables = AsyncMock(return_value=list_tables_result)

    with caplog.at_level(logging.WARNING, logger="archon_search"):
        legacy = asyncio.run(check_and_warn_legacy_graph_tables(mock_db))

    # Current heuristic treats this as new-pattern — NOT flagged as legacy.
    assert legacy == [], (
        "Known limitation: foo__bar_nodes matches E2d regex and is not flagged as legacy"
    )
    archon_warnings = [r for r in caplog.records if r.name.startswith("archon_search")]
    assert not archon_warnings


def test_check_and_warn_no_false_positive_underscored_segments(caplog) -> None:
    """Valid E2d tables with underscores in ns/col must NOT be flagged as legacy.

    E.g. _archon_graph_a_b__c_d_nodes has ns='a_b' and col='c_d' —
    the positive regex must match this as new-pattern and NOT warn.
    """
    import asyncio
    import logging

    from archon_search.graph_store import check_and_warn_legacy_graph_tables

    list_tables_result = MagicMock()
    list_tables_result.tables = [
        "_archon_graph_a_b__c_d_nodes",     # ns=a_b, col=c_d
        "_archon_graph_tenant_1__my_docs_edges",  # ns=tenant_1, col=my_docs
    ]
    mock_db = AsyncMock()
    mock_db.list_tables = AsyncMock(return_value=list_tables_result)

    with caplog.at_level(logging.WARNING, logger="archon_search"):
        legacy = asyncio.run(check_and_warn_legacy_graph_tables(mock_db))

    assert legacy == [], f"Expected no legacy tables for underscore-bearing names, got: {legacy}"
    archon_warnings = [r for r in caplog.records if r.name.startswith("archon_search")]
    assert not archon_warnings


def test_new_pattern_re_matches_all_table_name_helper_outputs() -> None:
    """_NEW_PATTERN_RE must match every table name produced by GraphStore's name helpers.

    This test mechanically binds the regex to the actual table name format produced by
    _table_name, preventing silent drift if the separator or suffix set changes.
    """
    from archon_search.graph_store import _NEW_PATTERN_RE, GraphStore

    # GraphStore table-name helpers require no DB connection — instantiate without __init__
    gs = GraphStore.__new__(GraphStore)

    # All four suffixes, two different ns/col pairs
    for ns, col in [("default", "mycol"), ("tenant_a", "my_docs")]:
        for method_name in ["_nodes_table_name", "_edges_table_name", "_communities_table_name", "_mentions_table_name"]:
            method = getattr(gs, method_name)
            table_name = method(col, ns)
            assert _NEW_PATTERN_RE.match(table_name), (
                f"_NEW_PATTERN_RE must match {method_name}({col!r}, ns={ns!r}) output {table_name!r}"
            )

    # Confirm legacy names (no __ separator) are NOT matched
    assert not _NEW_PATTERN_RE.match("_archon_graph_mycol_nodes"), (
        "_NEW_PATTERN_RE must NOT match legacy-pattern names"
    )


# ---------------------------------------------------------------------------
# BE-2 — name_embedding column in nodes schema
# ---------------------------------------------------------------------------


def test_nodes_schema_has_name_embedding_column() -> None:
    """_nodes_schema() includes a nullable list<float32> name_embedding field."""
    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    schema = GraphStore._nodes_schema()
    assert isinstance(schema, pa.Schema)
    assert "name_embedding" in schema.names, "name_embedding field must be present in _nodes_schema()"

    field = schema.field("name_embedding")
    assert pa.types.is_list(field.type), f"name_embedding must be list type, got {field.type}"
    assert pa.types.is_float32(field.type.value_type), (
        f"name_embedding list element must be float32, got {field.type.value_type}"
    )
    assert field.nullable is True, "name_embedding field must be nullable"


def test_arrow_to_nodes_handles_absent_name_embedding_column() -> None:
    """Tables without name_embedding column deserialize without error; name_embedding is None."""
    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    # Simulate a pre-E2f nodes table: no name_embedding column
    pre_e2f_schema = pa.schema([
        pa.field("id", pa.utf8()),
        pa.field("entity_name", pa.utf8()),
        pa.field("entity_type", pa.utf8()),
        pa.field("source_doc_id", pa.utf8()),
        pa.field("collection_name", pa.utf8()),
        pa.field("entity_subtype", pa.utf8()),
    ])
    arrow_table = pa.table(
        {
            "id": ["abc"],
            "entity_name": ["Alpha"],
            "entity_type": ["concept"],
            "source_doc_id": ["doc-1"],
            "collection_name": ["col1"],
            "entity_subtype": [None],
        },
        schema=pre_e2f_schema,
    )

    nodes = GraphStore._arrow_to_nodes(arrow_table)
    assert len(nodes) == 1
    assert nodes[0].entity_name == "Alpha"
    assert nodes[0].name_embedding is None, (
        "name_embedding must be None when column is absent from the Arrow table"
    )


def test_graph_store_creates_cosine_index_on_name_embedding() -> None:
    """Cosine index creation is idempotent: second call raises no exception and doesn't duplicate index."""
    import asyncio

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-cosine-index")

    # Simulate table with no existing indices (first call creates the index)
    index_info_empty: list = []

    mock_table = MagicMock()
    mock_table.list_indices = AsyncMock(return_value=index_info_empty)
    mock_table.create_index = AsyncMock(return_value=None)

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run_first() -> None:
        store._db = mock_db
        await store._ensure_cosine_index("test-col", ns="default")

    asyncio.run(_run_first())
    # First call: create_index must be called once
    assert mock_table.create_index.call_count == 1

    # Simulate table now has the index (second call must be a no-op).
    # Use a plain object so .columns is a real list, not a MagicMock attribute.
    class _FakeIndex:
        columns = ["name_embedding"]
        name = "name_embedding_idx"

    index_info_with_index = [_FakeIndex()]
    mock_table_indexed = MagicMock()
    mock_table_indexed.list_indices = AsyncMock(return_value=index_info_with_index)
    mock_table_indexed.create_index = AsyncMock(return_value=None)

    mock_db2 = AsyncMock()
    mock_db2.open_table = AsyncMock(return_value=mock_table_indexed)

    async def _run_second() -> None:
        store._db = mock_db2
        await store._ensure_cosine_index("test-col", ns="default")

    asyncio.run(_run_second())
    # Second call: create_index must NOT be called (index already exists)
    assert mock_table_indexed.create_index.call_count == 0


def test_graph_store_vector_search_nodes_returns_nearest_nodes() -> None:
    """vector_search_nodes returns nearest nodes ordered by cosine similarity."""
    import asyncio

    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-vector-search")

    node_a = _node("Alpha", EntityType.concept)
    node_b = _node("Beta", EntityType.concept)

    # Build a nodes arrow table with name_embedding column
    node_schema_with_emb = GraphStore._nodes_schema()
    nodes_arrow = pa.table(
        {
            "id": [node_a.id, node_b.id],
            "entity_name": [node_a.entity_name, node_b.entity_name],
            "entity_type": [node_a.entity_type.value, node_b.entity_type.value],
            "source_doc_id": [node_a.source_doc_id, node_b.source_doc_id],
            "collection_name": [node_a.collection_name, node_b.collection_name],
            "entity_subtype": [node_a.entity_subtype, node_b.entity_subtype],
            "name_embedding": [[1.0, 0.0], [0.0, 1.0]],
        },
        schema=node_schema_with_emb,
    )

    # Mock the LanceDB vector search chain: .vector_search().distance_type().limit().to_arrow()
    search_builder = MagicMock()
    search_builder.distance_type.return_value = search_builder
    search_builder.limit.return_value = search_builder
    search_builder.where.return_value = search_builder
    search_builder.to_arrow = AsyncMock(return_value=nodes_arrow)

    mock_table = MagicMock()
    mock_table.vector_search.return_value = search_builder

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> list:
        store._db = mock_db
        return await store.vector_search_nodes(
            "test-col", [1.0, 0.0], entity_type=None, limit=2, ns="default"
        )

    results = asyncio.run(_run())
    assert len(results) == 2
    assert results[0].entity_name == node_a.entity_name
    assert results[1].entity_name == node_b.entity_name
    # Verify vector_search was called with the query embedding
    mock_table.vector_search.assert_called_once_with([1.0, 0.0])


def test_graph_store_vector_search_nodes_filters_by_entity_type() -> None:
    """When entity_type is non-None, a SQL predicate is applied via .where()."""
    import asyncio

    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-db-vector-search-filter")

    node_a = _node("Alpha", EntityType.concept)

    node_schema_with_emb = GraphStore._nodes_schema()
    nodes_arrow = pa.table(
        {
            "id": [node_a.id],
            "entity_name": [node_a.entity_name],
            "entity_type": [node_a.entity_type.value],
            "source_doc_id": [node_a.source_doc_id],
            "collection_name": [node_a.collection_name],
            "entity_subtype": [node_a.entity_subtype],
            "name_embedding": [[1.0, 0.0]],
        },
        schema=node_schema_with_emb,
    )

    search_builder = MagicMock()
    search_builder.distance_type.return_value = search_builder
    search_builder.limit.return_value = search_builder
    search_builder.where.return_value = search_builder
    search_builder.to_arrow = AsyncMock(return_value=nodes_arrow)

    mock_table = MagicMock()
    mock_table.vector_search.return_value = search_builder

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> list:
        store._db = mock_db
        return await store.vector_search_nodes(
            "test-col", [1.0, 0.0], entity_type="concept", limit=2, ns="default"
        )

    asyncio.run(_run())

    # Assert .where() was called (entity_type filter applied)
    search_builder.where.assert_called_once()
    # Assert the predicate contains the entity_type value
    call_args = search_builder.where.call_args[0][0]
    assert "concept" in call_args

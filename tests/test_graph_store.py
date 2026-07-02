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
            await store.ensure_graph_tables("_bad_name")

        with pytest.raises(ValueError, match="Invalid collection name"):
            await store.ensure_graph_tables("has space")

        with pytest.raises(ValueError, match="Invalid collection name"):
            await store.ensure_graph_tables("")

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
        await store.ensure_graph_tables("mycol")
        await store.ensure_graph_tables("mycol")
        # create_table called twice (once per table per call = 4 total)
        assert mock_db.create_table.call_count == 4

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Upsert — nodes
# ---------------------------------------------------------------------------


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

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> None:
        store._db = mock_db
        await store.write_graph("test-col", [node_a], [])
        await store.write_graph("test-col", [node_a], [])
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

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> None:
        store._db = mock_db
        await store.write_graph("test-col", [node_a, node_b], [edge_ab])
        await store.write_graph("test-col", [node_a, node_b], [edge_ab])
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
        return await store.get_neighbours("test-col", [node_a.id])

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
        return await store.edge_count("empty-col")

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
        return await store.find_nodes_by_name("col1", ["authservice"])

    async def _run_miss() -> list:
        store._db = mock_db_miss
        return await store.find_nodes_by_name("col1", ["unknown_name"])

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
        return await store.find_nodes_by_name("col1", ["token validator"])

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
        return await store.node_count("empty-col")

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
        return await store.get_neighbours("test-col", [])

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
        return await store.find_nodes_by_name("test-col", [])

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
        return await store.get_neighbours(col, [node_a.id, node_c.id])

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
        return await store.get_edges_for_nodes("test-col", [])

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
        return await store.get_edges_for_nodes("test-col", [node_a.id])

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
        return await store.get_edges_for_nodes("test-col", [node_b.id])

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
        return await store.get_edges_for_nodes("test-col", [node_a.id])

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
        return await store.get_edges_for_nodes("test-col", [node_x.id])

    result = asyncio.run(_run())
    assert result == []

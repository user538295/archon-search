"""Unit tests for BE-2: _archon_graph_{col}_communities table in GraphStore.

Tests verify:
- PyArrow schema has correct field types; list fields are list_(utf8)
- get_chunks_by_ids returns only found chunks; missing IDs silently skipped
- get_chunks_for_doc returns all chunks for a given source document
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# test_communities_table_schema
# ---------------------------------------------------------------------------


def test_communities_table_schema() -> None:
    """PyArrow schema for communities table has correct field types.

    List fields (entity_ids, representative_chunk_ids) must be list_(utf8).
    community_id and built_at are utf8. summary_text is nullable utf8.
    """
    import pyarrow as pa

    from archon_search.graph_store import GraphStore

    schema = GraphStore._communities_schema()

    field_names = [schema.field(i).name for i in range(len(schema))]
    assert "community_id" in field_names
    assert "entity_ids" in field_names
    assert "representative_chunk_ids" in field_names
    assert "summary_text" in field_names
    assert "built_at" in field_names

    # community_id must be utf8
    assert schema.field("community_id").type == pa.utf8()

    # entity_ids must be list_(utf8)
    assert schema.field("entity_ids").type == pa.list_(pa.utf8()), (
        f"entity_ids must be list_(utf8), got {schema.field('entity_ids').type}"
    )

    # representative_chunk_ids must be list_(utf8)
    assert schema.field("representative_chunk_ids").type == pa.list_(pa.utf8()), (
        f"representative_chunk_ids must be list_(utf8), got {schema.field('representative_chunk_ids').type}"
    )

    # summary_text must be nullable utf8
    summary_field = schema.field("summary_text")
    assert summary_field.type == pa.utf8()
    assert summary_field.nullable

    # built_at must be utf8 (ISO 8601 string)
    assert schema.field("built_at").type == pa.utf8()


# ---------------------------------------------------------------------------
# test_ensure_communities_table_calls_create_table
# ---------------------------------------------------------------------------


def test_ensure_communities_table_calls_create_table() -> None:
    """ensure_communities_table must call db.create_table with the correct table name."""
    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-be2-ensure")

    mock_db = AsyncMock()
    mock_db.create_table = AsyncMock(return_value=AsyncMock())

    async def _run() -> None:
        store._db = mock_db
        await store.ensure_communities_table("mycol")

    asyncio.run(_run())

    assert mock_db.create_table.call_count == 1
    called_name = mock_db.create_table.call_args[0][0]
    assert "_archon_graph_mycol_communities" == called_name
    call_kwargs = mock_db.create_table.call_args[1]
    assert call_kwargs.get("exist_ok") is True


# ---------------------------------------------------------------------------
# test_get_chunks_by_ids_returns_only_found
# ---------------------------------------------------------------------------


def test_get_chunks_by_ids_returns_only_found() -> None:
    """get_chunks_by_ids filters out missing IDs without raising.

    Setup: store has chunks with IDs [id0..id4]. Request ids [0,1,2] + [unknown_a, unknown_b].
    Expect: exactly 3 rows returned; no error.
    """
    from archon_search.store import SearchStore

    store = SearchStore("/tmp/fake-be2-chunks-by-ids")

    # Simulate LanceDB returning only the 3 matching rows
    known_ids = [f"{'a'*64}-{i:06d}" for i in range(3)]
    rows_returned = [{"chunk_id": cid, "doc_id": "d" * 64, "text": "t"} for cid in known_ids]

    query_mock = MagicMock()
    query_mock.where.return_value = query_mock
    query_mock.to_list = AsyncMock(return_value=rows_returned)

    mock_table = MagicMock()
    mock_table.query.return_value = query_mock

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> list[dict]:
        store._db = mock_db
        all_request_ids = known_ids + ["unknown_a" * 4, "unknown_b" * 4]
        return await store.get_chunks_by_ids("testcol", all_request_ids)

    result = asyncio.run(_run())

    assert len(result) == 3, f"Expected 3 rows, got {len(result)}"
    returned_ids = {r["chunk_id"] for r in result}
    for cid in known_ids:
        assert cid in returned_ids


# ---------------------------------------------------------------------------
# test_get_chunks_by_ids_empty_input_returns_empty
# ---------------------------------------------------------------------------


def test_get_chunks_by_ids_empty_input_returns_empty() -> None:
    """get_chunks_by_ids with empty list returns [] immediately without DB call."""
    from archon_search.store import SearchStore

    store = SearchStore("/tmp/fake-be2-empty-ids")
    mock_db = AsyncMock()

    async def _run() -> list[dict]:
        store._db = mock_db
        return await store.get_chunks_by_ids("testcol", [])

    result = asyncio.run(_run())
    assert result == []
    mock_db.open_table.assert_not_called()


# ---------------------------------------------------------------------------
# test_get_chunks_for_doc_returns_all_chunks
# ---------------------------------------------------------------------------


def test_get_chunks_for_doc_returns_all_chunks() -> None:
    """get_chunks_for_doc returns all chunks matching the given doc_id.

    Setup: 4 chunks for doc_a, 2 for doc_b.
    When get_chunks_for_doc(doc_a) is called, DB returns 4 rows.
    Expect exactly 4 dicts returned.
    """
    from archon_search.store import SearchStore

    store = SearchStore("/tmp/fake-be2-chunks-for-doc")

    doc_a = "a" * 64
    doc_b = "b" * 64
    doc_a_rows = [{"chunk_id": f"{doc_a}-{i:06d}", "doc_id": doc_a, "text": f"chunk {i}"} for i in range(4)]

    query_mock = MagicMock()
    query_mock.where.return_value = query_mock
    query_mock.to_list = AsyncMock(return_value=doc_a_rows)

    mock_table = MagicMock()
    mock_table.query.return_value = query_mock

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> list[dict]:
        store._db = mock_db
        return await store.get_chunks_for_doc("testcol", doc_a)

    result = asyncio.run(_run())

    assert len(result) == 4, f"Expected 4 chunks for doc_a, got {len(result)}"
    for row in result:
        assert row["doc_id"] == doc_a


# ---------------------------------------------------------------------------
# test_get_chunks_for_doc_missing_collection_returns_empty
# ---------------------------------------------------------------------------


def test_get_chunks_for_doc_missing_collection_returns_empty() -> None:
    """get_chunks_for_doc returns [] when collection table does not exist."""
    from archon_search.store import SearchStore

    store = SearchStore("/tmp/fake-be2-missing-col")

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=FileNotFoundError("table not found"))

    async def _run() -> list[dict]:
        store._db = mock_db
        return await store.get_chunks_for_doc("notexist", "a" * 64)

    result = asyncio.run(_run())
    assert result == []


# ---------------------------------------------------------------------------
# test_get_communities_for_entities_uses_python_side_filter
# ---------------------------------------------------------------------------


def test_get_communities_for_entities_uses_python_side_filter() -> None:
    """get_communities_for_entities must scan all communities and filter in-process.

    This verifies that a scan is performed (to_list() called), and the returned
    Community objects contain only those where any entity_id matches.
    """
    import pyarrow as pa

    from archon_search.graph_types import Community
    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-be2-entities-filter")

    built_at_iso = "2026-01-01T00:00:00.000000Z"
    # Community 1 contains entity A and B
    # Community 2 contains entity C only
    # Query for entity A → should return only Community 1
    rows = [
        {
            "community_id": "comm-1",
            "entity_ids": ["entity-A", "entity-B"],
            "representative_chunk_ids": ["chunk-1"],
            "summary_text": None,
            "built_at": built_at_iso,
        },
        {
            "community_id": "comm-2",
            "entity_ids": ["entity-C"],
            "representative_chunk_ids": ["chunk-2"],
            "summary_text": None,
            "built_at": built_at_iso,
        },
    ]

    query_mock = MagicMock()
    query_mock.to_list = AsyncMock(return_value=rows)

    mock_table = MagicMock()
    mock_table.query.return_value = query_mock

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> list[Community]:
        store._db = mock_db
        return await store.get_communities_for_entities("testcol", ["entity-A"])

    result = asyncio.run(_run())

    assert len(result) == 1, f"Expected 1 community, got {len(result)}"
    assert result[0].community_id == "comm-1"
    assert "entity-A" in result[0].entity_ids


# ---------------------------------------------------------------------------
# test_get_community_stats_empty_returns_zero_none
# ---------------------------------------------------------------------------


def test_get_community_stats_empty_returns_zero_none() -> None:
    """get_community_stats returns (0, None) when no communities are built."""
    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-be2-stats-empty")

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(side_effect=FileNotFoundError("table not found"))

    async def _run() -> tuple:
        store._db = mock_db
        return await store.get_community_stats("testcol")

    count, last_built = asyncio.run(_run())

    assert count == 0
    assert last_built is None


# ---------------------------------------------------------------------------
# test_list_community_representatives_returns_all
# ---------------------------------------------------------------------------


def test_list_community_representatives_returns_all() -> None:
    """list_community_representatives returns all Community objects from the table."""
    from archon_search.graph_types import Community
    from archon_search.graph_store import GraphStore

    store = GraphStore("/tmp/fake-be2-list-reps")

    built_at_iso = "2026-01-01T00:00:00.000000Z"
    rows = [
        {
            "community_id": f"comm-{i}",
            "entity_ids": [f"entity-{i}"],
            "representative_chunk_ids": [f"chunk-{i}"],
            "summary_text": None,
            "built_at": built_at_iso,
        }
        for i in range(3)
    ]

    query_mock = MagicMock()
    query_mock.to_list = AsyncMock(return_value=rows)

    mock_table = MagicMock()
    mock_table.query.return_value = query_mock

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    async def _run() -> list[Community]:
        store._db = mock_db
        return await store.list_community_representatives("testcol")

    result = asyncio.run(_run())

    assert len(result) == 3
    ids = {c.community_id for c in result}
    assert ids == {"comm-0", "comm-1", "comm-2"}
    for community in result:
        assert len(community.representative_chunk_ids) >= 1

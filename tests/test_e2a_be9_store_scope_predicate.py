"""Tests for BE-9: store_filters.py scope predicate + hybrid_search_with_trace scope_filter.

Plan: Documentation/Backlog/e2a-ttl-scoping-team-plan.md Task BE-9.

TDD: tests are written first; implementation goes in store_filters.py and store.py.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._types import normalize_iso_utc


# ---------------------------------------------------------------------------
# Unit: _where_list_has_or_null helper
# ---------------------------------------------------------------------------


def test_where_list_has_or_null_helper() -> None:
    """_where_list_has_or_null returns correct SQL expression."""
    from archon_search.store_filters import _where_list_has_or_null

    result = _where_list_has_or_null("scopes", "user:alice")
    assert result == "(scopes IS NULL OR list_has(scopes, 'user:alice'))"


def test_where_list_has_or_null_uses_sql_quote_str() -> None:
    """_where_list_has_or_null uses _sql_quote_str to quote the value (single-quote safety)."""
    from archon_search.store_filters import _where_list_has_or_null, _sql_quote_str

    value = "user:o'malley"
    result = _where_list_has_or_null("scopes", value)
    expected_quoted = _sql_quote_str(value)  # "'user:o''malley'"
    assert expected_quoted in result
    assert "list_has(scopes, " + expected_quoted + ")" in result
    assert result == f"(scopes IS NULL OR list_has(scopes, {expected_quoted}))"


def test_where_list_has_or_null_custom_column() -> None:
    """_where_list_has_or_null works with any column name."""
    from archon_search.store_filters import _where_list_has_or_null

    result = _where_list_has_or_null("tags", "env:prod")
    assert result == "(tags IS NULL OR list_has(tags, 'env:prod'))"


# ---------------------------------------------------------------------------
# Unit: build_where with scope_filter
# ---------------------------------------------------------------------------


def test_build_where_scope_none() -> None:
    """No scope_filter — predicate unchanged (no scope clause added)."""
    from archon_search.store_filters import build_where
    from archon_search.filters import SearchFilters

    result = build_where(SearchFilters(), scope_filter=None)
    assert "scopes" not in result
    assert "list_has" not in result


def test_build_where_scope_exact_match() -> None:
    """Exact scope_filter → (scopes IS NULL OR list_has(scopes, '<value>')) ANDed in."""
    from archon_search.store_filters import build_where
    from archon_search.filters import SearchFilters

    result = build_where(SearchFilters(), scope_filter="user:alice")
    assert "(scopes IS NULL OR list_has(scopes, 'user:alice'))" in result


def test_build_where_scope_exact_match_with_filters() -> None:
    """Exact scope_filter ANDs with existing SearchFilters predicates."""
    from archon_search.store_filters import build_where
    from archon_search.filters import SearchFilters

    result = build_where(SearchFilters(file_type="md"), scope_filter="user:alice")
    assert "file_type = 'md'" in result
    assert "(scopes IS NULL OR list_has(scopes, 'user:alice'))" in result
    assert " AND " in result


def test_build_where_scope_wildcard_not_in_predicate() -> None:
    """Wildcard scope_filter (ending with *) → scope predicate omitted (post-filter handles it)."""
    from archon_search.store_filters import build_where
    from archon_search.filters import SearchFilters

    result = build_where(SearchFilters(), scope_filter="user:alice*")
    assert "list_has" not in result
    assert "scopes" not in result


def test_build_where_scope_wildcard_bare_star_not_in_predicate() -> None:
    """Bare '*' scope_filter → predicate omitted."""
    from archon_search.store_filters import build_where
    from archon_search.filters import SearchFilters

    result = build_where(SearchFilters(), scope_filter="*")
    assert "list_has" not in result


def test_build_where_scope_filter_with_none_filters() -> None:
    """build_where accepts filters=None; scope_filter still produces predicate."""
    from archon_search.store_filters import build_where

    result = build_where(None, scope_filter="user:alice")
    assert "(scopes IS NULL OR list_has(scopes, 'user:alice'))" in result


def test_build_where_scope_filter_none_filters_no_scope() -> None:
    """build_where with filters=None and scope_filter=None returns empty string."""
    from archon_search.store_filters import build_where

    result = build_where(None, scope_filter=None)
    assert result == ""


# ---------------------------------------------------------------------------
# Unit: hybrid_search_with_trace scope predicate applied to both legs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_search_with_trace_passes_scope_predicate() -> None:
    """exact scope_filter → WHERE predicate applied to both vector and FTS legs."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch, call

    from archon_search.store import SearchStore

    store = MagicMock(spec=SearchStore)
    store._validate_collection = MagicMock()
    store._require_connected = MagicMock()

    # Build a mock table with recorded where calls
    mock_table = AsyncMock()

    # FTS path: table.search() returns something we can chain .where().limit().to_list() on
    fts_chain = MagicMock()
    fts_chain.where = MagicMock(return_value=fts_chain)
    fts_chain.limit = MagicMock(return_value=fts_chain)
    fts_chain.to_list = AsyncMock(return_value=[])
    mock_table.search = AsyncMock(return_value=fts_chain)

    # Vector path: table.vector_search() returns something we can chain .where().limit().to_list() on
    vec_chain = MagicMock()
    vec_chain.where = MagicMock(return_value=vec_chain)
    vec_chain.limit = MagicMock(return_value=vec_chain)
    vec_chain.to_list = AsyncMock(return_value=[])
    mock_table.vector_search = MagicMock(return_value=vec_chain)

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._require_connected.return_value = mock_db

    from archon_search.store import _hybrid_search_with_trace

    await _hybrid_search_with_trace(
        store,
        "col",
        query_vector=[0.1, 0.2, 0.3, 0.4],
        query_text="hello",
        candidate_depth=10,
        scope_filter="user:alice",
    )

    # Both legs must have had .where() called with the scope predicate
    expected_pred = "(scopes IS NULL OR list_has(scopes, 'user:alice'))"
    vec_chain.where.assert_called_once_with(expected_pred)
    fts_chain.where.assert_called_once_with(expected_pred)


@pytest.mark.asyncio
async def test_hybrid_search_with_trace_combined_filters_and_scope() -> None:
    """Both filters and scope_filter → combined ANDed predicate applied to both legs."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.filters import SearchFilters
    from archon_search.store import SearchStore, _hybrid_search_with_trace

    store = MagicMock(spec=SearchStore)
    store._validate_collection = MagicMock()
    store._require_connected = MagicMock()

    fts_chain = MagicMock()
    fts_chain.where = MagicMock(return_value=fts_chain)
    fts_chain.limit = MagicMock(return_value=fts_chain)
    fts_chain.to_list = AsyncMock(return_value=[])

    vec_chain = MagicMock()
    vec_chain.where = MagicMock(return_value=vec_chain)
    vec_chain.limit = MagicMock(return_value=vec_chain)
    vec_chain.to_list = AsyncMock(return_value=[])

    mock_table = AsyncMock()
    mock_table.search = AsyncMock(return_value=fts_chain)
    mock_table.vector_search = MagicMock(return_value=vec_chain)

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._require_connected.return_value = mock_db

    await _hybrid_search_with_trace(
        store,
        "col",
        query_vector=[0.1, 0.2, 0.3, 0.4],
        query_text="hello",
        candidate_depth=10,
        filters=SearchFilters(file_type="md"),
        scope_filter="user:alice",
    )

    expected_pred = "file_type = 'md' AND (scopes IS NULL OR list_has(scopes, 'user:alice'))"
    vec_chain.where.assert_called_once_with(expected_pred)
    fts_chain.where.assert_called_once_with(expected_pred)


@pytest.mark.asyncio
async def test_hybrid_search_with_trace_wildcard_scope_no_where_call() -> None:
    """Wildcard scope_filter → no SQL predicate; .where() not called (post-filter is caller's job)."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.store import SearchStore, _hybrid_search_with_trace

    store = MagicMock(spec=SearchStore)
    store._validate_collection = MagicMock()
    store._require_connected = MagicMock()

    fts_chain = MagicMock()
    fts_chain.where = MagicMock(return_value=fts_chain)
    fts_chain.limit = MagicMock(return_value=fts_chain)
    fts_chain.to_list = AsyncMock(return_value=[])

    vec_chain = MagicMock()
    vec_chain.where = MagicMock(return_value=vec_chain)
    vec_chain.limit = MagicMock(return_value=vec_chain)
    vec_chain.to_list = AsyncMock(return_value=[])

    mock_table = AsyncMock()
    mock_table.search = AsyncMock(return_value=fts_chain)
    mock_table.vector_search = MagicMock(return_value=vec_chain)

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._require_connected.return_value = mock_db

    await _hybrid_search_with_trace(
        store,
        "col",
        query_vector=[0.1, 0.2, 0.3, 0.4],
        query_text="hello",
        candidate_depth=10,
        scope_filter="user:alice*",
    )

    # Wildcard → no SQL predicate applied; caller handles post-filtering
    vec_chain.where.assert_not_called()
    fts_chain.where.assert_not_called()


@pytest.mark.asyncio
async def test_hybrid_search_with_trace_no_scope_filter_no_where_call() -> None:
    """No scope_filter and no filters → .where() not called (empty predicate)."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.store import SearchStore, _hybrid_search_with_trace

    store = MagicMock(spec=SearchStore)
    store._validate_collection = MagicMock()
    store._require_connected = MagicMock()

    fts_chain = MagicMock()
    fts_chain.where = MagicMock(return_value=fts_chain)
    fts_chain.limit = MagicMock(return_value=fts_chain)
    fts_chain.to_list = AsyncMock(return_value=[])

    vec_chain = MagicMock()
    vec_chain.where = MagicMock(return_value=vec_chain)
    vec_chain.limit = MagicMock(return_value=vec_chain)
    vec_chain.to_list = AsyncMock(return_value=[])

    mock_table = AsyncMock()
    mock_table.search = AsyncMock(return_value=fts_chain)
    mock_table.vector_search = MagicMock(return_value=vec_chain)

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._require_connected.return_value = mock_db

    await _hybrid_search_with_trace(
        store,
        "col",
        query_vector=[0.1, 0.2, 0.3, 0.4],
        query_text="hello",
        candidate_depth=10,
        scope_filter=None,
    )

    vec_chain.where.assert_not_called()
    fts_chain.where.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: real LanceDB store — scope predicate filtering
# ---------------------------------------------------------------------------


def _make_chunk_row(
    doc_id: str,
    text: str,
    scopes: list[str] | None,
    *,
    now_iso: str,
    dim: int = 4,
) -> dict:
    """Build a minimal chunk row dict for direct table.add()."""
    chunk_id = doc_id + "-000000"
    return {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": text,
        "vector": [0.1] * dim,
        "source_path": f"/tmp/{doc_id[:8]}.txt",
        "indexed_at": now_iso,
        "file_type": "",
        "language": "",
        "metadata": "{}",
        "custom_score": None,
        "ingested_by": "cli",
        "updated_at": now_iso,
        "acl": None,
        "expires_at": None,
        "scopes": scopes,
    }


async def _open_migrated_store(tmp_path: Path, dim: int = 4):
    """Open a SearchStore, run startup + E2a migrations, return (store, collection_name)."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    await store._run_startup_migrations()
    await store.ensure_collection("col", embedding_dim=dim)
    pending = await store.pending_migrations("col", "default")
    if pending:
        await store.apply_in_place_migrations("col", "default", pending)
    return store


@pytest.mark.asyncio
@pytest.mark.integration
async def test_scope_exact_predicate_filters_store(tmp_path: Path) -> None:
    """Exact scope_filter → only chunks with matching scope (or null scope) returned."""
    store = await _open_migrated_store(tmp_path)
    try:
        now_iso = normalize_iso_utc(datetime.now(UTC))
        db = store._require_connected()
        table = await db.open_table("col")

        await table.add([
            _make_chunk_row("a" * 64, "alice chunk", ["user:alice"], now_iso=now_iso),
            _make_chunk_row("b" * 64, "bob chunk", ["user:bob"], now_iso=now_iso),
        ])

        results = await store.hybrid_search_with_trace(
            "col",
            query_vector=[0.1, 0.1, 0.1, 0.1],
            query_text="chunk",
            candidate_depth=20,
            scope_filter="user:alice",
        )

        chunk_ids = {r.chunk_id for r in results}
        assert "a" * 64 + "-000000" in chunk_ids, "alice chunk must be present"
        assert "b" * 64 + "-000000" not in chunk_ids, "bob chunk must be excluded"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_scope_predicate_passes_through_null_scoped_chunks(tmp_path: Path) -> None:
    """Null-scoped chunks always pass the scope predicate (shared/global semantics)."""
    store = await _open_migrated_store(tmp_path)
    try:
        now_iso = normalize_iso_utc(datetime.now(UTC))
        db = store._require_connected()
        table = await db.open_table("col")

        await table.add([
            _make_chunk_row("a" * 64, "alice chunk", ["user:alice"], now_iso=now_iso),
            _make_chunk_row("u" * 64, "unscoped chunk", None, now_iso=now_iso),
        ])

        results = await store.hybrid_search_with_trace(
            "col",
            query_vector=[0.1, 0.1, 0.1, 0.1],
            query_text="chunk",
            candidate_depth=20,
            scope_filter="user:alice",
        )

        chunk_ids = {r.chunk_id for r in results}
        assert "a" * 64 + "-000000" in chunk_ids, "alice chunk must be present"
        assert "u" * 64 + "-000000" in chunk_ids, "unscoped chunk must pass through"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_scope_predicate_no_match_returns_empty(tmp_path: Path) -> None:
    """scope_filter with no matching scope (and no unscoped chunks) returns empty."""
    store = await _open_migrated_store(tmp_path)
    try:
        now_iso = normalize_iso_utc(datetime.now(UTC))
        db = store._require_connected()
        table = await db.open_table("col")

        # ONLY bob-scoped chunks; no unscoped chunks
        await table.add([
            _make_chunk_row("b" * 64, "bob chunk", ["user:bob"], now_iso=now_iso),
        ])

        results = await store.hybrid_search_with_trace(
            "col",
            query_vector=[0.1, 0.1, 0.1, 0.1],
            query_text="chunk",
            candidate_depth=20,
            scope_filter="user:alice",
        )

        assert results == [], "No chunks should match user:alice when only user:bob chunks exist"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_scope_filter_with_single_quote_roundtrip(tmp_path: Path) -> None:
    """Single-quote in scope value is properly escaped via _sql_quote_str."""
    store = await _open_migrated_store(tmp_path)
    try:
        now_iso = normalize_iso_utc(datetime.now(UTC))
        db = store._require_connected()
        table = await db.open_table("col")

        scope_with_quote = "user:o'malley"
        await table.add([
            _make_chunk_row("o" * 64, "omalley chunk", [scope_with_quote], now_iso=now_iso),
            _make_chunk_row("b" * 64, "bob chunk", ["user:bob"], now_iso=now_iso),
        ])

        results = await store.hybrid_search_with_trace(
            "col",
            query_vector=[0.1, 0.1, 0.1, 0.1],
            query_text="chunk",
            candidate_depth=20,
            scope_filter=scope_with_quote,
        )

        chunk_ids = {r.chunk_id for r in results}
        assert "o" * 64 + "-000000" in chunk_ids, "o'malley chunk must be returned"
        assert "b" * 64 + "-000000" not in chunk_ids, "bob chunk must be excluded"
    finally:
        await store.disconnect()

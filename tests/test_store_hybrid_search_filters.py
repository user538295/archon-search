"""Unit tests for hybrid_search filter support (A2) — no LanceDB connection required."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import ChunkRecord
from archon_search.filters import SearchFilters
from archon_search.store import SearchStore

_DIM = 4


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _make_row(doc_id: str, idx: int, **overrides: Any) -> dict[str, Any]:
    chunk_id = f"{doc_id}-{idx:06d}"
    return {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "hello world",
        "source_path": f"/tmp/{doc_id[:8]}.md",
        "file_type": "md",
        "indexed_at": "2026-01-01T00:00:00.000000Z",
        "updated_at": "2026-01-01T00:00:00.000000Z",
        "ingested_by": "cli",
        "metadata": "{}",
        "language": None,
        "acl": None,
        **overrides,
    }


def _make_vec_query_chain(rows: list[dict[str, Any]]) -> MagicMock:
    """Build a mock vector search query chain that supports .limit().where().to_list()."""
    inner = MagicMock()
    inner.where = MagicMock(return_value=inner)
    inner.to_list = AsyncMock(return_value=rows)
    # limit() returns inner; inner.where() returns inner; inner.to_list() returns rows
    inner.limit = MagicMock(return_value=inner)
    vec_query = MagicMock()
    vec_query.limit = MagicMock(return_value=inner)
    return vec_query, inner  # type: ignore[return-value]


def _make_store_with_mock_table(
    rows: list[dict[str, Any]],
    tmp_path: Path,
    *,
    fts_fails: bool = False,
) -> SearchStore:
    """Return a SearchStore with a pre-configured mock LanceDB table."""
    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_table = MagicMock()

    # Vector search: vector_search() -> .limit() -> (.where()) -> .to_list()
    vec_inner = MagicMock()
    vec_inner.where = MagicMock(return_value=vec_inner)
    vec_inner.to_list = AsyncMock(return_value=rows)
    vec_query = MagicMock()
    vec_query.limit = MagicMock(return_value=vec_inner)
    mock_table.vector_search = MagicMock(return_value=vec_query)

    if fts_fails:
        mock_table.search = AsyncMock(side_effect=Exception("fts index not found"))
    else:
        # FTS search: await table.search() -> fts_result -> .where() -> .limit() -> .to_list()
        fts_inner = MagicMock()
        fts_inner.to_list = AsyncMock(return_value=rows)
        fts_result = MagicMock()
        fts_result.where = MagicMock(return_value=fts_result)
        fts_result.limit = MagicMock(return_value=fts_inner)
        mock_table.search = AsyncMock(return_value=fts_result)

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    return store


def test_filter_calls_where_on_both_branches(tmp_path: Path) -> None:
    """When a filter predicate is non-empty, .where() is called on both branches."""
    doc_id = _doc_id()
    rows = [_make_row(doc_id, 0)]

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_table = MagicMock()

    # Vector: vector_search().limit().where().to_list()
    vec_inner = MagicMock()
    vec_inner.where = MagicMock(return_value=vec_inner)
    vec_inner.to_list = AsyncMock(return_value=rows)
    vec_query = MagicMock()
    vec_query.limit = MagicMock(return_value=vec_inner)
    mock_table.vector_search = MagicMock(return_value=vec_query)

    # FTS: await table.search().where().limit().to_list()
    fts_inner = MagicMock()
    fts_inner.to_list = AsyncMock(return_value=rows)
    fts_result = MagicMock()
    fts_result.where = MagicMock(return_value=fts_result)
    fts_result.limit = MagicMock(return_value=fts_inner)
    mock_table.search = AsyncMock(return_value=fts_result)

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    f = SearchFilters(file_type="md")
    asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", 5, filters=f))

    # .where() must have been called on both branches
    vec_inner.where.assert_called_once()
    fts_result.where.assert_called_once()


def test_no_filters_does_not_call_where(tmp_path: Path) -> None:
    """When filters=None, .where() is never called."""
    doc_id = _doc_id()
    rows = [_make_row(doc_id, 0)]

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_table = MagicMock()

    # Vector: vector_search().limit().to_list() (no .where())
    vec_inner = MagicMock()
    vec_inner.where = MagicMock(return_value=vec_inner)
    vec_inner.to_list = AsyncMock(return_value=rows)
    vec_query = MagicMock()
    vec_query.limit = MagicMock(return_value=vec_inner)
    mock_table.vector_search = MagicMock(return_value=vec_query)

    # FTS: await table.search().limit().to_list() (no .where())
    fts_inner = MagicMock()
    fts_inner.to_list = AsyncMock(return_value=rows)
    fts_result = MagicMock()
    fts_result.where = MagicMock(return_value=fts_result)
    fts_result.limit = MagicMock(return_value=fts_inner)
    mock_table.search = AsyncMock(return_value=fts_result)

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", 5, filters=None))

    # .where() must NOT have been called on either branch
    vec_inner.where.assert_not_called()
    fts_result.where.assert_not_called()


def test_hybrid_search_never_calls_postfilter(tmp_path: Path) -> None:
    """hybrid_search must not call .postfilter() (deprecated LanceDB API)."""
    doc_id = _doc_id()
    rows = [_make_row(doc_id, 0)]
    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_table = MagicMock()

    vec_inner = MagicMock()
    vec_inner.postfilter = MagicMock(side_effect=AssertionError("postfilter must not be called"))
    vec_inner.where = MagicMock(return_value=vec_inner)
    vec_inner.to_list = AsyncMock(return_value=rows)
    vec_query = MagicMock()
    vec_query.limit = MagicMock(return_value=vec_inner)
    mock_table.vector_search = MagicMock(return_value=vec_query)

    fts_inner = MagicMock()
    fts_inner.to_list = AsyncMock(return_value=rows)
    fts_result = MagicMock()
    fts_result.postfilter = MagicMock(side_effect=AssertionError("postfilter must not be called"))
    fts_result.where = MagicMock(return_value=fts_result)
    fts_result.limit = MagicMock(return_value=fts_inner)
    mock_table.search = AsyncMock(return_value=fts_result)

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    # Should not raise
    asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", 5, filters=None))


def test_glob_post_filter_keeps_matching_rows(tmp_path: Path) -> None:
    """source_path_glob filters out non-matching rows after RRF scoring."""
    doc_id1 = _doc_id()
    doc_id2 = _doc_id()
    rows = [
        _make_row(doc_id1, 0, source_path="/docs/report.md"),
        _make_row(doc_id2, 0, source_path="/src/main.py"),
    ]
    store = _make_store_with_mock_table(rows, tmp_path)

    f = SearchFilters(source_path_glob="/docs/*.md")
    results = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", 5, filters=f))

    paths = {r.source_path for r in results}
    assert "/docs/report.md" in paths
    assert "/src/main.py" not in paths


def test_glob_under_delivery_emits_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """When glob post-filter reduces pool below top_k, a WARNING is logged."""
    doc_id = _doc_id()
    rows = [_make_row(doc_id, 0, source_path="/docs/file.md")]
    store = _make_store_with_mock_table(rows, tmp_path)

    f = SearchFilters(source_path_glob="/docs/*.md")
    with caplog.at_level(logging.WARNING, logger="archon"):
        asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", 10, filters=f))

    assert any("glob post-filter" in r.message for r in caplog.records)


def test_glob_star_matches_across_slashes(tmp_path: Path) -> None:
    """fnmatch.fnmatchcase with ** matches across directory separators."""
    import fnmatch
    assert fnmatch.fnmatchcase("/docs/a/b/file.md", "/docs/**/*.md")  # ** crosses slashes in fnmatch


def test_mixed_format_indexed_at_triggers_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Date filter on rows with non-fixed-width indexed_at emits WARNING."""
    doc_id = _doc_id()
    rows = [_make_row(doc_id, 0, indexed_at="2026-01-01T00:00:00")]  # no microseconds, no Z
    store = _make_store_with_mock_table(rows, tmp_path)

    f = SearchFilters(indexed_after=datetime(2025, 1, 1, tzinfo=timezone.utc))
    with caplog.at_level(logging.WARNING, logger="archon"):
        asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", 5, filters=f))

    assert any("legacy-format" in r.message for r in caplog.records)


def test_normalized_indexed_at_does_not_trigger_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Date filter on rows with fixed-width indexed_at does NOT emit WARNING."""
    doc_id = _doc_id()
    rows = [_make_row(doc_id, 0, indexed_at="2026-01-01T00:00:00.000000Z")]
    store = _make_store_with_mock_table(rows, tmp_path)

    f = SearchFilters(indexed_after=datetime(2025, 1, 1, tzinfo=timezone.utc))
    with caplog.at_level(logging.WARNING, logger="archon"):
        asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", 5, filters=f))

    assert not any("legacy-format" in r.message for r in caplog.records)


def test_language_field_populated_from_row(tmp_path: Path) -> None:
    """hybrid_search populates SearchResult.language from the row."""
    doc_id = _doc_id()
    rows = [_make_row(doc_id, 0, language="en")]
    store = _make_store_with_mock_table(rows, tmp_path)

    results = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", 5))
    assert len(results) == 1
    assert results[0].language == "en"


def test_language_field_none_when_not_in_row(tmp_path: Path) -> None:
    """hybrid_search sets language=None when row has no language."""
    doc_id = _doc_id()
    rows = [_make_row(doc_id, 0, language=None)]
    store = _make_store_with_mock_table(rows, tmp_path)

    results = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", 5))
    assert len(results) == 1
    assert results[0].language is None

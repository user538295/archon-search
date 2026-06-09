"""Tests for VectorStore.optimize_fts() — Task 2.1 of C6.

Unit tests use mocks; integration tests exercise real LanceDB disk I/O
and are therefore marked ``@pytest.mark.integration`` so they are excluded
from the default ``uv run pytest`` run.
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import ChunkRecord
from archon_search.store import FTSIndexNotFoundError, SearchStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIM = 4


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _chunk(doc_id: str, idx: int, text: str = "hello world") -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[float(idx + 1)] * _DIM,
        source_path=f"/tmp/{doc_id[:8]}.md",
        indexed_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Unit tests — mock LanceDB
# ---------------------------------------------------------------------------


def test_optimize_fts_validates_collection(tmp_path: Any) -> None:
    """optimize_fts() must raise ValueError for invalid collection names."""
    store = SearchStore(tmp_path / "db")

    async def _run() -> None:
        await store.optimize_fts("invalid name!")  # space + ! are not allowed

    with pytest.raises(ValueError, match="Invalid collection name"):
        asyncio.run(_run())


def test_optimize_fts_requires_connected_store(tmp_path: Any) -> None:
    """optimize_fts() must raise RuntimeError when store is not connected."""
    store = SearchStore(tmp_path / "db")

    async def _run() -> None:
        await store.optimize_fts("valid-collection")

    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(_run())


def test_optimize_fts_raises_when_no_fts_index(tmp_path: Any) -> None:
    """optimize_fts() must raise RuntimeError when the collection has no FTS index."""
    store = SearchStore(tmp_path / "db")

    mock_table = AsyncMock()
    mock_table.list_indices = AsyncMock(return_value=[])  # no FTS index

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    store._db = mock_db

    async def _run() -> None:
        await store.optimize_fts("my-collection")

    with pytest.raises(FTSIndexNotFoundError, match="no FTS index"):
        asyncio.run(_run())


def _make_mock_fts_index() -> Any:
    """Return a mock object that looks like an FTS index entry (index_type='FTS')."""
    idx = MagicMock()
    idx.index_type = "FTS"
    return idx


def test_optimize_fts_calls_table_optimize(tmp_path: Any) -> None:
    """optimize_fts() must await table.optimize() exactly once."""
    store = SearchStore(tmp_path / "db")

    mock_table = AsyncMock()
    mock_table.optimize = AsyncMock(return_value=None)
    mock_table.list_indices = AsyncMock(return_value=[_make_mock_fts_index()])

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    store._db = mock_db  # inject mock connection

    async def _run() -> None:
        await store.optimize_fts("my-collection")

    asyncio.run(_run())

    mock_table.optimize.assert_awaited_once()


def test_optimize_fts_opens_correct_table(tmp_path: Any) -> None:
    """optimize_fts() must open the table matching the collection name."""
    store = SearchStore(tmp_path / "db")

    mock_table = AsyncMock()
    mock_table.optimize = AsyncMock(return_value=None)
    mock_table.list_indices = AsyncMock(return_value=[_make_mock_fts_index()])

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    store._db = mock_db

    async def _run() -> None:
        await store.optimize_fts("my-special-collection")

    asyncio.run(_run())

    mock_db.open_table.assert_awaited_once_with("my-special-collection")


# ---------------------------------------------------------------------------
# Integration tests — real LanceDB disk I/O
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_optimize_fts_makes_new_chunks_searchable(tmp_path: Any) -> None:
    """After ingest_chunks (no FTS rebuild) + optimize_fts(), hybrid_search must find the new content."""
    store = SearchStore(tmp_path / "db")

    async def _run() -> None:
        await store.connect()
        try:
            col = f"test-{uuid.uuid4().hex[:8]}"
            await store.ensure_collection(col, embedding_dim=_DIM)

            # Build initial FTS index (empty, but present so optimize can run)
            await store.rebuild_fts_index(col)

            doc_id = _doc_id()
            chunks = [_chunk(doc_id, i, f"incremental fts test content {i}") for i in range(3)]

            # Ingest without rebuilding FTS — store.ingest_chunks always appends
            # rows; FTS is maintained separately via optimize_fts or rebuild_fts_index.
            await store.ingest_chunks(col, chunks)

            # Optimize to incorporate new rows into FTS
            await store.optimize_fts(col)

            # Now hybrid_search should find the new content
            query_vec = [1.0] * _DIM
            results = await store.hybrid_search(col, query_vec, "incremental fts test content", 10)
            returned_doc_ids = {r.doc_id for r in results}
            assert doc_id in returned_doc_ids, (
                f"Expected doc_id {doc_id!r} in FTS results after optimize_fts; got {returned_doc_ids!r}"
            )
        finally:
            await store.disconnect()

    asyncio.run(_run())

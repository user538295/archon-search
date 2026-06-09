"""Tests for FTS maintenance in VectorStore.delete_document() — Task 2.2 of C6.

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
from unittest.mock import AsyncMock, MagicMock, patch

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


def _make_store_with_mock_db(
    tmp_path: Any, count: int = 1
) -> tuple[SearchStore, AsyncMock, AsyncMock]:
    """Create a SearchStore with a mock DB and table injected.

    Returns (store, mock_db, mock_table).
    The mock table's count_rows returns *count*, delete is a no-op, and
    _do_fetch_doc_vectors_unlocked is patched to return an empty list so the
    complex query() chain is bypassed.
    """
    store = SearchStore(tmp_path / "db")

    mock_table = AsyncMock()
    mock_table.count_rows = AsyncMock(return_value=count)
    mock_table.delete = AsyncMock(return_value=None)

    mock_db = AsyncMock()
    mock_db.open_table = AsyncMock(return_value=mock_table)

    store._db = mock_db
    store._config = MagicMock()
    store._config.centroid_incremental_enabled = False

    return store, mock_db, mock_table


# ---------------------------------------------------------------------------
# Unit tests — mock LanceDB
# ---------------------------------------------------------------------------


def test_delete_document_calls_optimize_by_default(tmp_path: Any) -> None:
    """delete_document() must call optimize_fts() after deleting (default skip_fts_optimize=False)."""
    store, _, _ = _make_store_with_mock_db(tmp_path, count=1)

    doc_id = _doc_id()
    optimize_calls: list[str] = []

    async def mock_optimize_fts(collection: str) -> None:
        optimize_calls.append(collection)

    store.optimize_fts = mock_optimize_fts  # type: ignore[method-assign]

    async def _run() -> int:
        with patch.object(store, "_do_fetch_doc_vectors_unlocked", AsyncMock(return_value=[])):
            return await store.delete_document("my-collection", doc_id)

    result = asyncio.run(_run())

    assert result == 1
    assert optimize_calls == ["my-collection"], (
        f"Expected optimize_fts called once with 'my-collection'; got {optimize_calls!r}"
    )


def test_delete_document_skips_optimize_when_flag_set(tmp_path: Any) -> None:
    """delete_document(skip_fts_optimize=True) must NOT call optimize_fts()."""
    store, _, _ = _make_store_with_mock_db(tmp_path, count=1)

    doc_id = _doc_id()
    optimize_calls: list[str] = []

    async def mock_optimize_fts(collection: str) -> None:
        optimize_calls.append(collection)

    store.optimize_fts = mock_optimize_fts  # type: ignore[method-assign]

    async def _run() -> int:
        with patch.object(store, "_do_fetch_doc_vectors_unlocked", AsyncMock(return_value=[])):
            return await store.delete_document("my-collection", doc_id, skip_fts_optimize=True)

    result = asyncio.run(_run())

    assert result == 1
    assert optimize_calls == [], (
        f"Expected optimize_fts NOT called when skip_fts_optimize=True; got {optimize_calls!r}"
    )


def test_delete_document_skips_optimize_when_count_zero(tmp_path: Any) -> None:
    """delete_document() must NOT call optimize_fts() if the document was not present."""
    store, _, _ = _make_store_with_mock_db(tmp_path, count=0)

    doc_id = _doc_id()
    optimize_calls: list[str] = []

    async def mock_optimize_fts(collection: str) -> None:
        optimize_calls.append(collection)

    store.optimize_fts = mock_optimize_fts  # type: ignore[method-assign]

    async def _run() -> int:
        with patch.object(store, "_do_fetch_doc_vectors_unlocked", AsyncMock(return_value=[])):
            return await store.delete_document("my-collection", doc_id)

    result = asyncio.run(_run())

    assert result == 0
    assert optimize_calls == [], (
        f"Expected optimize_fts NOT called when doc not present; got {optimize_calls!r}"
    )


def test_delete_document_optimize_called_after_lock_release(tmp_path: Any) -> None:
    """optimize_fts() must be called AFTER the per-collection lock is released."""
    store, _, _ = _make_store_with_mock_db(tmp_path, count=1)

    doc_id = _doc_id()
    lock_was_released: list[bool] = []

    async def mock_optimize_fts(collection: str) -> None:
        # At call time the lock must already be released
        lock = store._collection_locks.get(collection)
        if lock is not None:
            lock_was_released.append(not lock.locked())
        else:
            lock_was_released.append(True)  # no lock entry at all — also released

    store.optimize_fts = mock_optimize_fts  # type: ignore[method-assign]

    async def _run() -> int:
        with patch.object(store, "_do_fetch_doc_vectors_unlocked", AsyncMock(return_value=[])):
            return await store.delete_document("my-collection", doc_id)

    asyncio.run(_run())

    assert lock_was_released == [True], (
        f"Lock should be released before optimize_fts is called; locked={lock_was_released!r}"
    )


def test_delete_document_calls_rebuild_when_plan_b_active(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Plan B is active (supports_incremental_fts_delete=False), delete_document must
    call rebuild_fts_index (not optimize_fts) after deleting."""
    import archon_search.store as store_module

    monkeypatch.setattr(store_module, "FTS_OPTIMIZE_REMOVES_DELETED", False)

    store, _, _ = _make_store_with_mock_db(tmp_path, count=1)

    doc_id = _doc_id()
    optimize_calls: list[str] = []
    rebuild_calls: list[str] = []

    async def mock_optimize_fts(collection: str) -> None:
        optimize_calls.append(collection)

    async def mock_rebuild_fts_index(collection: str, *, language: str = "") -> None:
        rebuild_calls.append(collection)

    async def mock_get_dominant_language(collection: str) -> str:
        return "en"

    store.optimize_fts = mock_optimize_fts  # type: ignore[method-assign]
    store.rebuild_fts_index = mock_rebuild_fts_index  # type: ignore[method-assign]
    store.get_dominant_language = mock_get_dominant_language  # type: ignore[method-assign]

    async def _run() -> int:
        with patch.object(store, "_do_fetch_doc_vectors_unlocked", AsyncMock(return_value=[])):
            return await store.delete_document("my-collection", doc_id)

    result = asyncio.run(_run())

    assert result == 1
    assert optimize_calls == [], (
        f"optimize_fts must NOT be called under Plan B; got {optimize_calls!r}"
    )
    assert rebuild_calls == ["my-collection"], (
        f"rebuild_fts_index must be called under Plan B; got {rebuild_calls!r}"
    )


def test_delete_document_does_not_raise_when_no_fts_index(tmp_path: Any) -> None:
    """delete_document() must silently skip FTS maintenance when no FTS index exists.

    If ``optimize_fts`` raises ``FTSIndexNotFoundError`` (no FTS index present), the
    delete must still complete successfully — no phantom hits are possible if FTS
    was never created.
    """
    store, _, _ = _make_store_with_mock_db(tmp_path, count=1)

    doc_id = _doc_id()

    async def mock_optimize_fts_no_index(collection: str) -> None:
        raise FTSIndexNotFoundError(f"optimize_fts: collection {collection!r} has no FTS index")

    store.optimize_fts = mock_optimize_fts_no_index  # type: ignore[method-assign]

    async def _run() -> int:
        with patch.object(store, "_do_fetch_doc_vectors_unlocked", AsyncMock(return_value=[])):
            return await store.delete_document("my-collection", doc_id)

    # Must NOT raise — FTSIndexNotFoundError is silently swallowed
    result = asyncio.run(_run())
    assert result == 1, "delete_document must return the deleted chunk count even without FTS"


def test_delete_document_exception_before_count_does_not_raise_unbound(tmp_path: Any) -> None:
    """If an exception is raised before count_rows, UnboundLocalError must NOT occur.

    Ensures ``count`` is initialized to 0 before the try block so that
    FTS maintenance guard (`if count > 0`) never sees an unbound name.
    """
    store, mock_db, mock_table = _make_store_with_mock_db(tmp_path, count=1)

    # Simulate a DB error before count_rows is reached
    mock_table.count_rows = AsyncMock(side_effect=RuntimeError("simulated DB error"))

    doc_id = _doc_id()

    async def _run() -> None:
        with patch.object(store, "_do_fetch_doc_vectors_unlocked", AsyncMock(return_value=[])):
            await store.delete_document("my-collection", doc_id)

    # Should raise the DB error, NOT UnboundLocalError
    with pytest.raises(RuntimeError, match="simulated DB error"):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Integration tests — real LanceDB disk I/O
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_delete_document_removes_from_fts(tmp_path: Any) -> None:
    """After delete_document(), hybrid_search must not return the deleted document."""
    store = SearchStore(tmp_path / "db")

    async def _run() -> None:
        await store.connect()
        try:
            col = f"test-{uuid.uuid4().hex[:8]}"
            await store.ensure_collection(col, embedding_dim=_DIM)

            doc_id = _doc_id()
            unique_word = f"deleteme{uuid.uuid4().hex[:8]}"
            chunks = [_chunk(doc_id, i, f"{unique_word} content {i}") for i in range(3)]

            # Ingest, then rebuild FTS so the document is searchable
            await store.ingest_chunks(col, chunks)
            await store.rebuild_fts_index(col)

            # Verify the document is searchable before delete
            query_vec = [1.0] * _DIM
            results_before = await store.hybrid_search(col, query_vec, unique_word, 10)
            assert any(r.doc_id == doc_id for r in results_before), (
                "Document should be searchable before delete"
            )

            # Delete — default skip_fts_optimize=False means FTS is maintained
            count = await store.delete_document(col, doc_id)
            assert count == 3

            # After delete + optimize, FTS should not return the deleted document
            results_after = await store.hybrid_search(col, query_vec, unique_word, 10)
            assert not any(r.doc_id == doc_id for r in results_after), (
                f"Phantom hit after delete_document: doc_id {doc_id!r} still in FTS results"
            )
        finally:
            await store.disconnect()

    asyncio.run(_run())


@pytest.mark.integration
def test_delete_by_source_path_also_removes_from_fts(tmp_path: Any) -> None:
    """delete_by_source_path() inherits FTS maintenance via delegation to delete_document()."""
    store = SearchStore(tmp_path / "db")

    async def _run() -> None:
        await store.connect()
        try:
            col = f"test-{uuid.uuid4().hex[:8]}"
            await store.ensure_collection(col, embedding_dim=_DIM)

            # Use a resolved path so the doc_id matches what delete_by_source_path
            # computes via Path(source_path).resolve() (on macOS /tmp → /private/tmp).
            source_path = str((tmp_path / f"testfile-{uuid.uuid4().hex[:8]}.md").resolve())
            doc_id = hashlib.sha256(source_path.encode()).hexdigest()
            unique_word = f"bypath{uuid.uuid4().hex[:8]}"
            chunks = [_chunk(doc_id, i, f"{unique_word} content {i}") for i in range(2)]
            # Override source_path to match what delete_by_source_path will compute
            for c in chunks:
                object.__setattr__(c, "source_path", source_path)

            await store.ingest_chunks(col, chunks)
            await store.rebuild_fts_index(col)

            query_vec = [1.0] * _DIM
            results_before = await store.hybrid_search(col, query_vec, unique_word, 10)
            assert any(r.doc_id == doc_id for r in results_before), (
                "Document should be searchable before delete_by_source_path"
            )

            count = await store.delete_by_source_path(col, source_path)
            assert count == 2

            results_after = await store.hybrid_search(col, query_vec, unique_word, 10)
            assert not any(r.doc_id == doc_id for r in results_after), (
                f"Phantom hit after delete_by_source_path: doc_id {doc_id!r} still in FTS results"
            )
        finally:
            await store.disconnect()

    asyncio.run(_run())

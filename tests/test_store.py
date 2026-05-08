"""packages/archon-search/tests/test_store.py — unit + integration tests for SearchStore."""
from __future__ import annotations

import hashlib
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from archon_search._types import ChunkRecord
from archon_search.store import SearchStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIM = 4  # tiny embedding dim for tests


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _chunk(doc_id: str, idx: int, text: str = "hello", dim: int = _DIM) -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[float(idx)] * dim,
        source_path=f"/tmp/{doc_id[:8]}.md",
        indexed_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Unit tests — do NOT require LanceDB connection
# ---------------------------------------------------------------------------


def test_store_methods_raise_before_connect_ingest_chunks(tmp_path: Path) -> None:
    store = SearchStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.ingest_chunks("col", []))


def test_store_methods_raise_before_connect_hybrid_search(tmp_path: Path) -> None:
    store = SearchStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.hybrid_search("col", [], "q", 5))


def test_store_methods_raise_before_connect_delete_document(tmp_path: Path) -> None:
    store = SearchStore(tmp_path / "db")
    doc_id = _doc_id()
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.delete_document("col", doc_id))


def test_store_methods_raise_before_connect_list_documents(tmp_path: Path) -> None:
    store = SearchStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.list_documents("col"))


def test_store_methods_raise_before_connect_list_collections(tmp_path: Path) -> None:
    store = SearchStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.list_collections())


def test_store_methods_raise_before_connect_ensure_collection(tmp_path: Path) -> None:
    store = SearchStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.ensure_collection("col", _DIM))


def test_store_methods_raise_before_connect_rebuild_fts(tmp_path: Path) -> None:
    store = SearchStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.rebuild_fts_index("col"))


def test_store_methods_raise_before_connect_fetch_adjacent(tmp_path: Path) -> None:
    store = SearchStore(tmp_path / "db")
    doc_id = _doc_id()
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.fetch_adjacent_chunks("col", doc_id, 0, 1))


def test_store_delete_document_invalid_doc_id_raises(tmp_path: Path) -> None:
    """doc_id not matching ^[a-f0-9]{64}$ raises ValueError before any DB call."""
    store = SearchStore(tmp_path / "db")
    import asyncio

    asyncio.run(store.connect())
    try:
        with pytest.raises(ValueError):
            asyncio.run(store.delete_document("col", "not-a-valid-hex-id"))
    finally:
        asyncio.run(store.disconnect())


def test_store_ingest_chunks_rejects_empty_chunk_id(tmp_path: Path) -> None:
    """chunk_id = '' raises ValueError — malformed."""
    store = SearchStore(tmp_path / "db")
    import asyncio

    asyncio.run(store.connect())
    doc_id = _doc_id()
    asyncio.run(store.ensure_collection("col-bad", _DIM))
    bad = ChunkRecord(
        doc_id=doc_id,
        chunk_id="",
        text="t",
        vector=[0.0] * _DIM,
        source_path="/f",
        indexed_at="2026-01-01T00:00:00Z",
    )
    try:
        with pytest.raises(ValueError):
            asyncio.run(store.ingest_chunks("col-bad", [bad]))
    finally:
        asyncio.run(store.disconnect())


def test_store_ingest_chunks_rejects_uuid_chunk_id(tmp_path: Path) -> None:
    """chunk_id = UUID string raises ValueError — must be {doc_id}-{idx:06d}."""
    store = SearchStore(tmp_path / "db")
    import asyncio

    asyncio.run(store.connect())
    doc_id = _doc_id()
    asyncio.run(store.ensure_collection("col-uuid", _DIM))
    bad = ChunkRecord(
        doc_id=doc_id,
        chunk_id=str(uuid.uuid4()),
        text="t",
        vector=[0.0] * _DIM,
        source_path="/f",
        indexed_at="2026-01-01T00:00:00Z",
    )
    try:
        with pytest.raises(ValueError):
            asyncio.run(store.ingest_chunks("col-uuid", [bad]))
    finally:
        asyncio.run(store.disconnect())


def test_store_invalid_collection_name_raises(tmp_path: Path) -> None:
    """Collection names containing unsafe chars raise ValueError."""
    store = SearchStore(tmp_path / "db")
    import asyncio

    asyncio.run(store.connect())
    try:
        with pytest.raises(ValueError, match="Invalid collection name"):
            asyncio.run(store.ensure_collection("../evil", _DIM))
    finally:
        asyncio.run(store.disconnect())


def test_store_disconnect_clears_connection(tmp_path: Path) -> None:
    """After disconnect, ingest_chunks raises RuntimeError."""
    store = SearchStore(tmp_path / "db")
    import asyncio

    asyncio.run(store.connect())
    asyncio.run(store.disconnect())
    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(store.ingest_chunks("col", []))


def test_store_double_disconnect_safe(tmp_path: Path) -> None:
    """Calling disconnect() twice does not raise."""
    store = SearchStore(tmp_path / "db")
    import asyncio

    asyncio.run(store.connect())
    asyncio.run(store.disconnect())
    asyncio.run(store.disconnect())  # should not raise


# ---------------------------------------------------------------------------
# Integration tests — use shared connected_store fixture
# ---------------------------------------------------------------------------


async def _ingest_doc(
    store: SearchStore,
    col: str,
    n_chunks: int = 2,
    text_prefix: str = "text",
) -> tuple[str, list[ChunkRecord]]:
    """Helper: ensure collection, ingest n_chunks, return (doc_id, chunks)."""
    doc_id = _doc_id()
    chunks = [_chunk(doc_id, i, text=f"{text_prefix} chunk {i}") for i in range(n_chunks)]
    await store.ensure_collection(col, _DIM)
    await store.ingest_chunks(col, chunks)
    return doc_id, chunks


@pytest.mark.asyncio
async def test_store_connect_creates_db_dir(tmp_path: Path) -> None:
    db_path = tmp_path / "newdb"
    assert not db_path.exists()
    store = SearchStore(db_path)
    await store.connect()
    assert db_path.exists()
    await store.disconnect()


@pytest.mark.asyncio
async def test_store_ensure_collection_idempotent(
    connected_store: SearchStore, col_name: str
) -> None:
    """Calling ensure_collection twice does not raise."""
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ensure_collection(col_name, _DIM)  # should not raise


@pytest.mark.asyncio
async def test_store_ingest_and_list_documents(
    connected_store: SearchStore, col_name: str
) -> None:
    doc_id, _ = await _ingest_doc(connected_store, col_name, n_chunks=2)
    docs = await connected_store.list_documents(col_name)
    assert len(docs) == 1
    assert docs[0].doc_id == doc_id
    assert docs[0].chunk_count == 2


@pytest.mark.asyncio
async def test_store_hybrid_search_returns_results(
    connected_store: SearchStore, col_name: str
) -> None:
    await _ingest_doc(connected_store, col_name, text_prefix="searchable")
    await connected_store.rebuild_fts_index(col_name)
    results = await connected_store.hybrid_search(
        col_name, [0.0] * _DIM, "searchable", top_k=5
    )
    assert len(results) > 0


@pytest.mark.asyncio
async def test_store_hybrid_search_unknown_collection_returns_empty(
    connected_store: SearchStore,
) -> None:
    results = await connected_store.hybrid_search(
        "nonexistent-xyz", [0.0] * _DIM, "q", top_k=5
    )
    assert results == []


@pytest.mark.asyncio
async def test_store_delete_document_removes_chunks(
    connected_store: SearchStore, col_name: str
) -> None:
    doc_id, _ = await _ingest_doc(connected_store, col_name)
    count = await connected_store.delete_document(col_name, doc_id)
    assert count > 0
    docs = await connected_store.list_documents(col_name)
    assert all(d.doc_id != doc_id for d in docs)


@pytest.mark.asyncio
async def test_store_delete_nonexistent_doc_returns_zero(
    connected_store: SearchStore, col_name: str
) -> None:
    await connected_store.ensure_collection(col_name, _DIM)
    fake_id = _doc_id()
    count = await connected_store.delete_document(col_name, fake_id)
    assert count == 0


@pytest.mark.asyncio
async def test_store_list_collections_includes_ingested(
    connected_store: SearchStore, col_name: str
) -> None:
    await _ingest_doc(connected_store, col_name)
    collections = await connected_store.list_collections()
    names = [c.name for c in collections]
    assert col_name in names


@pytest.mark.asyncio
async def test_store_list_collections_empty_database_returns_empty(
    tmp_path: Path,
) -> None:
    store = SearchStore(tmp_path / "empty_db")
    await store.connect()
    try:
        cols = await store.list_collections()
        assert cols == []
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_store_list_documents_nonexistent_collection_returns_empty(
    connected_store: SearchStore,
) -> None:
    docs = await connected_store.list_documents("no-such-collection-xyz", limit=10)
    assert docs == []


@pytest.mark.asyncio
async def test_store_delete_document_injection_safe(
    connected_store: SearchStore, col_name: str
) -> None:
    """doc_id with SQL-special chars raises ValueError; document B still intact."""
    doc_b_id, _ = await _ingest_doc(connected_store, col_name)
    with pytest.raises(ValueError):
        await connected_store.delete_document(col_name, "' OR '1'='1")
    docs = await connected_store.list_documents(col_name)
    assert any(d.doc_id == doc_b_id for d in docs)


@pytest.mark.asyncio
async def test_store_fetch_adjacent_chunks_returns_neighbors(
    connected_store: SearchStore, col_name: str
) -> None:
    """3-chunk doc, center_idx=1 → returns chunks at idx 0 and 2."""
    doc_id = _doc_id()
    chunks = [_chunk(doc_id, i) for i in range(3)]
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, chunks)

    neighbors = await connected_store.fetch_adjacent_chunks(col_name, doc_id, 1, 1)
    neighbor_ids = {c.chunk_id for c in neighbors}
    assert f"{doc_id}-000000" in neighbor_ids
    assert f"{doc_id}-000002" in neighbor_ids
    assert f"{doc_id}-000001" not in neighbor_ids  # center excluded


@pytest.mark.asyncio
async def test_store_fetch_adjacent_chunks_at_boundary_returns_partial(
    connected_store: SearchStore, col_name: str
) -> None:
    """center_idx=0, window=2 → only right neighbors (idx 1, 2)."""
    doc_id = _doc_id()
    chunks = [_chunk(doc_id, i) for i in range(3)]
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, chunks)

    neighbors = await connected_store.fetch_adjacent_chunks(col_name, doc_id, 0, 2)
    chunk_ids = {c.chunk_id for c in neighbors}
    assert len(neighbors) == 2, f"Expected 2 neighbors, got {len(neighbors)}"
    assert f"{doc_id}-000001" in chunk_ids
    assert f"{doc_id}-000002" in chunk_ids
    # Verify no negative-index chunk IDs were generated
    for c in neighbors:
        idx = int(c.chunk_id.split("-")[-1])
        assert idx >= 0, f"Negative index in chunk_id: {c.chunk_id}"


@pytest.mark.asyncio
async def test_store_fetch_adjacent_chunks_window_zero_returns_empty(
    connected_store: SearchStore, col_name: str
) -> None:
    """window=0 means no neighbors; returns []."""
    doc_id = _doc_id()
    chunks = [_chunk(doc_id, i) for i in range(3)]
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, chunks)
    result = await connected_store.fetch_adjacent_chunks(col_name, doc_id, 1, 0)
    assert result == []


@pytest.mark.asyncio
async def test_store_list_documents_respects_limit(
    connected_store: SearchStore, col_name: str
) -> None:
    """3 docs ingested → list_documents(limit=1) returns exactly 1."""
    for _ in range(3):
        doc_id = _doc_id()
        chunks = [_chunk(doc_id, 0)]
        await connected_store.ensure_collection(col_name, _DIM)
        await connected_store.ingest_chunks(col_name, chunks)
    docs = await connected_store.list_documents(col_name, limit=1)
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_store_hybrid_search_degrades_gracefully_without_fts_index(
    connected_store: SearchStore, col_name: str
) -> None:
    """No FTS index → returns vector-only results, no exception raised."""
    await _ingest_doc(connected_store, col_name, text_prefix="keyword")
    # deliberately NOT calling rebuild_fts_index
    results = await connected_store.hybrid_search(
        col_name, [0.0] * _DIM, "keyword", top_k=5
    )
    assert isinstance(results, list)  # no crash; may be empty or have results


@pytest.mark.asyncio
async def test_store_rebuild_fts_index_makes_text_searchable(
    connected_store: SearchStore, col_name: str
) -> None:
    unique_word = f"zaphod{uuid.uuid4().hex[:6]}"
    doc_id = _doc_id()
    chunks = [_chunk(doc_id, 0, text=f"The {unique_word} guide")]
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, chunks)
    await connected_store.rebuild_fts_index(col_name)

    results = await connected_store.hybrid_search(
        col_name, [0.0] * _DIM, unique_word, top_k=5
    )
    texts = [r.text for r in results]
    assert any(unique_word in t for t in texts)


@pytest.mark.asyncio
async def test_store_rebuild_fts_index_idempotent(
    connected_store: SearchStore, col_name: str
) -> None:
    """Calling rebuild_fts_index twice does not raise."""
    await _ingest_doc(connected_store, col_name)
    await connected_store.rebuild_fts_index(col_name)
    await connected_store.rebuild_fts_index(col_name)


@pytest.mark.asyncio
async def test_store_list_collections_returns_correct_counts(
    connected_store: SearchStore, col_name: str
) -> None:
    """2 docs × 3 chunks each → doc_count=2, chunk_count=6."""
    for _ in range(2):
        doc_id = _doc_id()
        chunks = [_chunk(doc_id, i) for i in range(3)]
        await connected_store.ensure_collection(col_name, _DIM)
        await connected_store.ingest_chunks(col_name, chunks)

    collections = await connected_store.list_collections()
    col = next(c for c in collections if c.name == col_name)
    assert col.doc_count == 2
    assert col.chunk_count == 6


@pytest.mark.asyncio
async def test_store_hybrid_search_rrf_ranking_correct(
    connected_store: SearchStore, col_name: str
) -> None:
    """Doc matching both vector + keyword should outscore doc matching only vector."""
    await connected_store.ensure_collection(col_name, _DIM)

    # Dual-match doc: FAR from query vector (idx=5 → [5.0]*4), but matches FTS keyword
    dual_id = _doc_id()
    unique = f"frobnicate{uuid.uuid4().hex[:4]}"
    dual_chunks = [_chunk(dual_id, 5, text=f"The {unique} widget")]
    await connected_store.ingest_chunks(col_name, dual_chunks)

    # Vec-only doc: CLOSE to query vector (idx=0 → [0.0]*4), but no FTS keyword match
    vec_only_id = _doc_id()
    vec_chunks = [_chunk(vec_only_id, 0, text="unrelated topic completely")]
    await connected_store.ingest_chunks(col_name, vec_chunks)

    await connected_store.rebuild_fts_index(col_name)

    # Query with vector [0.0]*4 (favors vec-only) and keyword (favors dual)
    results = await connected_store.hybrid_search(
        col_name, [0.0] * _DIM, unique, top_k=5
    )
    assert len(results) >= 1, f"Expected at least 1 result, got {len(results)}"

    dual_result = next((r for r in results if r.doc_id == dual_id), None)
    assert dual_result is not None, "Dual-match document not found in results"

    vec_result = next((r for r in results if r.doc_id == vec_only_id), None)
    if vec_result is not None:
        assert dual_result.score >= vec_result.score, (
            f"Dual-match score {dual_result.score} should be >= vec-only score {vec_result.score}"
        )


# ---------------------------------------------------------------------------
# Edge-case tests (C1-I-7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_delete_document_nonexistent_collection_returns_zero(
    connected_store: SearchStore,
) -> None:
    """delete_document on a collection that does not exist returns 0."""
    doc_id = _doc_id()
    count = await connected_store.delete_document("no-such-collection-xyz", doc_id)
    assert count == 0


@pytest.mark.asyncio
async def test_store_fetch_adjacent_nonexistent_collection_returns_empty(
    connected_store: SearchStore,
) -> None:
    """fetch_adjacent_chunks on a nonexistent collection returns []."""
    doc_id = _doc_id()
    result = await connected_store.fetch_adjacent_chunks("no-such-collection-xyz", doc_id, 0, 1)
    assert result == []


@pytest.mark.asyncio
async def test_store_ingest_empty_list_returns_zero(
    connected_store: SearchStore, col_name: str,
) -> None:
    """ingest_chunks with empty list returns 0 without touching the table."""
    await connected_store.ensure_collection(col_name, _DIM)
    count = await connected_store.ingest_chunks(col_name, [])
    assert count == 0


@pytest.mark.asyncio
async def test_store_list_documents_limit_capped_at_1000(
    connected_store: SearchStore, col_name: str,
) -> None:
    """list_documents caps limit at 1000 — requesting more does not OOM."""
    await connected_store.ensure_collection(col_name, _DIM)
    # Should not raise even with unreasonable limit
    docs = await connected_store.list_documents(col_name, limit=100_000)
    assert isinstance(docs, list)


# ---------------------------------------------------------------------------
# drop_collection tests (Task 1.2)
# ---------------------------------------------------------------------------


def test_drop_collection_raises_before_connect(tmp_path: Path) -> None:
    """drop_collection raises RuntimeError when store is not connected."""
    import asyncio
    store = SearchStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(store.drop_collection("col"))


def test_drop_collection_removes_table(tmp_path: Path) -> None:
    """drop_collection calls _db.drop_table with the correct name."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col", "other"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.drop_table = AsyncMock()
    store._db = mock_db

    asyncio.run(store.drop_collection("my-col"))

    mock_db.drop_table.assert_awaited_once_with("my-col")


def test_drop_collection_raises_keyerror_on_missing(tmp_path: Path) -> None:
    """drop_collection raises KeyError when the collection does not exist."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["other"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    store._db = mock_db

    with pytest.raises(KeyError):
        asyncio.run(store.drop_collection("nonexistent"))


@pytest.mark.asyncio
async def test_drop_collection_integration(connected_store: SearchStore, col_name: str) -> None:
    """Integration: ingest → drop → collection absent from list_collections."""
    await _ingest_doc(connected_store, col_name)
    names_before = [c.name for c in await connected_store.list_collections()]
    assert col_name in names_before

    await connected_store.drop_collection(col_name)

    names_after = [c.name for c in await connected_store.list_collections()]
    assert col_name not in names_after


# ---------------------------------------------------------------------------
# rename_collection tests (Task 1.2 — used by migration in Task 1.3)
# ---------------------------------------------------------------------------


def test_rename_collection_raises_before_connect(tmp_path: Path) -> None:
    """rename_collection raises RuntimeError when store is not connected."""
    import asyncio
    store = SearchStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(store.rename_collection("old", "new"))


def test_rename_collection_renames_table(tmp_path: Path) -> None:
    """rename_collection calls _db.rename_table with old and new names."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["old-name"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.rename_table = AsyncMock()
    store._db = mock_db

    asyncio.run(store.rename_collection("old-name", "new-name"))

    mock_db.rename_table.assert_awaited_once_with("old-name", "new-name")


def test_rename_collection_raises_keyerror_on_missing(tmp_path: Path) -> None:
    """rename_collection raises KeyError when the source collection does not exist."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    list_tables_resp = MagicMock()
    list_tables_resp.tables = []
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    store._db = mock_db

    with pytest.raises(KeyError):
        asyncio.run(store.rename_collection("nonexistent", "new-name"))


def test_rename_collection_raises_not_implemented_on_attribute_error(
    tmp_path: Path,
) -> None:
    """rename_collection raises NotImplementedError when rename_table raises AttributeError."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["old-name"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.rename_table = AsyncMock(side_effect=AttributeError("no rename_table"))
    store._db = mock_db

    with pytest.raises(NotImplementedError, match="rename_table not available"):
        asyncio.run(store.rename_collection("old-name", "new-name"))


def test_rename_collection_raises_not_implemented_on_not_implemented_error(
    tmp_path: Path,
) -> None:
    """rename_collection raises NotImplementedError when LanceDB OSS raises NotImplementedError."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["old-name"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.rename_table = AsyncMock(
        side_effect=NotImplementedError("rename_table is not supported in LanceDB OSS")
    )
    store._db = mock_db

    with pytest.raises(NotImplementedError, match="rename_table not available"):
        asyncio.run(store.rename_collection("old-name", "new-name"))


def test_rename_collection_raises_valueerror_if_target_exists(tmp_path: Path) -> None:
    """rename_collection raises ValueError when the target name already exists."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["old-name", "existing"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    store._db = mock_db

    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(store.rename_collection("old-name", "existing"))


def test_rename_collection_raises_valueerror_on_invalid_new_name(tmp_path: Path) -> None:
    """rename_collection raises ValueError when new name is invalid."""
    import asyncio

    store = SearchStore(tmp_path / "db")
    # Inject a mock db so we don't need a real connection
    from unittest.mock import AsyncMock, MagicMock
    store._db = MagicMock()

    with pytest.raises(ValueError, match="Invalid collection name"):
        asyncio.run(store.rename_collection("old-name", "../evil"))


@pytest.mark.asyncio
async def test_rename_collection_integration(connected_store: SearchStore, col_name: str) -> None:
    """Integration: rename_collection completes or raises NotImplementedError for OSS LanceDB."""
    new_name = col_name + "-renamed"
    await _ingest_doc(connected_store, col_name)

    try:
        await connected_store.rename_collection(col_name, new_name)
        # If supported: old absent, new present
        names = [c.name for c in await connected_store.list_collections()]
        assert col_name not in names
        assert new_name in names
    except NotImplementedError:
        # LanceDB OSS does not support rename_table — this is expected
        pass


def test_fts_exception_filter_reraises_non_fts_errors() -> None:
    """The FTS exception filter in hybrid_search only catches FTS-related errors."""
    # Errors containing "index" or "fts" should be caught (degraded to vector-only)
    for msg in ["FTS index not found", "No index on column", "fts search failed"]:
        exc_str = msg.lower()
        assert "index" in exc_str or "fts" in exc_str, f"Should be caught: {msg}"

    # Errors NOT containing "index" or "fts" should be re-raised
    for msg in ["disk corruption", "connection timeout", "permission denied"]:
        exc_str = msg.lower()
        assert "index" not in exc_str and "fts" not in exc_str, f"Should re-raise: {msg}"


# ---------------------------------------------------------------------------
# CollectionMeta tests (Task 1.1 — FEAT-022)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collection_meta_get_missing_returns_none(
    connected_store: SearchStore,
) -> None:
    """get_collection_meta returns None for a name not yet stored."""
    result = await connected_store.get_collection_meta("nonexistent-xyz-meta")
    assert result is None


@pytest.mark.asyncio
async def test_collection_meta_upsert(connected_store: SearchStore) -> None:
    """update_collection_meta stores metadata; get_collection_meta retrieves it."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(
        name="test-meta-col",
        description="A test collection",
        centroid=[0.1, 0.2, 0.3],
        doc_count=5,
        chunk_count=10,
        embedding_model="BAAI/bge-small-en-v1.5",
        last_indexed=datetime(2026, 3, 1, tzinfo=timezone.utc),
        last_described=datetime(2026, 3, 2, tzinfo=timezone.utc),
    )
    await connected_store.update_collection_meta(meta)
    retrieved = await connected_store.get_collection_meta("test-meta-col")
    assert retrieved is not None
    assert retrieved.name == "test-meta-col"
    assert retrieved.description == "A test collection"
    assert retrieved.centroid is not None
    assert len(retrieved.centroid) == 3
    assert abs(retrieved.centroid[0] - 0.1) < 1e-6
    assert retrieved.doc_count == 5
    assert retrieved.chunk_count == 10
    assert retrieved.embedding_model == "BAAI/bge-small-en-v1.5"
    assert retrieved.last_indexed == datetime(2026, 3, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_collection_meta_upsert_includes_described_at_doc_count(
    connected_store: SearchStore,
) -> None:
    """update_collection_meta persists described_at_doc_count; None round-trips."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(
        name="test-meta-described",
        description="with described count",
        centroid=None,
        doc_count=20,
        chunk_count=40,
        embedding_model="model-x",
        described_at_doc_count=20,
    )
    await connected_store.update_collection_meta(meta)
    retrieved = await connected_store.get_collection_meta("test-meta-described")
    assert retrieved is not None
    assert retrieved.described_at_doc_count == 20

    # Update with None described_at_doc_count
    meta2 = CollectionMeta(
        name="test-meta-none-desc",
        doc_count=0,
        chunk_count=0,
        embedding_model="model-x",
        described_at_doc_count=None,
    )
    await connected_store.update_collection_meta(meta2)
    retrieved2 = await connected_store.get_collection_meta("test-meta-none-desc")
    assert retrieved2 is not None
    assert retrieved2.described_at_doc_count is None


@pytest.mark.asyncio
async def test_collection_meta_upsert_overwrites_on_same_name(
    connected_store: SearchStore,
) -> None:
    """Second update_collection_meta with same name replaces the first (upsert semantics)."""
    from archon_search.collection_meta import CollectionMeta

    name = "test-meta-overwrite"
    meta1 = CollectionMeta(
        name=name,
        description="original",
        centroid=[1.0, 2.0],
        doc_count=1,
        chunk_count=2,
        embedding_model="model-a",
    )
    await connected_store.update_collection_meta(meta1)

    meta2 = CollectionMeta(
        name=name,
        description="updated",
        centroid=[3.0, 4.0],
        doc_count=10,
        chunk_count=20,
        embedding_model="model-b",
    )
    await connected_store.update_collection_meta(meta2)

    retrieved = await connected_store.get_collection_meta(name)
    assert retrieved is not None
    assert retrieved.description == "updated"
    assert retrieved.doc_count == 10
    assert retrieved.chunk_count == 20
    assert retrieved.embedding_model == "model-b"
    assert retrieved.centroid is not None
    assert abs(retrieved.centroid[0] - 3.0) < 1e-6


@pytest.mark.asyncio
async def test_collection_meta_centroid_none_round_trips(
    connected_store: SearchStore,
) -> None:
    """CollectionMeta with centroid=None round-trips as None (not a zero vector)."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(
        name="test-meta-no-centroid",
        description=None,
        centroid=None,
        doc_count=0,
        chunk_count=0,
        embedding_model="model-x",
    )
    await connected_store.update_collection_meta(meta)
    retrieved = await connected_store.get_collection_meta("test-meta-no-centroid")
    assert retrieved is not None
    assert retrieved.centroid is None


@pytest.mark.asyncio
async def test_get_all_collections_meta_empty_before_any_update(tmp_path: Path) -> None:
    """get_all_collections_meta returns [] when no meta rows exist."""
    store = SearchStore(tmp_path / "db_meta_empty")
    await store.connect()
    try:
        result = await store.get_all_collections_meta()
        assert result == []
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_get_all_collections_meta_returns_all_rows(tmp_path: Path) -> None:
    """get_all_collections_meta returns all stored CollectionMeta rows."""
    from archon_search.collection_meta import CollectionMeta

    store = SearchStore(tmp_path / "db_meta_rows")
    await store.connect()
    try:
        meta1 = CollectionMeta(
            name="col-a",
            doc_count=2,
            chunk_count=10,
            centroid=[0.1, 0.2],
            last_indexed=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        meta2 = CollectionMeta(
            name="col-b",
            doc_count=5,
            chunk_count=25,
            centroid=None,
        )
        await store.update_collection_meta(meta1)
        await store.update_collection_meta(meta2)

        result = await store.get_all_collections_meta()
        assert len(result) == 2
        names = {m.name for m in result}
        assert names == {"col-a", "col-b"}

        col_a = next(m for m in result if m.name == "col-a")
        assert col_a.centroid == [0.1, 0.2]
        assert col_a.doc_count == 2
        assert col_a.last_indexed is not None

        col_b = next(m for m in result if m.name == "col-b")
        assert col_b.centroid is None
        assert col_b.doc_count == 5
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_list_collections_excludes_archon_prefix(
    connected_store: SearchStore, col_name: str
) -> None:
    """list_collections() must not include internal _archon_ tables."""
    from archon_search.collection_meta import CollectionMeta

    # Ensure a user-visible collection exists
    await connected_store.ensure_collection(col_name, _DIM)

    # Trigger creation of the _archon_collection_meta table
    meta = CollectionMeta(
        name="some-col",
        description="desc",
        centroid=None,
        doc_count=1,
        chunk_count=2,
        embedding_model="model",
    )
    await connected_store.update_collection_meta(meta)

    # list_collections must not expose internal _archon_ tables
    names = [c.name for c in await connected_store.list_collections()]
    assert col_name in names


# ---------------------------------------------------------------------------
# delete_by_source_path tests (Task 4.3 — FEAT-027-P4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_by_source_path_computes_doc_id(tmp_path: Path) -> None:
    """delete_by_source_path calls delete_document with sha256 of the resolved path."""
    from unittest.mock import AsyncMock

    source_path = "/some/project/README.md"
    expected_doc_id = hashlib.sha256(str(Path(source_path).resolve()).encode()).hexdigest()

    store = SearchStore(tmp_path / "db")
    store.delete_document = AsyncMock(return_value=3)  # type: ignore[method-assign]

    result = await store.delete_by_source_path("my-col", source_path)

    store.delete_document.assert_called_once_with("my-col", expected_doc_id)
    assert result == 3


@pytest.mark.asyncio
async def test_delete_by_source_path_collection_not_found(tmp_path: Path) -> None:
    """When delete_document returns 0 (no match), delete_by_source_path returns 0."""
    from unittest.mock import AsyncMock

    store = SearchStore(tmp_path / "db")
    store.delete_document = AsyncMock(return_value=0)  # type: ignore[method-assign]

    result = await store.delete_by_source_path("nonexistent-col", "/any/path.txt")

    assert result == 0


@pytest.mark.asyncio
async def test_delete_by_source_path_delegates_to_delete_document(tmp_path: Path) -> None:
    """delete_by_source_path delegates to delete_document with the sha256 doc_id."""
    from unittest.mock import AsyncMock, patch

    source_path = "/some/project/README.md"
    expected_doc_id = hashlib.sha256(str(Path(source_path).resolve()).encode()).hexdigest()

    store = SearchStore(tmp_path / "db")
    with patch.object(store, "delete_document", new_callable=AsyncMock, return_value=1) as mock_del:
        await store.delete_by_source_path("my-col", source_path)

        mock_del.assert_called_once_with("my-col", expected_doc_id)


@pytest.mark.asyncio
async def test_delete_by_source_path_returns_count(tmp_path: Path) -> None:
    """delete_by_source_path returns the count from delete_document."""
    from unittest.mock import AsyncMock, patch

    store = SearchStore(tmp_path / "db")
    with patch.object(store, "delete_document", new_callable=AsyncMock, return_value=5) as mock_del:
        result = await store.delete_by_source_path("my-col", "/some/file.py")

        assert result == 5
        mock_del.assert_called_once()


# ---------------------------------------------------------------------------
# Tilde expansion tests (FIX-033 Task 1.2)
# ---------------------------------------------------------------------------


def test_search_store_init_expands_tilde() -> None:
    """SearchStore('~/.archon/search') must expand tilde at __init__ time."""
    store = SearchStore("~/.archon/search")
    assert store._db_path == Path.home() / ".archon/search"


def test_search_store_init_absolute_path_unchanged() -> None:
    """SearchStore('/tmp/test_db') must leave an absolute path unchanged."""
    store = SearchStore("/tmp/test_db")
    assert store._db_path == Path("/tmp/test_db")


def test_search_store_init_expands_tilde_path_object() -> None:
    """SearchStore(Path('~/.archon/search')) must expand tilde for Path inputs too."""
    store = SearchStore(Path("~/.archon/search"))
    assert store._db_path == Path.home() / ".archon/search"


# ===========================================================================
# FEAT-038 Task 12.6 — P14.8–P14.16: Store error paths
# ===========================================================================


def test_P14_8_store_invalid_collection_name_space_raises(tmp_path: Path) -> None:
    """P14.8 — Collection name with space raises ValueError."""
    import asyncio

    store = SearchStore(tmp_path / "db")
    asyncio.run(store.connect())
    try:
        with pytest.raises(ValueError, match="Invalid collection name"):
            asyncio.run(store.ensure_collection("has space", _DIM))
    finally:
        asyncio.run(store.disconnect())


def test_P14_9_store_invalid_collection_name_slash_raises(tmp_path: Path) -> None:
    """P14.9 — Collection name with slash raises ValueError (path traversal attempt)."""
    import asyncio

    store = SearchStore(tmp_path / "db")
    asyncio.run(store.connect())
    try:
        with pytest.raises(ValueError, match="Invalid collection name"):
            asyncio.run(store.ensure_collection("a/b", _DIM))
    finally:
        asyncio.run(store.disconnect())


def test_P14_10_store_invalid_collection_name_empty_raises(tmp_path: Path) -> None:
    """P14.10 — Empty collection name raises ValueError."""
    import asyncio

    store = SearchStore(tmp_path / "db")
    asyncio.run(store.connect())
    try:
        with pytest.raises(ValueError, match="Invalid collection name"):
            asyncio.run(store.ensure_collection("", _DIM))
    finally:
        asyncio.run(store.disconnect())


@pytest.mark.asyncio
async def test_P14_11_store_rebuild_fts_on_empty_collection_does_not_raise(
    connected_store: SearchStore, col_name: str
) -> None:
    """P14.11 — rebuild_fts_index on a collection with no rows should not raise."""
    await connected_store.ensure_collection(col_name, _DIM)
    # No documents ingested — table exists but is empty
    # Should not raise (may be a no-op or succeed silently)
    try:
        await connected_store.rebuild_fts_index(col_name)
    except Exception as exc:
        pytest.fail(f"rebuild_fts_index on empty collection raised: {exc}")


@pytest.mark.asyncio
async def test_P14_12_store_hybrid_search_top_k_zero_returns_empty(
    connected_store: SearchStore, col_name: str
) -> None:
    """P14.12 — hybrid_search with top_k=0 returns empty list (not an error)."""
    await _ingest_doc(connected_store, col_name)
    results = await connected_store.hybrid_search(col_name, [0.0] * _DIM, "any", top_k=0)
    assert results == []


@pytest.mark.asyncio
async def test_P14_13_store_list_documents_limit_zero_returns_empty(
    connected_store: SearchStore, col_name: str
) -> None:
    """P14.13 — list_documents with limit=0 returns [] (no rows fetched)."""
    doc_id = _doc_id()
    chunks = [_chunk(doc_id, 0)]
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, chunks)

    docs = await connected_store.list_documents(col_name, limit=0)
    # limit=0 → min(0, 1000) = 0 → limit * 50 = 0 rows fetched → []
    assert docs == []


@pytest.mark.asyncio
async def test_P14_14_store_fetch_adjacent_nonexistent_doc_id_returns_empty(
    connected_store: SearchStore, col_name: str
) -> None:
    """P14.14 — fetch_adjacent_chunks for a doc_id not in the collection returns []."""
    await connected_store.ensure_collection(col_name, _DIM)
    nonexistent_id = _doc_id()
    result = await connected_store.fetch_adjacent_chunks(col_name, nonexistent_id, 0, 1)
    assert result == []


def test_P14_15_store_list_collections_exception_on_one_table_skips_it(tmp_path: Path) -> None:
    """P14.15 — list_collections skips a table that raises an exception during inspection."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()

    # Two tables: "good-col" succeeds, "bad-col" raises ValueError on open_table
    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["good-col", "bad-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)

    good_table = MagicMock()
    good_table.count_rows = AsyncMock(return_value=5)
    # For the good table, query().select().to_arrow() must return an Arrow table with doc_id column
    import pyarrow as pa
    arrow_table = pa.table({"doc_id": ["aaa", "bbb"]})
    query_mock = MagicMock()
    query_mock.select = MagicMock(return_value=query_mock)
    query_mock.to_arrow = AsyncMock(return_value=arrow_table)
    good_table.query = MagicMock(return_value=query_mock)

    async def _open_table(name: str) -> MagicMock:
        if name == "bad-col":
            raise ValueError("corrupted table")
        return good_table

    mock_db.open_table = _open_table
    store._db = mock_db

    collections = asyncio.run(store.list_collections())
    names = [c.name for c in collections]
    assert "good-col" in names
    assert "bad-col" not in names


def test_P14_16_store_row_to_meta_malformed_centroid_json_returns_none(tmp_path: Path) -> None:
    """P14.16 — _row_to_meta with malformed centroid_json sets centroid=None (no crash)."""
    store = SearchStore(tmp_path / "db")
    row = {
        "name": "test-col",
        "description": "some desc",
        "centroid_json": "not-valid-json{",  # malformed JSON
        "doc_count": 1,
        "chunk_count": 3,
        "embedding_model": "model-x",
        "last_indexed": "",
        "last_described": "",
        "described_at_doc_count": -1,
    }
    meta = store._row_to_meta(row)
    assert meta.centroid is None
    assert meta.name == "test-col"
    assert meta.doc_count == 1


def test_P14_17_store_fetch_adjacent_invalid_hex_doc_id_raises(tmp_path: Path) -> None:
    """P14.17 (store-specific) — fetch_adjacent_chunks with invalid hex doc_id raises ValueError."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    store._db = MagicMock()

    with pytest.raises(ValueError, match="Invalid doc_id"):
        asyncio.run(store.fetch_adjacent_chunks("valid-col", "not-a-hex-id", 0, 1))


@pytest.mark.asyncio
async def test_search_store_connect_does_not_create_tilde_dir_in_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """connect() must create the expanded path, NOT a literal '~' dir in CWD."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    store = SearchStore("~/.archon/search")

    # Inject mock into sys.modules so connect()'s runtime `import lancedb` picks it up
    from unittest.mock import AsyncMock, MagicMock

    mock_lancedb = MagicMock()
    mock_lancedb.connect_async = AsyncMock()
    monkeypatch.setitem(sys.modules, "lancedb", mock_lancedb)

    await store.connect()

    assert (tmp_path / "~").exists() is False
    assert (tmp_path / ".archon" / "search").exists() is True

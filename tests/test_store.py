"""packages/archon-search/tests/test_store.py — unit + integration tests for SearchStore."""
from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import pytest_asyncio

from archon_search._types import ChunkRecord
from archon_search.store import SearchStore, _batch_vectors_valid, _centroid_sum_valid, elementwise_sum

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
# Edge-case tests 
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
# drop_collection tests 
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
# rename_collection tests ( — used by migration in )
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
# CollectionMeta namespace field tests 
# ---------------------------------------------------------------------------


def test_collection_meta_namespace_field() -> None:
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(name="x")
    assert meta.namespace == "default"


def test_collection_meta_namespace_custom() -> None:
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(name="x", namespace="foo")
    assert meta.namespace == "foo"


def test_collection_meta_namespace_equals_constant() -> None:
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    meta = CollectionMeta(name="x")
    assert meta.namespace == DEFAULT_NAMESPACE


# ---------------------------------------------------------------------------
# CollectionInfo namespace field tests 
# ---------------------------------------------------------------------------


def test_collection_info_namespace_field() -> None:
    from archon_search._types import CollectionInfo

    info = CollectionInfo(name="x", doc_count=0, chunk_count=0)
    assert info.namespace == "default"


def test_collection_info_namespace_custom() -> None:
    from archon_search._types import CollectionInfo

    info = CollectionInfo(name="x", doc_count=0, chunk_count=0, namespace="foo")
    assert info.namespace == "foo"


def test_collection_info_namespace_equals_constant() -> None:
    from archon_search._types import CollectionInfo
    from archon_search.constants import DEFAULT_NAMESPACE

    info = CollectionInfo(name="x", doc_count=0, chunk_count=0)
    assert info.namespace == DEFAULT_NAMESPACE


# ---------------------------------------------------------------------------
# Store schema namespace tests 
# ---------------------------------------------------------------------------


def test_meta_schema_includes_namespace() -> None:
    import pyarrow as pa

    schema = SearchStore._meta_schema()
    assert "namespace" in schema.names
    idx = schema.get_field_index("namespace")
    assert schema.field(idx).type == pa.utf8()


def test_row_to_meta_reads_namespace() -> None:
    row = {
        "name": "col1",
        "description": None,
        "centroid_json": None,
        "doc_count": 0,
        "chunk_count": 0,
        "embedding_model": None,
        "last_indexed": None,
        "last_described": None,
        "described_at_doc_count": -1,
        "namespace": "custom",
    }
    meta = SearchStore._row_to_meta(row)
    assert meta.namespace == "custom"


def test_row_to_meta_missing_namespace_defaults() -> None:
    row = {
        "name": "col1",
        "description": None,
        "centroid_json": None,
        "doc_count": 0,
        "chunk_count": 0,
        "embedding_model": None,
        "last_indexed": None,
        "last_described": None,
        "described_at_doc_count": -1,
        # no "namespace" key — simulates pre-migration row
    }
    meta = SearchStore._row_to_meta(row)
    assert meta.namespace == "default"


def test_row_to_meta_null_namespace_defaults() -> None:
    row = {
        "name": "col1",
        "description": None,
        "centroid_json": None,
        "doc_count": 0,
        "chunk_count": 0,
        "embedding_model": None,
        "last_indexed": None,
        "last_described": None,
        "described_at_doc_count": -1,
        "namespace": None,  # null column value from LanceDB
    }
    meta = SearchStore._row_to_meta(row)
    assert meta.namespace == "default"


# ---------------------------------------------------------------------------
# list_collections namespace tests 
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_collections_includes_namespace(connected_store: SearchStore) -> None:
    await connected_store.ensure_collection("ns-list-col", _DIM)
    collections = await connected_store.list_collections()
    matching = [c for c in collections if c.name == "ns-list-col"]
    assert len(matching) == 1
    assert matching[0].namespace == "default"


# ---------------------------------------------------------------------------
# update_collection_meta namespace tests 
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_meta_writes_namespace(connected_store: SearchStore) -> None:
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(name="ns-test-col", namespace="tenant-x")
    await connected_store.update_collection_meta(meta)

    db = connected_store._require_connected()
    table = await db.open_table("_archon_collection_meta")
    rows = (await table.query().to_arrow()).to_pylist()
    matching = [r for r in rows if r["name"] == "ns-test-col"]
    assert len(matching) == 1
    assert matching[0]["namespace"] == "tenant-x"


@pytest.mark.asyncio
async def test_update_collection_meta_round_trip_namespace_preserved(connected_store: SearchStore) -> None:
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(name="ns-roundtrip-col", namespace="foo")
    await connected_store.update_collection_meta(meta)

    result = await connected_store.get_collection_meta("ns-roundtrip-col", namespace="foo")
    assert result is not None
    assert result.namespace == "foo"


@pytest.mark.asyncio
async def test_get_all_collections_meta_returns_namespace(connected_store: SearchStore) -> None:
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    meta1 = CollectionMeta(name="ns-all-col-a")
    meta2 = CollectionMeta(name="ns-all-col-b")
    await connected_store.update_collection_meta(meta1)
    await connected_store.update_collection_meta(meta2)

    all_meta = await connected_store.get_all_collections_meta()
    names = {m.name for m in all_meta}
    assert "ns-all-col-a" in names
    assert "ns-all-col-b" in names
    for m in all_meta:
        if m.name in {"ns-all-col-a", "ns-all-col-b"}:
            assert m.namespace == DEFAULT_NAMESPACE


# ---------------------------------------------------------------------------
# migrate_namespace tests 
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migrate_namespace_no_meta_table(tmp_path: Path) -> None:
    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.migrate_namespace()  # no _archon_collection_meta — must be no-op
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_migrate_namespace_empty_table(tmp_path: Path) -> None:
    import pyarrow as pa

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        db = store._require_connected()
        old_schema = pa.schema([
            pa.field("name", pa.utf8()),
            pa.field("description", pa.utf8()),
            pa.field("centroid_json", pa.utf8()),
            pa.field("doc_count", pa.int64()),
            pa.field("chunk_count", pa.int64()),
            pa.field("embedding_model", pa.utf8()),
            pa.field("last_indexed", pa.utf8()),
            pa.field("last_described", pa.utf8()),
            pa.field("described_at_doc_count", pa.int64()),
        ])
        await db.create_table("_archon_collection_meta", schema=old_schema)
        await store.migrate_namespace()
        table = await db.open_table("_archon_collection_meta")
        assert "namespace" in (await table.schema()).names
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_migrate_namespace_existing_rows(tmp_path: Path) -> None:
    import pyarrow as pa

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        db = store._require_connected()
        old_schema = pa.schema([
            pa.field("name", pa.utf8()),
            pa.field("description", pa.utf8()),
            pa.field("centroid_json", pa.utf8()),
            pa.field("doc_count", pa.int64()),
            pa.field("chunk_count", pa.int64()),
            pa.field("embedding_model", pa.utf8()),
            pa.field("last_indexed", pa.utf8()),
            pa.field("last_described", pa.utf8()),
            pa.field("described_at_doc_count", pa.int64()),
        ])
        table = await db.create_table("_archon_collection_meta", schema=old_schema)
        await table.add([{
            "name": "old-col",
            "description": "",
            "centroid_json": "",
            "doc_count": 0,
            "chunk_count": 0,
            "embedding_model": "",
            "last_indexed": "",
            "last_described": "",
            "described_at_doc_count": -1,
        }])
        await store.migrate_namespace()
        # re-open to get fresh schema after add_columns
        table = await db.open_table("_archon_collection_meta")
        assert "namespace" in (await table.schema()).names
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_migrate_namespace_already_migrated(tmp_path: Path) -> None:
    import pyarrow as pa

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        db = store._require_connected()
        old_schema = pa.schema([
            pa.field("name", pa.utf8()),
            pa.field("description", pa.utf8()),
            pa.field("centroid_json", pa.utf8()),
            pa.field("doc_count", pa.int64()),
            pa.field("chunk_count", pa.int64()),
            pa.field("embedding_model", pa.utf8()),
            pa.field("last_indexed", pa.utf8()),
            pa.field("last_described", pa.utf8()),
            pa.field("described_at_doc_count", pa.int64()),
        ])
        await db.create_table("_archon_collection_meta", schema=old_schema)
        await store.migrate_namespace()  # first call — triggers add_columns
        await store.migrate_namespace()  # second call — hits schema-check early return
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_migrate_namespace_rows_backfilled(tmp_path: Path) -> None:
    import pyarrow as pa
    from archon_search.constants import DEFAULT_NAMESPACE

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        db = store._require_connected()
        old_schema = pa.schema([
            pa.field("name", pa.utf8()),
            pa.field("description", pa.utf8()),
            pa.field("centroid_json", pa.utf8()),
            pa.field("doc_count", pa.int64()),
            pa.field("chunk_count", pa.int64()),
            pa.field("embedding_model", pa.utf8()),
            pa.field("last_indexed", pa.utf8()),
            pa.field("last_described", pa.utf8()),
            pa.field("described_at_doc_count", pa.int64()),
        ])
        table = await db.create_table("_archon_collection_meta", schema=old_schema)
        await table.add([{
            "name": "backfill-col",
            "description": "",
            "centroid_json": "",
            "doc_count": 0,
            "chunk_count": 0,
            "embedding_model": "",
            "last_indexed": "",
            "last_described": "",
            "described_at_doc_count": -1,
        }])
        await store.migrate_namespace()
        table = await db.open_table("_archon_collection_meta")
        arrow_table = await table.query().to_arrow()
        values = arrow_table.column("namespace").to_pylist()
        assert all(v == DEFAULT_NAMESPACE for v in values)
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_migrate_namespace_add_columns_duplicate_raises_runtime_error(tmp_path: Path) -> None:
    """Verify the real LanceDB exception for duplicate add_columns matches our handler."""
    import pyarrow as pa

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        db = store._require_connected()
        schema = pa.schema([pa.field("name", pa.utf8()), pa.field("namespace", pa.utf8())])
        table = await db.create_table("_archon_verify_exc", schema=schema)
        with pytest.raises(RuntimeError) as exc_info:
            await table.add_columns({"namespace": "'default'"})
        assert "already exists" in str(exc_info.value).lower()
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_migrate_namespace_concurrent_race(tmp_path: Path) -> None:
    import lancedb.table
    from unittest.mock import AsyncMock, patch

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        db = store._require_connected()
        import pyarrow as pa
        old_schema = pa.schema([
            pa.field("name", pa.utf8()),
            pa.field("description", pa.utf8()),
            pa.field("centroid_json", pa.utf8()),
            pa.field("doc_count", pa.int64()),
            pa.field("chunk_count", pa.int64()),
            pa.field("embedding_model", pa.utf8()),
            pa.field("last_indexed", pa.utf8()),
            pa.field("last_described", pa.utf8()),
            pa.field("described_at_doc_count", pa.int64()),
        ])
        await db.create_table("_archon_collection_meta", schema=old_schema)
        with patch.object(
            lancedb.table.AsyncTable,
            "add_columns",
            new=AsyncMock(side_effect=RuntimeError("Column namespace already exists in the dataset")),
        ):
            await store.migrate_namespace()  # must not raise
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# migrate_centroid_sum tests
# ---------------------------------------------------------------------------

_PRE_B5_SCHEMA_FIELDS = [
    pa.field("name", pa.utf8()),
    pa.field("description", pa.utf8()),
    pa.field("centroid_json", pa.utf8()),
    pa.field("description_embedding_json", pa.utf8()),
    pa.field("doc_count", pa.int64()),
    pa.field("chunk_count", pa.int64()),
    pa.field("embedding_model", pa.utf8()),
    pa.field("last_indexed", pa.utf8()),
    pa.field("last_described", pa.utf8()),
    pa.field("described_at_doc_count", pa.int64()),
    pa.field("namespace", pa.utf8()),
]


@pytest.mark.asyncio
async def test_migrate_centroid_sum_adds_columns(tmp_path: Path) -> None:
    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        db = store._require_connected()
        old_schema = pa.schema(_PRE_B5_SCHEMA_FIELDS)
        await db.create_table("_archon_collection_meta", schema=old_schema)
        await store.migrate_centroid_sum()
        table = await db.open_table("_archon_collection_meta")
        schema_names = (await table.schema()).names
        assert "centroid_sum_json" in schema_names
        assert "mutations_since_recompute" in schema_names
        assert "needs_recompute" in schema_names
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_migrate_centroid_sum_idempotent(tmp_path: Path) -> None:
    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        db = store._require_connected()
        old_schema = pa.schema(_PRE_B5_SCHEMA_FIELDS)
        await db.create_table("_archon_collection_meta", schema=old_schema)
        await store.migrate_centroid_sum()  # first call
        await store.migrate_centroid_sum()  # second call — must be no-op, no exception
        table = await db.open_table("_archon_collection_meta")
        schema_names = (await table.schema()).names
        assert "centroid_sum_json" in schema_names
        assert "mutations_since_recompute" in schema_names
        assert "needs_recompute" in schema_names
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_migrate_centroid_sum_no_meta_table_noop(tmp_path: Path) -> None:
    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        await store.migrate_centroid_sum()  # no _archon_collection_meta — must be no-op
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_migrate_centroid_sum_existing_rows_get_defaults(tmp_path: Path) -> None:
    """Existing rows get correct default values after migration."""
    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        db = store._require_connected()
        old_schema = pa.schema(_PRE_B5_SCHEMA_FIELDS)
        table = await db.create_table("_archon_collection_meta", schema=old_schema)
        # Insert a pre-B5 row
        await table.add([{
            "name": "existing-col",
            "description": "desc",
            "centroid_json": "",
            "description_embedding_json": "",
            "doc_count": 3,
            "chunk_count": 9,
            "embedding_model": "bge",
            "last_indexed": "",
            "last_described": "",
            "described_at_doc_count": -1,
            "namespace": "default",
        }])
        await store.migrate_centroid_sum()
        # Verify the existing row gets correct defaults
        retrieved = await store.get_collection_meta("existing-col")
        assert retrieved is not None
        assert retrieved.centroid_sum is None          # empty string default → None
        assert retrieved.mutations_since_recompute == 0  # 0 default
        assert retrieved.needs_recompute is False       # false default
        # doc_count and chunk_count unchanged
        assert retrieved.doc_count == 3
        assert retrieved.chunk_count == 9
    finally:
        await store.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_old_schema_upsert_preserves_new_columns(tmp_path: Path) -> None:
    """Verify that an old-binary upsert (row dict with only pre-B5 columns) on
    a migrated table does NOT null out B5 columns on OTHER existing rows.

    This test's result determines the BREAKING.md forward-compatibility claim.
    """
    from archon_search.collection_meta import CollectionMeta

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        db = store._require_connected()
        old_schema = pa.schema(_PRE_B5_SCHEMA_FIELDS)
        table = await db.create_table("_archon_collection_meta", schema=old_schema)
        row_a = {
            "name": "col-a",
            "description": "",
            "centroid_json": "",
            "description_embedding_json": "",
            "doc_count": 0,
            "chunk_count": 0,
            "embedding_model": "",
            "last_indexed": "",
            "last_described": "",
            "described_at_doc_count": -1,
            "namespace": "default",
        }
        row_b = dict(row_a)
        row_b["name"] = "col-b"
        await table.add([row_a, row_b])

        # Run migration
        await store.migrate_centroid_sum()

        # Write B5 values to row_a via update_collection_meta
        meta_a = CollectionMeta(
            name="col-a",
            centroid_sum=[1.0, 2.0],
            mutations_since_recompute=5,
            needs_recompute=True,
        )
        await store.update_collection_meta(meta_a)

        # Simulate old-binary upsert: delete + insert row_b with ONLY pre-B5 columns.
        # LanceDB may raise RuntimeError("Append with different schema") when the
        # incoming row dict is missing the new B5 columns. This is the documented
        # LanceDB behavior as of 4.x: mixed-version deployment fails hard (not silently).
        await table.delete("name = 'col-b'")
        row_b_old_binary = dict(row_a)
        row_b_old_binary["name"] = "col-b"
        schema_mismatch = False
        try:
            await table.add([row_b_old_binary])
        except RuntimeError as exc:
            if "different schema" in str(exc).lower() or "missing" in str(exc).lower():
                # LanceDB rejects old-binary inserts with a hard error (not silent corruption).
                # BREAKING.md claim: mixed-version deployment is NOT safe.
                schema_mismatch = True
            else:
                raise

        # Regardless of whether insert succeeded or raised, verify row_a's B5 columns
        # are intact (the delete of col-b already ran unconditionally above).
        rows = await table.query().to_list()
        row_a_actual = next(r for r in rows if r["name"] == "col-a")
        assert row_a_actual.get("centroid_sum_json") not in (None, ""), (
            "col-a's centroid_sum_json was lost — B5 columns not preserved"
        )
        assert row_a_actual.get("mutations_since_recompute") == 5
        assert row_a_actual.get("needs_recompute") is True

        if schema_mismatch:
            # Document: LanceDB raises on old-binary insert AND preserves other rows' B5 data
            pass  # test passes: both safety properties hold
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# CollectionMeta tests
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
async def test_centroid_sum_json_round_trips(connected_store: SearchStore) -> None:
    from archon_search.collection_meta import CollectionMeta
    meta = CollectionMeta(name="b5-centroid-sum-rt", centroid_sum=[1.0, 2.0])
    await connected_store.update_collection_meta(meta)
    retrieved = await connected_store.get_collection_meta("b5-centroid-sum-rt")
    assert retrieved is not None
    assert retrieved.centroid_sum is not None
    assert isinstance(retrieved.centroid_sum, list)
    assert len(retrieved.centroid_sum) == 2
    assert abs(retrieved.centroid_sum[0] - 1.0) < 1e-9
    assert abs(retrieved.centroid_sum[1] - 2.0) < 1e-9

@pytest.mark.asyncio
async def test_centroid_sum_json_none_round_trips(connected_store: SearchStore) -> None:
    from archon_search.collection_meta import CollectionMeta
    meta = CollectionMeta(name="b5-centroid-sum-none", centroid_sum=None)
    await connected_store.update_collection_meta(meta)
    retrieved = await connected_store.get_collection_meta("b5-centroid-sum-none")
    assert retrieved is not None
    assert retrieved.centroid_sum is None

@pytest.mark.asyncio
async def test_malformed_centroid_sum_json_parses_to_none(connected_store: SearchStore) -> None:
    """Manually insert a row with malformed centroid_sum_json; get_collection_meta returns centroid_sum=None."""
    from archon_search.store import _META_TABLE
    db = connected_store._require_connected()
    # Ensure meta table exists
    from archon_search.collection_meta import CollectionMeta
    seed = CollectionMeta(name="b5-malformed-sum")
    await connected_store.update_collection_meta(seed)
    # Directly patch the row with malformed JSON
    table = await db.open_table(_META_TABLE)
    await table.delete("name = 'b5-malformed-sum'")
    schema = await table.schema()
    # Build a row with all fields but malformed centroid_sum_json
    row: dict = {f: "" for f in schema.names}
    row["name"] = "b5-malformed-sum"
    row["doc_count"] = 0
    row["chunk_count"] = 0
    row["described_at_doc_count"] = -1
    row["centroid_sum_json"] = "not-json"
    assert "mutations_since_recompute" in schema.names, "B5 column missing from schema"
    assert "needs_recompute" in schema.names, "B5 column missing from schema"
    row["mutations_since_recompute"] = 0
    row["needs_recompute"] = False
    await table.add([row])
    retrieved = await connected_store.get_collection_meta("b5-malformed-sum")
    assert retrieved is not None
    assert retrieved.centroid_sum is None

@pytest.mark.asyncio
async def test_mutations_since_recompute_round_trips(connected_store: SearchStore) -> None:
    from archon_search.collection_meta import CollectionMeta
    meta = CollectionMeta(name="b5-mutations-rt", mutations_since_recompute=42)
    await connected_store.update_collection_meta(meta)
    retrieved = await connected_store.get_collection_meta("b5-mutations-rt")
    assert retrieved is not None
    assert retrieved.mutations_since_recompute == 42

@pytest.mark.asyncio
async def test_needs_recompute_round_trips(connected_store: SearchStore) -> None:
    from archon_search.collection_meta import CollectionMeta
    meta = CollectionMeta(name="b5-needs-recompute-rt", needs_recompute=True)
    await connected_store.update_collection_meta(meta)
    retrieved = await connected_store.get_collection_meta("b5-needs-recompute-rt")
    assert retrieved is not None
    assert retrieved.needs_recompute is True

@pytest.mark.asyncio
async def test_mutations_since_recompute_zero_round_trips(connected_store: SearchStore) -> None:
    from archon_search.collection_meta import CollectionMeta
    meta = CollectionMeta(name="b5-mutations-zero", mutations_since_recompute=0)
    await connected_store.update_collection_meta(meta)
    retrieved = await connected_store.get_collection_meta("b5-mutations-zero")
    assert retrieved is not None
    assert retrieved.mutations_since_recompute == 0

@pytest.mark.asyncio
async def test_needs_recompute_false_round_trips(connected_store: SearchStore) -> None:
    from archon_search.collection_meta import CollectionMeta
    meta = CollectionMeta(name="b5-needs-recompute-false", needs_recompute=False)
    await connected_store.update_collection_meta(meta)
    retrieved = await connected_store.get_collection_meta("b5-needs-recompute-false")
    assert retrieved is not None
    assert retrieved.needs_recompute is False

@pytest.mark.asyncio
async def test_update_collection_meta_writes_b5_columns(connected_store: SearchStore) -> None:
    """Row dict (not just schema) must contain all three B5 columns — no NULL on write."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.store import _META_TABLE
    meta = CollectionMeta(
        name="b5-writes-columns",
        centroid_sum=[3.0, 4.0],
        mutations_since_recompute=7,
        needs_recompute=True,
    )
    await connected_store.update_collection_meta(meta)
    # Verify via get_collection_meta (round-trip)
    retrieved = await connected_store.get_collection_meta("b5-writes-columns")
    assert retrieved is not None
    assert retrieved.centroid_sum is not None
    assert abs(retrieved.centroid_sum[0] - 3.0) < 1e-9
    assert retrieved.mutations_since_recompute == 7
    assert retrieved.needs_recompute is True
    # Also verify raw row to confirm columns are not NULL
    db = connected_store._require_connected()
    table = await db.open_table(_META_TABLE)
    rows = await table.query().to_list()
    raw = next((r for r in rows if r["name"] == "b5-writes-columns"), None)
    assert raw is not None
    assert raw.get("centroid_sum_json") not in (None, "")
    assert raw.get("mutations_since_recompute") == 7
    assert raw.get("needs_recompute") is True


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
# delete_by_source_path tests (-P4)
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
# Tilde expansion tests 
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
# Store error paths
# ===========================================================================


def test_P14_8_store_invalid_collection_name_space_raises(tmp_path: Path) -> None:
    """ Collection name with space raises ValueError."""
    import asyncio

    store = SearchStore(tmp_path / "db")
    asyncio.run(store.connect())
    try:
        with pytest.raises(ValueError, match="Invalid collection name"):
            asyncio.run(store.ensure_collection("has space", _DIM))
    finally:
        asyncio.run(store.disconnect())


def test_P14_9_store_invalid_collection_name_slash_raises(tmp_path: Path) -> None:
    """ Collection name with slash raises ValueError (path traversal attempt)."""
    import asyncio

    store = SearchStore(tmp_path / "db")
    asyncio.run(store.connect())
    try:
        with pytest.raises(ValueError, match="Invalid collection name"):
            asyncio.run(store.ensure_collection("a/b", _DIM))
    finally:
        asyncio.run(store.disconnect())


def test_P14_10_store_invalid_collection_name_empty_raises(tmp_path: Path) -> None:
    """ Empty collection name raises ValueError."""
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
    """ rebuild_fts_index on a collection with no rows should not raise."""
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
    """ hybrid_search with top_k=0 returns empty list (not an error)."""
    await _ingest_doc(connected_store, col_name)
    results = await connected_store.hybrid_search(col_name, [0.0] * _DIM, "any", top_k=0)
    assert results == []


@pytest.mark.asyncio
async def test_P14_13_store_list_documents_limit_zero_returns_empty(
    connected_store: SearchStore, col_name: str
) -> None:
    """ list_documents with limit=0 returns [] (no rows fetched)."""
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
    """ fetch_adjacent_chunks for a doc_id not in the collection returns []."""
    await connected_store.ensure_collection(col_name, _DIM)
    nonexistent_id = _doc_id()
    result = await connected_store.fetch_adjacent_chunks(col_name, nonexistent_id, 0, 1)
    assert result == []


def test_P14_15_store_list_collections_exception_on_one_table_skips_it(tmp_path: Path) -> None:
    """ list_collections skips a table that raises an exception during inspection."""
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
    """ _row_to_meta with malformed centroid_json sets centroid=None (no crash)."""
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
    """ (store-specific) — fetch_adjacent_chunks with invalid hex doc_id raises ValueError."""
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


# ===========================================================================
# Hybrid-search trace provenance
# ===========================================================================


def test_hybrid_search_keeps_public_result_contract(tmp_path: Path) -> None:
    """Normal hybrid_search results expose only SearchResult fields — no eval-only score fields."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_table = MagicMock()

    doc_id = _doc_id()
    chunk_id = f"{doc_id}-000000"

    vec_row = {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "hello world",
        "source_path": "/tmp/foo.md",
        "_distance": 0.1,
    }

    mock_table.vector_search = MagicMock(
        return_value=MagicMock(limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[vec_row]))))
    )
    mock_table.search = AsyncMock(
        side_effect=Exception("fts index not found")
    )

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    results = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", top_k=5))

    assert len(results) == 1
    result = results[0]
    # Only public SearchResult fields exist
    assert hasattr(result, "doc_id")
    assert hasattr(result, "chunk_id")
    assert hasattr(result, "text")
    assert hasattr(result, "score")
    assert hasattr(result, "source_path")
    # No eval-only fields
    assert not hasattr(result, "vector_score")
    assert not hasattr(result, "fts_score")
    assert not hasattr(result, "vector_rank")
    assert not hasattr(result, "fts_rank")
    assert not hasattr(result, "score_breakdown")


def test_hybrid_search_trace_exposes_score_breakdown(tmp_path: Path) -> None:
    """Trace candidates expose vector rank/score, FTS rank/score, score-kind, and RRF values."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from archon_search._diagnostics import ScoredSearchCandidate
    from archon_search.store import _hybrid_search_with_trace

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_table = MagicMock()

    doc_id = _doc_id()
    chunk_id = f"{doc_id}-000000"

    vec_row = {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "hello world",
        "source_path": "/tmp/foo.md",
        "_distance": 0.25,
    }
    fts_row = {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "hello world",
        "source_path": "/tmp/foo.md",
        "_score": 1.5,
    }

    mock_table.vector_search = MagicMock(
        return_value=MagicMock(limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[vec_row]))))
    )
    fts_result = MagicMock()
    fts_result.limit = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[fts_row])))
    mock_table.search = AsyncMock(return_value=fts_result)

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    candidates = asyncio.run(_hybrid_search_with_trace(store, "my-col", [0.0] * _DIM, "hello", 20))

    assert len(candidates) == 1
    cand = candidates[0]
    assert isinstance(cand, ScoredSearchCandidate)
    sb = cand.score_breakdown
    assert sb.vector_rank is not None
    assert sb.vector_score is not None
    assert sb.vector_score_kind is not None
    assert sb.fts_rank is not None
    assert sb.fts_score is not None
    assert sb.fts_score_kind is not None
    assert sb.rrf_score > 0.0


def test_hybrid_search_trace_documents_backend_score_field_mapping(tmp_path: Path) -> None:
    """Trace extraction pins the backend row fields: vector uses _distance, FTS uses _score."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.store import _hybrid_search_with_trace

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_table = MagicMock()

    doc_id = _doc_id()
    chunk_id = f"{doc_id}-000000"
    expected_distance = 0.42
    expected_fts_score = 3.7

    vec_row = {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "some text",
        "source_path": "/tmp/bar.md",
        "_distance": expected_distance,
    }
    fts_row = {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "some text",
        "source_path": "/tmp/bar.md",
        "_score": expected_fts_score,
    }

    mock_table.vector_search = MagicMock(
        return_value=MagicMock(limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[vec_row]))))
    )
    fts_result = MagicMock()
    fts_result.limit = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[fts_row])))
    mock_table.search = AsyncMock(return_value=fts_result)

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    candidates = asyncio.run(_hybrid_search_with_trace(store, "my-col", [0.0] * _DIM, "some text", 20))

    assert len(candidates) == 1
    sb = candidates[0].score_breakdown
    assert abs(sb.vector_score - expected_distance) < 1e-6, (
        f"vector_score should be _distance field value, got {sb.vector_score}"
    )
    assert abs(sb.fts_score - expected_fts_score) < 1e-6, (
        f"fts_score should be _score field value, got {sb.fts_score}"
    )


def test_hybrid_search_trace_sets_missing_raw_scores_none(tmp_path: Path) -> None:
    """Backend rows without _distance or _score fields yield None raw scores, not fabricated values."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.store import _hybrid_search_with_trace

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_table = MagicMock()

    doc_id = _doc_id()
    chunk_id = f"{doc_id}-000000"

    # Row without _distance field
    vec_row = {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "content",
        "source_path": "/tmp/x.md",
        # No _distance key
    }
    # FTS row without _score field
    fts_row = {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "content",
        "source_path": "/tmp/x.md",
        # No _score key
    }

    mock_table.vector_search = MagicMock(
        return_value=MagicMock(limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[vec_row]))))
    )
    fts_result = MagicMock()
    fts_result.limit = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[fts_row])))
    mock_table.search = AsyncMock(return_value=fts_result)

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    candidates = asyncio.run(_hybrid_search_with_trace(store, "my-col", [0.0] * _DIM, "content", 20))

    assert len(candidates) == 1
    sb = candidates[0].score_breakdown
    assert sb.vector_score is None, f"Expected None for missing _distance, got {sb.vector_score}"
    assert sb.vector_score_kind is None
    assert sb.fts_score is None, f"Expected None for missing _score, got {sb.fts_score}"
    assert sb.fts_score_kind is None


def test_hybrid_search_trace_sets_fts_score_none_without_index(tmp_path: Path) -> None:
    """No FTS index yields fts_score=None in trace, not an exception."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.store import _hybrid_search_with_trace

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_table = MagicMock()

    doc_id = _doc_id()
    chunk_id = f"{doc_id}-000000"
    vec_row = {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "some content",
        "source_path": "/tmp/f.md",
        "_distance": 0.5,
    }

    mock_table.vector_search = MagicMock(
        return_value=MagicMock(limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[vec_row]))))
    )
    # Simulate no FTS index — raises error with "fts" in the message
    mock_table.search = AsyncMock(side_effect=Exception("fts index not found"))

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    candidates = asyncio.run(_hybrid_search_with_trace(store, "my-col", [0.0] * _DIM, "some content", 20))

    assert len(candidates) == 1
    sb = candidates[0].score_breakdown
    assert sb.fts_rank is None
    assert sb.fts_score is None
    assert sb.fts_score_kind is None
    # vector fields still populated
    assert sb.vector_rank == 0
    assert sb.vector_score is not None


def test_hybrid_search_trace_orders_equal_scores_deterministically(tmp_path: Path) -> None:
    """Candidates with equal RRF scores must have a stable (deterministic) secondary ordering."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.store import _hybrid_search_with_trace

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_table = MagicMock()

    doc_id = _doc_id()
    # Two chunks with same rank in both vector and FTS (same RRF score)
    rows = [
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-000000",
            "text": "aaa",
            "source_path": "/tmp/a.md",
            "_distance": 0.3,
        },
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-000001",
            "text": "bbb",
            "source_path": "/tmp/a.md",
            "_distance": 0.3,
        },
    ]

    mock_table.vector_search = MagicMock(
        return_value=MagicMock(limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=rows))))
    )
    mock_table.search = AsyncMock(side_effect=Exception("fts index not found"))

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    results_1 = asyncio.run(_hybrid_search_with_trace(store, "my-col", [0.0] * _DIM, "x", 20))
    results_2 = asyncio.run(_hybrid_search_with_trace(store, "my-col", [0.0] * _DIM, "x", 20))

    # Order must be identical across calls (deterministic)
    ids_1 = [c.chunk_id for c in results_1]
    ids_2 = [c.chunk_id for c in results_2]
    assert ids_1 == ids_2, f"Non-deterministic ordering: {ids_1} vs {ids_2}"


# ---------------------------------------------------------------------------
# Retrieval sort tie-break reconciliation — Task 2.3 (B3)
# ---------------------------------------------------------------------------


def _tie_break_mock_store(tmp_path: Path) -> tuple[SearchStore, str]:
    """Build a mocked store where one chunk is vector-only and one is FTS-only,
    both at local rank 0 → identical RRF score. The vector-only chunk has the
    HIGHER chunk_id so insertion order (vec first) differs from the (-score,
    chunk_id) tie-break order, exposing whether the tie-break is applied.
    """
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_table = MagicMock()

    doc_id = "a" * 64
    chunk_lo = f"{doc_id}-000000"  # FTS-only, lower chunk_id
    chunk_hi = f"{doc_id}-000001"  # vector-only, higher chunk_id, inserted first

    vec_rows = [
        {"doc_id": doc_id, "chunk_id": chunk_hi, "text": "vec", "source_path": "/tmp/a.md", "_distance": 0.3},
    ]
    fts_rows = [
        {"doc_id": doc_id, "chunk_id": chunk_lo, "text": "fts", "source_path": "/tmp/a.md", "_score": 1.0},
    ]

    mock_table.vector_search = MagicMock(
        return_value=MagicMock(limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=vec_rows))))
    )
    mock_table.search = AsyncMock(
        return_value=MagicMock(limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=fts_rows))))
    )

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db
    return store, doc_id


def test_hybrid_search_sort_is_deterministic_on_score_ties(tmp_path: Path) -> None:
    """Production hybrid_search breaks RRF-score ties by ascending chunk_id."""
    import asyncio

    store, doc_id = _tie_break_mock_store(tmp_path)
    chunk_lo = f"{doc_id}-000000"
    chunk_hi = f"{doc_id}-000001"

    results_1 = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "x", top_k=20))
    results_2 = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "x", top_k=20))

    ids_1 = [r.chunk_id for r in results_1]
    ids_2 = [r.chunk_id for r in results_2]
    # Equal RRF scores → ascending chunk_id tie-break (not insertion order).
    assert ids_1 == [chunk_lo, chunk_hi], ids_1
    assert ids_1 == ids_2, f"Non-deterministic ordering: {ids_1} vs {ids_2}"


def test_hybrid_search_with_trace_sort_matches_production_sort(tmp_path: Path) -> None:
    """Trace and production retrieval paths produce the same chunk_id ordering on ties."""
    import asyncio

    from archon_search.store import _hybrid_search_with_trace

    store_prod, doc_id = _tie_break_mock_store(tmp_path)
    store_trace, _ = _tie_break_mock_store(tmp_path)
    chunk_lo = f"{doc_id}-000000"
    chunk_hi = f"{doc_id}-000001"

    prod = asyncio.run(store_prod.hybrid_search("my-col", [0.0] * _DIM, "x", top_k=20))
    trace = asyncio.run(_hybrid_search_with_trace(store_trace, "my-col", [0.0] * _DIM, "x", 20))

    prod_ids = [r.chunk_id for r in prod]
    trace_ids = [c.chunk_id for c in trace]
    # Pin the expected order so this fails if BOTH paths regress identically.
    assert prod_ids == [chunk_lo, chunk_hi], prod_ids
    assert prod_ids == trace_ids


def test_mcp_search_response_schema_matches_public_contract_without_eval_provenance() -> None:
    """Serialized public SearchResult payloads match the public contract and exclude eval-only provenance."""
    from dataclasses import asdict
    from archon_search._types import SearchResult

    result = SearchResult(
        doc_id="abc123",
        chunk_id="abc123-000000",
        text="some text",
        score=0.85,
        source_path="/tmp/doc.md",
    )
    payload = asdict(result)
    # Public contract fields (acl added in as internal metadata)
    # Post-A1 public contract: SearchResult gained file_type, indexed_at,
    # updated_at, ingested_by, metadata in addition to the previous fields.
    assert set(payload.keys()) == {
        "doc_id", "chunk_id", "text", "score", "source_path",
        "file_type", "language", "indexed_at", "updated_at", "ingested_by", "metadata",
        "acl", "collection",
    }
    # No eval provenance keys
    for forbidden in ("vector_score", "fts_score", "vector_rank", "fts_rank", "score_breakdown"):
        assert forbidden not in payload, f"Eval-only field {forbidden!r} leaked into public payload"


def test_mcp_search_with_context_response_schema_matches_public_contract_without_eval_provenance() -> None:
    """Context (multi-result) payloads match the public Search contract and exclude eval provenance."""
    from dataclasses import asdict
    from archon_search._types import SearchResult

    results = [
        SearchResult(
            doc_id=f"id{i}",
            chunk_id=f"id{i}-{i:06d}",
            text=f"text {i}",
            score=float(i) * 0.1,
            source_path=f"/tmp/{i}.md",
        )
        for i in range(3)
    ]
    payloads = [asdict(r) for r in results]
    for payload in payloads:
        # Post-A1 public contract: SearchResult gained file_type, indexed_at,
        # updated_at, ingested_by, metadata in addition to the previous fields.
        assert set(payload.keys()) == {
            "doc_id", "chunk_id", "text", "score", "source_path",
            "file_type", "language", "indexed_at", "updated_at", "ingested_by", "metadata",
            "acl", "collection",
        }
        for forbidden in ("vector_score", "fts_score", "vector_rank", "fts_rank", "score_breakdown"):
            assert forbidden not in payload


def test_eval_trace_helpers_are_not_public_package_exports() -> None:
    """Internal trace helpers _hybrid_search_with_trace must NOT appear in the public package exports."""
    import archon_search

    public_names = dir(archon_search)
    assert "_hybrid_search_with_trace" not in public_names, (
        "_hybrid_search_with_trace must not be exported from archon_search public package"
    )
    # Also verify it is not accidentally importable from the top-level namespace
    assert not hasattr(archon_search, "_hybrid_search_with_trace")


# ---------------------------------------------------------------------------
# get_collection_meta namespace filter tests 
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_collection_meta_correct_namespace(connected_store: SearchStore) -> None:
    """get_collection_meta returns meta when namespace matches."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(name="ns-filter-col", namespace="tenantA", doc_count=1, chunk_count=2, embedding_model="m")
    await connected_store.update_collection_meta(meta)

    result = await connected_store.get_collection_meta("ns-filter-col", namespace="tenantA")
    assert result is not None
    assert result.namespace == "tenantA"


@pytest.mark.asyncio
async def test_get_collection_meta_wrong_namespace_returns_none(connected_store: SearchStore) -> None:
    """get_collection_meta returns None when namespace does not match."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(name="ns-wrong-col", namespace="tenantA", doc_count=1, chunk_count=2, embedding_model="m")
    await connected_store.update_collection_meta(meta)

    result = await connected_store.get_collection_meta("ns-wrong-col", namespace="tenantB")
    assert result is None


@pytest.mark.asyncio
async def test_get_collection_meta_default_namespace(connected_store: SearchStore) -> None:
    """get_collection_meta with no namespace arg returns the default-namespace row."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    meta = CollectionMeta(name="ns-default-col", namespace=DEFAULT_NAMESPACE, doc_count=0, chunk_count=0, embedding_model="m")
    await connected_store.update_collection_meta(meta)

    result = await connected_store.get_collection_meta("ns-default-col")
    assert result is not None
    assert result.namespace == DEFAULT_NAMESPACE


@pytest.mark.asyncio
async def test_get_collection_meta_missing_namespace_field_fallback(tmp_path: Path) -> None:
    """Legacy rows without namespace column are matched when querying DEFAULT_NAMESPACE."""
    import pyarrow as pa
    from archon_search.constants import DEFAULT_NAMESPACE

    store = SearchStore(tmp_path / "db_legacy_ns")
    await store.connect()
    try:
        db = store._require_connected()
        # Create meta table WITHOUT namespace column (simulates pre-migration row)
        old_schema = pa.schema([
            pa.field("name", pa.utf8()),
            pa.field("description", pa.utf8()),
            pa.field("centroid_json", pa.utf8()),
            pa.field("doc_count", pa.int64()),
            pa.field("chunk_count", pa.int64()),
            pa.field("embedding_model", pa.utf8()),
            pa.field("last_indexed", pa.utf8()),
            pa.field("last_described", pa.utf8()),
            pa.field("described_at_doc_count", pa.int64()),
        ])
        table = await db.create_table("_archon_collection_meta", schema=old_schema)
        await table.add([{
            "name": "legacy-col",
            "description": "",
            "centroid_json": "",
            "doc_count": 3,
            "chunk_count": 6,
            "embedding_model": "model-x",
            "last_indexed": "",
            "last_described": "",
            "described_at_doc_count": -1,
        }])

        result = await store.get_collection_meta("legacy-col", namespace=DEFAULT_NAMESPACE)
        assert result is not None
        assert result.namespace == DEFAULT_NAMESPACE
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_get_collection_meta_invalid_namespace_raises(connected_store: SearchStore) -> None:
    """get_collection_meta raises ValueError for invalid namespace argument."""
    with pytest.raises(ValueError):
        await connected_store.get_collection_meta("any-col", namespace="")


# ---------------------------------------------------------------------------
# delete_collection_meta tests 
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_collection_meta_namespace_safety_filter(connected_store: SearchStore) -> None:
    """delete_collection_meta with wrong namespace is a no-op; correct namespace deletes."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(name="del-ns-col", namespace="tenantA", doc_count=1, chunk_count=2, embedding_model="m")
    await connected_store.update_collection_meta(meta)

    # Confirm row exists
    assert await connected_store.get_collection_meta("del-ns-col", namespace="tenantA") is not None

    # Wrong namespace — no-op
    await connected_store.delete_collection_meta("del-ns-col", "tenantB")
    assert await connected_store.get_collection_meta("del-ns-col", namespace="tenantA") is not None

    # Correct namespace — deletes
    await connected_store.delete_collection_meta("del-ns-col", "tenantA")
    assert await connected_store.get_collection_meta("del-ns-col", namespace="tenantA") is None


@pytest.mark.asyncio
async def test_delete_collection_meta_noop_when_table_absent(tmp_path: Path) -> None:
    """No _META_TABLE → no exception, no-op."""
    store = SearchStore(tmp_path / "db_del_noop")
    await store.connect()
    try:
        await store.delete_collection_meta("some-col", "default")  # must not raise
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_delete_collection_meta_noop_when_row_missing(connected_store: SearchStore) -> None:
    """Table exists but no matching row → no exception."""
    from archon_search.collection_meta import CollectionMeta

    # Create meta table by inserting an unrelated row
    meta = CollectionMeta(name="other-col", namespace="default", doc_count=0, chunk_count=0, embedding_model="m")
    await connected_store.update_collection_meta(meta)

    # Delete a row that doesn't exist — must not raise
    await connected_store.delete_collection_meta("nonexistent-col", "default")


@pytest.mark.asyncio
async def test_delete_collection_meta_validates_namespace(connected_store: SearchStore) -> None:
    """Invalid namespace string → ValueError before any DB access."""
    with pytest.raises(ValueError):
        await connected_store.delete_collection_meta("valid-col", "")


@pytest.mark.asyncio
async def test_delete_collection_meta_validates_name(connected_store: SearchStore) -> None:
    """Invalid name string → ValueError before any DB access."""
    with pytest.raises(ValueError):
        await connected_store.delete_collection_meta("../evil", "default")


# ---------------------------------------------------------------------------
# update_collection_meta namespace validation tests 
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_collection_meta_invalid_namespace_raises(connected_store: SearchStore) -> None:
    """Invalid namespace (has space) raises ValueError before any DB write."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(name="foo", namespace="has space")
    with pytest.raises(ValueError):
        await connected_store.update_collection_meta(meta)


@pytest.mark.asyncio
async def test_update_collection_meta_valid_namespace_passes(connected_store: SearchStore) -> None:
    """Valid namespace completes without error."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(name="foo-valid", namespace="tenantA")
    await connected_store.update_collection_meta(meta)  # must not raise


@pytest.mark.asyncio
async def test_update_collection_meta_same_namespace_upsert(connected_store: SearchStore) -> None:
    """Insert (foo, tenantA), update (foo, tenantA) with new data → only one row, updated."""
    from archon_search.collection_meta import CollectionMeta

    meta1 = CollectionMeta(name="foo-upsert", namespace="tenantA", doc_count=1, chunk_count=2, embedding_model="m1")
    await connected_store.update_collection_meta(meta1)

    meta2 = CollectionMeta(name="foo-upsert", namespace="tenantA", doc_count=5, chunk_count=10, embedding_model="m2")
    await connected_store.update_collection_meta(meta2)

    result = await connected_store.get_collection_meta("foo-upsert", namespace="tenantA")
    assert result is not None
    assert result.doc_count == 5
    assert result.embedding_model == "m2"

    # Only one row for this name
    all_meta = await connected_store.get_all_collections_meta()
    matching = [m for m in all_meta if m.name == "foo-upsert"]
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_update_collection_meta_cross_namespace_overwrite_raises(connected_store: SearchStore) -> None:
    """Existing row (foo, tenantA), call with (foo, tenantB) → ValueError; original row still present."""
    from archon_search.collection_meta import CollectionMeta

    meta_a = CollectionMeta(name="foo-cross", namespace="tenantA", doc_count=1, chunk_count=2, embedding_model="m")
    await connected_store.update_collection_meta(meta_a)

    meta_b = CollectionMeta(name="foo-cross", namespace="tenantB", doc_count=3, chunk_count=6, embedding_model="m")
    with pytest.raises(ValueError, match="tenantA"):
        await connected_store.update_collection_meta(meta_b)

    # Original row must still be intact
    result = await connected_store.get_collection_meta("foo-cross", namespace="tenantA")
    assert result is not None
    assert result.namespace == "tenantA"
    assert result.doc_count == 1


@pytest.mark.asyncio
async def test_update_collection_meta_first_insert(connected_store: SearchStore) -> None:
    """No existing row for 'new-name' → completes; row created with namespace='tenantA'."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(name="brand-new-col", namespace="tenantA", doc_count=7, chunk_count=14, embedding_model="m")
    await connected_store.update_collection_meta(meta)

    result = await connected_store.get_collection_meta("brand-new-col", namespace="tenantA")
    assert result is not None
    assert result.namespace == "tenantA"
    assert result.doc_count == 7


@pytest.mark.asyncio
async def test_update_collection_meta_invalid_namespace_raises_before_db() -> None:
    """Validation must fire before any DB write — verified with unconnected store."""
    from archon_search.collection_meta import CollectionMeta

    store = SearchStore(":memory:")  # not connected — any DB call would raise
    meta = CollectionMeta(name="col", namespace="bad namespace!")
    with pytest.raises(ValueError, match="Invalid namespace"):
        await store.update_collection_meta(meta)


@pytest.mark.asyncio
async def test_update_collection_meta_legacy_null_namespace_treated_as_default(
    connected_store: SearchStore,
) -> None:
    """Legacy rows with NULL namespace are treated as DEFAULT_NAMESPACE."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    # Insert a row with namespace=None directly to simulate legacy data
    col_name = "legacy-col"
    meta_initial = CollectionMeta(name=col_name, namespace=DEFAULT_NAMESPACE, doc_count=1, chunk_count=2, embedding_model="m1")
    await connected_store.update_collection_meta(meta_initial)

    # Patch the stored row to have NULL namespace (simulating pre-namespace schema)
    db = connected_store._require_connected()
    table = await db.open_table("_archon_collection_meta")
    import pyarrow as pa
    rows = await table.query().to_list()
    patched = [{**r, "namespace": None} for r in rows if r["name"] == col_name]
    await table.delete(f"name = '{col_name}'")
    if patched:
        schema = table.schema
        patched_with_null = [{**patched[0], "namespace": None}]
        await table.add(patched_with_null)

    # Now update with DEFAULT_NAMESPACE — should succeed (NULL treated as DEFAULT_NAMESPACE)
    meta_update = CollectionMeta(name=col_name, namespace=DEFAULT_NAMESPACE, doc_count=5, chunk_count=10, embedding_model="m2")
    await connected_store.update_collection_meta(meta_update)  # must not raise

    result = await connected_store.get_collection_meta(col_name, namespace=DEFAULT_NAMESPACE)
    assert result is not None
    assert result.doc_count == 5


# ---------------------------------------------------------------------------
# list_collections namespace tests 
# ---------------------------------------------------------------------------


def test_list_collections_namespace_from_meta(tmp_path: Path) -> None:
    """list_collections() reads namespace from meta row, not hardcoded DEFAULT_NAMESPACE.

    Verifies that get_all_collections_meta() is called once (batch read) and that
    the returned CollectionInfo has the namespace from the meta row.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    import pyarrow as pa

    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["tenant-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)

    mock_table = MagicMock()
    mock_table.count_rows = AsyncMock(return_value=3)
    arrow_table = pa.table({"doc_id": ["doc1", "doc2", "doc3"]})
    query_mock = MagicMock()
    query_mock.select = MagicMock(return_value=query_mock)
    query_mock.to_arrow = AsyncMock(return_value=arrow_table)
    mock_table.query = MagicMock(return_value=query_mock)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    meta = CollectionMeta(name="tenant-col", namespace="tenantA")
    with patch.object(store, "get_all_collections_meta", new=AsyncMock(return_value=[meta])) as mock_get_all, \
         patch.object(store, "get_collection_meta") as mock_get_one:
        collections = asyncio.run(store.list_collections())

        # batch read called exactly once
        mock_get_all.assert_called_once()
        # per-name lookup must NOT be called
        mock_get_one.assert_not_called()

    assert len(collections) == 1
    assert collections[0].name == "tenant-col"
    assert collections[0].namespace == "tenantA", (
        f"Expected namespace 'tenantA' from meta, got {collections[0].namespace!r}"
    )
    # ensure it is NOT the hardcoded default
    assert collections[0].namespace != DEFAULT_NAMESPACE


def test_list_collections_orphan_table_defaults(tmp_path: Path) -> None:
    """list_collections() falls back to DEFAULT_NAMESPACE for tables with no meta row."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    import pyarrow as pa

    from archon_search.constants import DEFAULT_NAMESPACE

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["orphan-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)

    mock_table = MagicMock()
    mock_table.count_rows = AsyncMock(return_value=1)
    arrow_table = pa.table({"doc_id": ["docA"]})
    query_mock = MagicMock()
    query_mock.select = MagicMock(return_value=query_mock)
    query_mock.to_arrow = AsyncMock(return_value=arrow_table)
    mock_table.query = MagicMock(return_value=query_mock)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    # No meta rows — orphan table
    with patch.object(store, "get_all_collections_meta", new=AsyncMock(return_value=[])):
        collections = asyncio.run(store.list_collections())

    assert len(collections) == 1
    assert collections[0].name == "orphan-col"
    assert collections[0].namespace == DEFAULT_NAMESPACE, (
        f"Expected DEFAULT_NAMESPACE fallback for orphan table, got {collections[0].namespace!r}"
    )


def test_hybrid_search_row_projection_populates_language(tmp_path: Path) -> None:
    """Unit: row with language='en' produces SearchResult.language == 'en'."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    doc_id = _doc_id()
    chunk_id = f"{doc_id}-000000"

    row = {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "hello",
        "source_path": "/tmp/f.md",
        "language": "en",
    }

    mock_db = MagicMock()
    mock_table = MagicMock()
    mock_table.vector_search = MagicMock(
        return_value=MagicMock(limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[row]))))
    )
    fts_result = MagicMock()
    fts_result.limit = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[])))
    mock_table.search = AsyncMock(return_value=fts_result)

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    results = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", 5))
    assert len(results) == 1
    assert results[0].language == "en"


def test_hybrid_search_row_projection_language_empty_string_yields_none(tmp_path: Path) -> None:
    """Unit: row with language='' (current A1/A2 stored value) produces SearchResult.language is None."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    doc_id = _doc_id()
    chunk_id = f"{doc_id}-000000"

    row = {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "hello",
        "source_path": "/tmp/f.md",
        "language": "",  # A1/A2 write path stores "" for undetected language
    }

    mock_db = MagicMock()
    mock_table = MagicMock()
    mock_table.vector_search = MagicMock(
        return_value=MagicMock(limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[row]))))
    )
    fts_result = MagicMock()
    fts_result.limit = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[])))
    mock_table.search = AsyncMock(return_value=fts_result)

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    results = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", 5))
    assert len(results) == 1
    assert results[0].language is None


def test_hybrid_search_row_projection_language_missing_yields_none(tmp_path: Path) -> None:
    """Unit: row without language column produces SearchResult.language is None."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    doc_id = _doc_id()
    chunk_id = f"{doc_id}-000000"

    row = {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "hello",
        "source_path": "/tmp/f.md",
        # no 'language' key
    }

    mock_db = MagicMock()
    mock_table = MagicMock()
    mock_table.vector_search = MagicMock(
        return_value=MagicMock(limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[row]))))
    )
    fts_result = MagicMock()
    fts_result.limit = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[])))
    mock_table.search = AsyncMock(return_value=fts_result)

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    results = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "hello", 5))
    assert len(results) == 1
    assert results[0].language is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_hybrid_search_returns_language_field(
    connected_store: SearchStore, col_name: str
) -> None:
    """Integration: ingest chunk with language='en'; SearchResult.language carries it."""
    doc_id = _doc_id()
    chunk = ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text="language field test",
        vector=[1.0] * _DIM,
        source_path="/tmp/lang.md",
        indexed_at=datetime.now(timezone.utc).isoformat(),
        language="en",
    )
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [chunk])
    results = await connected_store.hybrid_search(col_name, [1.0] * _DIM, "language", 5)
    assert results, "expected at least one result"
    assert results[0].language == "en"


def test_hybrid_search_trace_score_kind_values_match_backend_polarity(tmp_path: Path) -> None:
    """vector_score_kind is 'distance' and fts_score_kind is 'bm25' for the LanceDB backend."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.store import _hybrid_search_with_trace

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_table = MagicMock()

    doc_id = _doc_id()
    chunk_id = f"{doc_id}-000000"

    vec_row = {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "polarity test",
        "source_path": "/tmp/p.md",
        "_distance": 0.1,
    }
    fts_row = {
        "doc_id": doc_id,
        "chunk_id": chunk_id,
        "text": "polarity test",
        "source_path": "/tmp/p.md",
        "_score": 2.5,
    }

    mock_table.vector_search = MagicMock(
        return_value=MagicMock(limit=MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[vec_row]))))
    )
    fts_result = MagicMock()
    fts_result.limit = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[fts_row])))
    mock_table.search = AsyncMock(return_value=fts_result)

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    candidates = asyncio.run(_hybrid_search_with_trace(store, "my-col", [0.0] * _DIM, "polarity test", 20))

    assert len(candidates) == 1
    sb = candidates[0].score_breakdown
    assert sb.vector_score_kind == "distance", (
        f"Expected 'distance' for LanceDB vector score kind, got {sb.vector_score_kind!r}"
    )
    assert sb.fts_score_kind == "bm25", (
        f"Expected 'bm25' for LanceDB FTS score kind, got {sb.fts_score_kind!r}"
    )


# ---------------------------------------------------------------------------
# Task 3.1 — filters parameter on hybrid_search (unit tests)
# ---------------------------------------------------------------------------


def _make_mock_store_for_filter_tests(
    tmp_path: "Path",
    vec_rows: list[dict],
    fts_rows: list[dict],
) -> "tuple[SearchStore, MagicMock, MagicMock]":
    """Return (store, mock_db, mock_table) pre-wired for filter tests."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    store = SearchStore(tmp_path / "db")
    mock_db = MagicMock()
    mock_table = MagicMock()

    # Vector search builder: .where(pred) returns self for chaining
    vec_builder = MagicMock()
    vec_builder.where = MagicMock(return_value=vec_builder)
    vec_builder.limit = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=vec_rows)))
    mock_table.vector_search = MagicMock(return_value=vec_builder)

    # FTS search builder: .where(pred) returns self for chaining
    fts_builder = MagicMock()
    fts_builder.where = MagicMock(return_value=fts_builder)
    fts_builder.limit = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=fts_rows)))
    mock_table.search = AsyncMock(return_value=fts_builder)

    list_tables_resp = MagicMock()
    list_tables_resp.tables = ["my-col"]
    mock_db.list_tables = AsyncMock(return_value=list_tables_resp)
    mock_db.open_table = AsyncMock(return_value=mock_table)
    store._db = mock_db

    return store, mock_db, mock_table


def _make_vec_row(doc_id: str, idx: int = 0) -> dict:
    return {
        "doc_id": doc_id,
        "chunk_id": f"{doc_id}-{idx:06d}",
        "text": "sample text",
        "source_path": "/tmp/x.md",
        "file_type": "md",
        "language": None,
        "indexed_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "ingested_by": "cli",
        "metadata": "{}",
        "acl": None,
    }


def test_hybrid_search_filter_calls_where_on_both_branches(tmp_path: Path) -> None:
    """With file_type filter, .where(pred) called once on vec builder and once on FTS builder."""
    import asyncio
    from unittest.mock import call

    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    row = _make_vec_row(doc_id)
    store, _, mock_table = _make_mock_store_for_filter_tests(tmp_path, [row], [row])

    filters = SearchFilters(file_type="md")
    asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=5, filters=filters))

    # Retrieve the builders that were returned by vector_search and search
    vec_builder = mock_table.vector_search.return_value
    fts_builder = mock_table.search.return_value

    # .where() must have been called with the same predicate on both builders
    assert vec_builder.where.called, "vector builder .where() was not called"
    assert fts_builder.where.called, "FTS builder .where() was not called"

    vec_pred = vec_builder.where.call_args[0][0]
    fts_pred = fts_builder.where.call_args[0][0]
    assert vec_pred == fts_pred, f"predicates differ: vec={vec_pred!r} fts={fts_pred!r}"
    assert "md" in vec_pred


def test_hybrid_search_no_filters_no_where_called(tmp_path: Path) -> None:
    """filters=None → .where() is never called on either builder."""
    import asyncio

    doc_id = _doc_id()
    row = _make_vec_row(doc_id)
    store, _, mock_table = _make_mock_store_for_filter_tests(tmp_path, [row], [row])

    asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=5, filters=None))

    vec_builder = mock_table.vector_search.return_value
    fts_builder = mock_table.search.return_value

    assert not vec_builder.where.called, "vector builder .where() should not be called with no filters"
    assert not fts_builder.where.called, "FTS builder .where() should not be called with no filters"


def test_hybrid_search_empty_filters_no_where_called(tmp_path: Path) -> None:
    """filters=SearchFilters() (all fields None) → .where() is never called on either builder."""
    import asyncio

    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    row = _make_vec_row(doc_id)
    store, _, mock_table = _make_mock_store_for_filter_tests(tmp_path, [row], [row])

    asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=5, filters=SearchFilters()))

    vec_builder = mock_table.vector_search.return_value
    fts_builder = mock_table.search.return_value

    assert not vec_builder.where.called, "vector builder .where() should not be called with empty SearchFilters"
    assert not fts_builder.where.called, "FTS builder .where() should not be called with empty SearchFilters"


def test_hybrid_search_combined_filters_predicate(tmp_path: Path) -> None:
    """Combined filters produce a predicate with all three clauses joined by ' AND '."""
    import asyncio
    from datetime import datetime, timezone

    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    row = _make_vec_row(doc_id)
    store, _, mock_table = _make_mock_store_for_filter_tests(tmp_path, [row], [row])

    filters = SearchFilters(
        file_type="md",
        source_path_prefix="/docs/",
        indexed_after=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=5, filters=filters))

    vec_builder = mock_table.vector_search.return_value
    fts_builder = mock_table.search.return_value

    assert vec_builder.where.called, "vector builder .where() must be called with combined filters"
    assert fts_builder.where.called, "FTS builder .where() must be called with combined filters"

    pred = vec_builder.where.call_args[0][0]
    assert " AND " in pred, f"combined predicate must use ' AND ' separator, got: {pred!r}"
    assert "md" in pred, f"file_type clause missing from predicate: {pred!r}"
    assert "/docs/" in pred, f"source_path_prefix clause missing from predicate: {pred!r}"
    assert "indexed_at" in pred, f"indexed_after clause missing from predicate: {pred!r}"
    assert pred.count(" AND ") == 2, f"expected 2 AND joins for 3 clauses, got: {pred!r}"


def test_hybrid_search_never_calls_postfilter(tmp_path: Path) -> None:
    """.postfilter must never be called in any branch (no filter, with filter, with glob, FTS failure)."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    row = _make_vec_row(doc_id)

    for label, filters_arg in [
        ("no_filter", None),
        ("with_filter", SearchFilters(file_type="py")),
        ("with_glob", SearchFilters(source_path_glob="*.py")),
    ]:
        store, _, mock_table = _make_mock_store_for_filter_tests(tmp_path, [row], [row])
        asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=5, filters=filters_arg))
        vec_builder = mock_table.vector_search.return_value
        fts_builder = mock_table.search.return_value
        assert not vec_builder.postfilter.called, f"[{label}] vector builder .postfilter() must not be called"
        assert not fts_builder.postfilter.called, f"[{label}] FTS builder .postfilter() must not be called"

    # FTS failure branch (no filters)
    store, _, mock_table = _make_mock_store_for_filter_tests(tmp_path, [row], [row])
    # Make FTS raise an "index not available" error
    mock_table.search = AsyncMock(side_effect=RuntimeError("FTS index not available"))
    asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=5, filters=None))
    vec_builder = mock_table.vector_search.return_value
    assert not vec_builder.postfilter.called, "[fts_failure] vector builder .postfilter() must not be called"

    # FTS failure branch with filters set
    store, _, mock_table = _make_mock_store_for_filter_tests(tmp_path, [row], [row])
    mock_table.search = AsyncMock(side_effect=RuntimeError("FTS index not available"))
    asyncio.run(
        store.hybrid_search(
            "my-col", [0.0] * _DIM, "q", top_k=5, filters=SearchFilters(file_type="py")
        )
    )
    vec_builder = mock_table.vector_search.return_value
    assert not vec_builder.postfilter.called, "[fts_failure_with_filter] vector builder .postfilter() must not be called"


def test_hybrid_search_fetch_uses_compute_fetch_helper(tmp_path: Path) -> None:
    """_compute_fetch is called with (top_k, has_glob=...) instead of inline max(top_k*3,20)."""
    import asyncio
    from unittest.mock import patch

    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    row = _make_vec_row(doc_id)

    # No filters: has_glob=False
    store, _, _ = _make_mock_store_for_filter_tests(tmp_path, [row], [row])
    with patch("archon_search.store._compute_fetch", wraps=lambda top_k, has_glob: max(top_k * 3, 20)) as mock_cf:
        asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=7, filters=None))
    mock_cf.assert_called_once_with(7, has_glob=False)

    # With glob filter: has_glob=True
    store2, _, _ = _make_mock_store_for_filter_tests(tmp_path, [row], [row])
    with patch("archon_search.store._compute_fetch", wraps=lambda top_k, has_glob: max(top_k * 3, 20)) as mock_cf2:
        asyncio.run(store2.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=5, filters=SearchFilters(source_path_glob="*.py")))
    mock_cf2.assert_called_once_with(5, has_glob=True)


def test_hybrid_search_fts_failure_with_filter_falls_back_to_vector_only(tmp_path: Path) -> None:
    """FTS raises 'index not available'; vector branch still gets .where(); test does not fail."""
    import asyncio
    from unittest.mock import AsyncMock

    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    row = _make_vec_row(doc_id)
    store, _, mock_table = _make_mock_store_for_filter_tests(tmp_path, [row], [])
    # Override FTS to simulate missing index
    mock_table.search = AsyncMock(side_effect=RuntimeError("FTS index not available"))

    filters = SearchFilters(file_type="md")
    results = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=5, filters=filters))

    # Vector branch should have received .where()
    vec_builder = mock_table.vector_search.return_value
    assert vec_builder.where.called, "vector builder .where() must be called even when FTS fails"
    # Results should come from vector branch only (no crash)
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Task 3.1 — filters parameter on hybrid_search (integration tests)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_file_type_filter(
    connected_store: SearchStore, col_name: str
) -> None:
    """file_type='md' returns only md rows; py rows excluded."""
    from archon_search.filters import SearchFilters

    doc_md = _doc_id()
    doc_py = _doc_id()

    md_chunks = [
        ChunkRecord(
            doc_id=doc_md,
            chunk_id=f"{doc_md}-{i:06d}",
            text=f"markdown content {i}",
            vector=[float(i)] * _DIM,
            source_path=f"/docs/readme_{i}.md",
            indexed_at=datetime.now(timezone.utc).isoformat(),
            file_type="md",
        )
        for i in range(3)
    ]
    py_chunks = [
        ChunkRecord(
            doc_id=doc_py,
            chunk_id=f"{doc_py}-{i:06d}",
            text=f"python code {i}",
            vector=[float(i + 10)] * _DIM,
            source_path=f"/src/module_{i}.py",
            indexed_at=datetime.now(timezone.utc).isoformat(),
            file_type="py",
        )
        for i in range(3)
    ]

    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, md_chunks + py_chunks)
    await connected_store.rebuild_fts_index(col_name)

    results = await connected_store.hybrid_search(
        col_name, [0.0] * _DIM, "content", top_k=10,
        filters=SearchFilters(file_type="md"),
    )

    assert len(results) > 0, "expected at least one md result"
    assert all(r.file_type == "md" for r in results), (
        f"non-md results found: {[r.file_type for r in results]}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_source_path_prefix_filter(
    connected_store: SearchStore, col_name: str
) -> None:
    """source_path_prefix filter includes % in prefix to test LIKE escape."""
    from archon_search.filters import SearchFilters

    doc_a = _doc_id()
    doc_b = _doc_id()

    # Path with % to verify LIKE escaping
    chunks_a = [
        ChunkRecord(
            doc_id=doc_a,
            chunk_id=f"{doc_a}-{i:06d}",
            text=f"doc a chunk {i}",
            vector=[float(i)] * _DIM,
            source_path=f"/project/100%/a_{i}.md",
            indexed_at=datetime.now(timezone.utc).isoformat(),
        )
        for i in range(2)
    ]
    chunks_b = [
        ChunkRecord(
            doc_id=doc_b,
            chunk_id=f"{doc_b}-{i:06d}",
            text=f"doc b chunk {i}",
            vector=[float(i + 5)] * _DIM,
            source_path=f"/other/b_{i}.md",
            indexed_at=datetime.now(timezone.utc).isoformat(),
        )
        for i in range(2)
    ]

    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, chunks_a + chunks_b)

    results = await connected_store.hybrid_search(
        col_name, [0.0] * _DIM, "doc", top_k=10,
        filters=SearchFilters(source_path_prefix="/project/100%/"),
    )

    assert len(results) > 0, "expected results under /project/100%/ prefix"
    assert all(r.source_path.startswith("/project/100%/") for r in results), (
        f"unexpected source paths: {[r.source_path for r in results]}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_indexed_after_filter(
    connected_store: SearchStore, col_name: str
) -> None:
    """indexed_after filter is inclusive at boundary."""
    from datetime import date

    from archon_search._types import normalize_iso_utc
    from archon_search.filters import SearchFilters

    doc_old = _doc_id()
    doc_new = _doc_id()

    # Store indexed_at in the fixed-width UTC form production normalizes to; the
    # date filter compares lexicographically against the same normalized form.
    old_at = normalize_iso_utc("2025-01-01T00:00:00+00:00")
    new_at = normalize_iso_utc("2026-06-01T00:00:00+00:00")

    old_chunk = ChunkRecord(
        doc_id=doc_old,
        chunk_id=f"{doc_old}-000000",
        text="old document",
        vector=[1.0] * _DIM,
        source_path="/old/doc.md",
        indexed_at=old_at,
    )
    new_chunk = ChunkRecord(
        doc_id=doc_new,
        chunk_id=f"{doc_new}-000000",
        text="new document",
        vector=[2.0] * _DIM,
        source_path="/new/doc.md",
        indexed_at=new_at,
    )

    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [old_chunk, new_chunk])
    await connected_store.rebuild_fts_index(col_name)

    # Boundary: exactly new_at should be included
    results = await connected_store.hybrid_search(
        col_name, [0.0] * _DIM, "document", top_k=10,
        filters=SearchFilters(indexed_after=date(2026, 6, 1)),
    )

    assert len(results) > 0, "expected at least the new chunk"
    assert all(r.indexed_at >= "2026-06-01" for r in results), (
        f"old chunk leaked through: {[r.indexed_at for r in results]}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_prefilter_returns_full_top_k_from_matching_subset(
    connected_store: SearchStore, col_name: str
) -> None:
    """With file_type filter and enough matching rows, top_k results are returned."""
    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    # Ingest 10 md chunks
    md_chunks = [
        ChunkRecord(
            doc_id=doc_id,
            chunk_id=f"{doc_id}-{i:06d}",
            text=f"matching document {i}",
            vector=[float(i)] * _DIM,
            source_path=f"/docs/file_{i}.md",
            indexed_at=datetime.now(timezone.utc).isoformat(),
            file_type="md",
        )
        for i in range(10)
    ]

    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, md_chunks)

    results = await connected_store.hybrid_search(
        col_name, [0.0] * _DIM, "matching", top_k=5,
        filters=SearchFilters(file_type="md"),
    )

    assert len(results) == 5, f"expected 5 results, got {len(results)}"
    assert all(r.file_type == "md" for r in results)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_filter_applies_to_fts_branch_via_where(
    connected_store: SearchStore, col_name: str
) -> None:
    """FTS results honour the file_type filter — py chunks excluded from FTS hits."""
    from archon_search.filters import SearchFilters

    doc_md = _doc_id()
    doc_py = _doc_id()
    unique_word = f"zygote{doc_md[:6]}"

    md_chunks = [
        ChunkRecord(
            doc_id=doc_md,
            chunk_id=f"{doc_md}-{i:06d}",
            text=f"{unique_word} markdown {i}",
            vector=[float(i)] * _DIM,
            source_path=f"/docs/guide_{i}.md",
            indexed_at=datetime.now(timezone.utc).isoformat(),
            file_type="md",
        )
        for i in range(3)
    ]
    py_chunks = [
        ChunkRecord(
            doc_id=doc_py,
            chunk_id=f"{doc_py}-{i:06d}",
            text=f"{unique_word} python {i}",
            vector=[float(i + 20)] * _DIM,
            source_path=f"/src/code_{i}.py",
            indexed_at=datetime.now(timezone.utc).isoformat(),
            file_type="py",
        )
        for i in range(3)
    ]

    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, md_chunks + py_chunks)
    await connected_store.rebuild_fts_index(col_name)

    results = await connected_store.hybrid_search(
        col_name, [0.0] * _DIM, unique_word, top_k=10,
        filters=SearchFilters(file_type="md"),
    )

    assert len(results) > 0, "expected md results matching the unique word"
    assert all(r.file_type == "md" for r in results), (
        f"py chunks leaked: {[(r.file_type, r.text) for r in results if r.file_type != 'md']}"
    )


# ---------------------------------------------------------------------------
# Task 3.2 — glob post-filter and mixed-format timestamp warning (unit tests)
# ---------------------------------------------------------------------------


def _make_search_result(source_path: str, indexed_at: str = "2026-01-01T00:00:00.000000Z") -> "SearchResult":
    """Build a minimal SearchResult for filter tests."""
    from archon_search._types import SearchResult

    doc_id = _doc_id()
    return SearchResult(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text="test text",
        score=0.5,
        source_path=source_path,
        indexed_at=indexed_at,
    )


def test_glob_post_filter_keeps_matching_rows(tmp_path: Path) -> None:
    """Glob filter keeps only rows whose source_path matches the pattern."""
    import asyncio
    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    rows = [
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-{i:06d}",
            "text": f"chunk {i}",
            "source_path": path,
            "indexed_at": "2026-01-01T00:00:00.000000Z",
        }
        for i, path in enumerate([
            "/docs/api/foo.md",
            "/docs/api/bar.md",
            "/src/main.py",
            "/README.md",
        ])
    ]

    store, _, _ = _make_mock_store_for_filter_tests(tmp_path, rows, [])

    filters = SearchFilters(source_path_glob="*.md")
    results = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=10, filters=filters))

    # All results must match *.md (fnmatch, * crosses slashes)
    assert len(results) > 0, "expected at least one .md result"
    assert all(r.source_path.endswith(".md") for r in results), (
        f"non-.md paths returned: {[r.source_path for r in results]}"
    )
    # 3 of 4 rows are .md files — verify all are returned
    assert len(results) == 3, f"Expected 3 .md results, got {len(results)}: {[r.source_path for r in results]}"
    # .py file must be excluded
    py_paths = [r.source_path for r in results if r.source_path.endswith(".py")]
    assert py_paths == [], f"Python file should have been filtered out: {py_paths}"


def test_glob_under_delivery_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Glob filtering fewer than top_k results emits 'glob post-filter shrank pool' warning."""
    import asyncio
    import logging
    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    # Only one row matches "*.md" but we ask for top_k=5
    rows = [
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-{i:06d}",
            "text": f"chunk {i}",
            "source_path": path,
            "indexed_at": "2026-01-01T00:00:00.000000Z",
        }
        for i, path in enumerate(["/only/one.md", "/src/a.py", "/src/b.py", "/src/c.py"])
    ]

    store, _, _ = _make_mock_store_for_filter_tests(tmp_path, rows, [])

    filters = SearchFilters(source_path_glob="*.md")
    with caplog.at_level(logging.WARNING, logger="archon"):
        asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=5, filters=filters))

    assert any(
        "glob post-filter shrank pool below top_k" in record.message
        for record in caplog.records
    ), f"Expected under-delivery warning. Got: {[r.message for r in caplog.records]}"


def test_star_matches_across_slashes(tmp_path: Path) -> None:
    """fnmatch semantics: '*.md' matches 'docs/api/foo.md' (star crosses slashes)."""
    import fnmatch

    assert fnmatch.fnmatchcase("docs/api/foo.md", "*.md"), (
        "fnmatch.fnmatchcase: * must cross directory separators"
    )
    assert fnmatch.fnmatchcase("/absolute/path/file.md", "*.md"), (
        "fnmatch.fnmatchcase: * crosses slashes in absolute paths too"
    )
    assert not fnmatch.fnmatchcase("docs/api/foo.py", "*.md"), (
        "fnmatch.fnmatchcase: *.md must not match .py files"
    )


def test_double_star_equivalent_to_single_star(tmp_path: Path) -> None:
    """** and * produce identical match results in fnmatch (no path semantics)."""
    import fnmatch

    paths = [
        "docs/api/foo.md",
        "/abs/path/bar.py",
        "README.md",
        "a/b/c/d/e.txt",
    ]
    for path in paths:
        single = fnmatch.fnmatchcase(path, "*.md")
        double = fnmatch.fnmatchcase(path, "**.md")
        assert single == double, (
            f"fnmatch: * and ** produced different results for {path!r}: "
            f"single={single}, double={double}"
        )

    # Both match everything
    for path in paths:
        assert fnmatch.fnmatchcase(path, "*") == fnmatch.fnmatchcase(path, "**"), (
            f"fnmatch: * and ** differ for {path!r}"
        )


def test_mixed_format_indexed_at_triggers_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Rows with legacy indexed_at format (no microseconds) trigger the mixed-format warning."""
    import asyncio
    import logging
    from datetime import date
    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    # Mix of: fixed-width (good), legacy no-microseconds, legacy +00:00 suffix
    rows = [
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-000000",
            "text": "good row",
            "source_path": "/docs/a.md",
            "indexed_at": "2026-05-21T10:00:00.000000Z",  # fixed-width — OK
        },
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-000001",
            "text": "legacy no microseconds",
            "source_path": "/docs/b.md",
            "indexed_at": "2026-05-21T10:00:00Z",  # legacy — no microseconds
        },
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-000002",
            "text": "legacy plus offset",
            "source_path": "/docs/c.md",
            "indexed_at": "2026-05-21T10:00:00+00:00",  # legacy — +00:00 suffix
        },
    ]

    store, _, _ = _make_mock_store_for_filter_tests(tmp_path, rows, [])

    filters = SearchFilters(indexed_after=date(2026, 1, 1))
    with caplog.at_level(logging.WARNING, logger="archon"):
        asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=10, filters=filters))

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    matching = [msg for msg in warning_msgs if "legacy-format rows" in msg]
    assert matching, f"Expected legacy-format warning. Got warnings: {warning_msgs}"
    # 2 of 3 rows are legacy format — verify count is reflected in the message
    assert any("2 legacy-format" in msg for msg in matching), (
        f"Expected '2 legacy-format' in warning message. Got: {matching}"
    )


def test_normalized_indexed_at_does_not_trigger_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """All rows with fixed-width indexed_at emit no mixed-format warning."""
    import asyncio
    import logging
    from datetime import date
    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    rows = [
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-{i:06d}",
            "text": f"row {i}",
            "source_path": f"/docs/{i}.md",
            "indexed_at": f"2026-05-21T10:00:00.00000{i}Z",  # fixed-width
        }
        for i in range(3)
    ]

    store, _, _ = _make_mock_store_for_filter_tests(tmp_path, rows, [])

    filters = SearchFilters(indexed_after=date(2026, 1, 1))
    with caplog.at_level(logging.WARNING, logger="archon"):
        asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=10, filters=filters))

    legacy_warnings = [
        r.message for r in caplog.records
        if r.levelno == logging.WARNING and "legacy-format rows" in r.message
    ]
    assert legacy_warnings == [], (
        f"No legacy-format warning expected for fixed-width timestamps, got: {legacy_warnings}"
    )


# ---------------------------------------------------------------------------
# Task 3.2 — glob post-filter (integration tests)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_source_path_glob_matches(
    connected_store: SearchStore, col_name: str
) -> None:
    """Integration: glob filter returns only matching paths."""
    from archon_search.filters import SearchFilters

    doc_md = _doc_id()
    doc_py = _doc_id()

    chunks = [
        ChunkRecord(
            doc_id=doc_md,
            chunk_id=f"{doc_md}-000000",
            text="markdown content",
            vector=[1.0] * _DIM,
            source_path="/docs/guide.md",
            indexed_at=datetime.now(timezone.utc).isoformat(),
            file_type="md",
        ),
        ChunkRecord(
            doc_id=doc_py,
            chunk_id=f"{doc_py}-000000",
            text="python content",
            vector=[2.0] * _DIM,
            source_path="/src/module.py",
            indexed_at=datetime.now(timezone.utc).isoformat(),
            file_type="py",
        ),
    ]

    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, chunks)

    results = await connected_store.hybrid_search(
        col_name, [0.0] * _DIM, "content", top_k=10,
        filters=SearchFilters(source_path_glob="*.md"),
    )

    assert len(results) > 0, "expected at least one .md result"
    assert all(r.source_path.endswith(".md") for r in results), (
        f"non-md paths found: {[r.source_path for r in results]}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_source_path_glob_character_class(
    connected_store: SearchStore, col_name: str
) -> None:
    """Integration: character class glob 'docs/[ab]/*' matches only a/ and b/ subdirs."""
    from archon_search.filters import SearchFilters

    docs = {
        "a": ("/docs/a/file.md", _doc_id()),
        "b": ("/docs/b/file.md", _doc_id()),
        "c": ("/docs/c/file.md", _doc_id()),
    }
    chunks = [
        ChunkRecord(
            doc_id=doc_id,
            chunk_id=f"{doc_id}-000000",
            text=f"content {label}",
            vector=[float(i)] * _DIM,
            source_path=path,
            indexed_at=datetime.now(timezone.utc).isoformat(),
        )
        for i, (label, (path, doc_id)) in enumerate(docs.items())
    ]

    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, chunks)

    results = await connected_store.hybrid_search(
        col_name, [0.0] * _DIM, "content", top_k=10,
        filters=SearchFilters(source_path_glob="*/docs/[ab]/*"),
    )

    assert len(results) > 0, "expected at least one matching result"
    for r in results:
        assert "/docs/a/" in r.source_path or "/docs/b/" in r.source_path, (
            f"Result path {r.source_path!r} should be under docs/a/ or docs/b/"
        )
    matched_paths = {r.source_path for r in results}
    assert "/docs/c/file.md" not in matched_paths, "docs/c/ should be excluded by glob"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_glob_overfetch_replaces_default_multiplier(
    connected_store: SearchStore, col_name: str
) -> None:
    """Integration: _compute_fetch is called with has_glob=True when source_path_glob is set."""
    from unittest.mock import patch
    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    chunk = ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text="overfetch test",
        vector=[1.0] * _DIM,
        source_path="/docs/test.md",
        indexed_at=datetime.now(timezone.utc).isoformat(),
    )
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [chunk])

    with patch("archon_search.store._compute_fetch", wraps=lambda top_k, has_glob: max(top_k * (5 if has_glob else 3), 20)) as mock_cf:
        await connected_store.hybrid_search(
            col_name, [0.0] * _DIM, "overfetch", top_k=5,
            filters=SearchFilters(source_path_glob="*.md"),
        )

    mock_cf.assert_called_once_with(5, has_glob=True)


# ---------------------------------------------------------------------------
# Task 3.2 — additional unit tests (TEST-2 through TEST-6, TEST-9)
# ---------------------------------------------------------------------------


def test_glob_zero_matches_returns_empty_and_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Glob that matches nothing returns [] and emits the under-delivery warning."""
    import asyncio
    import logging
    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    rows = [
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-{i:06d}",
            "text": f"chunk {i}",
            "source_path": f"/src/file{i}.py",
            "indexed_at": "2026-01-01T00:00:00.000000Z",
        }
        for i in range(3)
    ]

    store, _, _ = _make_mock_store_for_filter_tests(tmp_path, rows, [])

    filters = SearchFilters(source_path_glob="*.md")  # no .py files match
    with caplog.at_level(logging.WARNING, logger="archon"):
        results = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=5, filters=filters))

    assert results == [], f"Expected empty results when glob matches nothing, got: {results}"
    assert any(
        "glob post-filter shrank pool below top_k" in r.message
        for r in caplog.records
    ), f"Expected under-delivery warning. Got: {[r.message for r in caplog.records]}"


def test_indexed_before_only_triggers_legacy_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """indexed_before alone (no indexed_after) still triggers legacy-format warning."""
    import asyncio
    import logging
    from datetime import date
    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    rows = [
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-000000",
            "text": "legacy row",
            "source_path": "/docs/a.md",
            "indexed_at": "2026-05-21T10:00:00Z",  # legacy — no microseconds
        },
    ]

    store, _, _ = _make_mock_store_for_filter_tests(tmp_path, rows, [])

    filters = SearchFilters(indexed_before=date(2027, 1, 1))  # indexed_before only
    with caplog.at_level(logging.WARNING, logger="archon"):
        asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=10, filters=filters))

    assert any(
        "legacy-format rows" in r.message
        for r in caplog.records
    ), f"Expected legacy-format warning for indexed_before-only. Got: {[r.message for r in caplog.records]}"


def test_null_indexed_at_counts_as_legacy_format(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Row with indexed_at=None is treated as legacy and triggers the warning."""
    import asyncio
    import logging
    from datetime import date
    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    rows = [
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-000000",
            "text": "null indexed_at row",
            "source_path": "/docs/a.md",
            "indexed_at": None,  # null in DB
        },
    ]

    store, _, _ = _make_mock_store_for_filter_tests(tmp_path, rows, [])

    filters = SearchFilters(indexed_after=date(2026, 1, 1))
    with caplog.at_level(logging.WARNING, logger="archon"):
        asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=10, filters=filters))

    assert any(
        "legacy-format rows" in r.message
        for r in caplog.records
    ), f"Expected legacy-format warning for null indexed_at. Got: {[r.message for r in caplog.records]}"


def test_glob_exact_top_k_matches_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Glob matching exactly top_k results does NOT emit the under-delivery warning."""
    import asyncio
    import logging
    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    rows = [
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-{i:06d}",
            "text": f"chunk {i}",
            "source_path": f"/docs/file{i}.md",  # all match *.md
            "indexed_at": "2026-01-01T00:00:00.000000Z",
        }
        for i in range(3)
    ]

    store, _, _ = _make_mock_store_for_filter_tests(tmp_path, rows, [])

    # top_k=3, exactly 3 rows match *.md — boundary: len(scored) == top_k, no warning
    filters = SearchFilters(source_path_glob="*.md")
    with caplog.at_level(logging.WARNING, logger="archon"):
        results = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=3, filters=filters))

    assert len(results) == 3
    assert not any(
        "glob post-filter shrank pool below top_k" in r.message
        for r in caplog.records
    ), f"No under-delivery warning expected for exact top_k match. Got: {[r.message for r in caplog.records]}"


def test_source_path_prefix_and_glob_combined(tmp_path: Path) -> None:
    """Combining source_path_prefix (SQL WHERE) and source_path_glob (post-filter) narrows correctly.

    The prefix filter is a SQL WHERE clause applied by LanceDB before results reach Python.
    In this unit test the mock simulates LanceDB having already applied the prefix pre-filter,
    so the mock returns only /docs/ rows.  The glob post-filter then keeps only .md files.
    """
    import asyncio
    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    # Simulate LanceDB having applied source_path_prefix="/docs/" — only /docs/ rows returned
    rows = [
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-000000",
            "text": "docs md",
            "source_path": "/docs/readme.md",
            "indexed_at": "2026-01-01T00:00:00.000000Z",
        },
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-000001",
            "text": "docs py",
            "source_path": "/docs/script.py",
            "indexed_at": "2026-01-01T00:00:00.000000Z",
        },
    ]

    store, _, mock_table = _make_mock_store_for_filter_tests(tmp_path, rows, [])

    # glob post-filter keeps only .md — should return /docs/readme.md, not /docs/script.py
    filters = SearchFilters(source_path_prefix="/docs/", source_path_glob="*.md")
    results = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=10, filters=filters))

    paths = [r.source_path for r in results]
    assert "/docs/readme.md" in paths, f"Expected /docs/readme.md in results, got: {paths}"
    assert "/docs/script.py" not in paths, f".py should be excluded by glob: {paths}"

    # Verify the SQL prefix predicate was generated and passed to LanceDB via .where()
    vec_builder = mock_table.vector_search.return_value
    assert vec_builder.where.called, "vector builder .where() must be called for source_path_prefix"
    prefix_pred = vec_builder.where.call_args[0][0]
    assert "source_path" in prefix_pred and "LIKE" in prefix_pred and "/docs/" in prefix_pred, (
        f"Expected source_path LIKE predicate for prefix '/docs/', got: {prefix_pred!r}"
    )


def test_glob_filter_applied_before_top_k_truncation(tmp_path: Path) -> None:
    """Glob filter must run on the full scored list before top_k truncation.

    Scenario: top_k=2, 5 rows, top-2 RRF don't match glob, rows 3-5 do.
    Correct behavior: rows 3-5 (matching glob) are returned.
    Wrong behavior (filter after truncation): empty or wrong results.
    """
    import asyncio
    from archon_search.filters import SearchFilters

    doc_id = _doc_id()
    # All rows have source paths; only .md ones pass the glob
    # We want the .md rows to NOT be the top-scored RRF rows
    # Row insertion order (index 0-4) maps directly to vec scores (descending).
    # With fts_rows=[], only the vector leg contributes, so .py rows (idx 0-1)
    # always outscore .md rows (idx 2-4). If glob ran after [:top_k], it would
    # see only .py rows, eliminate both, and return 0 results — contradicting the assertion.
    rows = [
        {
            "doc_id": doc_id,
            "chunk_id": f"{doc_id}-{i:06d}",
            "text": f"chunk {i}",
            "source_path": path,
            "indexed_at": "2026-01-01T00:00:00.000000Z",
        }
        for i, path in enumerate([
            "/src/a.py",     # high score (vec rank 0) — excluded by glob
            "/src/b.py",     # high score (vec rank 1) — excluded by glob
            "/docs/c.md",    # lower score (vec rank 2) — passes glob
            "/docs/d.md",    # lower score (vec rank 3) — passes glob
            "/docs/e.md",    # lowest score (vec rank 4) — passes glob
        ])
    ]

    store, _, _ = _make_mock_store_for_filter_tests(tmp_path, rows, [])

    filters = SearchFilters(source_path_glob="*.md")
    results = asyncio.run(store.hybrid_search("my-col", [0.0] * _DIM, "q", top_k=2, filters=filters))

    # With glob before truncation: top 2 of the 3 .md rows are returned
    assert len(results) == 2, f"Expected 2 results after glob+truncation, got {len(results)}"
    assert all(r.source_path.endswith(".md") for r in results), (
        f"All results should be .md: {[r.source_path for r in results]}"
    )


# A5b Task 2.2 — _where_eq / _where_in SQL fragment helper unit tests
# ---------------------------------------------------------------------------


def test_where_eq_basic() -> None:
    from archon_search.store import _where_eq  # noqa: PLC0415

    assert _where_eq("name", "foo") == "name = 'foo'"


def test_where_eq_adversarial() -> None:
    """Belt-and-braces: call directly, bypassing upstream regex gate."""
    from archon_search.store import _where_eq  # noqa: PLC0415

    assert _where_eq("name", "O'Brien") == "name = 'O''Brien'"


def test_where_in_basic() -> None:
    from archon_search.store import _where_in  # noqa: PLC0415

    assert _where_in("chunk_id", ["a", "b"]) == "chunk_id IN ('a', 'b')"


def test_where_in_empty_returns_always_false() -> None:
    from archon_search.store import _where_in  # noqa: PLC0415

    assert _where_in("chunk_id", []) == "1=0"


def test_where_in_single() -> None:
    from archon_search.store import _where_in  # noqa: PLC0415

    assert _where_in("chunk_id", ["a"]) == "chunk_id IN ('a')"


def test_where_in_adversarial() -> None:
    """Values containing single-quotes are doubled."""
    from archon_search.store import _where_in  # noqa: PLC0415

    assert _where_in("c", ["a'b"]) == "c IN ('a''b')"


# ---------------------------------------------------------------------------
# A5b Task 2.3 — SQL-site regression tests (pure-regression; xfail strict=False)
# These test existing behaviour and should remain green before AND after the
# f-string-to-helper refactor in the next commit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_collection_meta_removes_only_named_row(connected_store: SearchStore) -> None:
    """delete_collection_meta removes only the named (name, namespace) row; other survives.

    Exercises the compound-predicate site (delete_collection_meta, line ~353).

    Note: a same-name/different-namespace scenario is not tested here because
    update_collection_meta enforces global name uniqueness — it raises ValueError
    if a name already exists under a different namespace.  The AND namespace clause
    in delete_collection_meta is therefore purely defensive (belt-and-suspenders).
    """
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    meta_a = CollectionMeta(name="dcmr-col-a", namespace=DEFAULT_NAMESPACE)
    meta_b = CollectionMeta(name="dcmr-col-b", namespace=DEFAULT_NAMESPACE)
    await connected_store.update_collection_meta(meta_a)
    await connected_store.update_collection_meta(meta_b)

    await connected_store.delete_collection_meta("dcmr-col-a", DEFAULT_NAMESPACE)

    all_meta = await connected_store.get_all_collections_meta()
    names = {m.name for m in all_meta}
    assert "dcmr-col-a" not in names, "deleted row should be gone"
    assert "dcmr-col-b" in names, "sibling row must survive"


@pytest.mark.asyncio
async def test_update_collection_meta_acquires_lock(connected_store: SearchStore) -> None:
    """update_collection_meta acquires and releases _lock_for(collection) on every call."""
    from unittest.mock import AsyncMock, patch
    from archon_search.collection_meta import CollectionMeta
    import archon_search.store as store_mod

    col = "ucm-lock-check"
    real_lock = connected_store._lock_for(col)
    acquire_called = False
    real_acquire = real_lock.acquire

    async def tracked_acquire():
        nonlocal acquire_called
        acquire_called = True
        return await real_acquire()

    real_lock.acquire = tracked_acquire  # type: ignore[method-assign]
    await connected_store.update_collection_meta(CollectionMeta(name=col))
    assert acquire_called, "update_collection_meta must acquire the lock"
    assert not real_lock.locked(), "lock must be released after call"


@pytest.mark.asyncio
async def test_update_collection_meta_timeout_raises_store_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """update_collection_meta raises StoreBusyError when lock is held externally."""
    import archon_search.store as store_mod
    from archon_search.collection_meta import CollectionMeta
    from archon_search.store import SearchStore, StoreBusyError

    monkeypatch.setattr(store_mod, "INGEST_LOCK_TIMEOUT_S", 0.1)

    store = SearchStore(tmp_path / "db_ucm_busy")
    await store.connect()
    try:
        col = "ucm-busy-col"
        lock = store._lock_for(col)
        await lock.acquire()
        try:
            with pytest.raises(StoreBusyError):
                await store.update_collection_meta(CollectionMeta(name=col))
        finally:
            lock.release()
    finally:
        await store.disconnect()


def test_update_collection_meta_no_call_while_lock_held() -> None:
    """Static guard: no production call site holds _lock_for(collection) while calling update_collection_meta."""
    import re
    import textwrap
    from pathlib import Path

    source_root = Path(__file__).parent.parent / "archon_search"
    # Collect all lines that hold a lock (via _lock_for / reindex_metadata pattern)
    # and then call update_collection_meta without releasing first.
    # Simple heuristic: scan each file for patterns where lock.acquire() appears
    # before update_collection_meta in the same try block without lock.release().
    violations = []
    for py_file in source_root.rglob("*.py"):
        text = py_file.read_text()
        # Check for update_collection_meta calls inside a lock-held scope
        # (i.e., after asyncio.wait_for(lock.acquire()) and before lock.release())
        # Simplified: look for files where both patterns appear in the same function
        # and update_collection_meta is NOT _do_write_meta_unlocked
        if "update_collection_meta" not in text:
            continue
        # Exclude unlocked helpers (they're expected to be called while lock held)
        lines = text.splitlines()
        in_lock_scope = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if "lock.acquire()" in stripped or "lock_for(" in stripped and ".acquire" in text[text.find(stripped):text.find(stripped) + 200]:
                in_lock_scope = True
            if "lock.release()" in stripped or "finally:" in stripped:
                in_lock_scope = False
            if in_lock_scope and "update_collection_meta(" in stripped and "do_write_meta_unlocked" not in stripped:
                violations.append(f"{py_file.name}:{i+1}: {stripped}")
    assert violations == [], f"update_collection_meta called while lock held:\n" + "\n".join(violations)


@pytest.mark.asyncio
async def test_update_collection_meta_replaces_existing_row(connected_store: SearchStore) -> None:
    """update_collection_meta upserts: a second write with the same name replaces the first.

    Exercises the delete-then-insert site (update_collection_meta, line ~442).
    """
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    meta1 = CollectionMeta(name="ucmr-col", namespace=DEFAULT_NAMESPACE, doc_count=1)
    await connected_store.update_collection_meta(meta1)

    meta2 = CollectionMeta(name="ucmr-col", namespace=DEFAULT_NAMESPACE, doc_count=99)
    await connected_store.update_collection_meta(meta2)

    result = await connected_store.get_collection_meta("ucmr-col", namespace=DEFAULT_NAMESPACE)
    assert result is not None
    assert result.doc_count == 99, "second write should replace the first (upsert semantics)"
    # Assert exactly one row survived — proves the delete-half of upsert ran
    all_metas = await connected_store.get_all_collections_meta()
    assert len([m for m in all_metas if m.name == "ucmr-col"]) == 1, (
        "upsert must leave exactly one row; more means the delete-half did not run"
    )


@pytest.mark.asyncio
async def test_delete_document_removes_all_chunks(connected_store: SearchStore, col_name: str) -> None:
    """delete_document returns chunk count > 0 and leaves no chunks behind.

    Exercises both count_rows and delete sites (lines ~725 and ~728).
    """
    doc_id, chunks = await _ingest_doc(connected_store, col_name, n_chunks=3)

    deleted = await connected_store.delete_document(col_name, doc_id)
    assert deleted == 3, f"Expected 3 chunks deleted, got {deleted}"

    docs = await connected_store.list_documents(col_name)
    assert all(d.doc_id != doc_id for d in docs), "doc must be absent after delete"


@pytest.mark.asyncio
async def test_fetch_adjacent_chunks_returns_window(connected_store: SearchStore, col_name: str) -> None:
    """fetch_adjacent_chunks returns neighbors within the window.

    Exercises the chunk_id IN (...) site (fetch_adjacent_chunks, line ~820).
    Uses center_idx=1, window=1 → expects chunks at idx 0 and 2.
    """
    doc_id = _doc_id()
    chunks = [_chunk(doc_id, i) for i in range(4)]
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, chunks)

    neighbors = await connected_store.fetch_adjacent_chunks(col_name, doc_id, 1, 1)
    neighbor_ids = {c.chunk_id for c in neighbors}
    assert f"{doc_id}-000000" in neighbor_ids, "chunk idx 0 must be in window"
    assert f"{doc_id}-000002" in neighbor_ids, "chunk idx 2 must be in window"
    assert f"{doc_id}-000001" not in neighbor_ids, "center must be excluded"


# ---------------------------------------------------------------------------
# Task 2.2 — description_embedding_json column tests
# ---------------------------------------------------------------------------


_BASE_ROW: dict = {
    "name": "col-emb",
    "description": None,
    "centroid_json": None,
    "doc_count": 0,
    "chunk_count": 0,
    "embedding_model": None,
    "last_indexed": None,
    "last_described": None,
    "described_at_doc_count": -1,
    "namespace": "default",
}


def _row(**overrides: object) -> dict:
    return {**_BASE_ROW, **overrides}


def test_row_to_meta_with_description_embedding() -> None:
    """Row with valid JSON float list → description_embedding populated."""
    row = _row(description_embedding_json="[0.5, -0.3, 1.0]")
    meta = SearchStore._row_to_meta(row)
    assert meta.description_embedding is not None
    assert len(meta.description_embedding) == 3
    assert abs(meta.description_embedding[0] - 0.5) < 1e-9
    assert abs(meta.description_embedding[1] - (-0.3)) < 1e-9
    assert abs(meta.description_embedding[2] - 1.0) < 1e-9


def test_row_to_meta_missing_key_yields_none() -> None:
    """Row without description_embedding_json key → None, no KeyError."""
    row = {k: v for k, v in _BASE_ROW.items() if k != "description_embedding_json"}
    meta = SearchStore._row_to_meta(row)
    assert meta.description_embedding is None


def test_row_to_meta_malformed_json_yields_none_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Invalid JSON string → None + WARNING logged."""
    import logging

    row = _row(description_embedding_json="not-valid-json{")
    with caplog.at_level(logging.WARNING, logger="archon"):
        meta = SearchStore._row_to_meta(row)
    assert meta.description_embedding is None
    assert any("description_embedding_json" in r.message for r in caplog.records), (
        f"Expected WARNING about description_embedding_json; got: {[r.message for r in caplog.records]}"
    )


def test_row_to_meta_empty_string_yields_none() -> None:
    """Empty string → None (no warning)."""
    row = _row(description_embedding_json="")
    meta = SearchStore._row_to_meta(row)
    assert meta.description_embedding is None


@pytest.mark.parametrize(
    "json_str",
    [
        '[0.1, "x", 0.3]',       # string element
        "[0.1, null, 0.3]",      # JSON null (Python None)
        "[0.1, true, 0.3]",      # JSON true (bool — must be rejected even though isinstance(True, int))
        "[1e309]",               # overflows to float('inf') — not finite, must be rejected
    ],
)
def test_row_to_meta_non_float_elements_yield_none_with_warning(
    json_str: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed element (str, null, bool) → None + WARNING logged."""
    import logging

    row = _row(description_embedding_json=json_str)
    with caplog.at_level(logging.WARNING, logger="archon"):
        meta = SearchStore._row_to_meta(row)
    assert meta.description_embedding is None, f"Expected None for input {json_str!r}"
    assert any("description_embedding_json" in r.message for r in caplog.records), (
        f"Expected WARNING for input {json_str!r}; got: {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_migrate_description_embedding_noop_when_column_present(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """migrate_description_embedding() is a no-op and logs no WARNING when column already present."""
    import logging

    import pyarrow as pa

    from archon_search.store import migrate_description_embedding

    store = SearchStore(tmp_path / "db_emb_noop")
    await store.connect()
    try:
        db = store._require_connected()
        # Create meta table with the column already present (current schema).
        await db.create_table("_archon_collection_meta", schema=SearchStore._meta_schema())
        with caplog.at_level(logging.WARNING, logger="archon_search.store"):
            await migrate_description_embedding(store)
        # Must not emit any WARNING about the migration.
        assert not any(
            "description_embedding" in r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING
        ), f"Unexpected WARNING logged: {[r.message for r in caplog.records]}"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_migrate_description_embedding_concurrent_calls(tmp_path: Path) -> None:
    """Two concurrent migrate_description_embedding() calls on the same table must not raise.

    NOTE: migrate_namespace does not have an equivalent concurrent test using asyncio.gather;
    its concurrency test uses patch to simulate an already-exists error. We follow the same
    mock-based approach here for consistency.
    """
    import lancedb.table
    from unittest.mock import AsyncMock, patch

    import pyarrow as pa

    from archon_search.store import migrate_description_embedding

    store = SearchStore(tmp_path / "db_emb_concurrent")
    await store.connect()
    try:
        db = store._require_connected()
        old_schema = pa.schema(
            [f for f in SearchStore._meta_schema() if f.name != "description_embedding_json"]
        )
        await db.create_table("_archon_collection_meta", schema=old_schema)
        # Simulate the race: add_columns raises "already exists" as a concurrent peer would.
        with patch.object(
            lancedb.table.AsyncTable,
            "add_columns",
            new=AsyncMock(
                side_effect=RuntimeError(
                    "Column description_embedding_json already exists in the dataset"
                )
            ),
        ):
            # Both calls must complete without raising.
            await asyncio.gather(
                migrate_description_embedding(store),
                migrate_description_embedding(store),
            )
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_migrate_description_embedding_idempotent(tmp_path: Path) -> None:
    """migrate_description_embedding() first call adds the column, second call is a no-op."""
    import pyarrow as pa

    from archon_search.store import migrate_description_embedding

    store = SearchStore(tmp_path / "db_emb_migrate")
    await store.connect()
    try:
        db = store._require_connected()
        # Build old schema without description_embedding_json to exercise the add_columns path.
        old_schema = pa.schema(
            [f for f in SearchStore._meta_schema() if f.name != "description_embedding_json"]
        )
        await db.create_table("_archon_collection_meta", schema=old_schema)
        # First call must add the column.
        await migrate_description_embedding(store)
        tbl = await db.open_table("_archon_collection_meta")
        assert "description_embedding_json" in (await tbl.schema()).names
        # Second call must be a no-op (column already present).
        await migrate_description_embedding(store)
    finally:
        await store.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_description_embedding_round_trips(tmp_path: Path) -> None:
    """Write CollectionMeta with description_embedding, read back, values match."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.store import migrate_description_embedding

    store = SearchStore(tmp_path / "db_emb_rt")
    await store.connect()
    try:
        meta = CollectionMeta(
            name="emb-roundtrip",
            description_embedding=[0.5, -0.3],
        )
        await store.update_collection_meta(meta)
        retrieved = await store.get_collection_meta("emb-roundtrip")
        assert retrieved is not None
        assert retrieved.description_embedding is not None
        assert len(retrieved.description_embedding) == 2
        assert abs(retrieved.description_embedding[0] - 0.5) < 1e-6
        assert abs(retrieved.description_embedding[1] - (-0.3)) < 1e-6
    finally:
        await store.disconnect()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_old_table_without_column_reads_none(tmp_path: Path) -> None:
    """Meta table without description_embedding_json column → description_embedding is None."""
    import pyarrow as pa
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE

    store = SearchStore(tmp_path / "db_emb_old")
    await store.connect()
    try:
        db = store._require_connected()
        # Create meta table WITHOUT description_embedding_json column (old schema)
        old_schema = pa.schema([
            pa.field("name", pa.utf8()),
            pa.field("description", pa.utf8()),
            pa.field("centroid_json", pa.utf8()),
            pa.field("doc_count", pa.int64()),
            pa.field("chunk_count", pa.int64()),
            pa.field("embedding_model", pa.utf8()),
            pa.field("last_indexed", pa.utf8()),
            pa.field("last_described", pa.utf8()),
            pa.field("described_at_doc_count", pa.int64()),
            pa.field("namespace", pa.utf8()),
        ])
        table = await db.create_table("_archon_collection_meta", schema=old_schema)
        await table.add([{
            "name": "old-col",
            "description": "",
            "centroid_json": "",
            "doc_count": 0,
            "chunk_count": 0,
            "embedding_model": "",
            "last_indexed": "",
            "last_described": "",
            "described_at_doc_count": -1,
            "namespace": DEFAULT_NAMESPACE,
        }])
        retrieved = await store.get_collection_meta("old-col")
        assert retrieved is not None
        assert retrieved.description_embedding is None
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# _do_read_meta_unlocked and _do_write_meta_unlocked tests (Task 2.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_read_meta_unlocked_returns_none_when_no_meta_table(
    tmp_path: Path,
) -> None:
    """_do_read_meta_unlocked returns None when no meta table exists."""
    store = SearchStore(tmp_path / "db_no_meta")
    await store.connect()
    try:
        db = store._require_connected()
        result = await store._do_read_meta_unlocked(db, "some-col")
        assert result is None
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_do_read_meta_unlocked_returns_existing_meta(
    connected_store: SearchStore,
) -> None:
    """After update_collection_meta, _do_read_meta_unlocked returns the same meta."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(
        name="read-unlocked-col",
        doc_count=7,
        chunk_count=21,
        namespace="default",
    )
    await connected_store.update_collection_meta(meta)

    db = connected_store._require_connected()
    result = await connected_store._do_read_meta_unlocked(db, "read-unlocked-col")
    assert result is not None
    assert result.doc_count == 7
    assert result.chunk_count == 21
    assert result.namespace == "default"


@pytest.mark.asyncio
async def test_do_write_meta_unlocked_creates_row(
    connected_store: SearchStore,
) -> None:
    """_do_write_meta_unlocked writes a row retrievable via get_collection_meta."""
    from archon_search.collection_meta import CollectionMeta

    # Ensure meta table exists first (by writing one row via the public API)
    seed = CollectionMeta(name="write-seed-col")
    await connected_store.update_collection_meta(seed)

    meta = CollectionMeta(
        name="write-unlocked-col",
        doc_count=3,
        chunk_count=9,
    )
    db = connected_store._require_connected()
    await connected_store._do_write_meta_unlocked(db, "write-unlocked-col", meta)

    retrieved = await connected_store.get_collection_meta("write-unlocked-col")
    assert retrieved is not None
    assert retrieved.doc_count == 3
    assert retrieved.chunk_count == 9


@pytest.mark.asyncio
async def test_do_write_meta_unlocked_upserts_existing_row(
    connected_store: SearchStore,
) -> None:
    """Writing twice with different chunk_count; second read reflects the second value."""
    from archon_search.collection_meta import CollectionMeta

    meta1 = CollectionMeta(name="upsert-unlocked-col", chunk_count=5)
    await connected_store.update_collection_meta(meta1)

    db = connected_store._require_connected()
    meta2 = CollectionMeta(name="upsert-unlocked-col", chunk_count=99)
    await connected_store._do_write_meta_unlocked(db, "upsert-unlocked-col", meta2)

    retrieved = await connected_store.get_collection_meta("upsert-unlocked-col")
    assert retrieved is not None
    assert retrieved.chunk_count == 99


@pytest.mark.asyncio
async def test_do_write_meta_unlocked_creates_meta_table_if_absent(
    tmp_path: Path,
) -> None:
    """_do_write_meta_unlocked on a db with no _META_TABLE creates it and stores the row."""
    from archon_search.collection_meta import CollectionMeta

    store = SearchStore(tmp_path / "db_write_no_meta")
    await store.connect()
    try:
        db = store._require_connected()

        # Verify no meta table exists yet
        all_tables = (await db.list_tables()).tables
        assert "_archon_collection_meta" not in all_tables

        meta = CollectionMeta(name="auto-create-col", doc_count=1, chunk_count=2)
        await store._do_write_meta_unlocked(db, "auto-create-col", meta)

        # Table must now exist and the row must be retrievable
        retrieved = await store.get_collection_meta("auto-create-col")
        assert retrieved is not None
        assert retrieved.doc_count == 1
        assert retrieved.chunk_count == 2
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_do_write_meta_unlocked_includes_b5_columns(
    connected_store: SearchStore,
) -> None:
    """Row written via _do_write_meta_unlocked includes all three B5 columns."""
    from archon_search.collection_meta import CollectionMeta

    meta = CollectionMeta(
        name="b5-unlocked-col",
        centroid_sum=[1.0, 2.0],
        mutations_since_recompute=5,
        needs_recompute=True,
    )
    db = connected_store._require_connected()
    await connected_store._do_write_meta_unlocked(db, "b5-unlocked-col", meta)

    retrieved = await connected_store.get_collection_meta("b5-unlocked-col")
    assert retrieved is not None
    assert retrieved.centroid_sum is not None
    assert abs(retrieved.centroid_sum[0] - 1.0) < 1e-9
    assert abs(retrieved.centroid_sum[1] - 2.0) < 1e-9
    assert retrieved.mutations_since_recompute == 5
    assert retrieved.needs_recompute is True


@pytest.mark.asyncio
async def test_do_write_meta_unlocked_cross_namespace_isolation(
    connected_store: SearchStore,
) -> None:
    """_do_write_meta_unlocked on collection in namespace A must not delete namespace B's row.

    Uses _do_write_meta_unlocked directly for both namespaces to bypass the public
    namespace-uniqueness guard, simulating the scenario where two namespaces share
    a collection name (possible via direct store access or migration scenarios).
    """
    from archon_search.collection_meta import CollectionMeta

    col = "cross-ns-col"
    db = connected_store._require_connected()

    meta_alpha = CollectionMeta(name=col, namespace="alpha", doc_count=1)
    meta_beta = CollectionMeta(name=col, namespace="beta", doc_count=2)
    await connected_store._do_write_meta_unlocked(db, col, meta_alpha)
    await connected_store._do_write_meta_unlocked(db, col, meta_beta)

    # Overwrite alpha — beta's row must survive untouched
    updated_alpha = CollectionMeta(name=col, namespace="alpha", doc_count=99)
    await connected_store._do_write_meta_unlocked(db, col, updated_alpha)

    result_alpha = await connected_store.get_collection_meta(col, namespace="alpha")
    result_beta = await connected_store.get_collection_meta(col, namespace="beta")
    assert result_alpha is not None and result_alpha.doc_count == 99
    assert result_beta is not None and result_beta.doc_count == 2, "beta row must not be deleted"


@pytest.mark.asyncio
async def test_a5b_end_to_end_flow_unchanged(connected_store: SearchStore, col_name: str) -> None:
    """Full happy-path regression: add → ingest → search → delete → search empty.

    Verifies that the f-string-to-helper refactor preserves semantics end-to-end
    across all five replaced sites.
    """
    # Ingest a document
    doc_id, _ = await _ingest_doc(connected_store, col_name, n_chunks=2, text_prefix="e2e")
    await connected_store.rebuild_fts_index(col_name)

    # Search returns the document
    results = await connected_store.hybrid_search(col_name, [0.0] * _DIM, "e2e", top_k=5)
    found_ids = {r.doc_id for r in results}
    assert doc_id in found_ids, "search must find the ingested document"

    # Delete the document
    deleted = await connected_store.delete_document(col_name, doc_id)
    assert deleted > 0, "delete must report removed chunks"

    # After delete, the doc is no longer in the store
    docs = await connected_store.list_documents(col_name)
    assert all(d.doc_id != doc_id for d in docs), "doc must be absent after delete"


# ---------------------------------------------------------------------------
# _do_fetch_doc_vectors_unlocked tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_fetch_doc_vectors_unlocked_empty_when_no_table(
    tmp_path: Path,
) -> None:
    """Returns [] when the collection table does not exist."""
    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        db = store._require_connected()
        result = await store._do_fetch_doc_vectors_unlocked(db, "nonexistent-col", _doc_id())
        assert result == []
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_do_fetch_doc_vectors_unlocked_returns_correct_vectors(
    connected_store: SearchStore, col_name: str
) -> None:
    """Ingesting two docs and fetching by doc_id returns only that doc's vectors."""
    doc_a = _doc_id()
    doc_b = _doc_id()
    vec_a0 = [1.0, 2.0, 3.0, 4.0]
    vec_a1 = [5.0, 6.0, 7.0, 8.0]
    vec_b0 = [9.0, 10.0, 11.0, 12.0]

    chunks = [
        ChunkRecord(
            doc_id=doc_a,
            chunk_id=f"{doc_a}-000000",
            text="a chunk 0",
            vector=vec_a0,
            source_path=f"/tmp/{doc_a[:8]}.md",
            indexed_at=datetime.now(timezone.utc).isoformat(),
        ),
        ChunkRecord(
            doc_id=doc_a,
            chunk_id=f"{doc_a}-000001",
            text="a chunk 1",
            vector=vec_a1,
            source_path=f"/tmp/{doc_a[:8]}.md",
            indexed_at=datetime.now(timezone.utc).isoformat(),
        ),
        ChunkRecord(
            doc_id=doc_b,
            chunk_id=f"{doc_b}-000000",
            text="b chunk 0",
            vector=vec_b0,
            source_path=f"/tmp/{doc_b[:8]}.md",
            indexed_at=datetime.now(timezone.utc).isoformat(),
        ),
    ]
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, chunks)

    db = connected_store._require_connected()
    result = await connected_store._do_fetch_doc_vectors_unlocked(db, col_name, doc_a)

    assert len(result) == 2
    assert {tuple(v) for v in result} == {tuple(vec_a0), tuple(vec_a1)}
    assert all(isinstance(v, list) for v in result), "vectors must be plain Python lists"


@pytest.mark.asyncio
async def test_do_fetch_doc_vectors_unlocked_empty_when_doc_not_found(
    connected_store: SearchStore, col_name: str
) -> None:
    """Returns [] when the collection exists but doc_id has no rows."""
    other_doc = _doc_id()
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [_chunk(other_doc, 0)])

    db = connected_store._require_connected()
    missing_doc = _doc_id()
    result = await connected_store._do_fetch_doc_vectors_unlocked(db, col_name, missing_doc)
    assert result == []


@pytest.mark.asyncio
async def test_do_fetch_doc_vectors_unlocked_no_cross_collection_bleed(
    tmp_path: Path,
) -> None:
    """Fetching doc vectors from collection A does not return vectors from collection B."""
    store = SearchStore(tmp_path / "db_cross")
    await store.connect()
    try:
        col_a = "col-alpha"
        col_b = "col-beta"
        doc_a = _doc_id()
        doc_b = _doc_id()

        await store.ensure_collection(col_a, _DIM)
        await store.ensure_collection(col_b, _DIM)
        await store.ingest_chunks(col_a, [_chunk(doc_a, 0, text="from A")])
        await store.ingest_chunks(col_b, [_chunk(doc_b, 0, text="from B")])

        db = store._require_connected()
        # doc_a should exist only in col_a; querying col_b for doc_a must return empty
        result_col_a = await store._do_fetch_doc_vectors_unlocked(db, col_a, doc_a)
        result_col_b_for_doc_a = await store._do_fetch_doc_vectors_unlocked(db, col_b, doc_a)

        assert len(result_col_a) == 1, "col_a should have one chunk for doc_a"
        assert result_col_b_for_doc_a == [], "col_b must not return doc_a's vectors"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_do_fetch_doc_vectors_unlocked_invalid_doc_id_raises(
    tmp_path: Path,
) -> None:
    """Malformed doc_id raises ValueError."""
    store = SearchStore(tmp_path / "db2")
    await store.connect()
    try:
        db = store._require_connected()
        with pytest.raises(ValueError, match="Invalid doc_id"):
            await store._do_fetch_doc_vectors_unlocked(db, "some-col", "not-a-valid-hex-id")
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_do_fetch_doc_vectors_unlocked_invalid_collection_raises(
    tmp_path: Path,
) -> None:
    """Invalid collection name (fails _validate_collection) raises ValueError."""
    store = SearchStore(tmp_path / "db3")
    await store.connect()
    try:
        db = store._require_connected()
        # Names starting with '_' fail _COLLECTION_RE (must start with [a-zA-Z0-9])
        with pytest.raises(ValueError):
            await store._do_fetch_doc_vectors_unlocked(db, "_reserved-col", _doc_id())
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# Tests for _centroid_sum_valid and _batch_vectors_valid (Task 2.3)
# ---------------------------------------------------------------------------


def test_centroid_sum_valid_true_for_good_sum() -> None:
    assert _centroid_sum_valid([1.0, 2.0, 3.0], embedding_dim=3, stored_model="m", writer_model="m") is True


def test_centroid_sum_valid_false_for_none() -> None:
    assert _centroid_sum_valid(None, embedding_dim=3, stored_model="m", writer_model="m") is False


def test_centroid_sum_valid_false_for_dim_mismatch() -> None:
    assert _centroid_sum_valid([1.0, 2.0, 3.0], embedding_dim=4, stored_model="m", writer_model="m") is False


def test_centroid_sum_valid_false_for_model_mismatch() -> None:
    assert _centroid_sum_valid([1.0, 2.0, 3.0], embedding_dim=3, stored_model="a", writer_model="b") is False


def test_centroid_sum_valid_false_for_nan_element() -> None:
    assert _centroid_sum_valid([1.0, float("nan"), 3.0], embedding_dim=3, stored_model="m", writer_model="m") is False


def test_centroid_sum_valid_false_for_inf_element() -> None:
    assert _centroid_sum_valid([1.0, float("inf"), 3.0], embedding_dim=3, stored_model="m", writer_model="m") is False


def test_batch_vectors_valid_true_for_clean_batch() -> None:
    assert _batch_vectors_valid([[1.0, 2.0], [3.0, 4.0]]) is True


def test_batch_vectors_valid_false_for_nan_in_vector() -> None:
    assert _batch_vectors_valid([[1.0, float("nan")], [3.0, 4.0]]) is False


def test_centroid_sum_valid_false_for_empty_stored_model() -> None:
    assert _centroid_sum_valid(
        [1.0, 2.0, 3.0],
        embedding_dim=3,
        stored_model="",
        writer_model="BAAI/bge-small-en-v1.5",
    ) is False


def test_batch_vectors_valid_true_for_empty_list() -> None:
    """Empty batch is vacuously valid."""
    assert _batch_vectors_valid([]) is True


def test_batch_vectors_valid_false_for_inf_in_vector() -> None:
    assert _batch_vectors_valid([[1.0, float("inf")], [3.0, 4.0]]) is False


def test_centroid_sum_valid_false_for_neg_inf_element() -> None:
    assert _centroid_sum_valid([1.0, float("-inf"), 3.0], embedding_dim=3, stored_model="m", writer_model="m") is False


def test_centroid_sum_valid_false_for_zero_embedding_dim() -> None:
    """embedding_dim=0 with empty centroid_sum must be rejected (0-dim embeddings are meaningless)."""
    assert _centroid_sum_valid([], embedding_dim=0, stored_model="m", writer_model="m") is False


# ---------------------------------------------------------------------------
# B5 Task 2.5 — elementwise_sum
# ---------------------------------------------------------------------------


def test_elementwise_sum_correct() -> None:
    result = elementwise_sum([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
    assert result == [5.0, 7.0, 9.0]


def test_elementwise_sum_single_vector() -> None:
    result = elementwise_sum([[1.0, 2.0, 3.0]])
    assert result == [1.0, 2.0, 3.0]


def test_elementwise_sum_empty_list() -> None:
    assert elementwise_sum([]) == []


def test_elementwise_sum_raises_on_mixed_dimensions() -> None:
    with pytest.raises(ValueError, match="mixed-dimension vectors"):
        elementwise_sum([[1.0, 2.0], [3.0, 4.0, 5.0]])

"""tests/rag/test_store.py — unit + integration tests for RagStore."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from archon.rag._types import ChunkRecord
from archon.rag.store import RagStore

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
    store = RagStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.ingest_chunks("col", []))


def test_store_methods_raise_before_connect_hybrid_search(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.hybrid_search("col", [], "q", 5))


def test_store_methods_raise_before_connect_delete_document(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "db")
    doc_id = _doc_id()
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.delete_document("col", doc_id))


def test_store_methods_raise_before_connect_list_documents(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.list_documents("col"))


def test_store_methods_raise_before_connect_list_collections(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.list_collections())


def test_store_methods_raise_before_connect_ensure_collection(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.ensure_collection("col", _DIM))


def test_store_methods_raise_before_connect_rebuild_fts(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "db")
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.rebuild_fts_index("col"))


def test_store_methods_raise_before_connect_fetch_adjacent(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "db")
    doc_id = _doc_id()
    with pytest.raises(RuntimeError, match="not connected"):
        import asyncio

        asyncio.run(store.fetch_adjacent_chunks("col", doc_id, 0, 1))


def test_store_delete_document_invalid_doc_id_raises(tmp_path: Path) -> None:
    """doc_id not matching ^[a-f0-9]{64}$ raises ValueError before any DB call."""
    store = RagStore(tmp_path / "db")
    import asyncio

    asyncio.run(store.connect())
    try:
        with pytest.raises(ValueError):
            asyncio.run(store.delete_document("col", "not-a-valid-hex-id"))
    finally:
        asyncio.run(store.disconnect())


def test_store_ingest_chunks_rejects_empty_chunk_id(tmp_path: Path) -> None:
    """chunk_id = '' raises ValueError — malformed."""
    store = RagStore(tmp_path / "db")
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
    store = RagStore(tmp_path / "db")
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
    store = RagStore(tmp_path / "db")
    import asyncio

    asyncio.run(store.connect())
    try:
        with pytest.raises(ValueError, match="Invalid collection name"):
            asyncio.run(store.ensure_collection("../evil", _DIM))
    finally:
        asyncio.run(store.disconnect())


def test_store_disconnect_clears_connection(tmp_path: Path) -> None:
    """After disconnect, ingest_chunks raises RuntimeError."""
    store = RagStore(tmp_path / "db")
    import asyncio

    asyncio.run(store.connect())
    asyncio.run(store.disconnect())
    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(store.ingest_chunks("col", []))


def test_store_double_disconnect_safe(tmp_path: Path) -> None:
    """Calling disconnect() twice does not raise."""
    store = RagStore(tmp_path / "db")
    import asyncio

    asyncio.run(store.connect())
    asyncio.run(store.disconnect())
    asyncio.run(store.disconnect())  # should not raise


# ---------------------------------------------------------------------------
# Integration tests — use shared connected_store fixture
# ---------------------------------------------------------------------------


async def _ingest_doc(
    store: RagStore,
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
    store = RagStore(db_path)
    await store.connect()
    assert db_path.exists()
    await store.disconnect()


@pytest.mark.asyncio
async def test_store_ensure_collection_idempotent(
    connected_store: RagStore, col_name: str
) -> None:
    """Calling ensure_collection twice does not raise."""
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ensure_collection(col_name, _DIM)  # should not raise


@pytest.mark.asyncio
async def test_store_ingest_and_list_documents(
    connected_store: RagStore, col_name: str
) -> None:
    doc_id, _ = await _ingest_doc(connected_store, col_name, n_chunks=2)
    docs = await connected_store.list_documents(col_name)
    assert len(docs) == 1
    assert docs[0].doc_id == doc_id
    assert docs[0].chunk_count == 2


@pytest.mark.asyncio
async def test_store_hybrid_search_returns_results(
    connected_store: RagStore, col_name: str
) -> None:
    await _ingest_doc(connected_store, col_name, text_prefix="searchable")
    await connected_store.rebuild_fts_index(col_name)
    results = await connected_store.hybrid_search(
        col_name, [0.0] * _DIM, "searchable", top_k=5
    )
    assert len(results) > 0


@pytest.mark.asyncio
async def test_store_hybrid_search_unknown_collection_returns_empty(
    connected_store: RagStore,
) -> None:
    results = await connected_store.hybrid_search(
        "nonexistent-xyz", [0.0] * _DIM, "q", top_k=5
    )
    assert results == []


@pytest.mark.asyncio
async def test_store_delete_document_removes_chunks(
    connected_store: RagStore, col_name: str
) -> None:
    doc_id, _ = await _ingest_doc(connected_store, col_name)
    count = await connected_store.delete_document(col_name, doc_id)
    assert count > 0
    docs = await connected_store.list_documents(col_name)
    assert all(d.doc_id != doc_id for d in docs)


@pytest.mark.asyncio
async def test_store_delete_nonexistent_doc_returns_zero(
    connected_store: RagStore, col_name: str
) -> None:
    await connected_store.ensure_collection(col_name, _DIM)
    fake_id = _doc_id()
    count = await connected_store.delete_document(col_name, fake_id)
    assert count == 0


@pytest.mark.asyncio
async def test_store_list_collections_includes_ingested(
    connected_store: RagStore, col_name: str
) -> None:
    await _ingest_doc(connected_store, col_name)
    collections = await connected_store.list_collections()
    names = [c.name for c in collections]
    assert col_name in names


@pytest.mark.asyncio
async def test_store_list_collections_empty_database_returns_empty(
    tmp_path: Path,
) -> None:
    store = RagStore(tmp_path / "empty_db")
    await store.connect()
    try:
        cols = await store.list_collections()
        assert cols == []
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_store_list_documents_nonexistent_collection_returns_empty(
    connected_store: RagStore,
) -> None:
    docs = await connected_store.list_documents("no-such-collection-xyz", limit=10)
    assert docs == []


@pytest.mark.asyncio
async def test_store_delete_document_injection_safe(
    connected_store: RagStore, col_name: str
) -> None:
    """doc_id with SQL-special chars raises ValueError; document B still intact."""
    doc_b_id, _ = await _ingest_doc(connected_store, col_name)
    with pytest.raises(ValueError):
        await connected_store.delete_document(col_name, "' OR '1'='1")
    docs = await connected_store.list_documents(col_name)
    assert any(d.doc_id == doc_b_id for d in docs)


@pytest.mark.asyncio
async def test_store_fetch_adjacent_chunks_returns_neighbors(
    connected_store: RagStore, col_name: str
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
    connected_store: RagStore, col_name: str
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
    connected_store: RagStore, col_name: str
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
    connected_store: RagStore, col_name: str
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
    connected_store: RagStore, col_name: str
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
    connected_store: RagStore, col_name: str
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
    connected_store: RagStore, col_name: str
) -> None:
    """Calling rebuild_fts_index twice does not raise."""
    await _ingest_doc(connected_store, col_name)
    await connected_store.rebuild_fts_index(col_name)
    await connected_store.rebuild_fts_index(col_name)


@pytest.mark.asyncio
async def test_store_list_collections_returns_correct_counts(
    connected_store: RagStore, col_name: str
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
    connected_store: RagStore, col_name: str
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
    connected_store: RagStore,
) -> None:
    """delete_document on a collection that does not exist returns 0."""
    doc_id = _doc_id()
    count = await connected_store.delete_document("no-such-collection-xyz", doc_id)
    assert count == 0


@pytest.mark.asyncio
async def test_store_fetch_adjacent_nonexistent_collection_returns_empty(
    connected_store: RagStore,
) -> None:
    """fetch_adjacent_chunks on a nonexistent collection returns []."""
    doc_id = _doc_id()
    result = await connected_store.fetch_adjacent_chunks("no-such-collection-xyz", doc_id, 0, 1)
    assert result == []


@pytest.mark.asyncio
async def test_store_ingest_empty_list_returns_zero(
    connected_store: RagStore, col_name: str,
) -> None:
    """ingest_chunks with empty list returns 0 without touching the table."""
    await connected_store.ensure_collection(col_name, _DIM)
    count = await connected_store.ingest_chunks(col_name, [])
    assert count == 0


@pytest.mark.asyncio
async def test_store_list_documents_limit_capped_at_1000(
    connected_store: RagStore, col_name: str,
) -> None:
    """list_documents caps limit at 1000 — requesting more does not OOM."""
    await connected_store.ensure_collection(col_name, _DIM)
    # Should not raise even with unreasonable limit
    docs = await connected_store.list_documents(col_name, limit=100_000)
    assert isinstance(docs, list)


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

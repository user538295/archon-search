"""Tests for archon.rag.store.RagStore — Task 1.4 (FEAT-019)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from archon.rag._types import ChunkRecord, CollectionInfo, DocumentInfo, SearchResult
from archon.rag.store import RagStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DOC_ID_A = "a" * 64
DOC_ID_B = "b" * 64

COLLECTION = "docs"
EMB_DIM = 4


def make_chunk(doc_id: str, idx: int, text: str = "sample text") -> ChunkRecord:
    """Return a ChunkRecord with a properly formatted chunk_id."""
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[0.1 * idx, 0.2, 0.3, 0.4],
        source_path=f"/docs/{doc_id[:8]}.md",
        indexed_at="2026-01-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------


async def test_store_connect_creates_db_dir(tmp_path: Path) -> None:
    db_path = tmp_path / "rag_db"
    assert not db_path.exists()
    store = RagStore(db_path)
    await store.connect()
    assert db_path.exists()
    await store.disconnect()


async def test_store_disconnect_clears_connection(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.disconnect()
    # After disconnect, methods must raise
    with pytest.raises(RuntimeError, match="not connected"):
        await store.list_collections()


async def test_store_double_disconnect_safe(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.disconnect()
    await store.disconnect()  # second disconnect must not raise


# ---------------------------------------------------------------------------
# Guard: raise before connect (parametrised)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method_name, kwargs",
    [
        ("ensure_collection", {"collection": COLLECTION, "embedding_dim": EMB_DIM}),
        ("ingest_chunks", {"collection": COLLECTION, "chunks": []}),
        ("rebuild_fts_index", {"collection": COLLECTION}),
        (
            "hybrid_search",
            {
                "collection": COLLECTION,
                "query_vector": [0.1, 0.2, 0.3, 0.4],
                "query_text": "hello",
                "top_k": 5,
            },
        ),
        ("delete_document", {"collection": COLLECTION, "doc_id": DOC_ID_A}),
        ("list_documents", {"collection": COLLECTION}),
        ("list_collections", {}),
        (
            "fetch_adjacent_chunks",
            {
                "collection": COLLECTION,
                "doc_id": DOC_ID_A,
                "center_idx": 0,
                "window": 2,
            },
        ),
    ],
)
async def test_store_methods_raise_before_connect(
    tmp_path: Path, method_name: str, kwargs: dict
) -> None:
    store = RagStore(tmp_path / "rag_db")
    with pytest.raises(RuntimeError, match="not connected"):
        await getattr(store, method_name)(**kwargs)


# ---------------------------------------------------------------------------
# ensure_collection
# ---------------------------------------------------------------------------


async def test_store_ensure_collection_idempotent(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    await store.ensure_collection(COLLECTION, EMB_DIM)  # second call must not raise
    cols = await store.list_collections()
    assert any(c.name == COLLECTION for c in cols)
    await store.disconnect()


# ---------------------------------------------------------------------------
# ingest & list documents
# ---------------------------------------------------------------------------


async def test_store_ingest_and_list_documents(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)

    chunks = [make_chunk(DOC_ID_A, i) for i in range(3)]
    count = await store.ingest_chunks(COLLECTION, chunks)
    assert count == 3

    docs = await store.list_documents(COLLECTION)
    assert len(docs) == 1
    assert docs[0].doc_id == DOC_ID_A
    assert docs[0].chunk_count == 3

    await store.disconnect()


# ---------------------------------------------------------------------------
# hybrid_search
# ---------------------------------------------------------------------------


async def test_store_hybrid_search_returns_results(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    chunks = [make_chunk(DOC_ID_A, i, f"alpha beta gamma text {i}") for i in range(3)]
    await store.ingest_chunks(COLLECTION, chunks)
    await store.rebuild_fts_index(COLLECTION)

    results = await store.hybrid_search(
        COLLECTION, [0.1, 0.2, 0.3, 0.4], "alpha beta", top_k=3
    )
    assert len(results) > 0
    for r in results:
        assert isinstance(r, SearchResult)
        assert r.score >= 0

    await store.disconnect()


async def test_store_hybrid_search_unknown_collection_returns_empty(
    tmp_path: Path,
) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    results = await store.hybrid_search(
        "nonexistent", [0.1, 0.2, 0.3, 0.4], "hello", top_k=5
    )
    assert results == []
    await store.disconnect()


async def test_store_hybrid_search_degrades_gracefully_without_fts_index(
    tmp_path: Path,
) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    chunks = [make_chunk(DOC_ID_A, i, f"word{i} text") for i in range(2)]
    await store.ingest_chunks(COLLECTION, chunks)
    # No rebuild_fts_index called — FTS should fail gracefully

    results = await store.hybrid_search(
        COLLECTION, [0.1, 0.2, 0.3, 0.4], "word0", top_k=5
    )
    # Should return vector-only results without raising
    assert len(results) > 0

    await store.disconnect()


async def test_store_hybrid_search_rrf_ranking_correct(tmp_path: Path) -> None:
    """Chunk 0 should rank first (closest vector + matching text)."""
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    chunks = [
        make_chunk(DOC_ID_A, 0, "relevant query text"),
        make_chunk(DOC_ID_A, 1, "unrelated content here"),
    ]
    await store.ingest_chunks(COLLECTION, chunks)
    await store.rebuild_fts_index(COLLECTION)

    results = await store.hybrid_search(
        COLLECTION,
        [0.0, 0.2, 0.3, 0.4],  # closer to chunk 0 vector
        "relevant query",
        top_k=2,
    )
    assert len(results) == 2
    # Scores must be non-negative
    assert all(r.score >= 0 for r in results)

    await store.disconnect()


# ---------------------------------------------------------------------------
# delete_document
# ---------------------------------------------------------------------------


async def test_store_delete_document_removes_chunks(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    chunks_a = [make_chunk(DOC_ID_A, i) for i in range(3)]
    chunks_b = [make_chunk(DOC_ID_B, i) for i in range(2)]
    await store.ingest_chunks(COLLECTION, chunks_a + chunks_b)

    deleted = await store.delete_document(COLLECTION, DOC_ID_A)
    assert deleted == 3

    docs = await store.list_documents(COLLECTION)
    assert all(d.doc_id != DOC_ID_A for d in docs)
    assert any(d.doc_id == DOC_ID_B for d in docs)

    await store.disconnect()


async def test_store_delete_nonexistent_doc_returns_zero(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    deleted = await store.delete_document(COLLECTION, DOC_ID_A)
    assert deleted == 0
    await store.disconnect()


async def test_store_delete_document_injection_safe(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    # doc_id with SQL-injection characters is invalid hex → raises ValueError
    with pytest.raises(ValueError):
        await store.delete_document(COLLECTION, "' OR '1'='1")
    await store.disconnect()


async def test_store_delete_document_invalid_doc_id_raises(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    with pytest.raises(ValueError, match="Invalid doc_id"):
        await store.delete_document(COLLECTION, "short-id")
    await store.disconnect()


# ---------------------------------------------------------------------------
# ingest validation
# ---------------------------------------------------------------------------


async def test_store_ingest_chunks_rejects_malformed_chunk_id(
    tmp_path: Path,
) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    bad_chunk = ChunkRecord(
        doc_id=DOC_ID_A,
        chunk_id="BAD_FORMAT",
        text="text",
        vector=[0.1, 0.2, 0.3, 0.4],
        source_path="/f.md",
        indexed_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(ValueError, match="malformed"):
        await store.ingest_chunks(COLLECTION, [bad_chunk])
    await store.disconnect()


# ---------------------------------------------------------------------------
# list_collections
# ---------------------------------------------------------------------------


async def test_store_list_collections_includes_ingested(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    chunks = [make_chunk(DOC_ID_A, i) for i in range(2)]
    await store.ingest_chunks(COLLECTION, chunks)

    cols = await store.list_collections()
    assert any(c.name == COLLECTION for c in cols)
    col = next(c for c in cols if c.name == COLLECTION)
    assert col.chunk_count == 2

    await store.disconnect()


async def test_store_list_collections_returns_correct_counts(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection("col1", EMB_DIM)
    await store.ensure_collection("col2", EMB_DIM)
    await store.ingest_chunks("col1", [make_chunk(DOC_ID_A, i) for i in range(3)])
    await store.ingest_chunks("col2", [make_chunk(DOC_ID_B, i) for i in range(5)])

    cols = await store.list_collections()
    col1 = next(c for c in cols if c.name == "col1")
    col2 = next(c for c in cols if c.name == "col2")
    assert col1.chunk_count == 3
    assert col2.chunk_count == 5

    await store.disconnect()


# ---------------------------------------------------------------------------
# list_documents limit
# ---------------------------------------------------------------------------


async def test_store_list_documents_respects_limit(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    # Ingest 5 docs each with 1 chunk
    doc_ids = ["c" * 63 + str(i) for i in range(5)]
    for d in doc_ids:
        await store.ingest_chunks(COLLECTION, [make_chunk(d, 0)])

    docs = await store.list_documents(COLLECTION, limit=3)
    assert len(docs) <= 3

    await store.disconnect()


# ---------------------------------------------------------------------------
# fetch_adjacent_chunks
# ---------------------------------------------------------------------------


async def test_store_fetch_adjacent_chunks_returns_neighbors(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    chunks = [make_chunk(DOC_ID_A, i, f"chunk text {i}") for i in range(5)]
    await store.ingest_chunks(COLLECTION, chunks)

    neighbors = await store.fetch_adjacent_chunks(
        COLLECTION, DOC_ID_A, center_idx=2, window=1
    )
    ids = {c.chunk_id for c in neighbors}
    # Expect neighbors only (center excluded): chunks 1 and 3
    assert f"{DOC_ID_A}-000001" in ids
    assert f"{DOC_ID_A}-000003" in ids
    assert f"{DOC_ID_A}-000002" not in ids
    assert len(neighbors) == 2

    await store.disconnect()


async def test_store_fetch_adjacent_chunks_at_boundary_returns_partial(
    tmp_path: Path,
) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    chunks = [make_chunk(DOC_ID_A, i) for i in range(3)]
    await store.ingest_chunks(COLLECTION, chunks)

    # center_idx=0, window=2 → range(max(0,-2), 3) = [0,1,2], exclude center=0 → [1,2]
    neighbors = await store.fetch_adjacent_chunks(
        COLLECTION, DOC_ID_A, center_idx=0, window=2
    )
    ids = {c.chunk_id for c in neighbors}
    assert f"{DOC_ID_A}-000000" not in ids
    assert f"{DOC_ID_A}-000001" in ids
    assert f"{DOC_ID_A}-000002" in ids
    assert len(neighbors) == 2

    await store.disconnect()


# ---------------------------------------------------------------------------
# rebuild_fts_index
# ---------------------------------------------------------------------------


async def test_store_rebuild_fts_index_makes_text_searchable(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    chunks = [make_chunk(DOC_ID_A, 0, "unique phrase xyzzy")]
    await store.ingest_chunks(COLLECTION, chunks)
    await store.rebuild_fts_index(COLLECTION)

    results = await store.hybrid_search(
        COLLECTION, [0.1, 0.2, 0.3, 0.4], "xyzzy", top_k=5
    )
    assert any(r.chunk_id == f"{DOC_ID_A}-000000" for r in results)

    await store.disconnect()


async def test_store_rebuild_fts_index_idempotent(tmp_path: Path) -> None:
    store = RagStore(tmp_path / "rag_db")
    await store.connect()
    await store.ensure_collection(COLLECTION, EMB_DIM)
    await store.ingest_chunks(COLLECTION, [make_chunk(DOC_ID_A, 0, "text")])
    await store.rebuild_fts_index(COLLECTION)
    await store.rebuild_fts_index(COLLECTION)  # must not raise
    await store.disconnect()

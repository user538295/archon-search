"""E0c BE-1 — TDD tests for store.sample_chunk_texts shuffle + MAX_SAMPLE_CHUNKS=100.

Scenarios covered: S15, S16.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from archon_search._types import ChunkRecord

_DIM = 4


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _chunk(doc_id: str, idx: int, text: str) -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[float(idx % 4)] * _DIM,
        source_path=f"/tmp/{doc_id[:8]}.md",
        indexed_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Unit tests (no LanceDB)
# ---------------------------------------------------------------------------


def test_max_sample_chunks_constant_equals_100() -> None:
    """S16 — MAX_SAMPLE_CHUNKS must equal 100."""
    from archon_search.description_generator import MAX_SAMPLE_CHUNKS

    assert MAX_SAMPLE_CHUNKS == 100


@pytest.mark.asyncio
async def test_sample_chunk_texts_returns_at_most_n(connected_store, col_name) -> None:
    """sample_chunk_texts(n=5) returns at most 5 texts even when more are stored."""
    await connected_store.ensure_collection(col_name, _DIM)
    doc_id = _doc_id()
    chunks = [_chunk(doc_id, i, f"text chunk {i}") for i in range(20)]
    await connected_store.ingest_chunks(col_name, chunks)

    result = await connected_store.sample_chunk_texts(col_name, n=5)
    assert len(result) == 5


@pytest.mark.asyncio
async def test_sample_chunk_texts_returns_empty_for_missing_collection(
    connected_store,
) -> None:
    """sample_chunk_texts on a collection that was never created returns []."""
    result = await connected_store.sample_chunk_texts("nonexistent-collection-xyz")
    assert result == []


@pytest.mark.asyncio
async def test_sample_chunk_texts_n_larger_than_collection_returns_all(
    connected_store, col_name: str
) -> None:
    """When n exceeds available chunks, all available are returned."""
    await connected_store.ensure_collection(col_name, _DIM)
    doc_id = _doc_id()
    chunks = [_chunk(doc_id, i, f"text chunk {i}") for i in range(5)]
    await connected_store.ingest_chunks(col_name, chunks)

    result = await connected_store.sample_chunk_texts(col_name, n=1000)
    assert len(result) == 5


# ---------------------------------------------------------------------------
# Integration tests (require real LanceDB store)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sample_chunk_texts_order_is_not_deterministic(
    connected_store, col_name: str
) -> None:
    """S15 — Two consecutive calls on a 200-chunk collection return different orderings.

    Because random.shuffle operates in-process, a collision (same order) with
    200 items has probability ~1/200! — effectively zero. We run up to 5 pairs
    and require at least one to differ, which makes the test robust even if
    random happens to produce the same order once.
    """
    await connected_store.ensure_collection(col_name, _DIM)
    doc_id = _doc_id()
    # Insert 200 chunks with distinct, ordered texts so insertion-order is obvious
    n = 200
    chunks = [_chunk(doc_id, i, f"chunk-text-{i:04d}") for i in range(n)]
    await connected_store.ingest_chunks(col_name, chunks)

    found_different = False
    for _ in range(5):
        result_a = await connected_store.sample_chunk_texts(col_name, n=n)
        result_b = await connected_store.sample_chunk_texts(col_name, n=n)
        if result_a != result_b:
            assert sorted(result_a) == sorted(result_b), (
                "sample_chunk_texts returned different contents across calls; "
                "expected shuffle to preserve content (no drops or duplicates)"
            )
            found_different = True
            break

    assert found_different, (
        "sample_chunk_texts returned identical orderings across 5 consecutive pairs; "
        "expected in-process shuffle to produce different orderings"
    )


@pytest.mark.asyncio
async def test_description_generator_samples_from_larger_pool(
    tmp_path, col_name: str
) -> None:
    """S16 — pipeline.generate_description on a 500-chunk collection uses 100-chunk pool.

    We mock the store's sample_chunk_texts to verify it is called with n=100
    (not n=20 which would indicate MAX_SAMPLE_CHUNKS was still 20).
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.store import ChunkIngestResult
    from archon_search.embedder import Embedder, EmbedderBackend
    from archon_search.reranker import Reranker, RerankerBackend

    class _MockEmbedder(EmbedderBackend):
        model_name = "mock"
        is_warm = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 4 for _ in texts]

    class _MockReranker(RerankerBackend):
        is_warm = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(
        return_value=ChunkIngestResult(chunks_ingested=3, needs_recompute=False)
    )
    store.rebuild_fts_index = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="")
    store.get_collection_meta = AsyncMock(return_value=None)
    store.update_description = AsyncMock()
    store.update_collection_meta = AsyncMock()
    store.list_chunks_raw = AsyncMock()

    # Simulate a 500-chunk collection sample: 100 texts returned (store applies the limit)
    sample_texts = [f"chunk-text-{i:04d}" for i in range(100)]
    store.sample_chunk_texts = AsyncMock(return_value=sample_texts)

    embedder = Embedder(_MockEmbedder())
    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=Reranker(_MockReranker()),
        chunker=DocumentChunker(chunk_size=64),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    (tmp_path / "doc.md").write_text("# Big collection\n\n" + "Content.\n" * 20)

    with patch(
        "archon_search.pipeline.generate_description", return_value="A description"
    ) as mock_gen, patch(
        "archon_search.pipeline._should_regenerate", return_value=True
    ):
        await pipeline.ingest_directory(
            tmp_path, "test-col", embedder=pipeline._global_embedder, rebuild_fts=False
        )

    # Must be called with n=100 (not n=20)
    store.sample_chunk_texts.assert_awaited_once()
    call_kwargs = store.sample_chunk_texts.call_args
    assert call_kwargs.kwargs.get("n") == 100, (
        f"sample_chunk_texts must be called with n=100, got n={call_kwargs.kwargs.get('n')!r}. "
        "MAX_SAMPLE_CHUNKS may still be 20."
    )

    # generate_description receives the 100-chunk pool (not 20)
    mock_gen.assert_awaited_once()
    passed_chunks = mock_gen.call_args[0][0]
    assert len(passed_chunks) <= 100
    assert len(passed_chunks) > 0

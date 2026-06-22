"""tests/pipeline/test_pipeline_ingest_batch.py — Unit tests for batched ingest in SearchPipeline.

Covers: BE-2 batch-emit refactor of ingest_file(), _is_continuation parameter,
and related behavioural guarantees using a mock store.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from archon_search._types import ChunkRecord, IngestResult
from archon_search.config import SearchConfig
from archon_search.constants import _INGEST_CHUNK_BATCH_SIZE as _BATCH_SIZE
from archon_search.store import ChunkIngestResult
from archon_search.chunker import DocumentChunker
from archon_search.parser import DocumentParser
from archon_search.pipeline import SearchPipeline

from .conftest import MockEmbedderBackend, MockRerankerBackend, make_embedder, make_reranker


def _make_records(n: int, doc_id: str = "test-doc-id") -> list[ChunkRecord]:
    return [
        ChunkRecord(
            doc_id=doc_id,
            chunk_id=f"{doc_id}-{i:06d}",
            text=f"chunk {i}",
            vector=[0.1] * 4,
            source_path="/fake/path.txt",
            indexed_at="2024-01-01T00:00:00+00:00",
        )
        for i in range(n)
    ]


def _make_mock_store(ingest_side_effects=None) -> MagicMock:
    """Return a MagicMock store wired to work with SearchPipeline."""
    store = MagicMock()
    store._config = SearchConfig()
    store.supports_incremental_fts_delete = True

    store.ensure_collection = AsyncMock(return_value=None)
    store.delete_document = AsyncMock(return_value=0)
    store.optimize_fts = AsyncMock(return_value=None)
    store.rebuild_fts_index = AsyncMock(return_value=None)
    store.get_dominant_language = AsyncMock(return_value="")
    store.get_collection_meta = AsyncMock(return_value=None)
    store.write_collection_meta = AsyncMock(return_value=None)

    if ingest_side_effects is not None:
        store.ingest_chunks = AsyncMock(side_effect=ingest_side_effects)
    else:
        store.ingest_chunks = AsyncMock(
            return_value=ChunkIngestResult(chunks_ingested=0, needs_recompute=False)
        )

    return store


def _make_pipeline(store) -> SearchPipeline:
    return SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )


# ---------------------------------------------------------------------------
# Helpers for patching the chunker to return N records
# ---------------------------------------------------------------------------

def _patch_chunker(pipeline: SearchPipeline, records: list[ChunkRecord]):
    """Replace pipeline._chunker.chunk with a sync callable returning records."""
    pipeline._chunker.chunk = lambda *a, **kw: records  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_file_splits_into_two_batches(tmp_path: Path):
    """600-chunk file → ingest_chunks called 2 times; first with _is_continuation=False,
    second with _is_continuation=True; chunks_created == 600."""
    records = _make_records(600)
    batch1_result = ChunkIngestResult(chunks_ingested=_BATCH_SIZE, needs_recompute=False)
    batch2_result = ChunkIngestResult(chunks_ingested=600 - _BATCH_SIZE, needs_recompute=False)

    store = _make_mock_store(ingest_side_effects=[batch1_result, batch2_result])
    pipeline = _make_pipeline(store)
    _patch_chunker(pipeline, records)

    md_file = tmp_path / "big.md"
    md_file.write_text("x")

    result = await pipeline.ingest_file(md_file, "col", embedder=pipeline._global_embedder)

    assert result.status == "ok"
    assert result.chunks_created == 600

    assert store.ingest_chunks.call_count == 2

    first_call_kwargs = store.ingest_chunks.call_args_list[0].kwargs
    second_call_kwargs = store.ingest_chunks.call_args_list[1].kwargs

    assert first_call_kwargs.get("_is_continuation") is False or \
           first_call_kwargs.get("_is_continuation") == False  # noqa: E712
    assert second_call_kwargs.get("_is_continuation") is True

    first_call_chunks = store.ingest_chunks.call_args_list[0].args[1]
    second_call_chunks = store.ingest_chunks.call_args_list[1].args[1]

    assert len(first_call_chunks) == _BATCH_SIZE
    assert len(second_call_chunks) == 600 - _BATCH_SIZE
    assert set(c.chunk_id for c in first_call_chunks).isdisjoint(
        set(c.chunk_id for c in second_call_chunks)
    )


@pytest.mark.asyncio
async def test_ingest_file_single_batch_unchanged(tmp_path: Path):
    """≤512 chunks → exactly 1 ingest_chunks call with _is_continuation=False."""
    records = _make_records(512)
    store = _make_mock_store(
        ingest_side_effects=[ChunkIngestResult(chunks_ingested=512, needs_recompute=False)]
    )
    pipeline = _make_pipeline(store)
    _patch_chunker(pipeline, records)

    md_file = tmp_path / "doc.md"
    md_file.write_text("x")

    result = await pipeline.ingest_file(md_file, "col", embedder=pipeline._global_embedder)

    assert result.status == "ok"
    assert store.ingest_chunks.call_count == 1
    assert store.ingest_chunks.call_args.kwargs.get("_is_continuation") is False or \
           store.ingest_chunks.call_args.kwargs.get("_is_continuation") == False  # noqa: E712


@pytest.mark.asyncio
async def test_ingest_file_empty_returns_immediately(tmp_path: Path):
    """0 chunks → no ingest_chunks call."""
    store = _make_mock_store()
    pipeline = _make_pipeline(store)
    _patch_chunker(pipeline, [])

    md_file = tmp_path / "empty.md"
    md_file.write_text("x")

    result = await pipeline.ingest_file(md_file, "col", embedder=pipeline._global_embedder)

    assert result.status == "ok"
    assert result.chunks_created == 0
    store.ingest_chunks.assert_not_called()


@pytest.mark.asyncio
async def test_needs_recompute_or_across_batches(tmp_path: Path):
    """batch 2 of 3 returns needs_recompute=True → IngestResult.needs_recompute == True."""
    records = _make_records(1300)  # 3 batches: 512 + 512 + 276
    effects = [
        ChunkIngestResult(chunks_ingested=512, needs_recompute=False),
        ChunkIngestResult(chunks_ingested=512, needs_recompute=True),   # <-- True
        ChunkIngestResult(chunks_ingested=276, needs_recompute=False),
    ]
    store = _make_mock_store(ingest_side_effects=effects)
    pipeline = _make_pipeline(store)
    _patch_chunker(pipeline, records)
    # Prevent recompute_collection_meta from calling unmocked store methods
    pipeline.recompute_collection_meta = AsyncMock(return_value=None)  # type: ignore[method-assign]

    md_file = tmp_path / "doc.md"
    md_file.write_text("x")

    result = await pipeline.ingest_file(md_file, "col", embedder=pipeline._global_embedder)

    assert result.needs_recompute is True


@pytest.mark.asyncio
async def test_fts_fires_once_after_all_batches(tmp_path: Path):
    """N batches → optimize_fts called exactly 1 time (not once per batch), and after all ingest_chunks."""
    records = _make_records(1024)  # exactly 2 batches
    effects = [
        ChunkIngestResult(chunks_ingested=512, needs_recompute=False),
        ChunkIngestResult(chunks_ingested=512, needs_recompute=False),
    ]
    store = _make_mock_store(ingest_side_effects=effects)
    pipeline = _make_pipeline(store)
    _patch_chunker(pipeline, records)

    call_order: list[str] = []
    ingest_call_index = 0

    async def _track_ingest(*a, **kw):
        nonlocal ingest_call_index
        call_order.append("ingest")
        result = effects[ingest_call_index]
        ingest_call_index += 1
        return result

    async def _track_optimize(*a, **kw):
        call_order.append("optimize")

    store.ingest_chunks.side_effect = _track_ingest
    store.optimize_fts.side_effect = _track_optimize

    md_file = tmp_path / "doc.md"
    md_file.write_text("x")

    await pipeline.ingest_file(md_file, "col", embedder=pipeline._global_embedder, rebuild_fts=True)

    store.optimize_fts.assert_called_once()

    # Verify optimize_fts was called after all ingest_chunks calls
    assert "optimize" in call_order, "optimize_fts was never called"
    optimize_idx = call_order.index("optimize")
    ingest_indices = [i for i, op in enumerate(call_order) if op == "ingest"]
    assert all(i < optimize_idx for i in ingest_indices), (
        f"optimize_fts must be called after all ingest_chunks; call_order={call_order}"
    )


@pytest.mark.asyncio
async def test_ensure_collection_and_delete_called_once(tmp_path: Path):
    """2-batch ingest → ensure_collection called exactly 1 time, delete_document called exactly 1 time,
    and both are called before any ingest_chunks call."""
    records = _make_records(600)
    effects = [
        ChunkIngestResult(chunks_ingested=512, needs_recompute=False),
        ChunkIngestResult(chunks_ingested=88, needs_recompute=False),
    ]
    store = _make_mock_store(ingest_side_effects=effects)
    pipeline = _make_pipeline(store)
    _patch_chunker(pipeline, records)

    call_order: list[str] = []
    original_ensure = store.ensure_collection.side_effect
    original_delete = store.delete_document.side_effect
    original_ingest = store.ingest_chunks.side_effect

    async def _track_ensure(*a, **kw):
        call_order.append("ensure")
        if original_ensure is not None:
            return await original_ensure(*a, **kw)

    async def _track_delete(*a, **kw):
        call_order.append("delete")
        if original_delete is not None:
            return await original_delete(*a, **kw)

    async def _track_ingest(*a, **kw):
        call_order.append("ingest")
        result = effects[len([x for x in call_order if x == "ingest"]) - 1]
        return result

    store.ensure_collection.side_effect = _track_ensure
    store.delete_document.side_effect = _track_delete
    store.ingest_chunks.side_effect = _track_ingest

    md_file = tmp_path / "doc.md"
    md_file.write_text("x")

    await pipeline.ingest_file(md_file, "col", embedder=pipeline._global_embedder)

    store.ensure_collection.assert_called_once()
    store.delete_document.assert_called_once()

    # Verify ordering: ensure → delete → ingest(s)
    assert call_order[0] == "ensure", f"ensure_collection must be first, got: {call_order}"
    assert call_order[1] == "delete", f"delete_document must be second, got: {call_order}"
    assert all(op == "ingest" for op in call_order[2:]), (
        f"All calls after ensure+delete must be ingest_chunks, got: {call_order}"
    )


@pytest.mark.asyncio
async def test_vector_collector_populated_across_batches(tmp_path: Path):
    """2-batch ingest → _vector_collector contains all vectors from all batches combined."""
    n = 600
    records = _make_records(n)
    effects = [
        ChunkIngestResult(chunks_ingested=512, needs_recompute=False),
        ChunkIngestResult(chunks_ingested=88, needs_recompute=False),
    ]
    store = _make_mock_store(ingest_side_effects=effects)
    pipeline = _make_pipeline(store)
    _patch_chunker(pipeline, records)

    md_file = tmp_path / "doc.md"
    md_file.write_text("x")

    vector_collector: list[list[float]] = []
    await pipeline.ingest_file(
        md_file, "col",
        embedder=pipeline._global_embedder,
        _vector_collector=vector_collector,
    )

    assert len(vector_collector) == n


@pytest.mark.asyncio
async def test_chunk_collector_populated_across_batches(tmp_path: Path):
    """2-batch ingest → _chunk_collector contains all chunk texts (collected before the batch loop)."""
    n = 600
    records = _make_records(n)
    effects = [
        ChunkIngestResult(chunks_ingested=512, needs_recompute=False),
        ChunkIngestResult(chunks_ingested=88, needs_recompute=False),
    ]
    store = _make_mock_store(ingest_side_effects=effects)
    pipeline = _make_pipeline(store)
    _patch_chunker(pipeline, records)

    md_file = tmp_path / "doc.md"
    md_file.write_text("x")

    chunk_collector: list[str] = []
    await pipeline.ingest_file(
        md_file, "col",
        embedder=pipeline._global_embedder,
        _chunk_collector=chunk_collector,
    )

    assert len(chunk_collector) == n
    assert chunk_collector[0] == "chunk 0"
    assert chunk_collector[-1] == f"chunk {n - 1}"


@pytest.mark.asyncio
async def test_partial_batch_failure_returns_error(tmp_path: Path):
    """ingest_chunks raises on batch 2 → status='error', no batch 3 attempted."""
    from archon_search.store import StoreBusyError

    records = _make_records(1100)  # 3 batches if completed: 512+512+76
    effects = [
        ChunkIngestResult(chunks_ingested=512, needs_recompute=False),
        StoreBusyError(timeout_s=5),  # raises on batch 2
    ]
    store = _make_mock_store(ingest_side_effects=effects)
    pipeline = _make_pipeline(store)
    _patch_chunker(pipeline, records)

    md_file = tmp_path / "doc.md"
    md_file.write_text("x")

    result = await pipeline.ingest_file(md_file, "col", embedder=pipeline._global_embedder)

    assert result.status == "error"
    assert result.chunks_created == 0, (
        "StoreBusyError discards partial progress; chunks_created must be 0"
    )
    # batch 3 was never attempted — only 2 calls total
    assert store.ingest_chunks.call_count == 2

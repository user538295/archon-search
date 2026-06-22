"""tests/pipeline/test_pipeline_ingest_batch_integration.py — Integration tests for
batched ingest behaviour (BE-2).

Uses make_real_pipeline with a real SearchStore and real LanceDB to verify
doc_count correctness across batch boundaries and sample_chunk_texts.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_file_batched_centroid_correctness(tmp_path: Path, monkeypatch):
    """Ingest a file that produces >512 chunks → doc_count must be 1, not 2."""
    from tests.integration.conftest import make_real_pipeline

    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    # Write a file large enough to produce >512 chunks.
    # With chunk_size=128, each ~100-word paragraph yields ~1 chunk.
    # 600 paragraphs of ~100 words each should produce ≥600 chunks.
    big_text = ("word " * 100 + "\n\n") * 700
    doc_file = tmp_path / "big.txt"
    doc_file.write_text(big_text)

    result = await pipeline.ingest_file(doc_file, "testcol", embedder=pipeline._global_embedder)

    assert result.status == "ok"
    assert result.chunks_created > 512, "Need >512 chunks to exercise the batching path"

    meta = await store.get_collection_meta("testcol")
    assert meta is not None
    assert meta.doc_count == 1, f"Expected doc_count=1, got {meta.doc_count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reingest_centroid_correctness(tmp_path: Path, monkeypatch):
    """Re-ingest the same doc_id with different content → doc_count stays 1."""
    from tests.integration.conftest import make_real_pipeline

    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    big_text_a = ("alpha " * 100 + "\n\n") * 700
    big_text_b = ("beta " * 100 + "\n\n") * 400

    doc_file = tmp_path / "doc.txt"
    doc_file.write_text(big_text_a)

    r1 = await pipeline.ingest_file(doc_file, "testcol", embedder=pipeline._global_embedder)
    assert r1.status == "ok"
    assert r1.chunks_created > 512

    # Overwrite with different content (same path → same doc_id)
    doc_file.write_text(big_text_b)

    r2 = await pipeline.ingest_file(doc_file, "testcol", embedder=pipeline._global_embedder)
    assert r2.status == "ok"

    meta = await store.get_collection_meta("testcol")
    assert meta is not None
    assert meta.doc_count == 1, f"Expected doc_count=1 after re-ingest, got {meta.doc_count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_partial_batch_failure_leaves_store_partially_written(tmp_path: Path, monkeypatch):
    """When ingest_chunks raises on batch 2: status='error'; successful re-ingest gives doc_count=1."""
    from archon_search.store import StoreBusyError
    from tests.integration.conftest import make_real_pipeline

    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    big_text = ("word " * 100 + "\n\n") * 700
    doc_file = tmp_path / "big.txt"
    doc_file.write_text(big_text)

    call_count = 0
    original_ingest_chunks = store.ingest_chunks

    async def failing_ingest_chunks(collection, chunks, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise StoreBusyError(timeout_s=5)
        return await original_ingest_chunks(collection, chunks, **kwargs)

    store.ingest_chunks = failing_ingest_chunks

    result = await pipeline.ingest_file(doc_file, "testcol", embedder=pipeline._global_embedder)
    assert result.status == "error"

    # Batch 1 persisted successfully — verify chunks are in the store
    chunks_after_failure = await store.count_chunks("testcol", namespace="default")
    assert chunks_after_failure > 0, (
        f"Expected batch 1 chunks to persist after partial failure, got {chunks_after_failure}"
    )

    # Restore original and re-ingest
    store.ingest_chunks = original_ingest_chunks

    result2 = await pipeline.ingest_file(doc_file, "testcol", embedder=pipeline._global_embedder)
    assert result2.status == "ok"

    meta = await store.get_collection_meta("testcol")
    assert meta is not None
    assert meta.doc_count == 1, f"Expected doc_count=1 after successful re-ingest, got {meta.doc_count}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sample_chunk_texts_returns_limit_n(tmp_path: Path, monkeypatch):
    """sample_chunk_texts returns at most n items, all strings; [] on empty collection."""
    from tests.integration.conftest import make_real_pipeline

    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    # Ingest a file large enough to produce ≥200 chunks.
    # Each paragraph of ~100 words yields ~1 chunk at chunk_size=128.
    # 300 paragraphs × 100 words → ~300 chunks, well above the 200-chunk threshold.
    text = ("word " * 100 + "\n\n") * 300
    doc_file = tmp_path / "sample.txt"
    doc_file.write_text(text)

    r = await pipeline.ingest_file(doc_file, "testcol", embedder=pipeline._global_embedder)
    assert r.status == "ok"
    total_chunks = r.chunks_created

    # Precondition: file must produce ≥200 chunks for the large-sample test to be meaningful
    assert total_chunks >= 200, (
        f"Expected ≥200 chunks to exercise sample_chunk_texts limit; got {total_chunks}. "
        "Increase the file size if this assertion fails."
    )

    # n=50
    result_50 = await store.sample_chunk_texts("testcol", n=50)
    assert isinstance(result_50, list)
    assert len(result_50) == 50
    for item in result_50:
        assert isinstance(item, str)

    # n=200 — must return exactly 200 since total_chunks >= 200
    result_large = await store.sample_chunk_texts("testcol", n=200)
    assert len(result_large) == 200

    # Empty collection
    result_empty = await store.sample_chunk_texts("nonexistent_col")
    assert result_empty == []

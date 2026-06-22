"""tests/test_store_is_continuation.py — Unit test for _is_continuation parameter on
SearchStore.ingest_chunks() (BE-2).

Verifies that calling ingest_chunks twice with the same doc_id and
_is_continuation=True on the second call results in doc_count == 1.
"""
from __future__ import annotations

import pytest

from archon_search._types import ChunkRecord


@pytest.mark.integration
@pytest.mark.asyncio
async def test_is_continuation_suppresses_doc_count_increment(tmp_path, monkeypatch):
    """Second ingest_chunks call with _is_continuation=True must not increment doc_count."""
    from tests.integration.conftest import make_real_pipeline

    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    # Manually ingest two batches for the same doc_id, simulating batched ingest
    collection = "cont_test_col"
    doc_id = "aaaa" * 16  # 64-char hex

    def _make_chunks(start: int, end: int) -> list[ChunkRecord]:
        records = []
        for i in range(start, end):
            c = ChunkRecord(
                doc_id=doc_id,
                chunk_id=f"{doc_id}-{i:06d}",
                text=f"chunk text number {i}",
                vector=[0.1, 0.2, 0.3, 0.4],
                source_path="/fake/path.txt",
                indexed_at="2024-01-01T00:00:00+00:00",
            )
            records.append(c)
        return records

    await store.ensure_collection(collection, 4)

    batch1 = _make_chunks(0, 5)
    result1 = await store.ingest_chunks(
        collection,
        batch1,
        embedding_model="mock-embedder",
        namespace="default",
        _is_continuation=False,
    )
    assert result1.chunks_ingested == 5

    batch2 = _make_chunks(5, 10)
    result2 = await store.ingest_chunks(
        collection,
        batch2,
        embedding_model="mock-embedder",
        namespace="default",
        _is_continuation=True,
    )
    assert result2.chunks_ingested == 5

    meta = await store.get_collection_meta(collection)
    assert meta is not None
    assert meta.doc_count == 1, (
        f"Expected doc_count=1 (continuation batch must not increment), got {meta.doc_count}"
    )
    assert meta.chunk_count == 10, (
        f"Expected chunk_count=10 (5 from batch1 + 5 from batch2), got {meta.chunk_count}"
    )

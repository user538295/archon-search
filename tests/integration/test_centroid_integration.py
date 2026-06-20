"""Integration tests for incremental centroid correctness.

All tests are ``async def`` because they call async pipeline and store methods
(``pipeline.ingest_file``, ``pipeline.delete_document``, ``store.get_collection_meta``,
``pipeline.recompute_collection_meta``) directly without going through TestClient.

Since ``pyproject.toml`` sets ``asyncio_mode = 'auto'``, no ``@pytest.mark.asyncio``
decorator is needed — just defining as ``async def`` is sufficient.
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
from pathlib import Path

from tests.integration.conftest import make_real_pipeline

COLLECTION = "test-centroid"
EMBEDDING_DIM = 4
TOLERANCE = 1e-5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_doc(tmp_path: Path, name: str, text: str) -> Path:
    """Write a text file and return its path."""
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _doc_id(path: Path) -> str:
    """Compute the doc_id as pipeline.ingest_file does."""
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()


def _assert_vectors_close(a: list[float], b: list[float], tol: float = TOLERANCE) -> None:
    assert len(a) == len(b), f"Length mismatch: {len(a)} vs {len(b)}"
    for i, (x, y) in enumerate(zip(a, b)):
        assert abs(x - y) <= tol, f"Vectors differ at index {i}: {x} vs {y} (tol={tol})"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_multi_batch_ingest_centroid_correctness(tmp_path: Path, monkeypatch) -> None:
    """Two real ingest batches on the same collection.

    Assert centroid == arithmetic mean of all chunk vectors (tolerance 1e-5).
    Verifies ``_do_update_meta_on_add`` accumulation across batches.
    """
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    await store.ensure_collection(COLLECTION, EMBEDDING_DIM)

    try:
        # Batch 1: two documents
        doc_a = _make_doc(tmp_path, "a.txt", "batch one document alpha content here")
        doc_b = _make_doc(tmp_path, "b.txt", "batch one document beta content here")
        result_a = await pipeline.ingest_file(
            doc_a, COLLECTION, embedder=pipeline._global_embedder
        )
        result_b = await pipeline.ingest_file(
            doc_b, COLLECTION, embedder=pipeline._global_embedder
        )
        assert result_a.status == "ok" and result_a.chunks_created > 0
        assert result_b.status == "ok" and result_b.chunks_created > 0

        # Batch 2: one more document
        doc_c = _make_doc(tmp_path, "c.txt", "batch two document gamma content here")
        result_c = await pipeline.ingest_file(
            doc_c, COLLECTION, embedder=pipeline._global_embedder
        )
        assert result_c.status == "ok" and result_c.chunks_created > 0

        meta = await store.get_collection_meta(COLLECTION)
        assert meta is not None
        assert meta.centroid is not None
        assert meta.centroid_sum is not None

        total_chunks = meta.chunk_count
        assert total_chunks > 0

        # centroid = centroid_sum / chunk_count
        expected_centroid = [v / total_chunks for v in meta.centroid_sum]
        _assert_vectors_close(meta.centroid, expected_centroid)

        # The stub embedder returns [0.1, 0.2, 0.3, 0.4] for every chunk.
        # centroid_sum = [0.1*n, 0.2*n, 0.3*n, 0.4*n]; centroid == [0.1, 0.2, 0.3, 0.4].
        stub_vec = [0.1, 0.2, 0.3, 0.4]
        _assert_vectors_close(meta.centroid, stub_vec)
    finally:
        await store.disconnect()


async def test_reingest_changed_document_net_zero(tmp_path: Path, monkeypatch) -> None:
    """Ingest doc_id A (batch 1). Re-ingest same doc_id A with different text (batch 2).

    Assert final centroid equals the centroid of batch-2 vectors only
    (batch-1 contribution subtracted).
    """
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    await store.ensure_collection(COLLECTION, EMBEDDING_DIM)

    try:
        doc_path = _make_doc(tmp_path, "reingest.txt", "original text for first ingest batch")
        result1 = await pipeline.ingest_file(
            doc_path, COLLECTION, embedder=pipeline._global_embedder
        )
        assert result1.status == "ok" and result1.chunks_created > 0

        chunks_after_first = result1.chunks_created

        # Overwrite with different text (same path → same doc_id)
        doc_path.write_text("completely new replacement text for second ingest batch", encoding="utf-8")
        result2 = await pipeline.ingest_file(
            doc_path, COLLECTION, embedder=pipeline._global_embedder
        )
        assert result2.status == "ok" and result2.chunks_created > 0

        chunks_after_second = result2.chunks_created

        meta = await store.get_collection_meta(COLLECTION)
        assert meta is not None
        assert meta.centroid is not None
        assert meta.centroid_sum is not None

        # Only batch-2 chunks remain; batch-1 was replaced.
        assert meta.chunk_count == chunks_after_second, (
            f"Expected {chunks_after_second} chunks (batch-2 only), got {meta.chunk_count}; "
            f"batch-1 had {chunks_after_first} chunks"
        )

        # Stub embedder returns uniform vectors, so centroid == stub_vec regardless.
        stub_vec = [0.1, 0.2, 0.3, 0.4]
        _assert_vectors_close(meta.centroid, stub_vec)

        # centroid_sum should equal stub_vec * chunk_count (batch-2 only)
        n = meta.chunk_count
        expected_sum = [v * n for v in stub_vec]
        _assert_vectors_close(meta.centroid_sum, expected_sum)
    finally:
        await store.disconnect()


async def test_delete_then_verify_centroid(tmp_path: Path, monkeypatch) -> None:
    """Ingest doc X and doc Y. Delete doc X.

    Assert centroid equals doc Y's vectors only.
    """
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    await store.ensure_collection(COLLECTION, EMBEDDING_DIM)

    try:
        doc_x = _make_doc(tmp_path, "x.txt", "document x content for centroid test")
        doc_y = _make_doc(tmp_path, "y.txt", "document y content for centroid test")

        result_x = await pipeline.ingest_file(
            doc_x, COLLECTION, embedder=pipeline._global_embedder
        )
        result_y = await pipeline.ingest_file(
            doc_y, COLLECTION, embedder=pipeline._global_embedder
        )
        assert result_x.status == "ok" and result_x.chunks_created > 0
        assert result_y.status == "ok" and result_y.chunks_created > 0

        chunks_y = result_y.chunks_created
        doc_x_id = _doc_id(doc_x)

        # Delete doc X
        deleted = await pipeline.delete_document(doc_x_id, COLLECTION)
        assert deleted > 0

        meta = await store.get_collection_meta(COLLECTION)
        assert meta is not None
        assert meta.centroid is not None
        assert meta.centroid_sum is not None

        # Only doc Y's chunks remain
        assert meta.chunk_count == chunks_y, (
            f"Expected {chunks_y} chunks (doc Y only), got {meta.chunk_count}"
        )

        stub_vec = [0.1, 0.2, 0.3, 0.4]
        _assert_vectors_close(meta.centroid, stub_vec)

        expected_sum = [v * chunks_y for v in stub_vec]
        _assert_vectors_close(meta.centroid_sum, expected_sum)
    finally:
        await store.disconnect()


async def test_drift_guard(tmp_path: Path, monkeypatch) -> None:
    """Ingest 10 docs in 5 incremental batches.

    Assert ``store.get_collection_meta().centroid_sum`` matches
    ``recompute_collection_meta(force=True)`` output within 1e-5.
    Catches accumulated floating-point drift.
    """
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    await store.ensure_collection(COLLECTION, EMBEDDING_DIM)

    try:
        # 5 batches of 2 docs each = 10 docs total
        for batch in range(5):
            for i in range(2):
                doc = _make_doc(
                    tmp_path,
                    f"batch{batch}_doc{i}.txt",
                    f"drift guard batch {batch} document {i} with enough text to produce one chunk",
                )
                result = await pipeline.ingest_file(
                    doc, COLLECTION, embedder=pipeline._global_embedder
                )
                assert result.status == "ok" and result.chunks_created > 0

        meta_incremental = await store.get_collection_meta(COLLECTION)
        assert meta_incremental is not None
        assert meta_incremental.centroid_sum is not None

        # Force recompute from all stored vectors
        await pipeline.recompute_collection_meta(
            COLLECTION, pipeline._global_embedder, force=True
        )
        meta_recomputed = await store.get_collection_meta(COLLECTION)
        assert meta_recomputed is not None
        assert meta_recomputed.centroid_sum is not None

        _assert_vectors_close(meta_incremental.centroid_sum, meta_recomputed.centroid_sum)
        _assert_vectors_close(meta_incremental.centroid, meta_recomputed.centroid)
    finally:
        await store.disconnect()


async def test_concurrent_ingest_and_delete_serializes_correctly(
    tmp_path: Path, monkeypatch
) -> None:
    """Deterministic serialization test: hold lock during batch_2, queue delete,
    release, then assert final centroid equals force-recomputed centroid.

    Uses ``threading.Event`` (not ``asyncio.Event``) so it can be signalled
    from the test coroutine via ``asyncio.to_thread``.
    """
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    await store.ensure_collection(COLLECTION, EMBEDDING_DIM)

    try:
        # --- Step 1: ingest batch_1 with 3 docs ---
        batch1_docs = []
        for i in range(3):
            doc = _make_doc(
                tmp_path,
                f"batch1_doc{i}.txt",
                f"batch one document {i} content for serialization test",
            )
            result = await pipeline.ingest_file(
                doc, COLLECTION, embedder=pipeline._global_embedder
            )
            assert result.status == "ok" and result.chunks_created > 0
            batch1_docs.append(doc)

        meta_after_batch1 = await store.get_collection_meta(COLLECTION)
        assert meta_after_batch1 is not None

        # --- Step 2: prepare batch_2 doc and a hold_event ---
        batch2_doc = _make_doc(
            tmp_path,
            "batch2_doc.txt",
            "batch two document content for serialization test",
        )

        hold_event = threading.Event()

        # Patch ingest_chunks to delay execution until hold_event is set.
        # The block is placed before calling original_ingest_chunks, so the event loop
        # is free to run the delete_document task while batch_2 is waiting.  The delete
        # finishes first (no blocking), then hold_event is set, then batch_2 proceeds.
        # This serializes the two ops without LanceDB write conflicts.
        original_ingest_chunks = store.ingest_chunks

        async def _blocking_ingest_chunks(collection, records, *, embedding_model, namespace, _is_continuation=False):
            # Block in a thread-pool worker — doesn't block the event loop.
            await asyncio.to_thread(hold_event.wait)
            return await original_ingest_chunks(
                collection, records,
                embedding_model=embedding_model,
                namespace=namespace,
                _is_continuation=_is_continuation,
            )

        monkeypatch.setattr(store, "ingest_chunks", _blocking_ingest_chunks)

        # --- Step 3: start batch_2 ingest as a task (will block at hold_event) ---
        batch2_task = asyncio.create_task(
            pipeline.ingest_file(batch2_doc, COLLECTION, embedder=pipeline._global_embedder)
        )

        # Give batch_2 a moment to reach the blocking point inside _blocking_ingest_chunks
        await asyncio.sleep(0.05)

        # --- Step 4: run delete to completion while batch_2 is held ---
        # delete_document acquires and releases the lock independently; batch_2 is not
        # holding any LanceDB lock at this point (blocked pre-lock-acquisition).
        doc_to_delete_id = _doc_id(batch1_docs[0])
        deleted_count = await pipeline.delete_document(doc_to_delete_id, COLLECTION)
        assert deleted_count > 0

        # --- Step 5: release the hold_event to let batch_2 proceed ---
        hold_event.set()

        # --- Step 6: await batch_2 task ---
        batch2_result = await batch2_task

        assert batch2_result.status == "ok"

        # --- Step 7: read incremental state, then force-recompute and compare ---
        # meta_incremental must be read BEFORE recompute so we compare two distinct values.
        meta_incremental = await store.get_collection_meta(COLLECTION)
        assert meta_incremental is not None
        assert meta_incremental.centroid is not None

        await pipeline.recompute_collection_meta(
            COLLECTION, pipeline._global_embedder, force=True
        )
        meta_recomputed = await store.get_collection_meta(COLLECTION)
        assert meta_recomputed is not None
        assert meta_recomputed.centroid is not None

        _assert_vectors_close(meta_incremental.centroid, meta_recomputed.centroid)
    finally:
        await store.disconnect()


async def test_pre_b5_collection_seeds_on_first_ingest(
    tmp_path: Path, monkeypatch
) -> None:
    """Create a meta row with empty ``centroid_sum_json``. Ingest one batch.

    Assert ``centroid_sum_json`` is populated and equals the batch centroid.
    Verifies migration path for pre-B5 collections.
    """
    from archon_search.collection_meta import CollectionMeta

    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    await store.ensure_collection(COLLECTION, EMBEDDING_DIM)

    try:
        # Seed a legacy meta row: centroid_sum=None (as if pre-B5)
        legacy_meta = CollectionMeta(
            name=COLLECTION,
            centroid=None,
            centroid_sum=None,
            doc_count=0,
            chunk_count=0,
            active_embedding_model=pipeline._global_embedder.model_name,
        )
        await store.update_collection_meta(legacy_meta)

        # Verify the seed: centroid_sum should be None
        pre_ingest_meta = await store.get_collection_meta(COLLECTION)
        assert pre_ingest_meta is not None
        assert pre_ingest_meta.centroid_sum is None, (
            "Pre-condition failed: centroid_sum should be None for legacy meta"
        )

        # Ingest one batch
        doc = _make_doc(tmp_path, "pre_b5_doc.txt", "pre b5 collection first ingest document content here")
        result = await pipeline.ingest_file(
            doc, COLLECTION, embedder=pipeline._global_embedder
        )
        assert result.status == "ok" and result.chunks_created > 0

        meta = await store.get_collection_meta(COLLECTION)
        assert meta is not None

        # When centroid_sum is None (invalid), _do_update_meta_on_add sets needs_recompute=True
        # and pipeline.ingest_file automatically calls recompute_collection_meta before returning.
        # No manual fallback: if centroid_sum is still None here, the auto-trigger is broken.
        assert meta.centroid_sum is not None, (
            "centroid_sum must be populated by automatic recompute triggered during ingest"
        )
        assert meta.centroid is not None, "centroid must be populated after first ingest"

        # For the stub embedder returning [0.1, 0.2, 0.3, 0.4] uniformly:
        stub_vec = [0.1, 0.2, 0.3, 0.4]
        _assert_vectors_close(meta.centroid, stub_vec)

        # centroid_sum = stub_vec * chunk_count
        n = meta.chunk_count
        assert n > 0
        expected_sum = [v * n for v in stub_vec]
        _assert_vectors_close(meta.centroid_sum, expected_sum)
    finally:
        await store.disconnect()

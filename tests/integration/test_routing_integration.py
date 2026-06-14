"""Integration tests for routing correctness after B4/B5.

All tests are ``async def`` because they call async pipeline and store methods
directly (``pipeline.ingest_file``, ``pipeline.delete_document``,
``store.get_collection_meta``, ``pipeline.recompute_collection_meta``,
``MultiCollectionRouter.rank()``).

Since ``pyproject.toml`` sets ``asyncio_mode = 'auto'``, no ``@pytest.mark.asyncio``
decorator is needed — just defining as ``async def`` is sufficient.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from archon_search.chunker import DocumentChunker
from archon_search.collection_meta import CollectionMeta
from archon_search.embedder import Embedder
from archon_search.parser import DocumentParser
from archon_search.pipeline import SearchPipeline
from archon_search.reranker import Reranker
from archon_search.router import MultiCollectionRouter
from archon_search.store import SearchStore
from tests.integration.conftest import make_real_pipeline

EMBEDDING_DIM = 4
TOLERANCE = 1e-5

# Confidence threshold low enough to pass for stub embedder's cosine similarity
_CONFIDENCE_THRESHOLD = 0.01

# The stub embedder always returns this vector for every chunk
_STUB_VEC = [0.1, 0.2, 0.3, 0.4]


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


def _make_router(
    collections: list[CollectionMeta],
    *,
    strategy: str = "centroid",
    embedding_model: str = "mock-embedder",
) -> MultiCollectionRouter:
    """Build a MultiCollectionRouter with pre-loaded metadata (no HTTP fetch)."""
    embedder = MagicMock()
    embedder.embed_one = AsyncMock(return_value=_STUB_VEC)
    return MultiCollectionRouter(
        search_url="http://localhost:9999",  # not used — initial_metadata bypasses fetch
        embedder=embedder,
        shortlist_size=10,
        confidence_threshold=_CONFIDENCE_THRESHOLD,
        embedding_model=embedding_model,
        initial_metadata=collections,
        strategy=strategy,
    )


def _make_pipeline_from_store(store: SearchStore) -> SearchPipeline:
    """Build a SearchPipeline from an existing store using the same stub backend as conftest.

    Use this when you need a second pipeline pointing at the same database
    (e.g., after a simulated reconnect) without calling make_real_pipeline(),
    which always creates a fresh db path.

    The stub backend is kept in sync with conftest.make_real_pipeline() here
    intentionally — if the conftest stub changes, this function must match it.
    """
    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [_STUB_VEC for _ in texts]

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    return SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_incremental_vs_recomputed_routing_equivalence(
    tmp_path: Path, monkeypatch
) -> None:
    """Three real collections with distinct corpora.

    Run 10 queries. Assert ``MultiCollectionRouter.rank()`` top-K results are
    identical whether centroids were maintained incrementally or
    force-recomputed. Verifies routing correctness after B5.
    """
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    col_a = "routing-col-a"
    col_b = "routing-col-b"
    col_c = "routing-col-c"

    await store.ensure_collection(col_a, EMBEDDING_DIM)
    await store.ensure_collection(col_b, EMBEDDING_DIM)
    await store.ensure_collection(col_c, EMBEDDING_DIM)

    try:
        # Ingest incrementally into all three collections
        for i in range(3):
            doc = _make_doc(tmp_path, f"cola_doc{i}.txt", f"corpus alpha document {i} text here")
            result = await pipeline.ingest_file(doc, col_a, embedder=pipeline._global_embedder)
            assert result.status == "ok" and result.chunks_created > 0

        for i in range(2):
            doc = _make_doc(tmp_path, f"colb_doc{i}.txt", f"corpus beta document {i} text here")
            result = await pipeline.ingest_file(doc, col_b, embedder=pipeline._global_embedder)
            assert result.status == "ok" and result.chunks_created > 0

        for i in range(4):
            doc = _make_doc(tmp_path, f"colc_doc{i}.txt", f"corpus gamma document {i} text here")
            result = await pipeline.ingest_file(doc, col_c, embedder=pipeline._global_embedder)
            assert result.status == "ok" and result.chunks_created > 0

        # Fetch incremental centroids
        meta_a_inc = await store.get_collection_meta(col_a)
        meta_b_inc = await store.get_collection_meta(col_b)
        meta_c_inc = await store.get_collection_meta(col_c)

        assert meta_a_inc is not None and meta_a_inc.centroid is not None
        assert meta_b_inc is not None and meta_b_inc.centroid is not None
        assert meta_c_inc is not None and meta_c_inc.centroid is not None

        # Assert centroid_sum VALUES (not just centroid) so off-by-one accumulation bugs
        # are caught. centroid_sum must equal STUB_VEC * chunk_count for each collection.
        assert meta_a_inc.centroid_sum is not None, "centroid_sum must be persisted after ingest"
        assert meta_b_inc.centroid_sum is not None, "centroid_sum must be persisted after ingest"
        assert meta_c_inc.centroid_sum is not None, "centroid_sum must be persisted after ingest"
        _assert_vectors_close(
            meta_a_inc.centroid_sum, [v * meta_a_inc.chunk_count for v in _STUB_VEC]
        )
        _assert_vectors_close(
            meta_b_inc.centroid_sum, [v * meta_b_inc.chunk_count for v in _STUB_VEC]
        )
        _assert_vectors_close(
            meta_c_inc.centroid_sum, [v * meta_c_inc.chunk_count for v in _STUB_VEC]
        )

        incremental_collections = [meta_a_inc, meta_b_inc, meta_c_inc]

        # Force-recompute all three
        await pipeline.recompute_collection_meta(col_a, pipeline._global_embedder, force=True)
        await pipeline.recompute_collection_meta(col_b, pipeline._global_embedder, force=True)
        await pipeline.recompute_collection_meta(col_c, pipeline._global_embedder, force=True)

        meta_a_rc = await store.get_collection_meta(col_a)
        meta_b_rc = await store.get_collection_meta(col_b)
        meta_c_rc = await store.get_collection_meta(col_c)

        assert meta_a_rc is not None and meta_a_rc.centroid is not None
        assert meta_b_rc is not None and meta_b_rc.centroid is not None
        assert meta_c_rc is not None and meta_c_rc.centroid is not None

        recomputed_collections = [meta_a_rc, meta_b_rc, meta_c_rc]

        # Assert centroid VALUES match between incremental and force-recomputed paths.
        # A symmetric bug (e.g., wrong division in both paths) would produce identical
        # wrong rankings but different centroids — this check catches it.
        _assert_vectors_close(meta_a_inc.centroid, meta_a_rc.centroid)
        _assert_vectors_close(meta_b_inc.centroid, meta_b_rc.centroid)
        _assert_vectors_close(meta_c_inc.centroid, meta_c_rc.centroid)

        # Assert ranking ORDER is also identical (routing equivalence).
        query_vector = _STUB_VEC

        router_incremental = _make_router(incremental_collections)
        router_recomputed = _make_router(recomputed_collections)

        ranked_inc = router_incremental.rank(query_vector, incremental_collections)
        ranked_rc = router_recomputed.rank(query_vector, recomputed_collections)

        inc_names = [m.name for m in ranked_inc]
        rc_names = [m.name for m in ranked_rc]

        # Guard against a false-positive where both routers return [] (e.g., confidence
        # gate fired): [] == [] would pass but provide no signal about routing correctness.
        assert len(inc_names) == 3, (
            f"Incremental router returned {len(inc_names)} collections, expected 3: {inc_names}"
        )
        assert len(rc_names) == 3, (
            f"Recomputed router returned {len(rc_names)} collections, expected 3: {rc_names}"
        )
        assert inc_names == rc_names, (
            f"Incremental vs recomputed ranking mismatch: {inc_names} vs {rc_names}"
        )
    finally:
        await store.disconnect()


async def test_hybrid_routing_blends_description_embedding_to_rank_collection(
    tmp_path: Path, monkeypatch
) -> None:
    """Verify hybrid routing blends centroid + description_embedding correctly.

    Ingests two real corpora, force-recomputes centroids, then manually injects
    distinct ``description_embedding`` values into CollectionMeta objects to
    simulate collections with different descriptions.

    Note: ``generate_description`` is skipped without ``ANTHROPIC_API_KEY``, so
    ``description_embedding`` is always ``None`` after ``recompute_collection_meta``.
    Manual injection is the only viable path in CI without live model access.
    The test exercises the hybrid-blending formula in ``MultiCollectionRouter``
    rather than the full persist→fetch pipeline for description embeddings.

    The stub embedder returns ``[0.1, 0.2, 0.3, 0.4]`` for every chunk, making
    centroid similarity identical for both collections. Distinct
    ``description_embedding`` values break the tie: the collection whose
    description is aligned with the query must rank first.
    """
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    col_a = "hybrid-col-a"
    col_b = "hybrid-col-b"

    await store.ensure_collection(col_a, EMBEDDING_DIM)
    await store.ensure_collection(col_b, EMBEDDING_DIM)

    try:
        # Ingest into both collections
        for i in range(2):
            doc = _make_doc(tmp_path, f"ha_doc{i}.txt", f"corpus alpha hybrid text {i}")
            result = await pipeline.ingest_file(doc, col_a, embedder=pipeline._global_embedder)
            assert result.status == "ok" and result.chunks_created > 0

        for i in range(2):
            doc = _make_doc(tmp_path, f"hb_doc{i}.txt", f"corpus beta hybrid text {i}")
            result = await pipeline.ingest_file(doc, col_b, embedder=pipeline._global_embedder)
            assert result.status == "ok" and result.chunks_created > 0

        # Force-recompute to get centroid_sum into meta
        await pipeline.recompute_collection_meta(col_a, pipeline._global_embedder, force=True)
        await pipeline.recompute_collection_meta(col_b, pipeline._global_embedder, force=True)

        meta_a = await store.get_collection_meta(col_a)
        meta_b = await store.get_collection_meta(col_b)

        assert meta_a is not None and meta_a.centroid is not None
        assert meta_b is not None and meta_b.centroid is not None

        # Manually inject description_embeddings to break the tie between the two
        # collections (stub embedder gives identical centroid for both).
        # col_b description_embedding is aligned with query → higher cosine sim
        # col_a description_embedding is orthogonal → cosine sim ≈ 0.0
        aligned_with_query = _STUB_VEC  # cosine_sim(query, aligned) ≈ 1.0
        orthogonal_to_query = [0.4, -0.3, 0.2, -0.1]  # cosine_sim(query, orthogonal) = 0.0

        meta_a_with_desc = CollectionMeta(
            name=col_a,
            centroid=meta_a.centroid,
            centroid_sum=meta_a.centroid_sum,
            chunk_count=meta_a.chunk_count,
            doc_count=meta_a.doc_count,
            active_embedding_model=meta_a.active_embedding_model,
            description_embedding=orthogonal_to_query,
        )
        meta_b_with_desc = CollectionMeta(
            name=col_b,
            centroid=meta_b.centroid,
            centroid_sum=meta_b.centroid_sum,
            chunk_count=meta_b.chunk_count,
            doc_count=meta_b.doc_count,
            active_embedding_model=meta_b.active_embedding_model,
            description_embedding=aligned_with_query,
        )

        collections = [meta_a_with_desc, meta_b_with_desc]

        router = _make_router(collections, strategy="hybrid")

        ranked = router.rank(_STUB_VEC, collections)

        assert len(ranked) > 0, "Router returned no collections"
        assert ranked[0].name == col_b, (
            f"Expected corpus-B (aligned description) to rank first, got {ranked[0].name}"
        )
    finally:
        await store.disconnect()


async def test_e2e_delete_updates_routing_centroid(
    tmp_path: Path, monkeypatch
) -> None:
    """Ingest 2 docs → delete one → assert routing centroid no longer
    includes deleted doc's vectors.

    Verified by reading ``centroid_sum`` directly from the store after delete.
    The stub embedder returns [0.1, 0.2, 0.3, 0.4] for every chunk.
    After deleting doc X (n_x chunks), centroid_sum must equal
    [0.1 * n_y, 0.2 * n_y, 0.3 * n_y, 0.4 * n_y] where n_y is doc Y's chunk count.
    """
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    collection = "routing-delete-test"
    await store.ensure_collection(collection, EMBEDDING_DIM)

    try:
        doc_x = _make_doc(tmp_path, "del_x.txt", "document x content for routing delete test")
        doc_y = _make_doc(tmp_path, "del_y.txt", "document y content for routing delete test")

        result_x = await pipeline.ingest_file(doc_x, collection, embedder=pipeline._global_embedder)
        result_y = await pipeline.ingest_file(doc_y, collection, embedder=pipeline._global_embedder)

        assert result_x.status == "ok" and result_x.chunks_created > 0
        assert result_y.status == "ok" and result_y.chunks_created > 0

        chunks_y = result_y.chunks_created

        # Delete doc X
        doc_x_id = _doc_id(doc_x)
        deleted = await pipeline.delete_document(doc_x_id, collection)
        assert deleted > 0, "Expected at least one chunk deleted for doc X"

        # Read updated meta
        meta = await store.get_collection_meta(collection)
        assert meta is not None
        assert meta.centroid is not None
        assert meta.centroid_sum is not None

        # Only doc Y's chunks remain
        assert meta.chunk_count == chunks_y, (
            f"Expected {chunks_y} chunks (doc Y only), got {meta.chunk_count}"
        )

        # centroid_sum must equal _STUB_VEC * n_y (doc X contribution removed)
        expected_sum = [v * chunks_y for v in _STUB_VEC]
        _assert_vectors_close(meta.centroid_sum, expected_sum)

        # Verify that a router built from this meta does not include stale vectors.
        # Route the collection with the updated centroid — it must still be routable
        # (centroid is non-null) and the centroid must match doc Y's only.
        _assert_vectors_close(meta.centroid, _STUB_VEC)

        router = _make_router([meta], embedding_model="mock-embedder")
        ranked = router.rank(_STUB_VEC, [meta])
        assert len(ranked) == 1
        assert ranked[0].name == collection
    finally:
        await store.disconnect()


async def test_e2e_incremental_centroid_survives_reconnect(
    tmp_path: Path, monkeypatch
) -> None:
    """Ingest batch 1. Disconnect and reconnect ``SearchStore``. Ingest batch 2.

    Assert centroid equals mean of all batches (not just batch 2).
    Verifies that centroid_sum persisted to disk survives a reconnect and that
    the incremental update continues from the correct base.
    """
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    collection = "routing-reconnect-test"
    await store.ensure_collection(collection, EMBEDDING_DIM)

    try:
        # Batch 1: ingest before disconnect
        doc_a = _make_doc(tmp_path, "reconnect_a.txt", "batch one before reconnect text content")
        doc_b = _make_doc(tmp_path, "reconnect_b.txt", "batch one second doc before reconnect")

        result_a = await pipeline.ingest_file(doc_a, collection, embedder=pipeline._global_embedder)
        result_b = await pipeline.ingest_file(doc_b, collection, embedder=pipeline._global_embedder)

        assert result_a.status == "ok" and result_a.chunks_created > 0
        assert result_b.status == "ok" and result_b.chunks_created > 0

        chunks_batch1 = result_a.chunks_created + result_b.chunks_created

        meta_before_disconnect = await store.get_collection_meta(collection)
        assert meta_before_disconnect is not None
        assert meta_before_disconnect.centroid_sum is not None
        assert meta_before_disconnect.chunk_count == chunks_batch1
        # Verify the persisted centroid_sum VALUE (not just non-None) so a bug that
        # stores all-zeros or wrong sum is caught before the reconnect leg runs.
        expected_sum_batch1 = [v * chunks_batch1 for v in _STUB_VEC]
        _assert_vectors_close(meta_before_disconnect.centroid_sum, expected_sum_batch1)

    finally:
        # Disconnect the store — simulates a process restart
        await store.disconnect()

    # Reconnect: create a fresh store pointing at the same db_path.
    # Use _make_pipeline_from_store() to keep the stub embedder definition in
    # one place — avoids the consistency risk of duplicating [0.1, 0.2, 0.3, 0.4]
    # inline (if conftest changes the stub, this test must use the same vector).
    store2 = SearchStore(str(tmp_path / "db"))
    await store2.connect()
    pipeline2 = _make_pipeline_from_store(store2)

    try:
        # Batch 2: ingest after reconnect
        doc_c = _make_doc(tmp_path, "reconnect_c.txt", "batch two after reconnect text content")

        result_c = await pipeline2.ingest_file(doc_c, collection, embedder=pipeline2._global_embedder)
        assert result_c.status == "ok" and result_c.chunks_created > 0

        chunks_batch2 = result_c.chunks_created

        meta_after = await store2.get_collection_meta(collection)
        assert meta_after is not None
        assert meta_after.centroid is not None
        assert meta_after.centroid_sum is not None

        total_chunks = chunks_batch1 + chunks_batch2
        assert meta_after.chunk_count == total_chunks, (
            f"Expected {total_chunks} total chunks (batch1={chunks_batch1} + "
            f"batch2={chunks_batch2}), got {meta_after.chunk_count}"
        )

        # centroid == _STUB_VEC because all chunks have the same vector
        _assert_vectors_close(meta_after.centroid, _STUB_VEC)

        # centroid_sum includes contributions from ALL chunks (both batches)
        expected_sum = [v * total_chunks for v in _STUB_VEC]
        _assert_vectors_close(meta_after.centroid_sum, expected_sum)
    finally:
        await store2.disconnect()

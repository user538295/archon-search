"""tests/pipeline/test_pipeline_multi.py — Multi-collection tests for SearchPipeline.

Covers: search_many() fanout, _fuse_rag_fusion_results unit tests, RAG fusion
integration (search path), explain multi-collection, and cross-collection assertions.
Moved from tests/test_pipeline.py as part of C11 pipeline test split.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.embedder import Embedder

from .conftest import MockEmbedderBackend, MockRerankerBackend, make_embedder, make_reranker, make_pipeline


# ===========================================================================
# search_many (B3 Task 3.2) — multi-collection fan-out
# ===========================================================================


def _scored(collection: str, doc_id: str, chunk_id: str, rrf_score: float = 0.5):
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown

    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text=f"text-{chunk_id}",
        source_path=f"/path/to/{doc_id}.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=0,
            vector_score=0.9,
            vector_score_kind="distance",
            fts_rank=None,
            fts_score=None,
            fts_score_kind=None,
            rrf_score=rrf_score,
            reranker_score=None,
        ),
        collection=collection,
    )


def _meta(name: str, *, active_embedding_model: str = "mock-embedder", namespace: str = "default"):
    from archon_search.collection_meta import CollectionMeta

    return CollectionMeta(name=name, active_embedding_model=active_embedding_model, namespace=namespace)


def _search_many_pipeline(
    *,
    leg_map: dict | None = None,
    meta_list: list | None = None,
    fanout_leg_trim: int = 40,
    top_k_return: int = 5,
    top_k_retrieve: int = 10,
    fanout_timeout_seconds: float = 30.0,
):
    """Build a SearchPipeline with a MagicMock store wired for fan-out.

    ``leg_map`` maps collection-name -> list[ScoredSearchCandidate]; the
    store's hybrid_search_with_trace dispatches per collection.
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    leg_map = leg_map or {}

    async def _hybrid(collection, vector, query_text, candidate_depth, filters=None, scope_filter=None):
        return list(leg_map.get(collection, []))

    store.hybrid_search_with_trace = AsyncMock(side_effect=_hybrid)

    embedder = make_embedder()
    embedder.embed_one = AsyncMock(return_value=[0.1] * 4)  # type: ignore[method-assign]

    reranker = make_reranker()

    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=top_k_retrieve,
        top_k_return=top_k_return,
        fanout_leg_trim=fanout_leg_trim,
        fanout_timeout_seconds=fanout_timeout_seconds,
    )
    if meta_list is not None:
        pipeline.get_all_collections_meta = AsyncMock(return_value=meta_list)  # type: ignore[method-assign]
    return pipeline, store, embedder, reranker


@pytest.mark.asyncio
async def test_search_many_embeds_once() -> None:
    cols = ["A", "B", "C"]
    leg_map = {c: [_scored(c, "d" * 64, f"{'d' * 64}-000000")] for c in cols}
    pipeline, store, embedder, reranker = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta(c) for c in cols]
    )
    await pipeline.search_many("q", cols)
    assert embedder.embed_one.await_count == 1


@pytest.mark.asyncio
async def test_search_many_reranks_once() -> None:
    cols = ["A", "B"]
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-000000")],
    }
    pipeline, store, embedder, reranker = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta("A"), _meta("B")]
    )
    spy = AsyncMock(side_effect=reranker.rerank_candidates)
    reranker.rerank_candidates = spy  # type: ignore[method-assign]

    await pipeline.search_many("q", cols)

    assert spy.await_count == 1
    merged_passed = spy.await_args.args[1] if len(spy.await_args.args) > 1 else spy.await_args.kwargs["candidates"]
    # merged pool == sum of trimmed per-leg pools (1 + 1)
    assert len(merged_passed) == 2


@pytest.mark.asyncio
async def test_search_many_result_carries_collection_provenance() -> None:
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-000000")],
    }
    pipeline, *_ = _search_many_pipeline(leg_map=leg_map, meta_list=[_meta("A"), _meta("B")])
    result = await pipeline.search_many("q", ["A", "B"])
    by_doc = {r.doc_id: r.collection for r in result.results}
    assert by_doc["a" * 64] == "A"
    assert by_doc["b" * 64] == "B"


@pytest.mark.asyncio
async def test_search_many_merge_order_deterministic() -> None:
    """Merge concatenates legs in ascending collection-name order, regardless of
    the order collections are requested."""
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-000000")],
    }
    pipeline, _store, _embedder, reranker = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta("A"), _meta("B")]
    )
    spy = AsyncMock(side_effect=reranker.rerank_candidates)
    reranker.rerank_candidates = spy  # type: ignore[method-assign]

    # Request in reverse (non-alphabetical) order.
    await pipeline.search_many("q", ["B", "A"])

    merged = spy.await_args.args[1]
    # Merge must be alphabetical by collection name (A before B), not request order.
    assert [c.collection for c in merged] == ["A", "B"]


@pytest.mark.asyncio
async def test_search_many_namespace_scope_excludes_out_of_namespace() -> None:
    """A collection that exists only in namespace B is invisible from namespace A:
    it is never searched, and requesting it from A raises CollectionNotFoundError
    (no cross-namespace existence leak)."""
    from archon_search.pipeline import CollectionNotFoundError

    leg_map = {"A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")]}
    pipeline, store, *_ = _search_many_pipeline(leg_map=leg_map, meta_list=None)
    # Back the REAL pipeline.get_all_collections_meta with a store returning both
    # A (namespace A) and B (namespace B); the pipeline filters by namespace.
    store.get_all_collections_meta = AsyncMock(
        return_value=[_meta("A", namespace="A"), _meta("B", namespace="B")]
    )

    await pipeline.search_many("q", ["A"], namespace="A")
    called_cols = {c.args[0] for c in store.hybrid_search_with_trace.call_args_list}
    assert called_cols == {"A"}

    # B lives in namespace B → not found from namespace A (strict 404, no leak).
    with pytest.raises(CollectionNotFoundError):
        await pipeline.search_many("q", ["B"], namespace="A")


@pytest.mark.asyncio
async def test_search_many_missing_collection_raises_collection_not_found() -> None:
    from archon_search.pipeline import CollectionNotFoundError

    pipeline, *_ = _search_many_pipeline(leg_map={}, meta_list=[_meta("A")])
    with pytest.raises(CollectionNotFoundError):
        await pipeline.search_many("q", ["A", "MISSING"])


@pytest.mark.asyncio
async def test_search_many_model_mismatch_excludes_and_reports() -> None:
    from archon_search._types import ExcludedCollection

    leg_map = {"A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")]}
    pipeline, store, *_ = _search_many_pipeline(
        leg_map=leg_map,
        meta_list=[_meta("A"), _meta("B", active_embedding_model="other-model")],
    )
    result = await pipeline.search_many("q", ["A", "B"])
    assert ExcludedCollection(name="B", reason="embedding_model_mismatch") in result.excluded_collections
    called_cols = {c.args[0] for c in store.hybrid_search_with_trace.call_args_list}
    assert "B" not in called_cols


@pytest.mark.asyncio
async def test_search_many_leg_failure_cancels_siblings_and_raises() -> None:
    cancelled = asyncio.Event()

    async def _hybrid(collection, vector, query_text, candidate_depth, filters=None, scope_filter=None):
        if collection == "A":
            raise RuntimeError("leg failed")
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return []

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.hybrid_search_with_trace = AsyncMock(side_effect=_hybrid)
    embedder = make_embedder()
    embedder.embed_one = AsyncMock(return_value=[0.1] * 4)  # type: ignore[method-assign]
    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    pipeline.get_all_collections_meta = AsyncMock(return_value=[_meta("A"), _meta("B")])  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="leg failed"):
        await pipeline.search_many("q", ["A", "B"])
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_search_many_timeout_raises_fanout_timeout_error() -> None:
    from time import monotonic

    from archon_search.pipeline import FanoutTimeoutError

    async def _hybrid(collection, vector, query_text, candidate_depth, filters=None, scope_filter=None):
        await asyncio.sleep(999)
        return []

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.hybrid_search_with_trace = AsyncMock(side_effect=_hybrid)
    embedder = make_embedder()
    embedder.embed_one = AsyncMock(return_value=[0.1] * 4)  # type: ignore[method-assign]
    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        fanout_timeout_seconds=0.001,
    )
    pipeline.get_all_collections_meta = AsyncMock(return_value=[_meta("A"), _meta("B")])  # type: ignore[method-assign]

    t0 = monotonic()
    with pytest.raises(FanoutTimeoutError):
        await pipeline.search_many("q", ["A", "B"])
    assert (monotonic() - t0) < 2.0


@pytest.mark.asyncio
async def test_same_chunk_id_in_two_collections_both_survive() -> None:
    shared_chunk = f"{'a' * 64}-000000"
    leg_map = {
        "A": [_scored("A", "a" * 64, shared_chunk)],
        "B": [_scored("B", "a" * 64, shared_chunk)],
    }
    pipeline, store, embedder, reranker = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta("A"), _meta("B")]
    )
    spy = AsyncMock(side_effect=reranker.rerank_candidates)
    reranker.rerank_candidates = spy  # type: ignore[method-assign]

    await pipeline.search_many("q", ["A", "B"])

    merged_passed = spy.await_args.args[1]
    collections = sorted(c.collection for c in merged_passed)
    assert collections == ["A", "B"]


@pytest.mark.asyncio
async def test_search_many_all_collections_model_mismatched_returns_empty() -> None:
    from archon_search.pipeline import SearchPipelineResult

    pipeline, store, *_ = _search_many_pipeline(
        leg_map={},
        meta_list=[
            _meta("A", active_embedding_model="other-model"),
            _meta("B", active_embedding_model="other-model"),
        ],
    )
    result = await pipeline.search_many("q", ["A", "B"])
    assert isinstance(result, SearchPipelineResult)
    assert result.results == []
    assert {e.name for e in result.excluded_collections} == {"A", "B"}
    assert store.hybrid_search_with_trace.await_count == 0


@pytest.mark.asyncio
async def test_search_many_leg_trim_below_top_k_return() -> None:
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-{i:06d}", rrf_score=1.0 - i * 0.01) for i in range(10)],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-{i:06d}", rrf_score=1.0 - i * 0.01) for i in range(10)],
    }
    pipeline, *_ = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta("A"), _meta("B")], fanout_leg_trim=1, top_k_return=5
    )
    result = await pipeline.search_many("q", ["A", "B"])
    assert len(result.results) == 2


@pytest.mark.asyncio
async def test_search_many_meta_lookup_raises_propagates() -> None:
    from archon_search.pipeline import MetadataLookupError

    pipeline, *_ = _search_many_pipeline(leg_map={})
    pipeline.get_all_collections_meta = AsyncMock(side_effect=RuntimeError("store error"))  # type: ignore[method-assign]
    with pytest.raises(MetadataLookupError):
        await pipeline.search_many("q", ["A"])


@pytest.mark.asyncio
async def test_search_many_heterogeneous_leg_pool_sizes() -> None:
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-{i:06d}", rrf_score=1.0 - i * 0.001) for i in range(40)],
        "B": [],
    }
    pipeline, *_ = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta("A"), _meta("B")], fanout_leg_trim=40, top_k_return=50
    )
    result = await pipeline.search_many("q", ["A", "B"])
    assert all(r.collection == "A" for r in result.results)
    assert len(result.results) == 40


@pytest.mark.asyncio
async def test_search_many_populates_fanout_timings() -> None:
    """Result carries FanoutTimings with one leg_times entry per searched
    collection plus a non-negative rerank time."""
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-000000")],
    }
    pipeline, *_ = _search_many_pipeline(leg_map=leg_map, meta_list=[_meta("A"), _meta("B")])
    result = await pipeline.search_many("q", ["A", "B"])

    assert result.fanout_timings is not None
    assert set(result.fanout_timings.leg_times) == {"A", "B"}
    assert all(v >= 0 for v in result.fanout_timings.leg_times.values())
    assert result.fanout_timings.rerank_time_ms >= 0


@pytest.mark.asyncio
async def test_search_many_acl_filtered_propagates() -> None:
    """When ACL drops a candidate from the merged pool, acl_filtered is True."""
    a_open = _scored("A", "a" * 64, f"{'a' * 64}-000000")  # acl=None → open
    b_denied = _scored("B", "b" * 64, f"{'b' * 64}-000000")
    b_denied.acl = ["other-namespace"]  # not the search namespace → dropped
    leg_map = {"A": [a_open], "B": [b_denied]}
    pipeline, *_ = _search_many_pipeline(leg_map=leg_map, meta_list=[_meta("A"), _meta("B")])

    result = await pipeline.search_many("q", ["A", "B"], namespace="default")

    assert result.acl_filtered is True
    # The denied candidate must not survive into results.
    assert all(r.collection != "B" for r in result.results)


# ===========================================================================
# C4 Task 2.2 — search_many() query_vector parameter
# ===========================================================================

@pytest.mark.asyncio
async def test_search_many_uses_provided_query_vector() -> None:
    """search_many() uses caller-provided query_vector; _global_embedder.embed_one must NOT be called."""
    cols = ["A"]
    leg_map = {c: [_scored(c, "d" * 64, f"{'d' * 64}-000000")] for c in cols}
    pipeline, store, embedder, reranker = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta(c) for c in cols]
    )

    provided_vector = [0.9, 0.8, 0.7, 0.6]
    await pipeline.search_many("q", cols, query_vector=provided_vector)

    embedder.embed_one.assert_not_called()

    # Verify the provided vector was passed to the store's fan-out call.
    call_args = store.hybrid_search_with_trace.await_args_list
    assert len(call_args) == 1
    passed_vector = call_args[0].args[1] if call_args[0].args else call_args[0].kwargs.get("vector")
    assert list(passed_vector) == provided_vector


@pytest.mark.asyncio
async def test_search_many_embeds_when_no_query_vector() -> None:
    """search_many() calls _global_embedder.embed_one when query_vector is None (pre-C4 behaviour)."""
    cols = ["A"]
    leg_map = {c: [_scored(c, "d" * 64, f"{'d' * 64}-000000")] for c in cols}
    pipeline, store, embedder, reranker = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta(c) for c in cols]
    )

    await pipeline.search_many("q", cols, query_vector=None)

    embedder.embed_one.assert_awaited_once_with("q")


# ===========================================================================
# C1 embedding-model awareness — search_many and explain multi-collection
# ===========================================================================

@pytest.mark.asyncio
async def test_search_many_no_embedding_model_attribute_error() -> None:
    """search_many() must not raise AttributeError when collections have active_embedding_model.

    Verifies that search_many uses meta.active_embedding_model (not meta.embedding_model).
    """
    cols = ["A", "B"]
    leg_map = {c: [_scored(c, "e" * 64, f"{'e' * 64}-000000")] for c in cols}
    pipeline, *_ = _search_many_pipeline(
        leg_map=leg_map,
        meta_list=[_meta(c) for c in cols],
    )
    # Should not raise AttributeError
    result = await pipeline.search_many("query text", cols)
    assert result is not None


@pytest.mark.asyncio
async def test_explain_multi_collection_no_embedding_model_attribute_error() -> None:
    """explain() with collections= must not raise AttributeError when meta has active_embedding_model.

    Verifies that the multi-collection explain path uses meta.active_embedding_model.
    """
    cols = ["X", "Y"]
    leg_map = {c: [_scored(c, "f" * 64, f"{'f' * 64}-000000")] for c in cols}
    pipeline, store, *_ = _search_many_pipeline(
        leg_map=leg_map,
        meta_list=[_meta(c) for c in cols],
    )

    async def _hybrid_explain(collection, vector, query_text, candidate_depth, filters=None, scope_filter=None):
        return list(leg_map.get(collection, []))

    store.hybrid_search_with_trace = AsyncMock(side_effect=_hybrid_explain)

    # Should not raise AttributeError
    result = await pipeline.explain("query text", collections=cols)
    assert result is not None


# ---------------------------------------------------------------------------
# _fuse_rag_fusion_results — Task 2.1 (C5 RAG Fusion)
# ---------------------------------------------------------------------------

def _make_candidate(chunk_id: str, doc_id: str = "doc-x") -> "ScoredSearchCandidate":
    """Build a minimal ScoredSearchCandidate for fusion tests."""
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text="test text",
        source_path="/test/path",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=1,
            vector_score=0.9,
            vector_score_kind="similarity",
            fts_rank=1,
            fts_score=0.8,
            fts_score_kind="bm25",
            rrf_score=0.03,
            reranker_score=None,
        ),
        collection="test-col",
    )


def test_fuse_rag_fusion_results_two_variants() -> None:
    """Two variant lists with one overlapping chunk_id — overlapping chunk ranks higher."""
    from archon_search.pipeline import _fuse_rag_fusion_results

    shared = _make_candidate("chunk-shared")
    only_v0 = _make_candidate("chunk-only-v0")
    only_v1 = _make_candidate("chunk-only-v1")

    # variant 0: shared at rank 0, only_v0 at rank 1
    # variant 1: shared at rank 0, only_v1 at rank 1
    variant_results = [
        [shared, only_v0],
        [shared, only_v1],
    ]
    fused = _fuse_rag_fusion_results(variant_results)

    chunk_ids = [c.chunk_id for c in fused]
    assert "chunk-shared" in chunk_ids
    assert "chunk-only-v0" in chunk_ids
    assert "chunk-only-v1" in chunk_ids

    # shared must rank highest — it accumulated score from both variants
    assert fused[0].chunk_id == "chunk-shared"


def test_fuse_rag_fusion_results_no_overlap() -> None:
    """Two variant lists with no shared chunk_ids — all chunks present."""
    from archon_search.pipeline import _fuse_rag_fusion_results

    a = _make_candidate("chunk-a")
    b = _make_candidate("chunk-b")
    c = _make_candidate("chunk-c")
    d = _make_candidate("chunk-d")

    fused = _fuse_rag_fusion_results([[a, b], [c, d]])
    chunk_ids = {cand.chunk_id for cand in fused}
    assert chunk_ids == {"chunk-a", "chunk-b", "chunk-c", "chunk-d"}


def test_fuse_rag_fusion_results_empty_inputs() -> None:
    """All-empty variant lists return empty list."""
    from archon_search.pipeline import _fuse_rag_fusion_results

    assert _fuse_rag_fusion_results([[], []]) == []


def test_fuse_rag_fusion_results_single_variant() -> None:
    """Single variant list — all chunks ranked by their per-list RRF scores."""
    from archon_search.pipeline import _fuse_rag_fusion_results

    a = _make_candidate("chunk-a")
    b = _make_candidate("chunk-b")
    c = _make_candidate("chunk-c")

    fused = _fuse_rag_fusion_results([[a, b, c]])
    assert [cand.chunk_id for cand in fused] == ["chunk-a", "chunk-b", "chunk-c"]


def test_fuse_rag_fusion_results_multi_contribution_boost() -> None:
    """Chunk in both variants at rank 1 scores higher than chunk only in one variant at rank 1."""
    from archon_search.pipeline import _fuse_rag_fusion_results

    boosted = _make_candidate("chunk-boosted")
    single = _make_candidate("chunk-single")

    # boosted appears at rank 0 in both variants
    # single appears only in variant 0 at rank 0
    fused = _fuse_rag_fusion_results([
        [boosted, single],  # variant 0: boosted=rank0, single=rank1
        [boosted],          # variant 1: boosted=rank0
    ])
    # boosted has 2× accumulation; single has 1× — boosted must rank first
    assert fused[0].chunk_id == "chunk-boosted"
    assert fused[1].chunk_id == "chunk-single"


def test_fuse_rag_fusion_results_deterministic() -> None:
    """Same inputs always produce the same output."""
    from archon_search.pipeline import _fuse_rag_fusion_results

    a = _make_candidate("chunk-a")
    b = _make_candidate("chunk-b")
    c = _make_candidate("chunk-c")

    variant_results = [[a, b], [b, c]]
    first = [cand.chunk_id for cand in _fuse_rag_fusion_results(variant_results)]
    second = [cand.chunk_id for cand in _fuse_rag_fusion_results(variant_results)]
    assert first == second


def test_fuse_rag_fusion_results_same_doc_different_chunks() -> None:
    """Dedup is by chunk_id, not doc_id — both chunks from same doc survive."""
    from archon_search.pipeline import _fuse_rag_fusion_results

    chunk1 = _make_candidate("chunk-001", doc_id="doc-x")
    chunk2 = _make_candidate("chunk-002", doc_id="doc-x")

    fused = _fuse_rag_fusion_results([[chunk1], [chunk2]])
    chunk_ids = {c.chunk_id for c in fused}
    assert "chunk-001" in chunk_ids
    assert "chunk-002" in chunk_ids


# ---------------------------------------------------------------------------
# Task 2.2 (C5 RAG Fusion) — SearchPipelineResult fields + search() orchestration
# ---------------------------------------------------------------------------


def test_search_pipeline_result_has_rag_fusion_fields() -> None:
    """SearchPipelineResult created with only required fields has rag_fusion defaults."""
    from archon_search.pipeline import SearchPipelineResult

    result = SearchPipelineResult(results=[], acl_filtered=False)
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_queries_used == 0
    assert result.rag_fusion_attempted is False


@pytest.mark.asyncio
async def test_search_rag_fusion_calls_generate_variants() -> None:
    """With rag_fusion=True and 2 variants, store.hybrid_search_with_trace called 3 times."""
    from unittest.mock import AsyncMock

    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    def _cand(chunk_id: str) -> ScoredSearchCandidate:
        return ScoredSearchCandidate(
            doc_id="doc-a",
            chunk_id=chunk_id,
            text="text",
            source_path="/p",
            score_breakdown=SearchScoreBreakdown(
                vector_rank=0, vector_score=0.9, vector_score_kind="distance",
                fts_rank=None, fts_score=None, fts_score_kind=None,
                rrf_score=0.016, reranker_score=None,
            ),
            collection="col",
        )

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[_cand("chunk-001")])
    mock_store.has_vector_index = AsyncMock(return_value=True)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["variant1", "variant2"])

    rag_config = RAGFusionConfig(enabled=True, num_queries=2)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    result = await pipeline.search(
        "original query",
        "col",
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    assert mock_store.hybrid_search_with_trace.call_count == 3  # original + 2 variants
    assert result.rag_fusion_applied is True
    assert result.rag_fusion_queries_used == 2
    assert result.rag_fusion_attempted is True


@pytest.mark.asyncio
async def test_search_rag_fusion_empty_variants_still_searches() -> None:
    """When generator returns [], one search (original) is done; rag_fusion_applied=False."""
    from unittest.mock import AsyncMock

    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[])
    mock_store.has_vector_index = AsyncMock(return_value=True)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=[])

    rag_config = RAGFusionConfig(enabled=True, num_queries=2)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    result = await pipeline.search(
        "original query",
        "col",
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # Only the original query was searched (no variants)
    assert mock_store.hybrid_search_with_trace.call_count == 1
    assert result.rag_fusion_attempted is True
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_queries_used == 0


@pytest.mark.asyncio
async def test_search_rag_fusion_disabled_config_skips() -> None:
    """When rag_fusion_config.enabled=False, generator NOT called; rag_fusion_attempted=False."""
    from unittest.mock import AsyncMock

    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[])
    mock_store.has_vector_index = AsyncMock(return_value=True)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["v1"])

    rag_config = RAGFusionConfig(enabled=False)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    result = await pipeline.search(
        "query",
        "col",
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    mock_generator.generate_variants.assert_not_called()
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_attempted is False


@pytest.mark.asyncio
async def test_search_rag_fusion_no_generator_skips() -> None:
    """When rag_fusion_generator=None, standard single-query search; rag_fusion_applied=False."""
    from unittest.mock import AsyncMock

    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[])

    rag_config = RAGFusionConfig(enabled=True)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    result = await pipeline.search(
        "query",
        "col",
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=None,
        rag_fusion_config=rag_config,
    )

    assert result.rag_fusion_applied is False


@pytest.mark.asyncio
async def test_search_rag_fusion_fts_only_guard() -> None:
    """When store.has_vector_index returns False, generator NOT called; rag_fusion_applied=False."""
    from unittest.mock import AsyncMock

    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[])
    mock_store.has_vector_index = AsyncMock(return_value=False)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["v1"])

    rag_config = RAGFusionConfig(enabled=True)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    result = await pipeline.search(
        "query",
        "col",
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    mock_generator.generate_variants.assert_not_called()
    assert result.rag_fusion_applied is False


@pytest.mark.asyncio
async def test_search_rag_fusion_fts_only_guard_reports_attempted() -> None:
    """S272 regression: the FTS-only guard must still report rag_fusion_attempted=True.

    RAG Fusion was requested AND config-enabled AND a generator was supplied, so the
    RAG Fusion branch was entered — i.e. it WAS attempted, then aborted early because
    the collection has no vector index. Every other fallback inside that branch passes
    ``rag_fusion_attempted=True`` to ``_search_standard``; this one must too.
    """
    from unittest.mock import AsyncMock

    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[])
    mock_store.has_vector_index = AsyncMock(return_value=False)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["v1"])

    rag_config = RAGFusionConfig(enabled=True)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    result = await pipeline.search(
        "query",
        "col",
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    assert result.rag_fusion_applied is False
    assert result.rag_fusion_attempted is True


@pytest.mark.asyncio
async def test_search_rag_fusion_false_no_overhead() -> None:
    """With rag_fusion=False, generate_variants NOT called; no extra store calls."""
    from unittest.mock import AsyncMock

    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[])

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["v1"])

    rag_config = RAGFusionConfig(enabled=True)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    result = await pipeline.search(
        "query",
        "col",
        embedder=pipeline._global_embedder,
        rag_fusion=False,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    mock_generator.generate_variants.assert_not_called()
    # rag_fusion=False → standard path: hybrid_search_with_trace called once (standard search), not via RAG fusion
    assert mock_store.hybrid_search_with_trace.call_count == 1
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_attempted is False


@pytest.mark.asyncio
async def test_search_with_context_rag_fusion_forwarded() -> None:
    """search_with_context(..., rag_fusion=True, rag_fusion_generator=mock) forwards to search()."""
    from unittest.mock import AsyncMock

    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline, SearchPipelineResult, SearchWithContextResult

    def _cand(chunk_id: str) -> ScoredSearchCandidate:
        return ScoredSearchCandidate(
            doc_id="doc-a",
            chunk_id=chunk_id,
            text="text",
            source_path="/p",
            score_breakdown=SearchScoreBreakdown(
                vector_rank=0, vector_score=0.9, vector_score_kind="distance",
                fts_rank=None, fts_score=None, fts_score_kind=None,
                rrf_score=0.016, reranker_score=None,
            ),
            collection="col",
        )

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[_cand("chunk-001")])
    mock_store.has_vector_index = AsyncMock(return_value=True)
    mock_store.fetch_adjacent_chunks = AsyncMock(return_value=[])

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["variant1", "variant2"])

    rag_config = RAGFusionConfig(enabled=True, num_queries=2)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    result = await pipeline.search_with_context(
        "original query",
        "col",
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    assert isinstance(result, SearchWithContextResult)
    assert result.pipeline_result.rag_fusion_applied is True


@pytest.mark.asyncio
async def test_search_rag_fusion_reranker_uses_original_query() -> None:
    """Reranker is called once with the original query, not any variant text."""
    from unittest.mock import AsyncMock, call

    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    def _cand(chunk_id: str) -> ScoredSearchCandidate:
        return ScoredSearchCandidate(
            doc_id="doc-a",
            chunk_id=chunk_id,
            text="text",
            source_path="/p",
            score_breakdown=SearchScoreBreakdown(
                vector_rank=0, vector_score=0.9, vector_score_kind="distance",
                fts_rank=None, fts_score=None, fts_score_kind=None,
                rrf_score=0.016, reranker_score=None,
            ),
            collection="col",
        )

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[_cand("chunk-001")])
    mock_store.has_vector_index = AsyncMock(return_value=True)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["variant1", "variant2"])

    rag_config = RAGFusionConfig(enabled=True, num_queries=2)

    mock_reranker = AsyncMock()
    mock_reranker.rerank_candidates = AsyncMock(return_value=[_cand("chunk-001")])

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=mock_reranker,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    original_query = "my original question"
    result = await pipeline.search(
        original_query,
        "col",
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    assert mock_reranker.rerank_candidates.call_count == 1
    first_call_args = mock_reranker.rerank_candidates.call_args
    assert first_call_args[0][0] == original_query


@pytest.mark.asyncio
async def test_search_rag_fusion_config_none_skips() -> None:
    """search(..., rag_fusion=True, rag_fusion_config=None) falls back to standard search."""
    from unittest.mock import AsyncMock

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[])

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["v1"])

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    result = await pipeline.search(
        "query",
        "col",
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=None,
    )

    mock_generator.generate_variants.assert_not_called()
    assert result.rag_fusion_applied is False


@pytest.mark.asyncio
async def test_search_rag_fusion_acl_filter_applied_to_fused_results() -> None:
    """ACL filter runs on merged fused set — restricted candidate excluded."""
    from unittest.mock import AsyncMock

    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    def _cand(chunk_id: str, acl: list[str] | None = None) -> ScoredSearchCandidate:
        return ScoredSearchCandidate(
            doc_id="doc-a",
            chunk_id=chunk_id,
            text="text",
            source_path="/p",
            score_breakdown=SearchScoreBreakdown(
                vector_rank=0, vector_score=0.9, vector_score_kind="distance",
                fts_rank=None, fts_score=None, fts_score_kind=None,
                rrf_score=0.016, reranker_score=None,
            ),
            collection="col",
            acl=acl,
        )

    open_cand = _cand("chunk-open", acl=None)
    restricted_cand = _cand("chunk-restricted", acl=["restricted-namespace"])

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    # All variant searches return both candidates
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[open_cand, restricted_cand])
    mock_store.has_vector_index = AsyncMock(return_value=True)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["v1"])

    rag_config = RAGFusionConfig(enabled=True, num_queries=1)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=None,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    # Default namespace — restricted_cand should be filtered out
    result = await pipeline.search(
        "query",
        "col",
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    result_chunk_ids = {r.chunk_id for r in result.results}
    assert "chunk-open" in result_chunk_ids
    assert "chunk-restricted" not in result_chunk_ids


@pytest.mark.asyncio
async def test_search_rag_fusion_partial_search_failure() -> None:
    """When some variant searches fail, fusion uses successful ones; rag_fusion_applied=True."""
    from unittest.mock import AsyncMock

    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    def _cand(chunk_id: str) -> ScoredSearchCandidate:
        return ScoredSearchCandidate(
            doc_id="doc-a",
            chunk_id=chunk_id,
            text="text",
            source_path="/p",
            score_breakdown=SearchScoreBreakdown(
                vector_rank=0, vector_score=0.9, vector_score_kind="distance",
                fts_rank=None, fts_score=None, fts_score_kind=None,
                rrf_score=0.016, reranker_score=None,
            ),
            collection="col",
        )

    call_count = 0

    async def _mock_search_with_trace(collection, query_vector, query_text, candidate_depth, filters=None, scope_filter=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:  # variant 1 fails
            raise RuntimeError("LanceDB error")
        return [_cand(f"chunk-{call_count:03d}")]

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.hybrid_search_with_trace = _mock_search_with_trace
    mock_store.has_vector_index = AsyncMock(return_value=True)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["variant1", "variant2"])

    rag_config = RAGFusionConfig(enabled=True, num_queries=2)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=None,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    result = await pipeline.search(
        "query",
        "col",
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # 2 of 3 searches succeeded → partial fusion happened
    assert result.rag_fusion_applied is True
    # 1 successful variant (not counting original)
    assert result.rag_fusion_queries_used == 1


@pytest.mark.asyncio
async def test_search_rag_fusion_all_searches_fail() -> None:
    """When ALL variant searches raise, fallback to standard single-query; rag_fusion_applied=False."""
    from unittest.mock import AsyncMock

    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    _call_count = 0

    async def _fail_then_succeed(*args, **kwargs):
        nonlocal _call_count
        _call_count += 1
        # RAG fusion calls (original + variants) fail; the _search_standard fallback succeeds
        if _call_count <= 3:  # original + 2 variants
            raise RuntimeError("LanceDB error")
        return []

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.hybrid_search_with_trace = _fail_then_succeed
    mock_store.has_vector_index = AsyncMock(return_value=True)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["v1", "v2"])

    rag_config = RAGFusionConfig(enabled=True, num_queries=2)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    result = await pipeline.search(
        "query",
        "col",
        embedder=pipeline._global_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # All searches failed → fallback to standard single-query
    assert result.rag_fusion_applied is False


@pytest.mark.asyncio
async def test_search_rag_fusion_ignores_caller_query_vector() -> None:
    """With rag_fusion=True, caller-provided query_vector is ignored; pipeline re-embeds."""
    from unittest.mock import AsyncMock

    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    def _cand(chunk_id: str) -> ScoredSearchCandidate:
        return ScoredSearchCandidate(
            doc_id="doc-a",
            chunk_id=chunk_id,
            text="text",
            source_path="/p",
            score_breakdown=SearchScoreBreakdown(
                vector_rank=0, vector_score=0.9, vector_score_kind="distance",
                fts_rank=None, fts_score=None, fts_score_kind=None,
                rrf_score=0.016, reranker_score=None,
            ),
            collection="col",
        )

    embed_call_count = 0
    original_backend = MockEmbedderBackend()

    class TrackingEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            nonlocal embed_call_count
            embed_call_count += len(texts)
            return [[0.1] * 4 for _ in texts]

    tracking_embedder = Embedder(TrackingEmbedderBackend())
    # Warmup
    await tracking_embedder.embed(["warmup"])

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[_cand("chunk-001")])
    mock_store.has_vector_index = AsyncMock(return_value=True)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["v1"])

    rag_config = RAGFusionConfig(enabled=True, num_queries=1)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=tracking_embedder,
        reranker=None,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    embed_call_count = 0  # reset after warmup
    caller_vector = [9.9] * 4  # distinctive caller-provided vector

    result = await pipeline.search(
        "original query",
        "col",
        embedder=tracking_embedder,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
        query_vector=caller_vector,
    )

    # Embedder MUST have been called (pipeline re-embedded rather than using caller vector)
    assert embed_call_count > 0
    assert result.rag_fusion_applied is True


@pytest.mark.asyncio
async def test_search_rag_fusion_dependency_error_reraises() -> None:
    """RAGFusionDependencyError from generate_variants must propagate (not be swallowed)."""
    from unittest.mock import AsyncMock

    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.rag_fusion import RAGFusionDependencyError

    mock_store = MagicMock()
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.has_vector_index = AsyncMock(return_value=True)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(
        side_effect=RAGFusionDependencyError("Install archon-search[rag_fusion]")
    )

    rag_config = RAGFusionConfig(enabled=True)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    with pytest.raises(RAGFusionDependencyError, match="Install archon-search"):
        await pipeline.search(
            "query",
            "col",
            embedder=pipeline._global_embedder,
            rag_fusion=True,
            rag_fusion_generator=mock_generator,
            rag_fusion_config=rag_config,
        )


# ===========================================================================
# C5 Task 2.3 — search_many() RAG Fusion orchestration
# ===========================================================================


def _make_rag_fusion_search_many_pipeline(
    *,
    leg_map: dict | None = None,
    meta_list: list | None = None,
    has_vector_index_map: dict[str, bool] | None = None,
    fanout_leg_trim: int = 40,
    top_k_return: int = 5,
    top_k_retrieve: int = 10,
):
    """Build a SearchPipeline for RAG Fusion search_many tests.

    ``leg_map`` maps collection-name -> list[ScoredSearchCandidate].
    ``has_vector_index_map`` maps collection-name -> bool; defaults True for all.
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    leg_map = leg_map or {}
    has_vector_index_map = has_vector_index_map or {}

    async def _hybrid(collection, vector, query_text, candidate_depth, filters=None, scope_filter=None):
        return list(leg_map.get(collection, []))

    async def _has_vector_index(collection):
        return has_vector_index_map.get(collection, True)

    store.hybrid_search_with_trace = AsyncMock(side_effect=_hybrid)
    store.hybrid_search = AsyncMock(return_value=[])
    store.has_vector_index = AsyncMock(side_effect=_has_vector_index)

    embedder = make_embedder()
    embedder.embed_one = AsyncMock(return_value=[0.1] * 4)  # type: ignore[method-assign]

    reranker = make_reranker()

    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=top_k_retrieve,
        top_k_return=top_k_return,
        fanout_leg_trim=fanout_leg_trim,
    )
    if meta_list is not None:
        pipeline.get_all_collections_meta = AsyncMock(return_value=meta_list)  # type: ignore[method-assign]
    return pipeline, store, embedder, reranker


def _rag_scored(collection: str, doc_id: str, chunk_id: str, rrf_score: float = 0.5):
    """Helper: build a ScoredSearchCandidate for RAG Fusion search_many tests."""
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown

    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text=f"text-{chunk_id}",
        source_path=f"/path/{doc_id}.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=0,
            vector_score=0.9,
            vector_score_kind="distance",
            fts_rank=None,
            fts_score=None,
            fts_score_kind=None,
            rrf_score=rrf_score,
            reranker_score=None,
        ),
        collection=collection,
    )


@pytest.mark.asyncio
async def test_search_many_rag_fusion_generates_once() -> None:
    """With 2 collections and 2 variants, generate_variants called once;
    hybrid_search_with_trace called 2 collections × 3 queries = 6 times;
    final result contains docs from BOTH collections."""
    from archon_search.config import RAGFusionConfig

    leg_map = {
        "A": [_rag_scored("A", "a" * 64, f"{'a' * 64}-000000")],
        "B": [_rag_scored("B", "b" * 64, f"{'b' * 64}-000000")],
    }
    pipeline, store, embedder, reranker = _make_rag_fusion_search_many_pipeline(
        leg_map=leg_map,
        meta_list=[_meta("A"), _meta("B")],
    )

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["variant1", "variant2"])
    rag_config = RAGFusionConfig(enabled=True, num_queries=2)

    result = await pipeline.search_many(
        "original query",
        ["A", "B"],
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # generate_variants called exactly once (not per-collection)
    mock_generator.generate_variants.assert_awaited_once()

    # 2 collections × 3 queries (original + 2 variants) = 6 calls
    assert store.hybrid_search_with_trace.call_count == 6

    # Result carries rag_fusion metadata
    assert result.rag_fusion_applied is True
    assert result.rag_fusion_queries_used == 2
    assert result.rag_fusion_attempted is True

    # Contains docs from both collections
    collections_in_results = {r.collection for r in result.results}
    assert "A" in collections_in_results
    assert "B" in collections_in_results


@pytest.mark.asyncio
async def test_search_many_rag_fusion_false_unchanged() -> None:
    """rag_fusion=False: behavior identical to pre-C5 (generate_variants not called)."""
    from archon_search.config import RAGFusionConfig

    leg_map = {
        "A": [_rag_scored("A", "a" * 64, f"{'a' * 64}-000000")],
    }
    pipeline, store, embedder, reranker = _make_rag_fusion_search_many_pipeline(
        leg_map=leg_map,
        meta_list=[_meta("A")],
    )

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["variant1"])
    rag_config = RAGFusionConfig(enabled=True, num_queries=1)

    result = await pipeline.search_many(
        "query",
        ["A"],
        rag_fusion=False,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # generate_variants must NOT be called
    mock_generator.generate_variants.assert_not_called()
    # Standard path: 1 collection × 1 query = 1 call
    assert store.hybrid_search_with_trace.call_count == 1
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_attempted is False


@pytest.mark.asyncio
async def test_search_many_rag_fusion_mixed_collection_types() -> None:
    """3 collections: 2 with vector index, 1 FTS-only.
    - 2 vector-index collections × 3 queries = 6 calls
    - 1 FTS-only collection × 1 query = 1 call
    - Total = 7 hybrid_search_with_trace calls
    - Result contains docs from all 3 collections."""
    from archon_search.config import RAGFusionConfig

    leg_map = {
        "Vec1": [_rag_scored("Vec1", "v1" * 32, f"{'v1' * 32}-000000")],
        "Vec2": [_rag_scored("Vec2", "v2" * 32, f"{'v2' * 32}-000000")],
        "Fts1": [_rag_scored("Fts1", "f1" * 32, f"{'f1' * 32}-000000")],
    }
    has_vi_map = {"Vec1": True, "Vec2": True, "Fts1": False}
    pipeline, store, embedder, reranker = _make_rag_fusion_search_many_pipeline(
        leg_map=leg_map,
        meta_list=[_meta("Vec1"), _meta("Vec2"), _meta("Fts1")],
        has_vector_index_map=has_vi_map,
    )

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["variant1", "variant2"])
    rag_config = RAGFusionConfig(enabled=True, num_queries=2)

    result = await pipeline.search_many(
        "query",
        ["Vec1", "Vec2", "Fts1"],
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # Vec1 × 3 + Vec2 × 3 + Fts1 × 1 = 7 calls
    assert store.hybrid_search_with_trace.call_count == 7

    # Result contains docs from all collections
    collections_in_results = {r.collection for r in result.results}
    assert "Vec1" in collections_in_results
    assert "Vec2" in collections_in_results
    assert "Fts1" in collections_in_results


@pytest.mark.asyncio
async def test_search_many_rag_fusion_embedding_fallback_sets_attempted() -> None:
    """When embedding fails, rag_fusion_attempted=True must be set on the result."""
    from archon_search.config import RAGFusionConfig

    leg_map = {"A": [_rag_scored("A", "a" * 64, f"{'a' * 64}-000000")]}
    pipeline, store, embedder, reranker = _make_rag_fusion_search_many_pipeline(
        leg_map=leg_map,
        meta_list=[_meta("A")],
    )

    # Fail on the 2nd embed call (first is query, rest are variants) to simulate embedding failure.
    call_count = 0
    original_embed = embedder.embed_one.side_effect

    async def failing_embed(q):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("Embedding service unavailable")
        return [0.1] * 4

    embedder.embed_one = AsyncMock(side_effect=failing_embed)  # type: ignore[method-assign]

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["variant1"])
    rag_config = RAGFusionConfig(enabled=True, num_queries=1)

    result = await pipeline.search_many(
        "query",
        ["A"],
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # Must have attempted but not applied.
    assert result.rag_fusion_attempted is True
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_queries_used == 0


@pytest.mark.asyncio
async def test_search_many_rag_fusion_partial_collection_search_failure() -> None:
    """When some per-variant searches fail in a collection, only successful ones contribute."""
    from archon_search.config import RAGFusionConfig

    # Simulate: variant 1 search fails, variant 2 succeeds, original succeeds.
    call_count = 0

    async def _failing_hybrid(collection, vector, query_text, candidate_depth, filters=None, scope_filter=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:  # 2nd call = variant 1 search
            raise RuntimeError("Store transient error")
        return [_rag_scored(collection, "a" * 64, f"{'a' * 64}-{call_count:06d}")]

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.hybrid_search_with_trace = AsyncMock(side_effect=_failing_hybrid)
    store.hybrid_search = AsyncMock(return_value=[])
    store.has_vector_index = AsyncMock(return_value=True)

    embedder = make_embedder()
    embedder.embed_one = AsyncMock(return_value=[0.1] * 4)  # type: ignore[method-assign]

    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    pipeline.get_all_collections_meta = AsyncMock(return_value=[_meta("A")])  # type: ignore[method-assign]

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["variant1", "variant2"])
    rag_config = RAGFusionConfig(enabled=True, num_queries=2)

    result = await pipeline.search_many(
        "query",
        ["A"],
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # 3 searches attempted (original + 2 variants), 1 failed → rag_fusion_queries_used=1
    # rag_fusion_applied = True (at least 1 successful variant search)
    assert result.rag_fusion_attempted is True
    assert result.rag_fusion_applied is True
    assert result.rag_fusion_queries_used == 1  # only 1 variant succeeded


# ---------------------------------------------------------------------------
# Task 2.4 (C5 RAG Fusion) — ExplainPipelineResult fields + explain() orchestration
# ---------------------------------------------------------------------------


def test_explain_pipeline_result_has_rag_fusion_fields() -> None:
    """ExplainPipelineResult created with required fields has rag_fusion defaults."""
    from archon_search.pipeline import ExplainPipelineResult

    result = ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=False)
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_queries_used == 0
    assert result.rag_fusion_attempted is False
    assert result.rag_fusion_failure_reason is None
    assert result.rag_fusion_sub_query_results is None


@pytest.mark.asyncio
async def test_explain_rag_fusion_sub_query_results_populated() -> None:
    """With rag_fusion=True and 2 successful variants, explain returns 3 RagFusionSubQueryInfo entries."""
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import RagFusionSubQueryInfo, SearchPipeline

    def _cand(chunk_id: str, doc_id: str = "doc-a") -> ScoredSearchCandidate:
        return ScoredSearchCandidate(
            doc_id=doc_id,
            chunk_id=chunk_id,
            text="text",
            source_path="/p",
            score_breakdown=SearchScoreBreakdown(
                vector_rank=0, vector_score=0.9, vector_score_kind="distance",
                fts_rank=None, fts_score=None, fts_score_kind=None,
                rrf_score=0.016, reranker_score=None,
            ),
            collection="col",
        )

    mock_store = MagicMock()
    # Return different candidates for original and each variant search
    call_count = [0]

    async def _trace(coll, vector, query_text, candidate_depth, filters=None, scope_filter=None):
        call_count[0] += 1
        return [_cand(f"chunk-{call_count[0]:03d}", f"doc-{call_count[0]:03d}")]

    mock_store.hybrid_search_with_trace = AsyncMock(side_effect=_trace)
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.has_vector_index = AsyncMock(return_value=True)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["variant1", "variant2"])

    rag_config = RAGFusionConfig(enabled=True, num_queries=2)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    result = await pipeline.explain(
        "original query",
        "col",
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    assert result.rag_fusion_applied is True
    assert result.rag_fusion_queries_used == 2
    assert result.rag_fusion_attempted is True
    assert result.rag_fusion_sub_query_results is not None
    # 3 entries: original (variant_index=0) + 2 variants (variant_index=1, 2)
    assert len(result.rag_fusion_sub_query_results) == 3
    indices = [r.variant_index for r in result.rag_fusion_sub_query_results]
    assert 0 in indices
    assert 1 in indices
    assert 2 in indices
    for entry in result.rag_fusion_sub_query_results:
        assert isinstance(entry, RagFusionSubQueryInfo)
        assert isinstance(entry.result_count, int)
        assert isinstance(entry.top_doc_ids, list)


@pytest.mark.asyncio
async def test_explain_rag_fusion_failure_sets_attempted_and_reason() -> None:
    """When generate_variants raises asyncio.TimeoutError, explain still completes with fallback."""
    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    mock_store = MagicMock()
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[])
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.has_vector_index = AsyncMock(return_value=True)

    # Generator raises TimeoutError internally (the RAGFusionGenerator catches it and returns []
    # in real use, but here we simulate a scenario where the exception propagates from the generator)
    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(side_effect=asyncio.TimeoutError())

    rag_config = RAGFusionConfig(enabled=True, num_queries=2)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    result = await pipeline.explain(
        "original query",
        "col",
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # Generator raised TimeoutError — attempted is True, failure_reason is non-empty, explain completes
    assert result.rag_fusion_attempted is True
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_queries_used == 0
    assert result.rag_fusion_failure_reason is not None
    assert len(result.rag_fusion_failure_reason) > 0


@pytest.mark.asyncio
async def test_explain_rag_fusion_exception_sets_failure_reason() -> None:
    """When generate_variants raises an unexpected exception, failure_reason is set."""
    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    mock_store = MagicMock()
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[])
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.has_vector_index = AsyncMock(return_value=True)

    # Generator raises an exception (unexpected path)
    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(side_effect=RuntimeError("unexpected error"))

    rag_config = RAGFusionConfig(enabled=True, num_queries=2)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    result = await pipeline.explain(
        "original query",
        "col",
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    assert result.rag_fusion_attempted is True
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_failure_reason is not None
    assert len(result.rag_fusion_failure_reason) > 0


@pytest.mark.asyncio
async def test_explain_rag_fusion_false_unchanged() -> None:
    """With rag_fusion=False, explain() behavior is identical to pre-C5."""
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    def _cand(chunk_id: str) -> ScoredSearchCandidate:
        return ScoredSearchCandidate(
            doc_id="doc-a", chunk_id=chunk_id, text="text", source_path="/p",
            score_breakdown=SearchScoreBreakdown(
                vector_rank=0, vector_score=0.9, vector_score_kind="distance",
                fts_rank=None, fts_score=None, fts_score_kind=None,
                rrf_score=0.016, reranker_score=None,
            ),
            collection="col",
        )

    mock_store = MagicMock()
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[_cand("chunk-001")])
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.has_vector_index = AsyncMock(return_value=True)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["variant1"])

    rag_config = RAGFusionConfig(enabled=True, num_queries=1)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    result = await pipeline.explain(
        "original query",
        "col",
        rag_fusion=False,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # Standard path: generator should NOT be called
    mock_generator.generate_variants.assert_not_called()
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_attempted is False
    assert result.rag_fusion_queries_used == 0
    assert result.rag_fusion_sub_query_results is None


@pytest.mark.asyncio
async def test_explain_rag_fusion_partial_search_failure() -> None:
    """With some variant searches failing, only successful ones contribute; partial fusion applied."""
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.chunker import DocumentChunker
    from archon_search.config import RAGFusionConfig
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    def _cand(chunk_id: str) -> ScoredSearchCandidate:
        return ScoredSearchCandidate(
            doc_id="doc-a", chunk_id=chunk_id, text="text", source_path="/p",
            score_breakdown=SearchScoreBreakdown(
                vector_rank=0, vector_score=0.9, vector_score_kind="distance",
                fts_rank=None, fts_score=None, fts_score_kind=None,
                rrf_score=0.016, reranker_score=None,
            ),
            collection="col",
        )

    call_num = [0]

    async def _failing_trace(coll, vector, query_text, candidate_depth, filters=None, scope_filter=None):
        call_num[0] += 1
        if call_num[0] == 2:  # 2nd call (variant 1) fails
            raise RuntimeError("transient error")
        return [_cand(f"chunk-{call_num[0]:03d}")]

    mock_store = MagicMock()
    mock_store.hybrid_search_with_trace = AsyncMock(side_effect=_failing_trace)
    mock_store.hybrid_search = AsyncMock(return_value=[])
    mock_store.has_vector_index = AsyncMock(return_value=True)

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["variant1", "variant2"])

    rag_config = RAGFusionConfig(enabled=True, num_queries=2)

    pipeline = SearchPipeline(
        store=mock_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    result = await pipeline.explain(
        "original query",
        "col",
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # 3 searches: original success, variant1 fail, variant2 success
    # rag_fusion_queries_used = 1 (only variant2 succeeded)
    assert result.rag_fusion_applied is True
    assert result.rag_fusion_attempted is True
    assert result.rag_fusion_queries_used == 1
    # sub_query_results: 2 entries — original (idx=0) and variant2 (idx=2), not idx=1
    assert result.rag_fusion_sub_query_results is not None
    indices = [r.variant_index for r in result.rag_fusion_sub_query_results]
    assert 0 in indices  # original
    assert 1 not in indices  # failed variant omitted
    assert 2 in indices  # successful variant

"""Tests for SearchPipeline.explain (Task 2.3)."""
from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search._types import SearchResult
from archon_search.pipeline import ExplainPipelineResult, SearchPipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_breakdown(
    rrf_score: float = 0.1,
    reranker_score: float | None = None,
) -> SearchScoreBreakdown:
    return SearchScoreBreakdown(
        vector_rank=0,
        vector_score=0.5,
        vector_score_kind="distance",
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=rrf_score,
        reranker_score=reranker_score,
    )


def _make_candidate(
    doc_id: str = "a" * 64,
    chunk_id_suffix: str = "000000",
    rrf_score: float = 0.1,
    reranker_score: float | None = None,
    acl: list[str] | None = None,
) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{chunk_id_suffix}",
        text="sample text",
        source_path="/path/doc.md",
        score_breakdown=_make_breakdown(rrf_score=rrf_score, reranker_score=reranker_score),
        collection="col",
        acl=acl,
    )


def _make_pipeline(
    candidates: list[ScoredSearchCandidate] | None = None,
    reranked: list[ScoredSearchCandidate] | None = None,
    embed_vector: list[float] | None = None,
) -> SearchPipeline:
    """Create a SearchPipeline with mocked store, embedder, and reranker."""
    store = MagicMock()
    store.hybrid_search_with_trace = AsyncMock(return_value=candidates or [])

    embedder = MagicMock()
    embedder.embed_one = AsyncMock(return_value=embed_vector or [1.0, 0.0])
    embedder.model_name = "test-model"

    # Build reranked output: if provided, use it; otherwise return reranked version
    # of candidates with dummy scores.
    def _build_reranked(
        q: str, cands: list, top_k: int
    ) -> list[ScoredSearchCandidate]:
        out = reranked if reranked is not None else cands
        return out[:top_k]

    reranker = MagicMock()
    reranker._rerank_with_trace = AsyncMock(side_effect=_build_reranked)

    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=5,
        top_k_return=5,
    )
    return pipeline


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_returns_top_results_and_near_misses() -> None:
    candidates = [_make_candidate(doc_id=c * 64, rrf_score=1.0 - i * 0.05) for i, c in enumerate("abcdefghij")]
    pipeline = _make_pipeline(candidates=candidates)

    result = await pipeline.explain("query", "col", top_k=3, rerank=False)

    assert isinstance(result, ExplainPipelineResult)
    assert len(result.top_results) == 3
    # near_misses are the next candidates (not all 7 remaining; capped at 20)
    assert len(result.near_misses) == 7


@pytest.mark.asyncio
async def test_explain_rerank_false_uses_rrf_ordering() -> None:
    # Create candidates with descending rrf_score
    candidates = [
        _make_candidate(doc_id="a" * 64, chunk_id_suffix="000000", rrf_score=0.9),
        _make_candidate(doc_id="b" * 64, chunk_id_suffix="000000", rrf_score=0.5),
        _make_candidate(doc_id="c" * 64, chunk_id_suffix="000000", rrf_score=0.1),
    ]
    pipeline = _make_pipeline(candidates=candidates)

    result = await pipeline.explain("query", "col", top_k=2, rerank=False)

    # reranker should NOT be called
    pipeline._reranker._rerank_with_trace.assert_not_called()
    assert result.top_results[0].doc_id == "a" * 64
    assert result.top_results[1].doc_id == "b" * 64
    assert result.near_misses[0].doc_id == "c" * 64


@pytest.mark.asyncio
async def test_explain_uses_amplified_retrieval_pool() -> None:
    """candidate_depth must be max(top_k_retrieve * 3, 20)."""
    pipeline = _make_pipeline(candidates=[])

    await pipeline.explain("query", "col", top_k=5, rerank=False)

    call_args = pipeline.store.hybrid_search_with_trace.call_args
    _, _, _, candidate_depth = call_args.args
    # top_k_retrieve=5 → max(15, 20) = 20
    assert candidate_depth == 20


@pytest.mark.asyncio
async def test_explain_accepts_precomputed_query_vector_and_skips_embedding() -> None:
    pipeline = _make_pipeline(candidates=[])
    precomputed = [0.1, 0.2, 0.3]

    await pipeline.explain("query", "col", top_k=5, rerank=False, query_vector=precomputed)

    pipeline._embedder.embed_one.assert_not_called()
    # Verify the store was called with the precomputed vector
    call_args = pipeline.store.hybrid_search_with_trace.call_args
    _, query_vec, _, _ = call_args.args
    assert query_vec == precomputed


@pytest.mark.asyncio
async def test_explain_near_miss_pool_capped_at_20() -> None:
    # 30 candidates, top_k=5 → near_misses = candidates[5:25] = max 20
    candidates = [
        _make_candidate(doc_id=f"{chr(ord('a') + i % 26)}" * 64, chunk_id_suffix=f"{i:06d}", rrf_score=1.0 - i * 0.01)
        for i in range(30)
    ]
    pipeline = _make_pipeline(candidates=candidates)

    result = await pipeline.explain("query", "col", top_k=5, rerank=False)

    assert len(result.near_misses) == 20


@pytest.mark.asyncio
async def test_explain_small_corpus_returns_what_is_available() -> None:
    candidates = [_make_candidate(doc_id="a" * 64, rrf_score=0.5)]
    pipeline = _make_pipeline(candidates=candidates)

    result = await pipeline.explain("query", "col", top_k=5, rerank=False)

    assert len(result.top_results) == 1
    assert result.near_misses == []


@pytest.mark.asyncio
async def test_explain_acl_filtered_when_all_filtered() -> None:
    # All candidates have acl that blocks the default namespace
    candidates = [
        _make_candidate(doc_id="a" * 64, acl=["private-ns"]),
        _make_candidate(doc_id="b" * 64, acl=["other-ns"]),
    ]
    pipeline = _make_pipeline(candidates=candidates)

    result = await pipeline.explain("query", "col", top_k=5, rerank=False, namespace="default")

    assert result.acl_filtered is True
    assert result.top_results == []


@pytest.mark.asyncio
async def test_explain_identical_scores_tie_break_by_doc_chunk_id() -> None:
    """Candidates with identical rrf_score must be ordered by (doc_id, chunk_id) asc."""
    candidates = [
        _make_candidate(doc_id="c" * 64, chunk_id_suffix="000000", rrf_score=0.5),
        _make_candidate(doc_id="a" * 64, chunk_id_suffix="000000", rrf_score=0.5),
        _make_candidate(doc_id="b" * 64, chunk_id_suffix="000000", rrf_score=0.5),
    ]
    pipeline = _make_pipeline(candidates=candidates)

    result = await pipeline.explain("query", "col", top_k=3, rerank=False)

    doc_ids = [c.doc_id for c in result.top_results]
    assert doc_ids == ["a" * 64, "b" * 64, "c" * 64]


@pytest.mark.asyncio
async def test_explain_top_k_matches_search_when_rerank_true_and_top_k_equals_top_k_return() -> None:
    """AC3: When top_k == _top_k_return and rerank=True, explain top_results ordering
    must match search results ordering (same (doc_id, chunk_id) sequence)."""
    ids = ["a" * 64, "b" * 64, "c" * 64]
    reranker_scores = [0.9, 0.7, 0.5]

    # ScoredSearchCandidate list for explain path
    trace_candidates = [
        _make_candidate(doc_id=doc_id, chunk_id_suffix="000000", rrf_score=0.1, reranker_score=score)
        for doc_id, score in zip(ids, reranker_scores)
    ]

    # SearchResult list for search path — same order
    search_results = [
        SearchResult(
            doc_id=doc_id,
            chunk_id=f"{doc_id}-000000",
            text="text",
            score=score,
            source_path="/path/doc.md",
        )
        for doc_id, score in zip(ids, reranker_scores)
    ]

    store = MagicMock()
    store.hybrid_search_with_trace = AsyncMock(return_value=trace_candidates)
    store.hybrid_search = AsyncMock(return_value=search_results)

    embedder = MagicMock()
    embedder.embed_one = AsyncMock(return_value=[1.0, 0.0])
    embedder.model_name = "test-model"

    reranker = MagicMock()
    # _rerank_with_trace returns full list with reranker scores already set (explain path)
    reranker._rerank_with_trace = AsyncMock(return_value=trace_candidates)
    # rerank returns SearchResults in same order (search path)
    reranker.rerank = AsyncMock(return_value=search_results[:3])

    top_k_return = 3
    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=5,
        top_k_return=top_k_return,
    )

    explain_result = await pipeline.explain("query", "col", top_k=top_k_return, rerank=True)
    search_result = await pipeline.search("query", "col")

    explain_pairs = [(c.doc_id, c.chunk_id) for c in explain_result.top_results]
    search_pairs = [(r.doc_id, r.chunk_id) for r in search_result.results]

    assert explain_pairs == search_pairs

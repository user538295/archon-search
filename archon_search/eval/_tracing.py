"""Eval-only trace collector for the search pipeline (FEAT-039 Task 2.4).

This module provides ``collect_search_trace``, which runs the production search
path with trace helpers attached so that pre-rerank and post-rerank candidates
are both captured in a single query execution.

Design constraints (from spec):
- MUST reuse the pipeline's own embedder, store, and reranker instances.
- MUST NOT reimplement ranking logic.
- MUST NOT change the behaviour of the normal ``search()`` method.
- A drift guard verifies object identity of all three components.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from archon_search._diagnostics import ScoredSearchCandidate
from archon_search.eval.types import EvalSearchResult
from archon_search.store import _hybrid_search_with_trace

if TYPE_CHECKING:
    from archon_search.embedder import Embedder
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore


def _check_component_drift(
    pipeline: SearchPipeline,
    expected_embedder: Embedder,
    expected_store: SearchStore,
    expected_reranker: Reranker,
) -> None:
    """Raise RuntimeError if any pipeline component differs from the expected instance.

    Uses object identity (``is``) — not equality — to detect drift.
    """
    if pipeline._embedder is not expected_embedder:
        raise RuntimeError(
            "eval trace drift: pipeline._embedder is not the expected embedder instance"
        )
    if pipeline.store is not expected_store:
        raise RuntimeError(
            "eval trace drift: pipeline.store is not the expected store instance"
        )
    if pipeline._reranker is not expected_reranker:
        raise RuntimeError(
            "eval trace drift: pipeline._reranker is not the expected reranker instance"
        )


def _candidate_to_eval_result(candidate: ScoredSearchCandidate) -> EvalSearchResult:
    """Convert a ScoredSearchCandidate to an EvalSearchResult."""
    return EvalSearchResult(
        doc_id=candidate.doc_id,
        runtime_doc_id=candidate.doc_id,
        chunk_id=candidate.chunk_id,
        text=candidate.text,
        source_path=candidate.source_path,
        collection=candidate.collection,
        score_breakdown=candidate.score_breakdown,
    )


async def collect_search_trace(
    pipeline: SearchPipeline,
    query: str,
    collection: str,
    candidate_depth: int,
    return_depth: int,
    metric_depth: int,  # noqa: ARG001 — reserved for caller-side scoring; not used here
) -> tuple[list[EvalSearchResult], list[EvalSearchResult]]:
    """Run the production search path with trace sinks, capturing pre- and post-rerank results.

    Uses the pipeline's own embedder, store, and reranker instances — no separate
    ranking logic.  Drift guard verifies object identity before execution.

    Args:
        pipeline: A configured :class:`SearchPipeline`.
        query: Query string.
        collection: Target collection name.
        candidate_depth: Maximum raw candidates to retrieve (analogous to ``top_k_retrieve``).
        return_depth: Maximum results after reranking (analogous to ``top_k_return``).
        metric_depth: Passed through for caller-side metric computation; unused here.

    Returns:
        ``(pre_rerank, post_rerank)`` — both are ``list[EvalSearchResult]`` ordered by
        their respective ranking scores (RRF for pre-rerank, reranker score for post-rerank).
    """
    # Snapshot component references before execution
    embedder = pipeline._embedder
    store = pipeline.store
    reranker = pipeline._reranker

    # Drift guard: verify no component was swapped since snapshot
    _check_component_drift(pipeline, embedder, store, reranker)

    # Embed query using the pipeline's own embedder
    query_vector = await embedder.embed_one(query)

    # Retrieve pre-rerank candidates via the production trace helper
    pre_candidates = await _hybrid_search_with_trace(
        store, collection, query_vector, query, candidate_depth
    )

    # Rerank using the pipeline's own reranker trace method
    post_candidates = await reranker._rerank_with_trace(query, pre_candidates, return_depth)

    pre_results = [_candidate_to_eval_result(c) for c in pre_candidates]
    post_results = [_candidate_to_eval_result(c) for c in post_candidates]

    return pre_results, post_results

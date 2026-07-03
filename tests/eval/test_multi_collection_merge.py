"""Eval fixture: multi-collection MERGE CORRECTNESS for SearchPipeline.search_many.

B3 Task 8.1. This is the sanctioned merge-correctness eval fixture. It is
SELF-CONTAINED: it builds its own deterministic candidates/collections inline
via a MagicMock store and the deterministic eval reranker backend. It does NOT
touch the metric-computing corpus (documents.jsonl / queries.jsonl /
labels.jsonl / corpus/), so the single-collection baseline and its eval_hash
are unaffected.

Scope: merge correctness ONLY (provenance tags, no cross-collection dedup,
deterministic global rerank ordering over the merged pool, ascending leg
concatenation). Routing SELECTION is out of scope (that is B4).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.eval.backends import EvalRerankerBackend


# ---------------------------------------------------------------------------
# Deterministic fixture builders (inline, corpus-free)
# ---------------------------------------------------------------------------


def _scored(collection: str, doc_id: str, chunk_id: str, text: str, rrf_score: float):
    """Build a ScoredSearchCandidate with explicit text + rrf_score + provenance."""
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown

    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text=text,
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


def _meta(name: str, *, embedding_model: str = "eval-sha256-v1", namespace: str = "default"):
    from archon_search.collection_meta import CollectionMeta

    return CollectionMeta(name=name, active_embedding_model=embedding_model, namespace=namespace)


def _build_pipeline(leg_map: dict, meta_list: list, *, top_k_return: int = 10):
    """SearchPipeline wired to a MagicMock store + deterministic eval reranker.

    The embedder advertises model_name 'eval-sha256-v1' (matching _meta default)
    so all collections stay in scope. The reranker scores candidates by a fixed
    BM25-style function of (query, candidate.text), so ordering is reproducible.
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    class _EvalEmbedderBackend:
        model_name = "eval-sha256-v1"
        is_warm = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    store = MagicMock()

    async def _hybrid(collection, vector, query_text, candidate_depth, filters=None, scope_filter=None):
        return list(leg_map.get(collection, []))

    store.hybrid_search_with_trace = AsyncMock(side_effect=_hybrid)

    embedder = Embedder(_EvalEmbedderBackend())
    embedder.embed_one = AsyncMock(return_value=[0.1] * 4)  # type: ignore[method-assign]

    reranker = Reranker(EvalRerankerBackend())

    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=top_k_return,
        fanout_leg_trim=40,
        fanout_timeout_seconds=30.0,
    )
    pipeline.get_all_collections_meta = AsyncMock(return_value=meta_list)  # type: ignore[method-assign]
    return pipeline, store, reranker


def _spy_rerank(reranker):
    """Wrap rerank_candidates so the merged pool passed to it can be inspected."""
    spy = AsyncMock(side_effect=reranker.rerank_candidates)
    reranker.rerank_candidates = spy  # type: ignore[method-assign]
    return spy


# ---------------------------------------------------------------------------
# 1. Provenance preserved per collection
# ---------------------------------------------------------------------------


@pytest.mark.eval
@pytest.mark.asyncio
async def test_merge_preserves_per_collection_provenance() -> None:
    """Two collections each return 2 candidates; every returned result keeps the
    collection tag of its origin leg."""
    leg_map = {
        "alpha": [
            _scored("alpha", "a1" + "0" * 62, ("a1" + "0" * 62) + "-000000", "alpha apple banana", 0.9),
            _scored("alpha", "a2" + "0" * 62, ("a2" + "0" * 62) + "-000000", "alpha cherry", 0.8),
        ],
        "beta": [
            _scored("beta", "b1" + "0" * 62, ("b1" + "0" * 62) + "-000000", "beta date apple", 0.7),
            _scored("beta", "b2" + "0" * 62, ("b2" + "0" * 62) + "-000000", "beta elderberry", 0.6),
        ],
    }
    pipeline, *_ = _build_pipeline(leg_map, [_meta("alpha"), _meta("beta")])

    result = await pipeline.search_many("apple", ["alpha", "beta"])

    by_doc = {r.doc_id: r.collection for r in result.results}
    assert by_doc["a1" + "0" * 62] == "alpha"
    assert by_doc["a2" + "0" * 62] == "alpha"
    assert by_doc["b1" + "0" * 62] == "beta"
    assert by_doc["b2" + "0" * 62] == "beta"
    # Every result carries a non-empty provenance tag from one of the two legs.
    assert {r.collection for r in result.results} == {"alpha", "beta"}


# ---------------------------------------------------------------------------
# 2. No cross-collection dedup
# ---------------------------------------------------------------------------


@pytest.mark.eval
@pytest.mark.asyncio
async def test_merge_no_cross_collection_dedup() -> None:
    """The SAME chunk_id appearing in both collections survives twice in the
    merged pool (search_many does not dedup across collections)."""
    shared_chunk = "c" * 64 + "-000000"
    shared_doc = "c" * 64
    leg_map = {
        "alpha": [_scored("alpha", shared_doc, shared_chunk, "alpha shared text", 0.9)],
        "beta": [_scored("beta", shared_doc, shared_chunk, "beta shared text", 0.8)],
    }
    pipeline, _store, reranker = _build_pipeline(leg_map, [_meta("alpha"), _meta("beta")])
    spy = _spy_rerank(reranker)

    result = await pipeline.search_many("shared", ["alpha", "beta"])

    # rerank runs exactly once over the merged pool.
    assert spy.await_count == 1
    merged_pool = spy.await_args.args[1]
    # Both legs contribute their single (trimmed) candidate => pool size == 2.
    assert len(merged_pool) == 2
    # The duplicate chunk_id is present from both collections (no cross-leg dedup).
    assert [c.chunk_id for c in merged_pool] == [shared_chunk, shared_chunk]
    assert sorted(c.collection for c in merged_pool) == ["alpha", "beta"]
    # ...and BOTH survive into the final output (the reranker does not dedup either).
    assert [r.chunk_id for r in result.results] == [shared_chunk, shared_chunk]
    assert sorted(r.collection for r in result.results) == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# 3. Deterministic global rerank ordering over the merged pool
# ---------------------------------------------------------------------------


@pytest.mark.eval
@pytest.mark.asyncio
async def test_merge_global_rerank_ordering_is_deterministic() -> None:
    """With a deterministic reranker, the final results are ordered by descending
    reranker score across the merged cross-collection pool, and stable across runs.

    Query 'apple' (BM25-style EvalRerankerBackend) scores by 'apple' term
    frequency in candidate text:
      - alpha/a1: 'apple apple apple' -> tf=3  (highest)
      - beta/b1:  'apple apple'       -> tf=2
      - alpha/a2: 'apple'             -> tf=1
      - beta/b2:  'banana'            -> tf=0  (lowest)
    Expected descending order: a1, b1, a2, b2.
    """
    leg_map = {
        "alpha": [
            _scored("alpha", "a1" + "0" * 62, ("a1" + "0" * 62) + "-000000", "apple apple apple", 0.5),
            _scored("alpha", "a2" + "0" * 62, ("a2" + "0" * 62) + "-000000", "apple", 0.5),
        ],
        "beta": [
            _scored("beta", "b1" + "0" * 62, ("b1" + "0" * 62) + "-000000", "apple apple", 0.5),
            _scored("beta", "b2" + "0" * 62, ("b2" + "0" * 62) + "-000000", "banana", 0.5),
        ],
    }

    async def _run():
        pipeline, *_ = _build_pipeline(leg_map, [_meta("alpha"), _meta("beta")])
        result = await pipeline.search_many("apple", ["alpha", "beta"])
        return [(r.doc_id, r.score) for r in result.results]

    run1 = await _run()
    run2 = await _run()

    expected_doc_order = ["a1" + "0" * 62, "b1" + "0" * 62, "a2" + "0" * 62, "b2" + "0" * 62]
    assert [doc for doc, _ in run1] == expected_doc_order
    # Strictly descending reranker scores across the merged (cross-collection) pool.
    scores = [s for _, s in run1]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[1] > scores[2] > scores[3]
    # Stable across two independent runs.
    assert run1 == run2


# ---------------------------------------------------------------------------
# 4. Legs concatenated in ascending collection-name order before rerank
# ---------------------------------------------------------------------------


@pytest.mark.eval
@pytest.mark.asyncio
async def test_merge_leg_order_is_collection_name_ascending() -> None:
    """Collections requested in non-alphabetical order (['beta','alpha']) are
    concatenated into the merged pool in ASCENDING collection-name order
    (all alpha candidates before all beta candidates) before the global rerank."""
    leg_map = {
        "alpha": [
            _scored("alpha", "a1" + "0" * 62, ("a1" + "0" * 62) + "-000000", "alpha one", 0.9),
            _scored("alpha", "a2" + "0" * 62, ("a2" + "0" * 62) + "-000000", "alpha two", 0.8),
        ],
        "beta": [
            _scored("beta", "b1" + "0" * 62, ("b1" + "0" * 62) + "-000000", "beta one", 0.7),
            _scored("beta", "b2" + "0" * 62, ("b2" + "0" * 62) + "-000000", "beta two", 0.6),
        ],
    }
    pipeline, _store, reranker = _build_pipeline(leg_map, [_meta("alpha"), _meta("beta")])
    spy = _spy_rerank(reranker)

    # Request in reverse (non-alphabetical) order.
    await pipeline.search_many("query", ["beta", "alpha"])

    merged_pool = spy.await_args.args[1]
    # Concatenation must be alphabetical by collection name, not request order:
    # all alpha leg candidates, then all beta leg candidates.
    assert [c.collection for c in merged_pool] == ["alpha", "alpha", "beta", "beta"]

"""Tests for ranking metric functions — FEAT-039 Task 3.1.

Tests cover:
- compute_recall_at_k
- compute_mrr
- compute_ndcg_at_k
- deduplicate_to_doc_rankings
"""
from __future__ import annotations

import math

import pytest

from archon_search._diagnostics import SearchScoreBreakdown
from archon_search.eval.fixtures import RelevanceLabel
from archon_search.eval.metrics import (
    compute_mrr,
    compute_ndcg_at_k,
    compute_recall_at_k,
    deduplicate_to_doc_rankings,
)
from archon_search.eval.types import EvalSearchResult, QueryEvalTrace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_score_breakdown(score: float = 1.0) -> SearchScoreBreakdown:
    return SearchScoreBreakdown(
        vector_rank=1,
        vector_score=score,
        vector_score_kind="similarity",
        fts_rank=1,
        fts_score=score,
        fts_score_kind="bm25",
        rrf_score=score,
        reranker_score=score,
    )


def _make_result(doc_id: str, chunk_id: str = "c0", score: float = 1.0) -> EvalSearchResult:
    return EvalSearchResult(
        doc_id=doc_id,
        runtime_doc_id=doc_id,
        chunk_id=chunk_id,
        text=f"text for {doc_id}",
        source_path=f"{doc_id}.txt",
        collection="test",
        score_breakdown=_make_score_breakdown(score),
    )


def _make_trace(
    query_id: str,
    results: list[EvalSearchResult],
    pre_rerank: list[EvalSearchResult] | None = None,
) -> QueryEvalTrace:
    trace = QueryEvalTrace(
        query_id=query_id,
        query_text=f"query {query_id}",
        collection="test",
        metric_scope="retrieval",
        results=results,
    )
    if pre_rerank is not None:
        trace.pre_rerank_results = pre_rerank
    return trace


def _labels(*pairs: tuple[str, str, int]) -> list[RelevanceLabel]:
    """Build a list of RelevanceLabel from (query_id, doc_id, grade) triples."""
    return [RelevanceLabel(query_id=qid, doc_id=did, grade=g) for qid, did, g in pairs]


# ---------------------------------------------------------------------------
# 1. test_compute_recall_at_k
# ---------------------------------------------------------------------------

def test_compute_recall_at_k() -> None:
    """Miniature trace set returns expected recall values."""
    # Query q1: relevant = {docA}. Results: [docA, docB, docC]. k=1 → hit.
    # Query q2: relevant = {docB}. Results: [docC, docA, docB]. k=1 → miss, k=3 → hit.
    traces = [
        _make_trace("q1", [_make_result("docA"), _make_result("docB"), _make_result("docC")]),
        _make_trace("q2", [_make_result("docC"), _make_result("docA"), _make_result("docB")]),
    ]
    labels = _labels(("q1", "docA", 1), ("q2", "docB", 1))

    # k=1: q1 recall=1.0, q2 recall=0.0 → macro avg = 0.5
    assert compute_recall_at_k(traces, labels, k=1) == pytest.approx(0.5)
    # k=3: q1 recall=1.0, q2 recall=1.0 → macro avg = 1.0
    assert compute_recall_at_k(traces, labels, k=3) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 2. test_compute_recall_at_k_multi_relevant_fractional_macro
# ---------------------------------------------------------------------------

def test_compute_recall_at_k_multi_relevant_fractional_macro() -> None:
    """Multi-label queries use fractional per-query recall and macro aggregation."""
    # Query q1: relevant = {docA, docB, docC}. Results: [docA, docB, docX]. k=3 → 2/3 found.
    # Query q2: relevant = {docD}. Results: [docD]. k=3 → 1/1 = 1.0.
    # Macro avg = (2/3 + 1.0) / 2 = 5/6 ≈ 0.8333
    traces = [
        _make_trace("q1", [_make_result("docA"), _make_result("docB"), _make_result("docX")]),
        _make_trace("q2", [_make_result("docD")]),
    ]
    labels = _labels(
        ("q1", "docA", 1),
        ("q1", "docB", 1),
        ("q1", "docC", 1),
        ("q2", "docD", 1),
    )

    result = compute_recall_at_k(traces, labels, k=3)
    expected = (2 / 3 + 1.0) / 2
    assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 3. test_compute_mrr
# ---------------------------------------------------------------------------

def test_compute_mrr() -> None:
    """Reciprocal rank math matches a hand-worked example."""
    # Query q1: relevant = {docB}. Results: [docA, docB, docC]. docB at rank 2 → rr = 0.5.
    # Query q2: relevant = {docA}. Results: [docA, docB]. docA at rank 1 → rr = 1.0.
    # MRR = (0.5 + 1.0) / 2 = 0.75
    traces = [
        _make_trace("q1", [_make_result("docA"), _make_result("docB"), _make_result("docC")]),
        _make_trace("q2", [_make_result("docA"), _make_result("docB")]),
    ]
    labels = _labels(("q1", "docB", 1), ("q2", "docA", 1))

    assert compute_mrr(traces, labels) == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# 4. test_recall_and_mrr_ignore_zero_grade_labels
# ---------------------------------------------------------------------------

def test_recall_and_mrr_ignore_zero_grade_labels() -> None:
    """Explicit grade=0 labels are non-relevant for recall/MRR."""
    # Query q1: docA has grade=0 (non-relevant), docB has grade=1.
    # Results: [docA, docB]. For recall@1: only docB is relevant → miss → 0.
    # For MRR: docB at rank 2 → rr = 0.5.
    traces = [
        _make_trace("q1", [_make_result("docA"), _make_result("docB")]),
    ]
    labels = _labels(("q1", "docA", 0), ("q1", "docB", 1))

    assert compute_recall_at_k(traces, labels, k=1) == pytest.approx(0.0)
    assert compute_recall_at_k(traces, labels, k=2) == pytest.approx(1.0)
    assert compute_mrr(traces, labels) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 5. test_compute_ndcg_at_k_binary_labels
# ---------------------------------------------------------------------------

def test_compute_ndcg_at_k_binary_labels() -> None:
    """Binary labels produce the expected nDCG."""
    # Query q1: relevant = {docA, docB}. Results: [docA, docX, docB].
    # At k=3, ranked: docA(rel=1), docX(rel=0), docB(rel=1)
    # DCG = (2^1-1)/log2(2) + (2^0-1)/log2(3) + (2^1-1)/log2(4)
    #      = 1/1 + 0/1.585 + 1/2 = 1.0 + 0 + 0.5 = 1.5
    # IDCG = ideal: docA(1), docB(1), ... → (2^1-1)/log2(2) + (2^1-1)/log2(3)
    #       = 1.0 + 1/log2(3) ≈ 1.0 + 0.6309 = 1.6309
    # nDCG = 1.5 / 1.6309 ≈ 0.9197
    traces = [
        _make_trace("q1", [_make_result("docA"), _make_result("docX"), _make_result("docB")]),
    ]
    labels = _labels(("q1", "docA", 1), ("q1", "docB", 1))

    dcg = 1.0 + 0.0 + 1.0 / math.log2(4)
    idcg = 1.0 + 1.0 / math.log2(3)
    expected = dcg / idcg

    result = compute_ndcg_at_k(traces, labels, k=3)
    assert result == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# 6. test_compute_ndcg_at_k_graded_labels
# ---------------------------------------------------------------------------

def test_compute_ndcg_at_k_graded_labels() -> None:
    """Graded labels produce the expected nDCG."""
    # Query q1: docA grade=2, docB grade=1. Results: [docB, docA].
    # At k=2:
    # DCG = (2^1-1)/log2(2) + (2^2-1)/log2(3) = 1/1 + 3/log2(3) ≈ 1.0 + 1.8928 = 2.8928
    # IDCG (ideal order: docA grade=2 first, then docB grade=1):
    #       = (2^2-1)/log2(2) + (2^1-1)/log2(3) = 3/1 + 1/log2(3) ≈ 3.0 + 0.6309 = 3.6309
    # nDCG = 2.8928 / 3.6309 ≈ 0.7967
    traces = [
        _make_trace("q1", [_make_result("docB"), _make_result("docA")]),
    ]
    labels = _labels(("q1", "docA", 2), ("q1", "docB", 1))

    dcg = 1.0 + 3.0 / math.log2(3)
    idcg = 3.0 + 1.0 / math.log2(3)
    expected = dcg / idcg

    result = compute_ndcg_at_k(traces, labels, k=2)
    assert result == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# 7. test_compute_ndcg_at_k_uses_documented_gain_discount_and_top_k_idcg
# ---------------------------------------------------------------------------

def test_compute_ndcg_at_k_uses_documented_gain_discount_and_top_k_idcg() -> None:
    """nDCG implementation matches the documented formula and truncates IDCG to k.

    Formula:
      gain(i) = (2^rel_i - 1) / log2(i + 2)   (0-indexed i)
      IDCG is computed on at most k items.
    """
    # 3 relevant docs with grades 3, 2, 1. Results: [docC, docB, docA].
    # k=2 → only top-2 count for DCG.
    # DCG@2: i=0: (2^1-1)/log2(2)=1; i=1: (2^2-1)/log2(3)=3/log2(3)
    # IDCG@2 (ideal: grade 3 first, grade 2 second, capped at k=2):
    #   i=0: (2^3-1)/log2(2)=7; i=1: (2^2-1)/log2(3)=3/log2(3)
    traces = [
        _make_trace("q1", [
            _make_result("docC"),  # grade=1
            _make_result("docB"),  # grade=2
            _make_result("docA"),  # grade=3 — beyond k=2, shouldn't count
        ]),
    ]
    labels = _labels(("q1", "docA", 3), ("q1", "docB", 2), ("q1", "docC", 1))

    dcg = 1.0 / math.log2(2) + 3.0 / math.log2(3)  # only first 2
    idcg = 7.0 / math.log2(2) + 3.0 / math.log2(3)  # top 2 ideal grades: 3, 2
    expected = dcg / idcg

    result = compute_ndcg_at_k(traces, labels, k=2)
    assert result == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# 8. test_metric_aggregation_macro_averages_queries
# ---------------------------------------------------------------------------

def test_metric_aggregation_macro_averages_queries() -> None:
    """Query-level values are averaged equally rather than micro-averaged by label count."""
    # q1 has 3 relevant docs but only 1 returned → recall = 1/3
    # q2 has 1 relevant doc, it's returned → recall = 1/1 = 1.0
    # Macro avg = (1/3 + 1.0) / 2 = 2/3 ≈ 0.6667
    # Micro avg would weight q1 heavily: (1 + 1) / (3 + 1) = 0.5 — NOT what we compute.
    traces = [
        _make_trace("q1", [_make_result("docA")]),
        _make_trace("q2", [_make_result("docD")]),
    ]
    labels = _labels(
        ("q1", "docA", 1),
        ("q1", "docB", 1),
        ("q1", "docC", 1),
        ("q2", "docD", 1),
    )

    macro_expected = (1 / 3 + 1.0) / 2
    assert compute_recall_at_k(traces, labels, k=5) == pytest.approx(macro_expected)


# ---------------------------------------------------------------------------
# 9. test_metrics_dedupe_chunks_to_document_rankings
# ---------------------------------------------------------------------------

def test_metrics_dedupe_chunks_to_document_rankings() -> None:
    """Duplicate chunks from one document do not inflate metrics."""
    # docA has 2 chunks in the results. After dedup, docA appears at rank 1.
    # docB appears at rank 2 (original rank 3, but second unique doc).
    # Relevant: {docB}. k=2: docB at doc-rank 2 → recall=1.0, rr=0.5.
    results = [
        _make_result("docA", chunk_id="c0"),
        _make_result("docA", chunk_id="c1"),  # duplicate doc
        _make_result("docB", chunk_id="c0"),
    ]
    traces = [_make_trace("q1", results)]
    labels = _labels(("q1", "docB", 1))

    # After dedup: [docA, docB]. k=2 → docB is found → recall=1.0.
    assert compute_recall_at_k(traces, labels, k=2) == pytest.approx(1.0)
    # docB at doc-rank 2 → rr = 0.5.
    assert compute_mrr(traces, labels) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 10. test_metrics_reject_under_depth_after_chunk_dedupe
# ---------------------------------------------------------------------------

def test_metrics_reject_under_depth_after_chunk_dedupe() -> None:
    """Insufficient unique-document depth fails clearly when the corpus has enough documents."""
    # 3 unique docs exist, but after dedup we only have 1 unique doc in results.
    # k=3 but only 1 unique doc → ValueError.
    # (Corpus has 3 docs but results only show 1 unique doc, and we request k=3.)
    results = [
        _make_result("docA", chunk_id="c0"),
        _make_result("docA", chunk_id="c1"),
        _make_result("docA", chunk_id="c2"),
    ]
    traces = [_make_trace("q1", results)]
    labels = _labels(("q1", "docA", 1))

    # Corpus size (total distinct doc_ids in results across all traces) = 1.
    # k=3 but unique docs in results = 1 < k=3. But corpus has ≥ k docs...
    # The spec says: raise ValueError when corpus has >= k docs but deduped results < k.
    # We simulate a corpus_size argument to convey total corpus size.
    with pytest.raises(ValueError, match="unique-document depth"):
        compute_recall_at_k(traces, labels, k=3, corpus_size=5)

# ---------------------------------------------------------------------------
# Fix 4 — C1-T-4: nDCG corpus-depth check
# ---------------------------------------------------------------------------

def test_ndcg_reject_under_depth_after_chunk_dedupe() -> None:
    """compute_ndcg_at_k raises ValueError when corpus >= k but unique docs < k."""
    results = [
        _make_result("docA", chunk_id="c0"),
        _make_result("docA", chunk_id="c1"),
    ]
    traces = [_make_trace("q1", results)]
    labels = _labels(("q1", "docA", 1))

    with pytest.raises(ValueError, match="unique-document depth"):
        compute_ndcg_at_k(traces, labels, k=3, corpus_size=5)




# ---------------------------------------------------------------------------
# 11. test_compute_ndcg_at_k_fewer_relevant_than_k
# ---------------------------------------------------------------------------

def test_compute_ndcg_at_k_fewer_relevant_than_k() -> None:
    """Perfect top-n_rel ranking achieves nDCG=1.0 when n_rel < k."""
    # Only 2 relevant docs but k=5. If they're both in top-2, nDCG should be 1.0.
    # IDCG is computed on at most min(n_rel, k) = 2 items.
    traces = [
        _make_trace("q1", [
            _make_result("docA"),  # grade=1, rank 1
            _make_result("docB"),  # grade=1, rank 2
            _make_result("docX"),  # not relevant
            _make_result("docY"),  # not relevant
        ]),
    ]
    labels = _labels(("q1", "docA", 1), ("q1", "docB", 1))

    result = compute_ndcg_at_k(traces, labels, k=5)
    assert result == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# 12. test_compute_ndcg_at_k_empty_result_list
# ---------------------------------------------------------------------------

def test_compute_ndcg_at_k_empty_result_list() -> None:
    """A query that returns zero documents after deduplication gets nDCG = 0.0."""
    traces = [
        _make_trace("q1", []),  # no results
    ]
    labels = _labels(("q1", "docA", 1))

    result = compute_ndcg_at_k(traces, labels, k=5)
    assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 13. test_compute_mrr_when_no_relevant_document_in_results
# ---------------------------------------------------------------------------

def test_compute_mrr_when_no_relevant_document_in_results() -> None:
    """When no relevant document appears in any ranked list across all queries, MRR = 0.0."""
    traces = [
        _make_trace("q1", [_make_result("docX"), _make_result("docY")]),
        _make_trace("q2", [_make_result("docZ")]),
    ]
    labels = _labels(("q1", "docA", 1), ("q2", "docB", 1))

    result = compute_mrr(traces, labels)
    assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Additional: deduplicate_to_doc_rankings helper
# ---------------------------------------------------------------------------

def test_deduplicate_to_doc_rankings_keeps_first_chunk_position() -> None:
    """Multiple chunks from the same doc: only first-ranked chunk sets position."""
    results = [
        _make_result("docA", "c0"),
        _make_result("docB", "c0"),
        _make_result("docA", "c1"),  # duplicate, should be ignored
        _make_result("docC", "c0"),
    ]
    deduped = deduplicate_to_doc_rankings(results)
    assert deduped == ["docA", "docB", "docC"]


def test_deduplicate_to_doc_rankings_empty_list() -> None:
    """Empty input returns empty list."""
    assert deduplicate_to_doc_rankings([]) == []

# ---------------------------------------------------------------------------
# Fix 1 — C1-I-3: k <= 0 raises ValueError
# ---------------------------------------------------------------------------

def test_recall_at_k_raises_for_non_positive_k() -> None:
    """compute_recall_at_k raises ValueError for k=0 and k=-1."""
    traces = [_make_trace("q1", [_make_result("docA")])]
    labels = _labels(("q1", "docA", 1))
    with pytest.raises(ValueError, match="k must be positive"):
        compute_recall_at_k(traces, labels, k=0)
    with pytest.raises(ValueError, match="k must be positive"):
        compute_recall_at_k(traces, labels, k=-1)


def test_ndcg_at_k_raises_for_non_positive_k() -> None:
    """compute_ndcg_at_k raises ValueError for k=0 and k=-1."""
    traces = [_make_trace("q1", [_make_result("docA")])]
    labels = _labels(("q1", "docA", 1))
    with pytest.raises(ValueError, match="k must be positive"):
        compute_ndcg_at_k(traces, labels, k=0)
    with pytest.raises(ValueError, match="k must be positive"):
        compute_ndcg_at_k(traces, labels, k=-1)




# ---------------------------------------------------------------------------
# Note — MRR has no corpus_size parameter.
# MRR scores the rank of the first relevant document; depth coverage (top-k
# completeness relative to corpus size) is only meaningful for Recall@k and
# nDCG@k.  Under-depth validation therefore does not apply to MRR.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# C1-T-1: use_pre_rerank=True returns different value than post-rerank
# ---------------------------------------------------------------------------

def test_use_pre_rerank_recall_differs_from_post_rerank() -> None:
    """use_pre_rerank=True uses pre_rerank_results, producing a different metric value."""
    # Post-rerank: relevant doc (docA) is at rank 1 → recall@1 = 1.0
    # Pre-rerank: relevant doc (docA) is at rank 2 → recall@1 = 0.0
    post_rerank = [_make_result("docA"), _make_result("docB")]
    pre_rerank = [_make_result("docB"), _make_result("docA")]
    traces = [_make_trace("q1", results=post_rerank, pre_rerank=pre_rerank)]
    labels = _labels(("q1", "docA", 1))

    recall_post = compute_recall_at_k(traces, labels, k=1, use_pre_rerank=False)
    recall_pre = compute_recall_at_k(traces, labels, k=1, use_pre_rerank=True)
    assert recall_post == pytest.approx(1.0)
    assert recall_pre == pytest.approx(0.0)
    assert recall_post != recall_pre


def test_use_pre_rerank_mrr_differs_from_post_rerank() -> None:
    """use_pre_rerank=True uses pre_rerank_results for MRR, producing a different value."""
    post_rerank = [_make_result("docA"), _make_result("docB")]
    pre_rerank = [_make_result("docB"), _make_result("docA")]
    traces = [_make_trace("q1", results=post_rerank, pre_rerank=pre_rerank)]
    labels = _labels(("q1", "docA", 1))

    mrr_post = compute_mrr(traces, labels, use_pre_rerank=False)
    mrr_pre = compute_mrr(traces, labels, use_pre_rerank=True)
    assert mrr_post == pytest.approx(1.0)
    assert mrr_pre == pytest.approx(0.5)
    assert mrr_post != mrr_pre


def test_use_pre_rerank_ndcg_differs_from_post_rerank() -> None:
    """use_pre_rerank=True uses pre_rerank_results for nDCG, producing a different value."""
    post_rerank = [_make_result("docA"), _make_result("docB")]
    pre_rerank = [_make_result("docB"), _make_result("docA")]
    traces = [_make_trace("q1", results=post_rerank, pre_rerank=pre_rerank)]
    labels = _labels(("q1", "docA", 1))

    ndcg_post = compute_ndcg_at_k(traces, labels, k=2, use_pre_rerank=False)
    ndcg_pre = compute_ndcg_at_k(traces, labels, k=2, use_pre_rerank=True)
    assert ndcg_post == pytest.approx(1.0)
    assert ndcg_pre < ndcg_post



# ---------------------------------------------------------------------------
# Fix 6 — C1-T-5: use_pre_rerank=True raises when pre_rerank_results is None
# ---------------------------------------------------------------------------

def test_get_results_raises_when_pre_rerank_requested_but_missing() -> None:
    """ValueError is raised when use_pre_rerank=True but trace has no pre_rerank_results."""
    # Trace created without pre_rerank_results (defaults to None).
    traces = [_make_trace("q1", [_make_result("docA")])]
    labels = _labels(("q1", "docA", 1))

    with pytest.raises(ValueError, match="has no pre_rerank_results"):
        compute_recall_at_k(traces, labels, k=1, use_pre_rerank=True)

    with pytest.raises(ValueError, match="has no pre_rerank_results"):
        compute_mrr(traces, labels, use_pre_rerank=True)

    with pytest.raises(ValueError, match="has no pre_rerank_results"):
        compute_ndcg_at_k(traces, labels, k=1, use_pre_rerank=True)

# ---------------------------------------------------------------------------
# C1-T-3: query_id with NO labels at all is skipped consistently
# ---------------------------------------------------------------------------

def test_query_with_no_labels_is_skipped_in_all_metrics() -> None:
    """A query whose query_id has no labels at all is excluded from all three metrics."""
    # q1 has labels; q2 has no labels at all (not in labels list).
    # Only q1 contributes to the metrics.
    traces = [
        _make_trace("q1", [_make_result("docA")]),
        _make_trace("q2", [_make_result("docB")]),  # no labels for q2
    ]
    labels = _labels(("q1", "docA", 1))

    # All metrics should reflect only q1 (recall=1.0, mrr=1.0, ndcg=1.0).
    assert compute_recall_at_k(traces, labels, k=1) == pytest.approx(1.0)
    assert compute_mrr(traces, labels) == pytest.approx(1.0)
    assert compute_ndcg_at_k(traces, labels, k=1) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# C1-T-5: corpus_size < k does NOT raise ValueError
# ---------------------------------------------------------------------------

def test_corpus_size_less_than_k_does_not_raise() -> None:
    """corpus_size < k does not trigger the depth check (guard only fires when corpus_size >= k)."""
    results = [_make_result("docA")]  # only 1 unique doc
    traces = [_make_trace("q1", results)]
    labels = _labels(("q1", "docA", 1))

    # corpus_size=2 < k=3 → no ValueError should be raised
    recall = compute_recall_at_k(traces, labels, k=3, corpus_size=2)
    assert recall == pytest.approx(1.0)

    ndcg = compute_ndcg_at_k(traces, labels, k=3, corpus_size=2)
    assert ndcg == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# C1-T-7: empty traces list returns 0.0 for all metrics
# ---------------------------------------------------------------------------

def test_empty_traces_recall_returns_zero() -> None:
    """compute_recall_at_k on empty traces returns 0.0."""
    labels = _labels(("q1", "docA", 1))
    assert compute_recall_at_k([], labels, k=5) == pytest.approx(0.0)


def test_empty_traces_mrr_returns_zero() -> None:
    """compute_mrr on empty traces returns 0.0."""
    labels = _labels(("q1", "docA", 1))
    assert compute_mrr([], labels) == pytest.approx(0.0)


def test_empty_traces_ndcg_returns_zero() -> None:
    """compute_ndcg_at_k on empty traces returns 0.0."""
    labels = _labels(("q1", "docA", 1))
    assert compute_ndcg_at_k([], labels, k=5) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# C1-T-10: nDCG with all-zero grades — idcg == 0.0 → skipped → returns 0.0
# ---------------------------------------------------------------------------

def test_ndcg_all_zero_grades_returns_zero() -> None:
    """When all labels have grade=0, IDCG=0 → query is skipped (contributes 0.0).

    This is distinct from a query with NO labels (also skipped), but the
    distinction matters: a grade=0 query IS processed and scores 0.0, whereas
    a no-label query is excluded entirely.  Both contribute 0.0 to the
    macro-average, but only the grade>0 query in a mixed set drives the result.
    """
    # Case 1: all-zero grades — IDCG=0 path. q1 contributes 0.0 (skipped internally).
    traces_zero = [
        _make_trace("q1", [_make_result("docA"), _make_result("docB")]),
    ]
    labels_zero = _labels(("q1", "docA", 0), ("q1", "docB", 0))
    assert compute_ndcg_at_k(traces_zero, labels_zero, k=5) == pytest.approx(0.0)

    # Case 2: mixed — q1 has grade=0 (IDCG=0, skipped), q2 has grade=1 (scored).
    # Macro avg = only q2's nDCG = 1.0 (docD is retrieved at rank 1).
    # This proves grade=0 queries don't falsely dilute the average via the skip path.
    traces_mixed = [
        _make_trace("q1", [_make_result("docA")]),
        _make_trace("q2", [_make_result("docD")]),
    ]
    labels_mixed = _labels(("q1", "docA", 0), ("q2", "docD", 1))
    assert compute_ndcg_at_k(traces_mixed, labels_mixed, k=1) == pytest.approx(1.0)

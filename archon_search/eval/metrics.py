"""Ranking metric functions for the archon-search eval harness — FEAT-039 Task 3.1.

Metrics are document-level: chunk results are deduplicated by doc_id before scoring.
When multiple chunks from the same document appear, the first-ranked chunk sets the
document's position.

Macro-averaging: per-query values are averaged equally across queries.

For multi-label queries: fractional per-query recall = |found_relevant ∩ top_k| / |all_relevant|

Grade 0 is treated as non-relevant for recall and MRR (grade > 0 = relevant).

nDCG formula (0-indexed rank i):
  gain(i) = (2^rel_i - 1) / log2(i + 2)
  DCG = sum of gain(i) for i in 0..k-1
  IDCG = DCG of ideal ranking (top-k relevant docs sorted by grade desc), truncated to k
  nDCG = DCG / IDCG  (0.0 when IDCG = 0)
"""
from __future__ import annotations

import math
from collections import defaultdict

from archon_search.eval.fixtures import RelevanceLabel
from archon_search.eval.types import EvalSearchResult, QueryEvalTrace


def deduplicate_to_doc_rankings(chunk_results: list[EvalSearchResult]) -> list[str]:
    """Return an ordered list of doc_ids with duplicates removed.

    When multiple chunks from the same document appear, keep only the
    first-ranked chunk's position. Order is preserved.
    """
    seen: set[str] = set()
    result: list[str] = []
    for item in chunk_results:
        if item.doc_id not in seen:
            seen.add(item.doc_id)
            result.append(item.doc_id)
    return result


def _get_results(trace: QueryEvalTrace, use_pre_rerank: bool) -> list[EvalSearchResult]:
    """Return the appropriate result list from a trace."""
    if use_pre_rerank:
        if trace.pre_rerank_results is None:
            raise ValueError(
                f"use_pre_rerank=True but trace {trace.query_id!r} has no pre_rerank_results"
            )
        return trace.pre_rerank_results
    return trace.results


def _build_label_index(
    labels: list[RelevanceLabel],
) -> dict[str, dict[str, int]]:
    """Build a {query_id: {doc_id: grade}} index from a label list."""
    index: dict[str, dict[str, int]] = defaultdict(dict)
    for lbl in labels:
        index[lbl.query_id][lbl.doc_id] = lbl.grade
    return index


def _check_corpus_depth(
    doc_rankings: list[str],
    k: int,
    corpus_size: int | None,
) -> None:
    """Raise ValueError when corpus has >= k docs but dedup results have < k unique docs."""
    if corpus_size is not None and corpus_size >= k and len(doc_rankings) < k:
        raise ValueError(
            f"unique-document depth insufficient: corpus has {corpus_size} documents "
            f"but only {len(doc_rankings)} unique documents appear in results "
            f"(need >= {k} for k={k})"
        )


def compute_recall_at_k(
    traces: list[QueryEvalTrace],
    labels: list[RelevanceLabel],
    k: int,
    use_pre_rerank: bool = False,
    corpus_size: int | None = None,
) -> float:
    """Compute macro-averaged Recall@k across all traces.

    Args:
        traces: Query execution traces.
        labels: Relevance labels for all queries.
        k: Cutoff depth.
        use_pre_rerank: If True, use pre-rerank results instead of post-rerank.
        corpus_size: Total number of documents in the corpus. When provided
            and >= k, raises ValueError if any trace has fewer than k unique
            docs in its results.

    Returns:
        Macro-averaged recall, in [0.0, 1.0].
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    label_index = _build_label_index(labels)

    per_query_recalls: list[float] = []
    for trace in traces:
        chunk_results = _get_results(trace, use_pre_rerank)
        doc_rankings = deduplicate_to_doc_rankings(chunk_results)
        _check_corpus_depth(doc_rankings, k, corpus_size)

        relevant_for_query = {
            doc_id for doc_id, grade in label_index.get(trace.query_id, {}).items()
            if grade > 0
        }
        if not relevant_for_query:
            continue

        top_k_docs = set(doc_rankings[:k])
        found = len(relevant_for_query & top_k_docs)
        per_query_recalls.append(found / len(relevant_for_query))

    if not per_query_recalls:
        return 0.0
    return sum(per_query_recalls) / len(per_query_recalls)


def compute_mrr(
    traces: list[QueryEvalTrace],
    labels: list[RelevanceLabel],
    use_pre_rerank: bool = False,
) -> float:
    """Compute macro-averaged Mean Reciprocal Rank across all traces.

    Args:
        traces: Query execution traces.
        labels: Relevance labels for all queries.
        use_pre_rerank: If True, use pre-rerank results instead of post-rerank.

    Returns:
        Macro-averaged MRR, in [0.0, 1.0].
    """
    label_index = _build_label_index(labels)

    per_query_rr: list[float] = []
    for trace in traces:
        chunk_results = _get_results(trace, use_pre_rerank)
        doc_rankings = deduplicate_to_doc_rankings(chunk_results)

        relevant_for_query = {
            doc_id for doc_id, grade in label_index.get(trace.query_id, {}).items()
            if grade > 0
        }
        if not relevant_for_query:
            continue

        rr = 0.0
        for rank, doc_id in enumerate(doc_rankings, start=1):
            if doc_id in relevant_for_query:
                rr = 1.0 / rank
                break
        per_query_rr.append(rr)

    if not per_query_rr:
        return 0.0
    return sum(per_query_rr) / len(per_query_rr)


def _dcg(grades: list[int], k: int) -> float:
    """Compute DCG@k for an ordered list of grades (0-indexed positions)."""
    total = 0.0
    for i, grade in enumerate(grades[:k]):
        total += (2**grade - 1) / math.log2(i + 2)
    return total


def compute_ndcg_at_k(
    traces: list[QueryEvalTrace],
    labels: list[RelevanceLabel],
    k: int,
    use_pre_rerank: bool = False,
    corpus_size: int | None = None,
) -> float:
    """Compute macro-averaged nDCG@k across all traces.

    Supports binary and graded relevance. Grade 0 contributes zero gain.
    IDCG is truncated to k (not the number of relevant docs).

    Args:
        traces: Query execution traces.
        labels: Relevance labels for all queries.
        k: Cutoff depth.
        use_pre_rerank: If True, use pre-rerank results instead of post-rerank.
        corpus_size: Total number of documents in the corpus. When provided
            and >= k, raises ValueError if any trace has fewer than k unique
            docs in its results.

    Returns:
        Macro-averaged nDCG, in [0.0, 1.0].
    """
    if k <= 0:
        raise ValueError(f"k must be positive, got {k}")
    label_index = _build_label_index(labels)

    per_query_ndcg: list[float] = []
    for trace in traces:
        chunk_results = _get_results(trace, use_pre_rerank)
        doc_rankings = deduplicate_to_doc_rankings(chunk_results)
        _check_corpus_depth(doc_rankings, k, corpus_size)

        grade_map = label_index.get(trace.query_id, {})

        # Build actual grade sequence for the ranked results
        actual_grades = [grade_map.get(doc_id, 0) for doc_id in doc_rankings]

        # Ideal grade sequence: sort all known grades descending, take top-k
        ideal_grades = sorted(grade_map.values(), reverse=True)

        idcg = _dcg(ideal_grades, k)
        if idcg == 0.0:
            continue

        dcg = _dcg(actual_grades, k)
        per_query_ndcg.append(dcg / idcg)

    if not per_query_ndcg:
        return 0.0
    return sum(per_query_ndcg) / len(per_query_ndcg)

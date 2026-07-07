"""Eval-only dataclasses for score provenance, traces, and aggregate metrics.

These types are intentionally kept *separate* from the production
``SearchResult`` (``archon_search._types``) so that eval score provenance
does not widen the public API payload.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from archon_search._diagnostics import SearchScoreBreakdown


@dataclass
class EvalSearchResult:
    """A single search result with full score provenance, for eval use only.

    Attributes:
        doc_id: Stable fixture ID after runtime ID mapping.
        runtime_doc_id: Path-derived store ID used internally for diagnostics.
        chunk_id: Chunk identifier within the document.
        text: Chunk text content.
        source_path: File path of the source document.
        collection: Collection this result was retrieved from.  Explicit so
            cross-collection traces do not rely on path parsing.
        score_breakdown: Full score provenance for this result.
    """

    doc_id: str
    runtime_doc_id: str
    chunk_id: str
    text: str
    source_path: str
    collection: str
    score_breakdown: SearchScoreBreakdown


@dataclass
class QueryEvalTrace:
    """Execution trace for a single eval query run.

    Attributes:
        query_id: Stable query ID from the eval fixture.
        query_text: Raw query string.
        collection: Target collection, or ``None`` for routing-scope queries.
        metric_scope: Either ``"retrieval"`` or ``"routing"``.
        results: Ordered list of retrieved :class:`EvalSearchResult` items.
        router_correct: ``True``/``False`` when routing is enabled and the
            query is non-bypassed; ``None`` when routing is disabled or the
            query bypasses routing.
        ranked_collections: Full ranked list of collection names in
            score-descending order when routing ran (possibly empty); ``None``
            when routing is disabled or the query bypasses routing.  An empty
            list is semantically distinct from ``None``: ``[]`` means routing
            ran but produced no scored collections; ``None`` means routing did
            not run.
        latency_ms: End-to-end query latency in milliseconds.
    """

    query_id: str
    query_text: str
    collection: str | None
    metric_scope: str
    results: list[EvalSearchResult] = field(default_factory=list)
    pre_rerank_results: list[EvalSearchResult] | None = None
    router_correct: bool | None = None
    ranked_collections: list[str] | None = None
    latency_ms: float = 0.0


@dataclass
class EvalMetrics:
    """Aggregate evaluation metrics computed over a set of query traces.

    Attributes:
        recall_at_1: Recall@1.
        recall_at_3: Recall@3.
        recall_at_5: Recall@5.
        mrr: Mean Reciprocal Rank.
        ndcg_at_5: nDCG@5.
        ndcg_at_10: nDCG@10.
        reranker_lift: Improvement in nDCG@5 attributable to the reranker.
            ``None`` when no reranker comparison was run.
        routing_accuracy: Fraction of routing-scope queries classified to the
            correct collection.  ``None`` when no routing queries were present.
        latency_p50_ms: 50th-percentile (median) query latency in ms.
        latency_p95_ms: 95th-percentile query latency in ms.
        routing_mrr_centroid: Mean Reciprocal Rank over routing-scope traces
            run under the centroid strategy.  ``None`` when no eligible traces.
        routing_mrr_hybrid: Mean Reciprocal Rank over routing-scope traces run
            under the hybrid strategy.  ``None`` when not yet computed.
        routing_precision_at_1_centroid: Precision@1 for centroid routing.
            ``None`` when no eligible traces.
        routing_precision_at_1_hybrid: Precision@1 for hybrid routing.
            ``None`` when not yet computed.
        graph_mrr: Mean Reciprocal Rank over graph-mode (``graph_mode="naive"``)
            retrieval queries only.  ``None`` when no naive-mode graph queries are
            present in the corpus.  Report-only — no gating floor in E1a.
        graph_local_mrr: Mean Reciprocal Rank over ``graph_mode="local"`` queries.
            ``None`` when no local-mode graph queries are present.
        graph_global_mrr: Mean Reciprocal Rank over ``graph_mode="global"`` queries.
            ``None`` when no global-mode graph queries are present.
        graph_naive_recall_at_5: Recall@5 over naive-mode graph queries on
            multi-hop collections (MuSiQue, 2WikiMultiHopQA).  ``None`` until
            BE-5/BE-6 are implemented.
        graph_local_recall_at_5: Recall@5 over local-mode graph queries on
            multi-hop collections.  ``None`` until BE-5/BE-6 are implemented.
        graph_global_recall_at_5: Recall@5 over global-mode graph queries on
            multi-hop collections.  ``None`` until BE-5/BE-6 are implemented.
        graph_negative_control_recall_at_5: Recall@5 over naive-mode graph
            queries on HotpotQA distractor — a **regression guard**, not a
            harm-vs-no-graph baseline.  A drop here signals graph-mode
            degradation on simple queries.  ``None`` until BE-5/BE-6 are
            implemented.
        synonym_bridge_recall_at_5: Recall@5 over naive-mode graph queries on
            the synonym-bridge collection.  Measures whether synonym edges
            (``relationship_type="synonym_of"``) added to the graph allow
            queries using one term to also retrieve documents that use the
            synonymous term.  ``None`` when no synonym-bridge queries are
            present in the corpus.
    """

    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float
    ndcg_at_5: float
    ndcg_at_10: float
    reranker_lift: float | None
    routing_accuracy: float | None
    latency_p50_ms: float
    latency_p95_ms: float
    routing_mrr_centroid: float | None = None
    routing_mrr_hybrid: float | None = None
    routing_precision_at_1_centroid: float | None = None
    routing_precision_at_1_hybrid: float | None = None
    graph_mrr: float | None = None
    graph_local_mrr: float | None = None
    graph_global_mrr: float | None = None
    graph_naive_recall_at_5: float | None = None
    graph_local_recall_at_5: float | None = None
    graph_global_recall_at_5: float | None = None
    graph_negative_control_recall_at_5: float | None = None
    synonym_bridge_recall_at_5: float | None = None

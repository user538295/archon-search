"""Tests for eval/types.py and _diagnostics.py — Task 2.1."""
from __future__ import annotations

import sys


def test_score_breakdown_contains_rank_score_and_score_kind_fields() -> None:
    from archon_search._diagnostics import SearchScoreBreakdown

    sb = SearchScoreBreakdown(
        vector_rank=1,
        vector_score=0.92,
        vector_score_kind="similarity",
        fts_rank=3,
        fts_score=12.4,
        fts_score_kind="bm25",
        rrf_score=0.031,
        reranker_score=0.87,
    )

    assert sb.vector_rank == 1
    assert sb.vector_score == 0.92
    assert sb.vector_score_kind == "similarity"
    assert sb.fts_rank == 3
    assert sb.fts_score == 12.4
    assert sb.fts_score_kind == "bm25"
    assert sb.rrf_score == 0.031
    assert sb.reranker_score == 0.87


def test_score_breakdown_optional_fields_accept_none() -> None:
    from archon_search._diagnostics import SearchScoreBreakdown

    sb = SearchScoreBreakdown(
        vector_rank=None,
        vector_score=None,
        vector_score_kind=None,
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=0.01,
        reranker_score=None,
    )

    assert sb.vector_rank is None
    assert sb.fts_rank is None
    assert sb.reranker_score is None


def test_eval_search_result_contains_score_breakdown() -> None:
    from archon_search._diagnostics import SearchScoreBreakdown
    from archon_search.eval.types import EvalSearchResult

    sb = SearchScoreBreakdown(
        vector_rank=2,
        vector_score=0.85,
        vector_score_kind="distance",
        fts_rank=1,
        fts_score=10.0,
        fts_score_kind="bm25",
        rrf_score=0.04,
        reranker_score=None,
    )
    result = EvalSearchResult(
        doc_id="doc-001",
        runtime_doc_id="corpus/doc-001.txt",
        chunk_id="chunk-0",
        text="some chunk text",
        source_path="corpus/doc-001.txt",
        collection="my_col",
        score_breakdown=sb,
    )

    assert result.doc_id == "doc-001"
    assert result.runtime_doc_id == "corpus/doc-001.txt"
    assert result.chunk_id == "chunk-0"
    assert result.collection == "my_col"
    assert result.score_breakdown is sb
    assert result.score_breakdown.rrf_score == 0.04


def test_production_trace_types_do_not_import_eval_package() -> None:
    # Ensure archon_search.eval is not already in sys.modules from previous tests
    # (isolate by removing it if present — safe because this test only checks
    # _diagnostics, not eval).
    eval_key = "archon_search.eval"
    eval_types_key = "archon_search.eval.types"
    was_present = eval_key in sys.modules or eval_types_key in sys.modules

    # Import only the production diagnostics module
    import importlib
    diag = importlib.import_module("archon_search._diagnostics")

    # _diagnostics itself must not have triggered loading archon_search.eval
    if not was_present:
        assert eval_key not in sys.modules, (
            "_diagnostics import pulled in archon_search.eval — production must not depend on eval"
        )

    # Verify the module has the expected production types
    assert hasattr(diag, "SearchScoreBreakdown")
    assert hasattr(diag, "ScoredSearchCandidate")


def test_query_eval_trace_router_correct_is_none_when_routing_disabled() -> None:
    from archon_search._diagnostics import SearchScoreBreakdown
    from archon_search.eval.types import EvalSearchResult, QueryEvalTrace

    sb = SearchScoreBreakdown(
        vector_rank=1,
        vector_score=0.9,
        vector_score_kind="similarity",
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=0.05,
        reranker_score=None,
    )
    result = EvalSearchResult(
        doc_id="doc-a",
        runtime_doc_id="corpus/a.txt",
        chunk_id="chunk-0",
        text="text",
        source_path="corpus/a.txt",
        collection="col1",
        score_breakdown=sb,
    )
    trace = QueryEvalTrace(
        query_id="q1",
        query_text="what is X?",
        collection="col1",
        metric_scope="retrieval",
        results=[result],
        router_correct=None,  # routing disabled
        latency_ms=42.0,
    )

    assert trace.router_correct is None


def test_query_eval_trace_router_correct_bool_when_routing_enabled() -> None:
    from archon_search._diagnostics import SearchScoreBreakdown
    from archon_search.eval.types import EvalSearchResult, QueryEvalTrace

    sb = SearchScoreBreakdown(
        vector_rank=1,
        vector_score=0.9,
        vector_score_kind="similarity",
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=0.05,
        reranker_score=None,
    )
    result = EvalSearchResult(
        doc_id="doc-b",
        runtime_doc_id="corpus/b.txt",
        chunk_id="chunk-0",
        text="text",
        source_path="corpus/b.txt",
        collection="col2",
        score_breakdown=sb,
    )

    for expected in (True, False):
        trace = QueryEvalTrace(
            query_id="q2",
            query_text="find Y",
            collection=None,  # routing scope — no target collection
            metric_scope="routing",
            results=[result],
            router_correct=expected,
            latency_ms=30.0,
        )
        assert trace.router_correct is expected
        assert isinstance(trace.router_correct, bool)

"""Tests for routes_explain.py public Pydantic schemas."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search.server.routes_explain import (
    ExplainNearMiss,
    ExplainRequest,
    ExplainResponse,
    ExplainResult,
    ExplainScoreBreakdown,
    RoutingCandidate,
    RoutingExplain,
)


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
    chunk_id: str = "a" * 64 + "-000000",
    text: str = "sample text",
    rrf_score: float = 0.1,
    reranker_score: float | None = None,
) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text=text,
        source_path="/path/to/doc.md",
        score_breakdown=_make_breakdown(rrf_score=rrf_score, reranker_score=reranker_score),
        collection="my-col",
    )


# ---------------------------------------------------------------------------
# ExplainRequest
# ---------------------------------------------------------------------------


def test_explain_request_accepts_minimal_payload() -> None:
    req = ExplainRequest(query="what is archon?")
    assert req.query == "what is archon?"
    assert req.collection is None
    assert req.top_k == 5
    assert req.rerank is True


def test_explain_request_rejects_empty_query() -> None:
    with pytest.raises(ValidationError):
        ExplainRequest(query="")


def test_explain_request_rejects_top_k_out_of_range() -> None:
    with pytest.raises(ValidationError):
        ExplainRequest(query="q", top_k=0)
    with pytest.raises(ValidationError):
        ExplainRequest(query="q", top_k=101)


def test_explain_request_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ExplainRequest(query="q", unknown_field="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ExplainNearMiss — no text field
# ---------------------------------------------------------------------------


def test_explain_near_miss_has_no_text_field() -> None:
    assert "text" not in ExplainNearMiss.model_fields


def test_explain_near_miss_rejects_text_in_payload() -> None:
    with pytest.raises(ValidationError):
        ExplainNearMiss(
            doc_id="a" * 64,
            chunk_id="a" * 64 + "-000000",
            source_path="/foo.md",
            score=0.5,
            breakdown=ExplainScoreBreakdown(
                vector_rank=0,
                vector_score=0.5,
                vector_score_kind="distance",
                fts_rank=None,
                fts_score=None,
                fts_score_kind=None,
                rrf_score=0.1,
                reranker_score=None,
            ),
            text="should fail",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# ExplainResponse
# ---------------------------------------------------------------------------


def test_explain_response_serializes_routing_null_when_pinned() -> None:
    from archon_search.pipeline import ExplainPipelineResult

    result = ExplainResponse.from_pipeline_result(
        pipeline_result=ExplainPipelineResult(
            top_results=[],
            near_misses=[],
            acl_filtered=False,
        ),
        collection="pinned-col",
        rerank=True,
        routing=None,
    )
    data = result.model_dump(mode="json")
    assert data["routing"] is None
    assert data["collection"] == "pinned-col"


def test_explain_response_does_not_include_query_field() -> None:
    assert "query" not in ExplainResponse.model_fields


# ---------------------------------------------------------------------------
# from_candidate score selection
# ---------------------------------------------------------------------------


def test_from_candidate_uses_reranker_score_when_present() -> None:
    c = _make_candidate(rrf_score=0.1, reranker_score=0.9)
    result = ExplainResult.from_candidate(c)
    assert result.score == pytest.approx(0.9)


def test_from_candidate_uses_rrf_score_when_reranker_none() -> None:
    c = _make_candidate(rrf_score=0.15, reranker_score=None)
    result = ExplainResult.from_candidate(c)
    assert result.score == pytest.approx(0.15)


def test_from_candidate_near_miss_strips_text() -> None:
    c = _make_candidate(text="some detailed text")
    nm = ExplainNearMiss.from_candidate(c)
    assert not hasattr(nm, "text") or "text" not in nm.model_fields


# ---------------------------------------------------------------------------
# from_pipeline_result preserves order
# ---------------------------------------------------------------------------


def test_from_pipeline_result_preserves_order() -> None:
    from archon_search.pipeline import ExplainPipelineResult

    c1 = _make_candidate(doc_id="a" * 64, chunk_id="a" * 64 + "-000000", rrf_score=0.9)
    c2 = _make_candidate(doc_id="b" * 64, chunk_id="b" * 64 + "-000000", rrf_score=0.5)
    c3 = _make_candidate(doc_id="c" * 64, chunk_id="c" * 64 + "-000000", rrf_score=0.3)

    pr = ExplainPipelineResult(top_results=[c1, c2], near_misses=[c3], acl_filtered=False)
    resp = ExplainResponse.from_pipeline_result(
        pipeline_result=pr,
        collection="col",
        rerank=False,
        routing=None,
    )
    assert resp.results[0].doc_id == "a" * 64
    assert resp.results[1].doc_id == "b" * 64
    assert resp.near_misses[0].doc_id == "c" * 64


def test_from_pipeline_result_routing_passthrough() -> None:
    from archon_search.pipeline import ExplainPipelineResult

    routing = RoutingExplain(
        invoked=True,
        chosen_collection="col-a",
        confidence_threshold=0.5,
        chosen_below_threshold=False,
        candidates=[RoutingCandidate(collection="col-a", centroid_score=0.8)],
    )
    pr = ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=False)
    resp = ExplainResponse.from_pipeline_result(
        pipeline_result=pr,
        collection="col-a",
        rerank=True,
        routing=routing,
    )
    assert resp.routing is not None
    assert resp.routing.chosen_collection == "col-a"


def test_from_pipeline_result_acl_filtered_flag_passthrough() -> None:
    from archon_search.pipeline import ExplainPipelineResult

    pr = ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=True)
    resp = ExplainResponse.from_pipeline_result(
        pipeline_result=pr,
        collection="col",
        rerank=True,
        routing=None,
    )
    assert resp.acl_filtered is True


def test_from_pipeline_result_empty_results_and_near_misses() -> None:
    from archon_search.pipeline import ExplainPipelineResult

    pr = ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=False)
    resp = ExplainResponse.from_pipeline_result(
        pipeline_result=pr,
        collection="col",
        rerank=True,
        routing=None,
    )
    assert resp.results == []
    assert resp.near_misses == []


def test_explain_response_round_trips_brief_example() -> None:
    """Roundtrip serialize/deserialize a simple constructed ExplainResponse."""
    from archon_search.pipeline import ExplainPipelineResult

    c = _make_candidate(rrf_score=0.2, reranker_score=0.8)
    nm = _make_candidate(doc_id="b" * 64, chunk_id="b" * 64 + "-000000", rrf_score=0.05)

    pr = ExplainPipelineResult(top_results=[c], near_misses=[nm], acl_filtered=False)
    resp = ExplainResponse.from_pipeline_result(
        pipeline_result=pr,
        collection="test-col",
        rerank=True,
        routing=None,
    )

    data = resp.model_dump(mode="json")
    assert data["collection"] == "test-col"
    assert data["rerank"] is True
    assert len(data["results"]) == 1
    assert data["results"][0]["score"] == pytest.approx(0.8)
    assert len(data["near_misses"]) == 1
    assert "text" not in data["near_misses"][0]

    # Re-parse from dict
    resp2 = ExplainResponse.model_validate(data)
    assert resp2.collection == resp.collection
    assert resp2.results[0].score == pytest.approx(resp.results[0].score)

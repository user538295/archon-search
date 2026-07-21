"""Unit tests for the public /explain schemas (A4 Task 1.2)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search.pipeline import ExplainPipelineResult
from archon_search.server.routes_explain import (
    ExplainNearMiss,
    ExplainRequest,
    ExplainResponse,
    ExplainResult,
    RoutingCandidate,
    RoutingExplain,
)
from archon_search.server.schemas import AclGateSchema


def _breakdown(*, rrf: float = 0.03, reranker: float | None = None) -> SearchScoreBreakdown:
    return SearchScoreBreakdown(
        vector_rank=1,
        vector_score=0.74,
        vector_score_kind="distance",
        fts_rank=3,
        fts_score=4.2,
        fts_score_kind="bm25",
        rrf_score=rrf,
        reranker_score=reranker,
    )


def _candidate(
    doc_id: str = "d1",
    chunk_id: str = "d1-000000",
    *,
    rrf: float = 0.03,
    reranker: float | None = None,
    text: str = "body",
    acl: list[str] | None = None,
    language: str = "",
) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text=text,
        source_path="/tmp/x.md",
        score_breakdown=_breakdown(rrf=rrf, reranker=reranker),
        collection="docs",
        acl=acl,
        language=language,
    )


def test_explain_request_accepts_minimal_payload() -> None:
    req = ExplainRequest(query="foo")
    assert req.collection is None
    assert req.top_k == 5
    assert req.rerank is True


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_explain_request_rejects_empty_query(bad: str) -> None:
    with pytest.raises(ValidationError):
        ExplainRequest(query=bad)


@pytest.mark.parametrize("bad", [0, -1])
def test_explain_request_rejects_top_k_out_of_range(bad: int) -> None:
    with pytest.raises(ValidationError):
        ExplainRequest(query="foo", top_k=bad)


def test_explain_request_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ExplainRequest(query="foo", unexpected="x")


def test_explain_request_blank_collection_rejected() -> None:
    with pytest.raises(ValidationError):
        ExplainRequest(query="foo", collection="   ")


def test_explain_near_miss_has_no_text_field() -> None:
    assert "text" not in ExplainNearMiss.model_fields


def test_explain_near_miss_rejects_text_in_payload() -> None:
    with pytest.raises(ValidationError):
        ExplainNearMiss(
            doc_id="d1",
            chunk_id="d1-000000",
            source_path="/tmp/x.md",
            score=0.4,
            breakdown=ExplainResult.from_candidate(_candidate()).breakdown,
            text="leak",
        )


def test_explain_response_serializes_routing_null_when_pinned() -> None:
    resp = ExplainResponse(
        rerank=True,
        routing=None,
        collection="docs",
        acl_filtered=False,
        results=[],
        near_misses=[],
    )
    dumped = resp.model_dump(mode="json", exclude_none=False)
    assert dumped["routing"] is None


def test_explain_response_does_not_include_query_field() -> None:
    assert "query" not in ExplainResponse.model_fields


def test_from_candidate_uses_reranker_score_when_present() -> None:
    result = ExplainResult.from_candidate(_candidate(rrf=0.03, reranker=0.91))
    assert result.score == 0.91


def test_from_candidate_uses_rrf_score_when_reranker_none() -> None:
    result = ExplainResult.from_candidate(_candidate(rrf=0.03, reranker=None))
    assert result.score == 0.03


def test_from_candidate_near_miss_strips_text() -> None:
    nm = ExplainNearMiss.from_candidate(_candidate(text="leak"))
    assert "text" not in nm.model_dump(mode="json")


def test_from_candidate_populates_acl_and_language_on_both_models() -> None:
    cand = _candidate(acl=["ns1", "ns2"], language="en")
    result = ExplainResult.from_candidate(cand)
    near_miss = ExplainNearMiss.from_candidate(cand)
    assert result.acl == ["ns1", "ns2"]
    assert result.language == "en"
    assert near_miss.acl == ["ns1", "ns2"]
    assert near_miss.language == "en"


def test_explain_result_and_near_miss_expose_search_parity_fields() -> None:
    # Superset parity: /explain carries every /search SearchResultSchema metadata
    # field (incl. acl) plus language for future filter-debugging.
    search_parity = {"file_type", "indexed_at", "updated_at", "ingested_by", "metadata", "acl"}
    for model in (ExplainResult, ExplainNearMiss):
        missing = search_parity - set(model.model_fields)
        assert not missing, f"{model.__name__} missing parity fields: {missing}"
        assert "language" in model.model_fields


def test_from_pipeline_result_preserves_order() -> None:
    top = [_candidate("a", "a-000000"), _candidate("b", "b-000000")]
    near = [_candidate("c", "c-000000"), _candidate("d", "d-000000")]
    result = ExplainPipelineResult(top_results=top, near_misses=near, acl_filtered=False)
    resp = ExplainResponse.from_pipeline_result(
        rerank=True, collection="docs", routing=None, result=result
    )
    assert [(r.doc_id, r.chunk_id) for r in resp.results] == [("a", "a-000000"), ("b", "b-000000")]
    assert [(n.doc_id, n.chunk_id) for n in resp.near_misses] == [("c", "c-000000"), ("d", "d-000000")]


def test_from_pipeline_result_routing_passthrough() -> None:
    routing = RoutingExplain(
        invoked=True,
        chosen_collection="docs",
        confidence_threshold=0.3,
        chosen_below_threshold=False,
        candidates=[RoutingCandidate(collection="docs", centroid_score=0.83)],
    )
    result = ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=False)
    resp = ExplainResponse.from_pipeline_result(
        rerank=True, collection="docs", routing=routing, result=result
    )
    assert resp.routing is routing

    resp_none = ExplainResponse.from_pipeline_result(
        rerank=True, collection="docs", routing=None, result=result
    )
    assert resp_none.routing is None


def test_from_pipeline_result_acl_filtered_flag_passthrough() -> None:
    result = ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=True)
    resp = ExplainResponse.from_pipeline_result(
        rerank=True, collection="docs", routing=None, result=result
    )
    assert resp.acl_filtered is True


def test_from_pipeline_result_empty_results_and_near_misses() -> None:
    result = ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=False)
    resp = ExplainResponse.from_pipeline_result(
        rerank=False, collection="docs", routing=None, result=result
    )
    assert resp.results == []
    assert resp.near_misses == []


def test_explain_response_round_trips_brief_example() -> None:
    breakdown = {
        "vector_rank": 1,
        "vector_score": 0.74,
        "vector_score_kind": "distance",
        "fts_rank": 3,
        "fts_score": 4.2,
        "fts_score_kind": "bm25",
        "rrf_score": 0.032,
        "reranker_score": 0.91,
    }
    payload = {
        "rerank": True,
        "routing": {
            "invoked": True,
            "chosen_collection": "docs",
            "confidence_threshold": 0.30,
            "chosen_below_threshold": False,
            "candidates": [
                {"collection": "docs", "centroid_score": 0.83},
                {"collection": "code", "centroid_score": 0.61},
            ],
        },
        "collection": "docs",
        "acl_filtered": False,
        "results": [
            {
                "doc_id": "a",
                "chunk_id": "a-000000",
                "source_path": "/tmp/a.md",
                "text": "body",
                "score": 0.91,
                "breakdown": breakdown,
                "acl_gate": {
                    "allowed_principals": None,
                    "source": None,
                    "sidecar_path": None,
                    "warnings": [],
                },
            }
        ],
        "near_misses": [
            {
                "doc_id": "b",
                "chunk_id": "b-000000",
                "source_path": "/tmp/b.md",
                "score": 0.42,
                "breakdown": {**breakdown, "reranker_score": 0.42},
            }
        ],
        "excluded_collections": [],
        "embedding_model": "",
        "hyde_applied": False,
        "stage_timings_ms": None,
        "rag_fusion_applied": False,
        "rag_fusion_queries_used": 0,
        "rag_fusion_attempted": False,
        "rag_fusion_failure_reason": None,
        "rag_fusion_sub_queries": None,
        "graph_mode_applied": None,
        "ppr_entities_matched": None,
    }
    resp = ExplainResponse.model_validate(payload)
    dumped = resp.model_dump(mode="json", exclude_none=False)
    assert set(dumped.keys()) == set(payload.keys())
    assert set(dumped["results"][0].keys()) >= set(payload["results"][0].keys())
    assert "text" not in dumped["near_misses"][0]


# ---------------------------------------------------------------------------
# BE-7 — acl_gate on ExplainResult; absent from ExplainNearMiss (C2, S6)
# ---------------------------------------------------------------------------


def test_explain_result_has_acl_gate() -> None:
    """ExplainResult carries acl_gate (non-nullable); ExplainNearMiss does not."""
    assert "acl_gate" in ExplainResult.model_fields
    field_info = ExplainResult.model_fields["acl_gate"]
    # Non-nullable: field must be required (no default) and annotation must be exactly AclGateSchema
    assert field_info.is_required(), "acl_gate must be a required field on ExplainResult"
    assert field_info.annotation is AclGateSchema, (
        "acl_gate annotation must be AclGateSchema, not a Union that could include None"
    )

    assert "acl_gate" not in ExplainNearMiss.model_fields


def test_explain_near_miss_rejects_acl_gate_field() -> None:
    """ExplainNearMiss with extra='forbid' must reject an acl_gate key."""
    with pytest.raises(ValidationError):
        ExplainNearMiss(
            doc_id="d1",
            chunk_id="d1-000000",
            source_path="/tmp/x.md",
            score=0.4,
            breakdown=ExplainResult.from_candidate(_candidate()).breakdown,
            acl_gate={"allowed_principals": None, "source": None, "sidecar_path": None, "warnings": []},
        )


def test_explain_result_from_candidate_builds_gate() -> None:
    """ExplainResult.from_candidate() populates all AclGateSchema fields from provenance."""
    candidate = ScoredSearchCandidate(
        doc_id="doc1",
        chunk_id="doc1-000000",
        text="some text",
        source_path="/tmp/doc1.md",
        score_breakdown=_breakdown(rrf=0.05),
        collection="docs",
        acl=["ns1"],
        acl_source="sidecar",
        acl_sidecar_path="docs/doc1.md.acl",
        acl_warning=["truncated to filename"],
    )
    result = ExplainResult.from_candidate(candidate)

    assert isinstance(result.acl_gate, AclGateSchema)
    assert result.acl_gate.allowed_principals == ["ns1"]
    assert result.acl_gate.source == "sidecar"
    assert result.acl_gate.sidecar_path == "docs/doc1.md.acl"
    assert result.acl_gate.warnings == ["truncated to filename"]


def test_explain_result_from_candidate_builds_gate_null_acl() -> None:
    """acl_gate built even when acl is None (collection_default case)."""
    candidate = ScoredSearchCandidate(
        doc_id="doc2",
        chunk_id="doc2-000000",
        text="open doc",
        source_path="/tmp/doc2.md",
        score_breakdown=_breakdown(),
        collection="docs",
        acl=None,
        acl_source="collection_default",
        acl_sidecar_path=None,
        acl_warning=[],
    )
    result = ExplainResult.from_candidate(candidate)

    assert isinstance(result.acl_gate, AclGateSchema)
    assert result.acl_gate.allowed_principals is None
    assert result.acl_gate.source == "collection_default"
    assert result.acl_gate.sidecar_path is None
    assert result.acl_gate.warnings == []


def test_explain_result_from_candidate_builds_gate_pre_g15() -> None:
    """Pre-G15 candidate (all provenance None/[]) builds acl_gate with null source."""
    candidate = ScoredSearchCandidate(
        doc_id="doc3",
        chunk_id="doc3-000000",
        text="old chunk",
        source_path="/tmp/doc3.md",
        score_breakdown=_breakdown(),
        collection="docs",
        acl=None,
        acl_source=None,
        acl_sidecar_path=None,
        acl_warning=[],
    )
    result = ExplainResult.from_candidate(candidate)

    assert isinstance(result.acl_gate, AclGateSchema)
    assert result.acl_gate.source is None
    assert result.acl_gate.warnings == []


def test_explain_result_from_candidate_coerces_unknown_acl_source() -> None:
    """Unknown acl_source strings are coerced to None, not raised as ValidationError."""
    candidate = ScoredSearchCandidate(
        doc_id="doc4",
        chunk_id="doc4-000000",
        text="chunk",
        source_path="/tmp/doc4.md",
        score_breakdown=_breakdown(),
        collection="docs",
        acl=None,
        acl_source="bogus-unknown-value",
        acl_sidecar_path=None,
        acl_warning=[],
    )
    result = ExplainResult.from_candidate(candidate)
    # Unknown source must be coerced to None, not raise ValidationError
    assert result.acl_gate.source is None

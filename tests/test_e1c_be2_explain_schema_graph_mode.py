"""BE-2: Tests for graph_mode on ExplainRequest/ExplainResult/ExplainResponse + ExplainPipelineResult delta.

Tests target:
- ``ExplainRequest.graph_mode`` field (Literal or None, defaults None)
- ``ExplainResult.graph_provenance`` field + updated ``from_candidate()``
- ``ExplainResponse.graph_mode_applied`` field + updated ``from_pipeline_result()``
- ``ExplainPipelineResult.graph_mode_applied`` field

Covers scenarios: S1 (null pass-through), S10 (all-null rejected, schema), S11 (empty steps preserved),
plus explicit graph_mode validation.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from archon_search._diagnostics import (
    GraphProvenance,
    ScoredSearchCandidate,
    SearchScoreBreakdown,
    TraversalStep,
)
from archon_search.pipeline import ExplainPipelineResult
from archon_search.server.routes_explain import (
    ExplainRequest,
    ExplainResponse,
    ExplainResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_breakdown() -> SearchScoreBreakdown:
    return SearchScoreBreakdown(
        vector_rank=None,
        vector_score=None,
        vector_score_kind=None,
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=0.5,
        reranker_score=None,
    )


def _minimal_candidate(**kwargs) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id="abc123",
        chunk_id="abc123-000000",
        text="some text",
        source_path="/tmp/foo.md",
        score_breakdown=_minimal_breakdown(),
        collection="my-col",
        **kwargs,
    )


def _minimal_pipeline_result(**kwargs) -> ExplainPipelineResult:
    return ExplainPipelineResult(
        top_results=[],
        near_misses=[],
        acl_filtered=False,
        **kwargs,
    )


def _make_app(tmp_path: Path) -> tuple:
    """Create a minimal FastAPI test app with a mocked pipeline."""
    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")

    with patch("archon_search.chunker.DocumentChunker.__init__", return_value=None):
        app = create_app(config, job_store)

    from fastapi.testclient import TestClient

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {key}"})
    return app, client


# ---------------------------------------------------------------------------
# Unit tests — ExplainRequest.graph_mode field
# ---------------------------------------------------------------------------


def test_explain_request_graph_mode_defaults_none() -> None:
    """Omitting graph_mode → field is None (default)."""
    req = ExplainRequest(query="hello")
    assert req.graph_mode is None


def test_explain_request_graph_mode_valid_naive() -> None:
    """graph_mode='naive' is accepted."""
    req = ExplainRequest(query="hello", graph_mode="naive")
    assert req.graph_mode == "naive"


def test_explain_request_graph_mode_valid_local() -> None:
    """graph_mode='local' is accepted."""
    req = ExplainRequest(query="hello", graph_mode="local")
    assert req.graph_mode == "local"


def test_explain_request_graph_mode_valid_global() -> None:
    """graph_mode='global' is accepted."""
    req = ExplainRequest(query="hello", graph_mode="global")
    assert req.graph_mode == "global"


def test_explain_request_graph_mode_explicit_none() -> None:
    """Explicitly setting graph_mode=None is accepted."""
    req = ExplainRequest(query="hello", graph_mode=None)
    assert req.graph_mode is None


def test_explain_request_invalid_graph_mode_rejected() -> None:
    """graph_mode='invalid' is rejected with Pydantic ValidationError."""
    with pytest.raises(ValidationError):
        ExplainRequest(query="hello", graph_mode="invalid")  # type: ignore[call-arg]


def test_explain_request_invalid_graph_mode_wrong_case_rejected() -> None:
    """graph_mode='NAIVE' (wrong case) is rejected with Pydantic ValidationError."""
    with pytest.raises(ValidationError):
        ExplainRequest(query="hello", graph_mode="NAIVE")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Unit tests — ExplainResult.from_candidate() + graph_provenance field
# ---------------------------------------------------------------------------


def test_explain_result_from_candidate_no_provenance() -> None:
    """ScoredSearchCandidate with graph_provenance=None → ExplainResult.graph_provenance is None."""
    c = _minimal_candidate(graph_provenance=None)
    result = ExplainResult.from_candidate(c)
    assert result.graph_provenance is None


def test_explain_result_from_candidate_with_provenance() -> None:
    """ScoredSearchCandidate with populated GraphProvenance → ExplainResult.graph_provenance matches."""
    step = TraversalStep(entity="Foo", entity_id="foo-001", relationship="CALLS")
    prov = GraphProvenance(steps=[step])
    c = _minimal_candidate(graph_provenance=prov)
    result = ExplainResult.from_candidate(c)
    assert result.graph_provenance is not None
    assert len(result.graph_provenance.steps) == 1
    assert result.graph_provenance.steps[0].entity == "Foo"
    assert result.graph_provenance.steps[0].entity_id == "foo-001"
    assert result.graph_provenance.steps[0].relationship == "CALLS"


def test_explain_result_from_candidate_empty_steps_preserved() -> None:
    """ScoredSearchCandidate with GraphProvenance(steps=[]) → ExplainResult.graph_provenance is not None;
    graph_provenance.steps == [] (empty list, NOT coerced to null) — S11."""
    prov = GraphProvenance(steps=[])
    c = _minimal_candidate(graph_provenance=prov)
    result = ExplainResult.from_candidate(c)
    assert result.graph_provenance is not None
    assert result.graph_provenance.steps == []


def test_explain_result_from_candidate_community_step() -> None:
    """Community-mode step with community_id → maps correctly."""
    step = TraversalStep(entity="Cluster", entity_id="clust-1", community_id="community-42")
    prov = GraphProvenance(steps=[step])
    c = _minimal_candidate(graph_provenance=prov)
    result = ExplainResult.from_candidate(c)
    assert result.graph_provenance is not None
    assert result.graph_provenance.steps[0].community_id == "community-42"


def test_explain_result_from_candidate_terminal_step() -> None:
    """Terminal chunk step with chunk_id → maps correctly."""
    step = TraversalStep(entity="Leaf", entity_id="leaf-1", chunk_id="chunk-abc")
    prov = GraphProvenance(steps=[step])
    c = _minimal_candidate(graph_provenance=prov)
    result = ExplainResult.from_candidate(c)
    assert result.graph_provenance is not None
    assert result.graph_provenance.steps[0].chunk_id == "chunk-abc"


def test_explain_result_from_candidate_degenerate_step_raises() -> None:
    """TraversalStep with all optional fields None → ValidationError from from_candidate().

    TraversalStep dataclass is constraint-free (no validation), but TraversalStepResponse
    enforces _at_least_one_optional_set. When from_candidate() calls
    GraphProvenanceResponse.from_provenance(), the Pydantic validator fires and raises
    ValidationError on degenerate steps (S10).
    """
    step = TraversalStep(entity="X", entity_id="x-1")  # relationship/community_id/chunk_id all None
    prov = GraphProvenance(steps=[step])
    c = _minimal_candidate(graph_provenance=prov)
    with pytest.raises(ValidationError):
        ExplainResult.from_candidate(c)


# ---------------------------------------------------------------------------
# Unit tests — ExplainResponse.graph_mode_applied field + from_pipeline_result()
# ---------------------------------------------------------------------------


def test_explain_response_graph_mode_applied_null() -> None:
    """ExplainResponse.from_pipeline_result with graph_mode_applied=None → field is None."""
    pr = _minimal_pipeline_result()
    resp = ExplainResponse.from_pipeline_result(
        rerank=True,
        collection="col",
        routing=None,
        result=pr,
        graph_mode_applied=None,
    )
    assert resp.graph_mode_applied is None


def test_explain_response_graph_mode_applied_naive() -> None:
    """ExplainResponse.from_pipeline_result with graph_mode_applied='naive' → field matches."""
    pr = _minimal_pipeline_result(graph_mode_applied="naive")
    resp = ExplainResponse.from_pipeline_result(
        rerank=True,
        collection="col",
        routing=None,
        result=pr,
        graph_mode_applied="naive",
    )
    assert resp.graph_mode_applied == "naive"


def test_explain_response_graph_mode_applied_local() -> None:
    """ExplainResponse.from_pipeline_result with graph_mode_applied='local' → field matches."""
    pr = _minimal_pipeline_result(graph_mode_applied="local")
    resp = ExplainResponse.from_pipeline_result(
        rerank=True,
        collection="col",
        routing=None,
        result=pr,
        graph_mode_applied="local",
    )
    assert resp.graph_mode_applied == "local"


def test_explain_response_graph_mode_applied_global() -> None:
    """ExplainResponse.from_pipeline_result with graph_mode_applied='global' → field matches."""
    pr = _minimal_pipeline_result(graph_mode_applied="global")
    resp = ExplainResponse.from_pipeline_result(
        rerank=True,
        collection="col",
        routing=None,
        result=pr,
        graph_mode_applied="global",
    )
    assert resp.graph_mode_applied == "global"


# ---------------------------------------------------------------------------
# Unit tests — ExplainPipelineResult.graph_mode_applied field
# ---------------------------------------------------------------------------


def test_explain_pipeline_result_graph_mode_applied_defaults_none() -> None:
    """ExplainPipelineResult.graph_mode_applied defaults to None."""
    pr = _minimal_pipeline_result()
    assert pr.graph_mode_applied is None


def test_explain_pipeline_result_graph_mode_applied_naive() -> None:
    """ExplainPipelineResult can hold graph_mode_applied='naive'."""
    pr = _minimal_pipeline_result(graph_mode_applied="naive")
    assert pr.graph_mode_applied == "naive"


# ---------------------------------------------------------------------------
# Integration tests — ExplainRequest schema validation
# ---------------------------------------------------------------------------


def test_explain_schemas_extra_forbid_graph_mode() -> None:
    """ExplainRequest rejects unknown fields per extra='forbid'."""
    with pytest.raises(ValidationError) as exc_info:
        ExplainRequest(query="test", unknown_field_xyz="value")  # type: ignore[call-arg]
    assert "unknown_field_xyz" in str(exc_info.value) or "extra" in str(exc_info.value).lower()


def test_explain_route_invalid_graph_mode_returns_422(tmp_path: Path) -> None:
    """POST /explain with graph_mode='NAIVE' (wrong case) → 422 (Pydantic validation error)."""
    app, client = _make_app(tmp_path)
    response = client.post(
        "/explain",
        json={
            "query": "test",
            "graph_mode": "NAIVE",
        },
    )
    assert response.status_code == 422

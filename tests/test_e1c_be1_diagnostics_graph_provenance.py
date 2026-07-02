"""BE-1: Tests for TraversalStep, GraphProvenance dataclasses and ScoredSearchCandidate.graph_provenance.

Tests target:
- ``archon_search._diagnostics.TraversalStep`` (dataclass)
- ``archon_search._diagnostics.GraphProvenance`` (dataclass)
- ``archon_search._diagnostics.ScoredSearchCandidate.graph_provenance`` field
- ``archon_search.server.routes_explain.TraversalStepResponse`` (Pydantic model with validator)
- ``archon_search.server.routes_explain.GraphProvenanceResponse`` (Pydantic model)

Unit tests cover S10 (all-null optionals rejected) and valid construction variants.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from archon_search._diagnostics import (
    GraphProvenance,
    ScoredSearchCandidate,
    SearchScoreBreakdown,
    TraversalStep,
)
from archon_search.server.routes_explain import (
    GraphProvenanceResponse,
    TraversalStepResponse,
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


# ---------------------------------------------------------------------------
# TraversalStepResponse — Pydantic model (validator lives here)
# ---------------------------------------------------------------------------


def test_traversal_step_valid_naive() -> None:
    """entity + relationship set, community_id/chunk_id null → valid."""
    step = TraversalStepResponse(
        entity="AuthService",
        entity_id="ent-abc123",
        relationship="CALLS",
        community_id=None,
        chunk_id=None,
    )
    assert step.entity == "AuthService"
    assert step.entity_id == "ent-abc123"
    assert step.relationship == "CALLS"
    assert step.community_id is None
    assert step.chunk_id is None


def test_traversal_step_valid_community() -> None:
    """entity + community_id set, relationship null → valid."""
    step = TraversalStepResponse(
        entity="OrderModule",
        entity_id="ent-def456",
        relationship=None,
        community_id="comm-001",
        chunk_id=None,
    )
    assert step.entity == "OrderModule"
    assert step.community_id == "comm-001"
    assert step.relationship is None
    assert step.chunk_id is None


def test_traversal_step_terminal_step() -> None:
    """entity + chunk_id set → valid (terminal/leaf step)."""
    step = TraversalStepResponse(
        entity="PaymentService",
        entity_id="ent-ghi789",
        relationship=None,
        community_id=None,
        chunk_id="chunk-payservice-001",
    )
    assert step.chunk_id == "chunk-payservice-001"
    assert step.relationship is None
    assert step.community_id is None


def test_traversal_step_all_null_optionals_rejected() -> None:
    """relationship/community_id/chunk_id all null → Pydantic ValidationError (S10)."""
    with pytest.raises(ValidationError):
        TraversalStepResponse(
            entity="SomeEntity",
            entity_id="ent-xyz",
            relationship=None,
            community_id=None,
            chunk_id=None,
        )


def test_traversal_step_all_optional_set_valid() -> None:
    """All optional fields set simultaneously → valid (no restriction on that)."""
    step = TraversalStepResponse(
        entity="ComboEntity",
        entity_id="ent-combo",
        relationship="DEPENDS_ON",
        community_id="comm-007",
        chunk_id="chunk-combo-001",
    )
    assert step.relationship == "DEPENDS_ON"
    assert step.community_id == "comm-007"
    assert step.chunk_id == "chunk-combo-001"


# ---------------------------------------------------------------------------
# GraphProvenanceResponse — Pydantic model
# ---------------------------------------------------------------------------


def test_graph_provenance_response_empty_steps() -> None:
    """GraphProvenanceResponse with empty steps list is valid (S11 — signals graph-layer bug)."""
    prov = GraphProvenanceResponse(steps=[])
    assert prov.steps == []


def test_graph_provenance_response_with_steps() -> None:
    """GraphProvenanceResponse with one valid step is valid."""
    step = TraversalStepResponse(
        entity="SomeEntity",
        entity_id="ent-001",
        relationship="USES",
        community_id=None,
        chunk_id=None,
    )
    prov = GraphProvenanceResponse(steps=[step])
    assert len(prov.steps) == 1
    assert prov.steps[0].entity == "SomeEntity"


# ---------------------------------------------------------------------------
# GraphProvenanceResponse.from_provenance() factory method
# ---------------------------------------------------------------------------


def test_from_provenance_happy_path_single_step() -> None:
    """from_provenance with one valid step maps all fields correctly."""
    step = TraversalStep(
        entity="AuthService",
        entity_id="ent-001",
        relationship="CALLS",
        community_id=None,
        chunk_id=None,
    )
    prov = GraphProvenance(steps=[step])
    result = GraphProvenanceResponse.from_provenance(prov)
    assert len(result.steps) == 1
    assert result.steps[0].entity == "AuthService"
    assert result.steps[0].entity_id == "ent-001"
    assert result.steps[0].relationship == "CALLS"
    assert result.steps[0].community_id is None
    assert result.steps[0].chunk_id is None


def test_from_provenance_empty_steps_preserved() -> None:
    """from_provenance with empty GraphProvenance.steps → empty GraphProvenanceResponse.steps (S11 via factory)."""
    prov = GraphProvenance(steps=[])
    result = GraphProvenanceResponse.from_provenance(prov)
    assert result.steps == []


def test_from_provenance_rejects_all_null_optional_step() -> None:
    """from_provenance propagates ValidationError when a TraversalStep has all-null optionals (S10 via factory)."""
    degenerate_step = TraversalStep(
        entity="SomeEntity",
        entity_id="ent-bad",
        relationship=None,
        community_id=None,
        chunk_id=None,
    )
    prov = GraphProvenance(steps=[degenerate_step])
    with pytest.raises(ValidationError):
        GraphProvenanceResponse.from_provenance(prov)


# ---------------------------------------------------------------------------
# TraversalStep dataclass (no Pydantic validation — pure data)
# ---------------------------------------------------------------------------


def test_traversal_step_dataclass_valid_construction() -> None:
    """TraversalStep dataclass constructs without validation errors."""
    step = TraversalStep(
        entity="SomeEntity",
        entity_id="ent-001",
        relationship="CALLS",
        community_id=None,
        chunk_id=None,
    )
    assert step.entity == "SomeEntity"
    assert step.relationship == "CALLS"


def test_traversal_step_dataclass_all_null_no_error() -> None:
    """TraversalStep dataclass does NOT enforce the optional constraint — that is Pydantic's job."""
    # Plain dataclass: no validation on creation. Validation happens at the Pydantic layer.
    step = TraversalStep(
        entity="SomeEntity",
        entity_id="ent-001",
        relationship=None,
        community_id=None,
        chunk_id=None,
    )
    # Construction succeeds — validation is deferred to TraversalStepResponse
    assert step.relationship is None
    assert step.community_id is None
    assert step.chunk_id is None


# ---------------------------------------------------------------------------
# GraphProvenance dataclass
# ---------------------------------------------------------------------------


def test_graph_provenance_dataclass_empty_steps() -> None:
    """GraphProvenance dataclass with empty steps is valid."""
    prov = GraphProvenance(steps=[])
    assert prov.steps == []


def test_graph_provenance_dataclass_with_steps() -> None:
    """GraphProvenance dataclass holds TraversalStep objects."""
    step = TraversalStep(
        entity="SomeEntity",
        entity_id="ent-001",
        relationship="CALLS",
        community_id=None,
        chunk_id=None,
    )
    prov = GraphProvenance(steps=[step])
    assert len(prov.steps) == 1
    assert prov.steps[0].entity == "SomeEntity"


# ---------------------------------------------------------------------------
# ScoredSearchCandidate.graph_provenance field
# ---------------------------------------------------------------------------


def test_scored_search_candidate_graph_provenance_defaults_none() -> None:
    """ScoredSearchCandidate.graph_provenance defaults to None."""
    cand = _minimal_candidate()
    assert cand.graph_provenance is None


def test_scored_search_candidate_accepts_graph_provenance() -> None:
    """ScoredSearchCandidate.graph_provenance stores a GraphProvenance instance."""
    step = TraversalStep(
        entity="AuthService",
        entity_id="ent-001",
        relationship="CALLS",
        community_id=None,
        chunk_id=None,
    )
    prov = GraphProvenance(steps=[step])
    cand = _minimal_candidate(graph_provenance=prov)
    assert cand.graph_provenance is not None
    assert len(cand.graph_provenance.steps) == 1


def test_scored_search_candidate_graph_provenance_accepts_empty_steps() -> None:
    """ScoredSearchCandidate.graph_provenance can hold a GraphProvenance with empty steps (S11)."""
    prov = GraphProvenance(steps=[])
    cand = _minimal_candidate(graph_provenance=prov)
    assert cand.graph_provenance is not None
    assert cand.graph_provenance.steps == []

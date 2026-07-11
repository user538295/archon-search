"""Unit tests for BE-7: PPR provenance in ExplainPipelineResult and ExplainResponse.

Covers:
- ExplainPipelineResult.ppr_entities_matched field exists and defaults to None
- graph_mode_applied="ppr" is accepted by ExplainPipelineResult
- ExplainResponse.from_pipeline_result correctly passes ppr_entities_matched
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_explainPipelineResult_pprLiteral_acceptsPpr() -> None:
    """ExplainPipelineResult accepts graph_mode_applied='ppr'."""
    from archon_search.pipeline import ExplainPipelineResult

    result = ExplainPipelineResult(
        top_results=[], near_misses=[], acl_filtered=False, graph_mode_applied="ppr"
    )
    assert result.graph_mode_applied == "ppr"


def test_explainPipelineResult_pprEntitiesMatched_defaultsToNone() -> None:
    """ExplainPipelineResult.ppr_entities_matched defaults to None."""
    from archon_search.pipeline import ExplainPipelineResult

    result = ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=False)
    assert result.ppr_entities_matched is None


def test_explainPipelineResult_pprEntitiesMatched_canBeSet() -> None:
    """ExplainPipelineResult.ppr_entities_matched can be set to an integer."""
    from archon_search.pipeline import ExplainPipelineResult

    result = ExplainPipelineResult(
        top_results=[], near_misses=[], acl_filtered=False, ppr_entities_matched=3
    )
    assert result.ppr_entities_matched == 3


def test_explainResponse_fromPipelineResult_pprFieldsPopulated() -> None:
    """ExplainResponse.from_pipeline_result passes ppr_entities_matched through."""
    from archon_search.pipeline import ExplainPipelineResult
    from archon_search.server.routes_explain import ExplainResponse

    pipeline_result = ExplainPipelineResult(
        top_results=[],
        near_misses=[],
        acl_filtered=False,
        graph_mode_applied="ppr",
        ppr_entities_matched=5,
    )
    response = ExplainResponse.from_pipeline_result(
        rerank=True,
        collection="test",
        routing=None,
        result=pipeline_result,
        graph_mode_applied="ppr",
        ppr_entities_matched=5,
    )
    assert response.ppr_entities_matched == 5
    assert response.graph_mode_applied == "ppr"


def test_explainResponse_fromPipelineResult_pprNoneByDefault() -> None:
    """ExplainResponse.from_pipeline_result leaves ppr_entities_matched=None when not passed."""
    from archon_search.pipeline import ExplainPipelineResult
    from archon_search.server.routes_explain import ExplainResponse

    pipeline_result = ExplainPipelineResult(
        top_results=[], near_misses=[], acl_filtered=False
    )
    response = ExplainResponse.from_pipeline_result(
        rerank=False,
        collection="test",
        routing=None,
        result=pipeline_result,
    )
    assert response.ppr_entities_matched is None
    assert response.graph_mode_applied is None

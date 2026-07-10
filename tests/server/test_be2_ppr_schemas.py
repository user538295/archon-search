"""BE-2 — TDD tests for 'ppr' graph_mode and ppr_entities_matched field.

Verifies:
- SearchRequest accepts graph_mode="ppr"
- ExplainRequest accepts graph_mode="ppr"
- SearchResponse has ppr_entities_matched: int | None field
- ExplainResponse has ppr_entities_matched: int | None field
- _VALID_GRAPH_MODES in mcp.py includes "ppr"
- search_with_context rejection message lists "ppr"
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from archon_search.server.routes_search import SearchRequest, SearchResponse
from archon_search.server.routes_explain import ExplainRequest, ExplainResponse


# ---------------------------------------------------------------------------
# SearchRequest
# ---------------------------------------------------------------------------


def test_searchRequest_pprMode_acceptedByPydantic() -> None:
    """SearchRequest(query='q', collection='c', graph_mode='ppr') validates without error."""
    req = SearchRequest(query="q", collection="col", graph_mode="ppr")
    assert req.graph_mode == "ppr"


def test_searchRequest_invalidMode_rejected() -> None:
    """SearchRequest rejects an unknown graph_mode value."""
    with pytest.raises(ValidationError):
        SearchRequest(query="q", collection="col", graph_mode="invalid_mode")


# ---------------------------------------------------------------------------
# ExplainRequest
# ---------------------------------------------------------------------------


def test_explainRequest_pprMode_acceptedByPydantic() -> None:
    """ExplainRequest(query='q', graph_mode='ppr') validates without error."""
    req = ExplainRequest(query="q", collection="col", graph_mode="ppr")
    assert req.graph_mode == "ppr"


# ---------------------------------------------------------------------------
# SearchResponse
# ---------------------------------------------------------------------------


def test_searchResponse_pprEntitiesMatched_field_isOptionalInt() -> None:
    """SearchResponse.model_fields includes ppr_entities_matched as optional int."""
    assert "ppr_entities_matched" in SearchResponse.model_fields
    # Default must be None
    instance = SearchResponse(results=[], acl_filtered=False)
    assert instance.ppr_entities_matched is None


def test_searchResponse_pprEntitiesMatched_acceptsInt() -> None:
    """SearchResponse accepts ppr_entities_matched as an integer."""
    instance = SearchResponse(results=[], acl_filtered=False, ppr_entities_matched=5)
    assert instance.ppr_entities_matched == 5


# ---------------------------------------------------------------------------
# ExplainResponse
# ---------------------------------------------------------------------------


def test_explainResponse_pprEntitiesMatched_field_isOptionalInt() -> None:
    """ExplainResponse.model_fields includes ppr_entities_matched as optional int."""
    assert "ppr_entities_matched" in ExplainResponse.model_fields
    instance = ExplainResponse.from_pipeline_result(
        rerank=False,
        collection="col",
        routing=None,
        result=_make_explain_result(),
    )
    assert instance.ppr_entities_matched is None


def test_explainResponse_pprEntitiesMatched_acceptsInt() -> None:
    """ExplainResponse accepts ppr_entities_matched as an integer."""
    instance = ExplainResponse(
        rerank=False,
        routing=None,
        acl_filtered=False,
        results=[],
        near_misses=[],
    )
    instance2 = instance.model_copy(update={"ppr_entities_matched": 3})
    assert instance2.ppr_entities_matched == 3


# ---------------------------------------------------------------------------
# MCP _VALID_GRAPH_MODES
# ---------------------------------------------------------------------------


def test_mcpValidModes_includesPpr() -> None:
    """_VALID_GRAPH_MODES in mcp.py includes 'ppr'."""
    from archon_search.server.mcp import _VALID_GRAPH_MODES
    assert "ppr" in _VALID_GRAPH_MODES


def test_mcpSearchWithContext_rejectionMessage_includesPpr() -> None:
    """search_with_context rejection message for graph_mode lists 'ppr' explicitly."""
    import inspect
    from archon_search.server import mcp as mcp_module
    source = inspect.getsource(mcp_module)
    # The specific rejection string at the search_with_context guard must include 'ppr'
    expected = "graph_mode (naive, local, global, ppr) on search_with_context is not supported"
    assert expected in source


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_explain_result():
    """Return a minimal ExplainPipelineResult-like object for from_pipeline_result."""
    from archon_search.pipeline import ExplainPipelineResult
    return ExplainPipelineResult(
        top_results=[],
        near_misses=[],
        acl_filtered=False,
    )

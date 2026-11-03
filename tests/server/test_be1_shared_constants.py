"""BE-1 — the shared graph-mode set, ACL-source set and search timeout live once in ``constants``."""
from __future__ import annotations

import inspect
from typing import get_args

import pytest
from pydantic import ValidationError

from archon_search import constants
from archon_search._types import SearchResult
from archon_search.server import mcp, routes_explain, routes_search
from archon_search.server.routes_explain import ExplainRequest, ExplainResult
from archon_search.server.routes_search import SearchRequest
from archon_search.services import search_budget, search_service

_REST_GRAPH_MODE_ENUM = ["naive", "local", "global", "ppr"]


def test_valid_graph_modes_are_the_graph_mode_alias_values() -> None:
    assert list(constants.VALID_GRAPH_MODES) == _REST_GRAPH_MODE_ENUM
    assert get_args(constants.GraphMode) == tuple(constants.VALID_GRAPH_MODES)


@pytest.mark.parametrize("mode", constants.VALID_GRAPH_MODES)
def test_every_shared_graph_mode_is_accepted_by_search_and_explain_requests(mode: str) -> None:
    assert SearchRequest(query="q", collection="docs", graph_mode=mode).graph_mode == mode
    assert ExplainRequest(query="q", collection="docs", graph_mode=mode).graph_mode == mode


def test_unknown_graph_mode_is_rejected_by_search_and_explain_requests() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="q", collection="docs", graph_mode="bogus")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ExplainRequest(query="q", collection="docs", graph_mode="bogus")  # type: ignore[arg-type]


def test_mcp_validates_graph_mode_against_the_shared_set() -> None:
    assert mcp.VALID_GRAPH_MODES is constants.VALID_GRAPH_MODES
    assert not hasattr(mcp, "_VALID_GRAPH_MODES")


def test_unknown_acl_source_is_sanitised_with_the_shared_valid_set() -> None:
    assert constants.VALID_ACL_SOURCES == frozenset({"frontmatter", "sidecar", "collection_default"})
    assert not hasattr(routes_search, "_VALID_ACL_SOURCES")
    assert not hasattr(routes_explain, "_VALID_ACL_SOURCES")

    result = SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000001",
        text="t",
        score=0.5,
        source_path="/doc.md",
        acl_source="legacy_value",
    )
    assert search_service._build_acl_gate(result).source is None

    from archon_search._diagnostics import ScoredSearchCandidate
    from tests.server.test_routes_explain_schemas import _breakdown

    candidate = ScoredSearchCandidate(
        doc_id="d",
        chunk_id="d-000000",
        text="t",
        source_path="/d.md",
        score_breakdown=_breakdown(),
        collection="docs",
        acl=None,
        acl_source="legacy_value",
        acl_sidecar_path=None,
        acl_warning=[],
    )
    assert ExplainResult.from_candidate(candidate).acl_gate.source is None


def test_search_budget_default_timeout_is_the_constants_value() -> None:
    default = inspect.signature(search_budget.run_within_budget).parameters["timeout"].default
    assert default is constants.SEARCH_TIMEOUT_SECONDS
    assert search_budget.SEARCH_TIMEOUT_SECONDS is constants.SEARCH_TIMEOUT_SECONDS


def test_rest_openapi_graph_mode_enum_stays_naive_local_global_ppr() -> None:
    for model in (SearchRequest, ExplainRequest):
        schema = model.model_json_schema()["properties"]["graph_mode"]
        enum = next(branch["enum"] for branch in schema["anyOf"] if "enum" in branch)
        assert enum == _REST_GRAPH_MODE_ENUM


def test_acl_gate_wire_enum_matches_the_shared_valid_acl_sources() -> None:
    from archon_search.server.schemas import AclGateSchema

    source = AclGateSchema.model_json_schema()["properties"]["source"]
    enum = next(branch["enum"] for branch in source["anyOf"] if "enum" in branch)
    assert set(enum) == constants.VALID_ACL_SOURCES

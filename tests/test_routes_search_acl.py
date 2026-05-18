"""Tests for SearchResponse schema and ACL field isolation (Task 3.3)."""
from __future__ import annotations

from archon_search.server.routes_search import SearchResponse, SearchResultSchema


def test_search_response_schema_fields() -> None:
    result = SearchResponse(results=[], acl_filtered=False).model_dump()
    assert result == {"results": [], "acl_filtered": False}


def test_search_result_schema_no_acl_field() -> None:
    assert "acl" not in SearchResultSchema.model_fields


def test_search_response_is_never_bare_array() -> None:
    keys = set(SearchResponse.model_fields.keys())
    assert "results" in keys
    assert "acl_filtered" in keys

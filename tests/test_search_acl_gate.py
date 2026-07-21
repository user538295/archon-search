"""Unit tests for AclGateSchema, acl_context on SearchRequest, and acl_gate on SearchResultSchema (g15 BE-5)."""
from __future__ import annotations

import pytest

from archon_search._types import SearchResult
from archon_search.server.routes_search import SearchRequest, SearchResultSchema
from archon_search.server.schemas import AclGateSchema


# ---------------------------------------------------------------------------
# AclGateSchema field/type tests
# ---------------------------------------------------------------------------


def test_acl_gate_schema_fields() -> None:
    """AclGateSchema has all four required fields with correct types."""
    fields = AclGateSchema.model_fields
    assert "allowed_principals" in fields
    assert "source" in fields
    assert "sidecar_path" in fields
    assert "warnings" in fields

    # warnings is always a non-null list (default_factory)
    gate = AclGateSchema()
    assert gate.warnings == []
    assert gate.allowed_principals is None
    assert gate.source is None
    assert gate.sidecar_path is None

    # source accepts the three valid literal values
    for v in ("frontmatter", "sidecar", "collection_default"):
        g = AclGateSchema(source=v)
        assert g.source == v


def test_acl_gate_schema_warnings_always_non_null() -> None:
    """warnings field is always a list, never None, even when not supplied."""
    gate = AclGateSchema(allowed_principals=["ns-a"], source="frontmatter", sidecar_path=None)
    assert gate.warnings == []
    assert isinstance(gate.warnings, list)


def test_acl_gate_schema_full_round_trip() -> None:
    """AclGateSchema round-trips through model_dump() with all fields."""
    gate = AclGateSchema(
        allowed_principals=["ns-a", "ns-b"],
        source="sidecar",
        sidecar_path="/path/to/file.acl",
        warnings=["truncated sidecar"],
    )
    dumped = gate.model_dump()
    assert dumped == {
        "allowed_principals": ["ns-a", "ns-b"],
        "source": "sidecar",
        "sidecar_path": "/path/to/file.acl",
        "warnings": ["truncated sidecar"],
    }


# ---------------------------------------------------------------------------
# SearchRequest.acl_context default
# ---------------------------------------------------------------------------


def test_search_request_acl_context_default_false() -> None:
    """SearchRequest.acl_context defaults to False when not supplied."""
    req = SearchRequest(query="hello", collection="docs")
    assert req.acl_context is False


def test_search_request_acl_context_explicit_true() -> None:
    """SearchRequest.acl_context can be set to True."""
    req = SearchRequest(query="hello", collection="docs", acl_context=True)
    assert req.acl_context is True


# ---------------------------------------------------------------------------
# SearchResultSchema.acl_gate
# ---------------------------------------------------------------------------


def test_search_result_schema_acl_gate_absent_by_default() -> None:
    """SearchResultSchema.acl_gate is None when not built (default)."""
    r = SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000001",
        text="hello",
        score=0.9,
        source_path="/doc.md",
    )
    schema = SearchResultSchema.from_result(r, include_acl_gate=False)
    assert schema.acl_gate is None


def test_search_result_schema_acl_gate_built_when_requested() -> None:
    """SearchResultSchema.acl_gate is populated when include_acl_gate=True."""
    r = SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000001",
        text="hello",
        score=0.9,
        source_path="/doc.md",
        acl=["ns-a"],
        acl_source="frontmatter",
        acl_sidecar_path=None,
        acl_warning=[],
    )
    schema = SearchResultSchema.from_result(r, include_acl_gate=True)
    assert schema.acl_gate is not None
    assert schema.acl_gate.allowed_principals == ["ns-a"]
    assert schema.acl_gate.source == "frontmatter"
    assert schema.acl_gate.sidecar_path is None
    assert schema.acl_gate.warnings == []


def test_search_result_schema_acl_gate_warnings_always_list() -> None:
    """acl_gate.warnings is always a list, never None, even when acl_warning is empty."""
    r = SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000001",
        text="hello",
        score=0.9,
        source_path="/doc.md",
        acl_warning=[],
    )
    schema = SearchResultSchema.from_result(r, include_acl_gate=True)
    assert schema.acl_gate is not None
    assert isinstance(schema.acl_gate.warnings, list)


def test_search_result_schema_acl_source_unknown_coerced_to_none() -> None:
    """An unrecognized acl_source value is coerced to None (no 500 on bad DB data)."""
    r = SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000001",
        text="hello",
        score=0.9,
        source_path="/doc.md",
        acl_source="legacy_value",  # out-of-enum
        acl_warning=[],
    )
    schema = SearchResultSchema.from_result(r, include_acl_gate=True)
    assert schema.acl_gate is not None
    assert schema.acl_gate.source is None  # coerced, not raised

"""Unit tests for AclGateSchema, acl_context on SearchRequest, and acl_gate on SearchResultSchema (g15 BE-5)."""
from __future__ import annotations

from archon_search._types import SearchResult
from archon_search.server.routes_search import SearchRequest, SearchResultSchema
from archon_search.server.schemas import AclGateSchema
from archon_search.services.search_service import AclGate, SearchHit

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
    schema = SearchResultSchema.from_result(r)
    assert schema.acl_gate is None


def test_search_result_schema_from_hit_carries_the_gate_the_service_built() -> None:
    """from_hit renders the service's AclGate as the wire acl_gate; a hit without one has none."""
    r = SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000001",
        text="hello",
        score=0.9,
        source_path="/doc.md",
    )
    gate = AclGate(allowed_principals=["ns-a"], source="sidecar", sidecar_path="doc.md.acl", warnings=["w"])

    with_gate = SearchResultSchema.from_hit(SearchHit(result=r, acl_gate=gate))
    without_gate = SearchResultSchema.from_hit(SearchHit(result=r, acl_gate=None))

    assert with_gate.acl_gate == AclGateSchema(
        allowed_principals=["ns-a"], source="sidecar", sidecar_path="doc.md.acl", warnings=["w"]
    )
    assert without_gate.acl_gate is None

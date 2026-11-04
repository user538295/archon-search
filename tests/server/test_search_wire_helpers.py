"""The two steps REST and MCP share when they render a search outcome: the ACL gate and the ``metadata`` blanking."""
from __future__ import annotations

import pytest

from archon_search._types import SearchResult
from archon_search.server.mcp_schemas import McpSearchToolResultSchema
from archon_search.server.routes_search import SearchResultSchema
from archon_search.server.schemas import AclGateSchema, blank_unrequested_metadata
from archon_search.services.search_service import AclGate

_METADATA = {"author": "ada", "topic": "search"}


def _result() -> SearchResult:
    return SearchResult(doc_id="d", chunk_id="c", text="t", score=0.5, source_path="/p", metadata=dict(_METADATA))


def test_from_gate_without_a_gate_is_none() -> None:
    assert AclGateSchema.from_gate(None) is None


def test_from_gate_carries_every_field_of_the_service_gate() -> None:
    gate = AclGate(allowed_principals=["team-a"], source="sidecar", sidecar_path="/docs/a.acl", warnings=["unusable"])

    schema = AclGateSchema.from_gate(gate)

    assert schema == AclGateSchema(
        allowed_principals=["team-a"], source="sidecar", sidecar_path="/docs/a.acl", warnings=["unusable"]
    )


def test_from_gate_keeps_open_to_all_distinct_from_deny_all() -> None:
    open_to_all = AclGateSchema.from_gate(AclGate(None, "collection_default", None, []))
    deny_all = AclGateSchema.from_gate(AclGate([], "sidecar", "/x.acl", ["unusable"]))

    assert open_to_all is not None and open_to_all.allowed_principals is None
    assert deny_all is not None and deny_all.allowed_principals == []


@pytest.mark.parametrize("schema_class", [SearchResultSchema, McpSearchToolResultSchema])
def test_metadata_is_blanked_when_it_was_not_requested(schema_class: type) -> None:
    items = [schema_class.from_result(_result()) for _ in range(2)]

    blank_unrequested_metadata(items, include_metadata=False)

    assert [item.metadata for item in items] == [{}, {}]


@pytest.mark.parametrize("schema_class", [SearchResultSchema, McpSearchToolResultSchema])
def test_metadata_is_kept_when_it_was_requested(schema_class: type) -> None:
    items = [schema_class.from_result(_result())]

    blank_unrequested_metadata(items, include_metadata=True)

    assert items[0].metadata == _METADATA

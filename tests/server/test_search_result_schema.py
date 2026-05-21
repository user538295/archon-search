"""Tests pinning ``SearchResultSchema`` field parity with ``SearchResult``
and the ACL-preservation contract.

Implements Task 4.2 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from archon_search._types import SearchResult
from archon_search.server.routes_search import SearchResultSchema


def test_search_result_schema_contains_every_search_result_field() -> None:
    """Every SearchResult field must be present on the schema (one-direction
    subset; the schema may carry additional REST-only fields)."""
    sr_fields = {f.name for f in dataclasses.fields(SearchResult)}
    schema_fields = set(SearchResultSchema.model_fields.keys())
    missing = sr_fields - schema_fields
    assert not missing, f"SearchResultSchema missing fields: {missing}"


@pytest.mark.parametrize("acl", [None, [], ["team-a"], ["team-a", "team-b"]])
def test_search_result_schema_from_result_preserves_acl_none_and_empty_list(acl):
    r = SearchResult(
        doc_id="d",
        chunk_id="c",
        text="t",
        score=0.5,
        source_path="/tmp/x.md",
        acl=acl,
    )
    schema = SearchResultSchema.from_result(r)
    assert schema.acl == acl


def test_search_result_schema_includes_metadata() -> None:
    r = SearchResult(
        doc_id="d",
        chunk_id="c",
        text="t",
        score=0.5,
        source_path="/tmp/x.md",
        metadata={"k": "v"},
    )
    schema = SearchResultSchema.from_result(r)
    assert schema.metadata == {"k": "v"}


def test_search_result_schema_serializes_new_fields_to_json() -> None:
    r = SearchResult(
        doc_id="d",
        chunk_id="c",
        text="t",
        score=0.5,
        source_path="/tmp/x.md",
        file_type="md",
        indexed_at="2026-05-21T10:00:00+00:00",
        updated_at="2026-05-21T11:00:00+00:00",
        ingested_by="cli",
        metadata={"a": "b"},
        acl=["team-a"],
    )
    payload = json.loads(SearchResultSchema.from_result(r).model_dump_json())
    for key in ("file_type", "indexed_at", "updated_at", "ingested_by", "metadata", "acl"):
        assert key in payload, f"missing key in JSON: {key}"
    assert payload["file_type"] == "md"
    assert payload["ingested_by"] == "cli"
    assert payload["metadata"] == {"a": "b"}
    assert payload["acl"] == ["team-a"]

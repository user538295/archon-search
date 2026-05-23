"""Tests pinning the ``SearchResult`` dataclass field set.

Implements Task 4.1 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

import dataclasses

from archon_search._types import SearchResult


_EXPECTED_FIELDS = {
    "doc_id",
    "chunk_id",
    "text",
    "score",
    "source_path",
    "file_type",
    "indexed_at",
    "updated_at",
    "ingested_by",
    "metadata",
    "language",
    "acl",
}


def test_search_result_field_set() -> None:
    names = {f.name for f in dataclasses.fields(SearchResult)}
    assert names == _EXPECTED_FIELDS


def test_search_result_has_language() -> None:
    """language is added in A2 (reserved; populated by C2 language detector)."""
    names = {f.name for f in dataclasses.fields(SearchResult)}
    assert "language" in names


def test_search_result_does_not_have_custom_score() -> None:
    names = {f.name for f in dataclasses.fields(SearchResult)}
    assert "custom_score" not in names


def test_search_result_does_not_have_vector() -> None:
    names = {f.name for f in dataclasses.fields(SearchResult)}
    assert "vector" not in names

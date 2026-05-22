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
    "language",  # A2 addition (extractor lands in C2)
    "indexed_at",
    "updated_at",
    "ingested_by",
    "metadata",
    "acl",
}


def test_search_result_field_set() -> None:
    names = {f.name for f in dataclasses.fields(SearchResult)}
    assert names == _EXPECTED_FIELDS


def test_search_result_has_language() -> None:
    """A2 adds language to SearchResult (extractor lands in C2)."""
    names = {f.name for f in dataclasses.fields(SearchResult)}
    assert "language" in names


def test_search_result_does_not_have_custom_score() -> None:
    names = {f.name for f in dataclasses.fields(SearchResult)}
    assert "custom_score" not in names


def test_search_result_does_not_have_vector() -> None:
    names = {f.name for f in dataclasses.fields(SearchResult)}
    assert "vector" not in names

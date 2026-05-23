"""Unit tests for SearchFilters validation (A2)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from archon_search.filters import SearchFilters


def test_empty_filters_is_valid() -> None:
    """SearchFilters() with no args creates a valid instance with all defaults."""
    f = SearchFilters()
    assert f.file_type is None
    assert f.source_path_prefix is None
    assert f.source_path_glob is None
    assert f.indexed_after is None
    assert f.indexed_before is None
    assert f.language is None
    assert f.include_metadata is False


def test_file_type_strips_leading_dot_and_lowercases() -> None:
    """file_type='.MD' → 'md' (dot stripped, lowercased)."""
    f = SearchFilters(file_type=".MD")
    assert f.file_type == "md"


def test_file_type_empty_string_raises() -> None:
    """file_type='' (or '.') → ValidationError."""
    with pytest.raises(ValidationError, match="file_type must not be empty"):
        SearchFilters(file_type=".")


def test_file_type_none_is_valid() -> None:
    f = SearchFilters(file_type=None)
    assert f.file_type is None


def test_source_path_prefix_empty_raises() -> None:
    """source_path_prefix='' → ValidationError."""
    with pytest.raises(ValidationError, match="source_path_prefix must not be empty"):
        SearchFilters(source_path_prefix="")


def test_source_path_glob_empty_raises() -> None:
    """source_path_glob='' → ValidationError."""
    with pytest.raises(ValidationError, match="source_path_glob must not be empty"):
        SearchFilters(source_path_glob="")


def test_source_path_glob_valid_pattern() -> None:
    """Valid glob patterns are accepted."""
    f = SearchFilters(source_path_glob="/docs/**/*.md")
    assert f.source_path_glob == "/docs/**/*.md"


def test_language_nonempty_raises() -> None:
    """language='en' → ValidationError (reserved, not yet supported)."""
    with pytest.raises(ValidationError, match="language filtering not yet supported"):
        SearchFilters(language="en")


def test_language_none_is_valid() -> None:
    f = SearchFilters(language=None)
    assert f.language is None


def test_indexed_after_before_reversed_raises() -> None:
    """indexed_after > indexed_before → ValidationError."""
    after = datetime(2026, 6, 1, tzinfo=timezone.utc)
    before = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValidationError, match="indexed_after must be <= indexed_before"):
        SearchFilters(indexed_after=after, indexed_before=before)


def test_indexed_after_equals_before_is_valid() -> None:
    """indexed_after == indexed_before is allowed."""
    dt = datetime(2026, 3, 15, tzinfo=timezone.utc)
    f = SearchFilters(indexed_after=dt, indexed_before=dt)
    assert f.indexed_after == f.indexed_before


def test_naive_datetime_coerced_to_utc() -> None:
    """Naive datetime in indexed_after/indexed_before is treated as UTC."""
    naive = datetime(2026, 1, 1)
    f = SearchFilters(indexed_after=naive)
    assert f.indexed_after is not None
    assert f.indexed_after.tzinfo == timezone.utc


def test_extra_field_raises() -> None:
    """Unknown fields raise ValidationError (extra='forbid')."""
    with pytest.raises(ValidationError):
        SearchFilters(**{"unknown_field": "value"})  # type: ignore[arg-type]


def test_include_metadata_defaults_false() -> None:
    f = SearchFilters()
    assert f.include_metadata is False


def test_include_metadata_true() -> None:
    f = SearchFilters(include_metadata=True)
    assert f.include_metadata is True

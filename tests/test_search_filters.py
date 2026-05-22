"""Tests for SearchFilters Pydantic model (archon_search/filters.py)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from archon_search.filters import SearchFilters


def test_defaults_all_none_or_false():
    f = SearchFilters()
    assert f.file_type is None
    assert f.source_path_prefix is None
    assert f.source_path_glob is None
    assert f.indexed_after is None
    assert f.indexed_before is None
    assert f.language is None
    assert f.include_metadata is False


def test_file_type_strip_leading_dot_and_lowercase():
    f = SearchFilters(file_type=".MD")
    assert f.file_type == "md"


def test_file_type_empty_string_rejected():
    with pytest.raises(ValidationError):
        SearchFilters(file_type="")


def test_source_path_prefix_empty_rejected():
    with pytest.raises(ValidationError):
        SearchFilters(source_path_prefix="")


def test_source_path_glob_empty_rejected():
    with pytest.raises(ValidationError):
        SearchFilters(source_path_glob="")


def test_indexed_after_naive_treated_as_utc():
    naive_dt = datetime(2025, 1, 15, 10, 30, 0)
    f = SearchFilters(indexed_after=naive_dt)
    assert f.indexed_after is not None
    assert f.indexed_after.tzinfo == timezone.utc


def test_indexed_after_date_only_coerced_to_start_of_day():
    d = date(2025, 3, 10)
    f = SearchFilters(indexed_after=d)
    assert f.indexed_after == datetime(2025, 3, 10, 0, 0, 0, 0, tzinfo=timezone.utc)


def test_indexed_before_date_only_coerced_to_end_of_day():
    d = date(2025, 3, 10)
    f = SearchFilters(indexed_before=d)
    assert f.indexed_before == datetime(2025, 3, 10, 23, 59, 59, 999999, tzinfo=timezone.utc)


def test_indexed_after_greater_than_indexed_before_rejected():
    with pytest.raises(ValidationError):
        SearchFilters(
            indexed_after=datetime(2025, 5, 20, tzinfo=timezone.utc),
            indexed_before=datetime(2025, 5, 10, tzinfo=timezone.utc),
        )


def test_language_non_empty_rejected_references_c2():
    with pytest.raises(ValidationError) as exc_info:
        SearchFilters(language="en")
    assert "C2" in str(exc_info.value)


def test_extra_field_rejected():
    with pytest.raises(ValidationError):
        SearchFilters(unknown_field="oops")


# --- Bug 1: file_type="." ---

def test_file_type_dot_only_rejected():
    with pytest.raises(ValidationError):
        SearchFilters(file_type=".")


def test_file_type_multi_dots_stripped():
    f = SearchFilters(file_type="..md")
    assert f.file_type == "md"


# --- Bug 2: JSON date strings for indexed_before / indexed_after ---

def test_indexed_before_json_date_string_coerced_to_end_of_day():
    f = SearchFilters.model_validate({"indexed_before": "2025-03-10"})
    assert f.indexed_before == datetime(2025, 3, 10, 23, 59, 59, 999999, tzinfo=timezone.utc)


def test_indexed_after_json_date_string_coerced_to_start_of_day():
    f = SearchFilters.model_validate({"indexed_after": "2025-03-10"})
    assert f.indexed_after == datetime(2025, 3, 10, 0, 0, 0, 0, tzinfo=timezone.utc)


def test_indexed_after_equals_indexed_before_accepted():
    dt = datetime(2025, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
    f = SearchFilters(indexed_after=dt, indexed_before=dt)
    assert f.indexed_after == f.indexed_before


def test_indexed_before_naive_treated_as_utc():
    naive_dt = datetime(2025, 1, 15, 10, 30, 0)
    f = SearchFilters(indexed_before=naive_dt)
    assert f.indexed_before is not None
    assert f.indexed_before.tzinfo == timezone.utc


# --- Bug 3: language="" ---

def test_language_empty_string_treated_as_none():
    f = SearchFilters(language="")
    assert f.language is None


# --- Coverage gaps ---

def test_include_metadata_true_accepted():
    f = SearchFilters(include_metadata=True)
    assert f.include_metadata is True


def test_source_path_glob_valid_pattern_accepted():
    f = SearchFilters(source_path_glob="*.md")
    assert f.source_path_glob == "*.md"


def test_source_path_prefix_valid_accepted():
    f = SearchFilters(source_path_prefix="/docs/")
    assert f.source_path_prefix == "/docs/"


def test_both_indexed_as_date_objects_same_day():
    d = date(2025, 3, 10)
    f = SearchFilters(indexed_after=d, indexed_before=d)
    assert f.indexed_after == datetime(2025, 3, 10, 0, 0, 0, 0, tzinfo=timezone.utc)
    assert f.indexed_before == datetime(2025, 3, 10, 23, 59, 59, 999999, tzinfo=timezone.utc)

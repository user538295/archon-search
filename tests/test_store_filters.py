"""Unit tests for store_filters pure functions (A2)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from archon_search.store_filters import (
    GLOB_OVERFETCH_FACTOR,
    _compute_fetch,
    _sql_quote_str,
    build_where,
    escape_like,
)
from archon_search.filters import SearchFilters


# ---------------------------------------------------------------------------
# _sql_quote_str
# ---------------------------------------------------------------------------


def test_sql_quote_str_wraps_in_single_quotes() -> None:
    assert _sql_quote_str("hello") == "'hello'"


def test_sql_quote_str_doubles_embedded_single_quote() -> None:
    """Single quote inside the string must be doubled (SQL escaping)."""
    assert _sql_quote_str("it's") == "'it''s'"


def test_sql_quote_str_empty_string() -> None:
    assert _sql_quote_str("") == "''"


def test_sql_quote_str_multiple_quotes() -> None:
    assert _sql_quote_str("a'b'c") == "'a''b''c'"


# ---------------------------------------------------------------------------
# escape_like
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # (input, expected output after LIKE escaping)
        ("/docs/file.md", "/docs/file.md"),          # no metacharacters → unchanged
        ("/tmp/100% done", "/tmp/100\\% done"),       # % → \%
        ("/tmp/under_score", "/tmp/under\\_score"),   # _ → \_
        ("/path\\to\\file", "/path\\\\to\\\\file"),   # backslash → \\
        ("/tmp/%_\\all", "/tmp/\\%\\_\\\\all"),       # all three metacharacters
    ],
)
def test_escape_like_parametrized(raw: str, expected: str) -> None:
    assert escape_like(raw) == expected


# ---------------------------------------------------------------------------
# build_where
# ---------------------------------------------------------------------------


def test_build_where_empty_filters_returns_empty_string() -> None:
    f = SearchFilters()
    assert build_where(f) == ""


def test_build_where_file_type_produces_equality_clause() -> None:
    f = SearchFilters(file_type="md")
    pred = build_where(f)
    assert "file_type = 'md'" in pred


def test_build_where_source_path_prefix_produces_like_clause() -> None:
    f = SearchFilters(source_path_prefix="/docs/")
    pred = build_where(f)
    assert "source_path LIKE '/docs/%'" in pred
    assert "ESCAPE" in pred


def test_build_where_indexed_after_produces_gte_clause() -> None:
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    f = SearchFilters(indexed_after=dt)
    pred = build_where(f)
    assert "indexed_at >=" in pred
    assert "2026-01-01" in pred


def test_build_where_indexed_before_produces_lte_clause() -> None:
    dt = datetime(2026, 12, 31, tzinfo=timezone.utc)
    f = SearchFilters(indexed_before=dt)
    pred = build_where(f)
    assert "indexed_at <=" in pred
    assert "2026-12-31" in pred


def test_build_where_multiple_filters_joined_by_and() -> None:
    f = SearchFilters(file_type="py", source_path_prefix="/src/")
    pred = build_where(f)
    assert " AND " in pred
    assert "file_type" in pred
    assert "source_path" in pred


def test_build_where_glob_not_emitted_as_sql() -> None:
    """source_path_glob is post-RRF only — must NOT appear in the SQL predicate."""
    f = SearchFilters(source_path_glob="/docs/**/*.md")
    pred = build_where(f)
    assert pred == ""


def test_build_where_include_metadata_not_emitted_as_sql() -> None:
    """include_metadata is a response-shaping flag — must NOT appear in SQL."""
    f = SearchFilters(include_metadata=True)
    pred = build_where(f)
    assert pred == ""


def test_build_where_prefix_with_special_chars_escaped() -> None:
    """LIKE metacharacters in source_path_prefix are escaped."""
    f = SearchFilters(source_path_prefix="/tmp/100%_done")
    pred = build_where(f)
    assert "\\%" in pred
    assert "\\_" in pred


# ---------------------------------------------------------------------------
# _compute_fetch
# ---------------------------------------------------------------------------


def test_compute_fetch_no_glob_uses_3x_multiplier() -> None:
    assert _compute_fetch(5, has_glob=False) == 20  # max(15, 20) = 20


def test_compute_fetch_no_glob_floor_is_20() -> None:
    assert _compute_fetch(1, has_glob=False) == 20


def test_compute_fetch_with_glob_uses_factor() -> None:
    result = _compute_fetch(20, has_glob=True)
    assert result == max(20 * GLOB_OVERFETCH_FACTOR, 60)


def test_compute_fetch_with_glob_floor_is_60() -> None:
    assert _compute_fetch(1, has_glob=True) == 60


def test_compute_fetch_large_top_k_no_glob() -> None:
    assert _compute_fetch(100, has_glob=False) == 300

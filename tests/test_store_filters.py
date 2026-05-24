"""Tests for archon_search/store_filters.py.

TDD: all tests written before implementation.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from archon_search.filters import SearchFilters


# ---------------------------------------------------------------------------
# LIKE simulator (oracle for Hypothesis property test)
# ---------------------------------------------------------------------------

def _sql_like_match(pattern: str, s: str, escape: str = "\\") -> bool:
    """Simulate SQL LIKE with a backslash escape char."""
    regex_parts = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == escape and i + 1 < len(pattern):
            # next char is literal
            regex_parts.append(re.escape(pattern[i + 1]))
            i += 2
        elif c == "%":
            regex_parts.append(".*")
            i += 1
        elif c == "_":
            regex_parts.append(".")
            i += 1
        else:
            regex_parts.append(re.escape(c))
            i += 1
    return bool(re.fullmatch("".join(regex_parts), s))


# ---------------------------------------------------------------------------
# Tests for _sql_quote_str
# ---------------------------------------------------------------------------

def test_sql_quote_str_doubles_internal_single_quotes():
    from archon_search.store_filters import _sql_quote_str
    assert _sql_quote_str("O'Reilly") == "'O''Reilly'"


def test_sql_quote_str_wraps_plain_string():
    from archon_search.store_filters import _sql_quote_str
    assert _sql_quote_str("abc") == "'abc'"


# ---------------------------------------------------------------------------
# Tests for escape_like
# ---------------------------------------------------------------------------

def test_escape_like_percent_underscore_backslash():
    from archon_search.store_filters import escape_like
    assert escape_like("%") == r"\%"
    assert escape_like("_") == r"\_"
    assert escape_like("\\") == "\\\\"


def test_like_simulator_hand_verified_cases():
    cases = [
        ("a%b", "acb", True),        # % matches 1 char
        ("a\\%b", "a%b", True),      # escaped % matches literal %
        ("a_b", "acb", True),        # _ matches 1 char
        ("a\\_b", "a_b", True),      # escaped _ matches literal _
        ("a\\\\b", "a\\b", True),    # escaped \\ matches literal \
        ("a%b", "ac", False),        # % matches but trailing b absent
        ("a\\_b", "acb", False),     # escaped _ means literal _, not wildcard
        ("abc", "abd", False),       # exact mismatch
    ]
    for pattern, s, expected in cases:
        result = _sql_like_match(pattern, s)
        assert result == expected, f"_sql_like_match({pattern!r}, {s!r}) = {result}, expected {expected}"


@given(st.text())
@settings(max_examples=500)
def test_escape_like_round_trip(s: str):
    """escape_like(s) used as LIKE pattern matches ONLY s under simulator semantics."""
    from archon_search.store_filters import escape_like
    escaped = escape_like(s)
    # The escaped pattern should match s exactly
    assert _sql_like_match(escaped, s), f"escaped pattern {escaped!r} did not match original {s!r}"
    # A different string must NOT match (escaped pattern has no wildcards)
    if s:  # non-empty: appending a char makes it strictly different
        different = s + "\x00"
        assert not _sql_like_match(escaped, different), (
            f"escaped pattern {escaped!r} incorrectly matched {different!r}"
        )


# ---------------------------------------------------------------------------
# Tests for build_where
# ---------------------------------------------------------------------------

def test_build_where_empty_filters_returns_empty_string():
    from archon_search.store_filters import build_where
    assert build_where(SearchFilters()) == ""


def test_build_where_file_type_only():
    from archon_search.store_filters import build_where
    predicate = build_where(SearchFilters(file_type="md"))
    assert "file_type = 'md'" in predicate


def test_build_where_source_path_prefix_uses_escape_clause():
    from archon_search.store_filters import build_where
    predicate = build_where(SearchFilters(source_path_prefix="/docs"))
    assert "LIKE" in predicate
    assert "ESCAPE" in predicate
    assert "ESCAPE '\\'" in predicate  # Python string: ESCAPE '\'


def test_build_where_source_path_prefix_with_special_chars():
    from archon_search.store_filters import build_where
    # Prefix with %, _, \, and ' — all must be properly escaped
    predicate = build_where(SearchFilters(source_path_prefix="/my%docs_path\\dir'x"))
    # The raw special chars should not appear unescaped in the LIKE operand
    # The % must be escaped to \% in the LIKE pattern
    assert "\\%" in predicate or r"\%" in predicate
    # The _ must be escaped
    assert "\\_" in predicate or r"\_" in predicate
    # Internal single quote in prefix must be doubled (SQL quoting)
    assert "''" in predicate
    # ESCAPE clause present
    assert "ESCAPE" in predicate


def test_build_where_indexed_after_normalized_to_fixed_width():
    from archon_search.store_filters import build_where
    dt = datetime(2024, 3, 15, 10, 30, 0, tzinfo=timezone.utc)
    predicate = build_where(SearchFilters(indexed_after=dt))
    # Fixed-width UTC format: YYYY-MM-DDTHH:MM:SS.ffffffZ
    assert "2024-03-15T10:30:00.000000Z" in predicate
    assert "indexed_at >=" in predicate


def test_build_where_combined_filters_anded():
    from archon_search.store_filters import build_where
    dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
    predicate = build_where(SearchFilters(file_type="py", indexed_after=dt))
    assert " AND " in predicate
    assert "file_type" in predicate
    assert "indexed_at" in predicate


def test_build_where_glob_not_emitted_as_sql():
    from archon_search.store_filters import build_where
    predicate = build_where(SearchFilters(source_path_glob="**/*.md"))
    # source_path_glob is post-RRF only — must NOT appear in SQL predicate
    assert "glob" not in predicate.lower()
    assert "source_path_glob" not in predicate
    # Empty predicate since glob is the only filter
    assert predicate == ""


def test_build_where_handles_every_search_filters_field():
    """Every filterable field (excluding post-RRF/response-only fields) produces output when set."""
    from archon_search.store_filters import build_where

    # Fields that should produce SQL output
    sql_fields = {
        f for f in SearchFilters.model_fields
        if f not in {"include_metadata", "source_path_glob", "language"}
    }

    dt = datetime(2024, 6, 1, tzinfo=timezone.utc)
    for field_name in sql_fields:
        if field_name in ("indexed_after", "indexed_before"):
            filters = SearchFilters(**{field_name: dt})
        else:
            filters = SearchFilters(**{field_name: "/tmp/test" if field_name == "source_path_prefix" else "md"})
        predicate = build_where(filters)
        assert predicate != "", f"Field {field_name!r} produced empty predicate"


# ---------------------------------------------------------------------------
# Tests for _compute_fetch
# ---------------------------------------------------------------------------

def test_compute_fetch_branches():
    from archon_search.store_filters import _compute_fetch, GLOB_OVERFETCH_FACTOR

    # No glob: max(top_k * 3, 20)
    assert _compute_fetch(1, has_glob=False) == 20   # min floor
    assert _compute_fetch(10, has_glob=False) == 30  # 10*3
    assert _compute_fetch(7, has_glob=False) == 21   # 7*3 > 20

    # With glob: max(top_k * GLOB_OVERFETCH_FACTOR, 60)
    assert _compute_fetch(1, has_glob=True) == 60                                # floor
    assert _compute_fetch(20, has_glob=True) == 20 * GLOB_OVERFETCH_FACTOR      # 100
    assert _compute_fetch(13, has_glob=True) == max(13 * GLOB_OVERFETCH_FACTOR, 60)


# ---------------------------------------------------------------------------
# Additional tests (Fix 6, 8)
# ---------------------------------------------------------------------------

def test_build_where_indexed_before_normalized_to_fixed_width():
    from archon_search.store_filters import build_where
    dt = datetime(2024, 3, 15, 10, 30, 0, tzinfo=timezone.utc)
    predicate = build_where(SearchFilters(indexed_before=dt))
    assert "2024-03-15T10:30:00.000000Z" in predicate
    assert "indexed_at <=" in predicate


def test_sql_quote_str_empty_string():
    from archon_search.store_filters import _sql_quote_str
    assert _sql_quote_str("") == "''"


def test_sql_quote_str_multiple_quotes():
    from archon_search.store_filters import _sql_quote_str
    assert _sql_quote_str("a'b'c") == "'a''b''c'"


def test_build_where_include_metadata_not_in_sql():
    from archon_search.store_filters import build_where
    predicate = build_where(SearchFilters(include_metadata=True))
    assert predicate == ""


def test_build_where_language_guard_raises():
    from archon_search.store_filters import build_where
    # SearchFilters rejects non-empty language at validation, but we bypass it
    # to test the defense-in-depth guard in build_where itself.
    import types
    fake = types.SimpleNamespace(
        language="en",
        file_type=None,
        source_path_prefix=None,
        indexed_after=None,
        indexed_before=None,
        source_path_glob=None,
        include_metadata=False,
    )
    with pytest.raises(ValueError, match="language filter reached build_where"):
        build_where(fake)  # type: ignore[arg-type]


def test_build_where_all_sql_filters():
    from archon_search.store_filters import build_where
    dt_after = datetime(2024, 1, 1, tzinfo=timezone.utc)
    dt_before = datetime(2024, 12, 31, tzinfo=timezone.utc)
    predicate = build_where(SearchFilters(
        file_type="py",
        source_path_prefix="/src",
        indexed_after=dt_after,
        indexed_before=dt_before,
    ))
    assert predicate.count(" AND ") == 3  # 4 clauses = 3 ANDs
    assert "file_type" in predicate
    assert "source_path LIKE" in predicate
    assert "indexed_at >=" in predicate
    assert "indexed_at <=" in predicate

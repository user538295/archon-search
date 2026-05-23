"""Tests for archon_search._types dataclasses and normalize_iso_utc."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from archon_search._types import SearchResult, normalize_iso_utc


# ---------------------------------------------------------------------------
# SearchResult.language field
# ---------------------------------------------------------------------------


def test_search_result_language_defaults_to_none() -> None:
    """SearchResult.language defaults to None when not supplied."""
    r = SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000000",
        text="hello",
        score=0.9,
        source_path="/tmp/x.md",
    )
    assert r.language is None


def test_search_result_language_carried_when_set() -> None:
    """SearchResult.language is carried through when explicitly set."""
    r = SearchResult(
        doc_id="b" * 64,
        chunk_id="b" * 64 + "-000001",
        text="hello",
        score=0.8,
        source_path="/tmp/y.md",
        language="en",
    )
    assert r.language == "en"


def test_search_result_ingested_by_remains_ingested_by_literal() -> None:
    """SearchResult.ingested_by defaults to 'cli'."""
    r = SearchResult(
        doc_id="c" * 64,
        chunk_id="c" * 64 + "-000000",
        text="hi",
        score=0.5,
        source_path="/tmp/z.md",
    )
    assert r.ingested_by == "cli"


# ---------------------------------------------------------------------------
# normalize_iso_utc
# ---------------------------------------------------------------------------


def test_normalize_iso_utc_datetime_aware_utc() -> None:
    dt = datetime(2026, 1, 15, 10, 30, 45, 123456, tzinfo=timezone.utc)
    result = normalize_iso_utc(dt)
    assert result == "2026-01-15T10:30:45.123456Z"


def test_normalize_iso_utc_datetime_naive_treated_as_utc() -> None:
    dt = datetime(2026, 3, 1, 0, 0, 0, 0)
    result = normalize_iso_utc(dt)
    assert result == "2026-03-01T00:00:00.000000Z"


def test_normalize_iso_utc_string_with_z_suffix() -> None:
    result = normalize_iso_utc("2026-05-01T12:00:00Z")
    assert result == "2026-05-01T12:00:00.000000Z"


def test_normalize_iso_utc_string_with_plus00() -> None:
    result = normalize_iso_utc("2026-05-01T12:00:00+00:00")
    assert result == "2026-05-01T12:00:00.000000Z"


def test_normalize_iso_utc_string_no_tz_treated_as_utc() -> None:
    result = normalize_iso_utc("2026-07-04T08:15:30")
    assert result == "2026-07-04T08:15:30.000000Z"


def test_normalize_iso_utc_string_with_microseconds() -> None:
    result = normalize_iso_utc("2026-01-01T00:00:00.654321Z")
    assert result == "2026-01-01T00:00:00.654321Z"


def test_normalize_iso_utc_output_ends_with_z() -> None:
    result = normalize_iso_utc(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert result.endswith("Z")


def test_normalize_iso_utc_output_has_fixed_width_format() -> None:
    import re
    result = normalize_iso_utc(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$", result)

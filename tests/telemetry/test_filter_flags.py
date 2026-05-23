"""Tests for FilterFlags and TelemetryEntry.filter_flags (A2)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from archon_search.telemetry.entry import FilterFlags, TelemetryEntry


# ---------------------------------------------------------------------------
# FilterFlags model
# ---------------------------------------------------------------------------


def test_filter_flags_default_all_false() -> None:
    """FilterFlags() with no args → all flags False."""
    ff = FilterFlags()
    assert ff.file_type is False
    assert ff.source_path_prefix is False
    assert ff.source_path_glob is False
    assert ff.indexed_after is False
    assert ff.indexed_before is False


def test_filter_flags_rejects_unknown_field() -> None:
    """FilterFlags with unknown field raises ValidationError (extra='forbid')."""
    with pytest.raises(ValidationError):
        FilterFlags(**{"unknown": True})  # type: ignore[arg-type]


def test_filter_flags_rejects_non_bool_value() -> None:
    """FilterFlags with a non-coercible value raises ValidationError (e.g., a list)."""
    with pytest.raises(ValidationError):
        FilterFlags(file_type=["not", "a", "bool"])  # type: ignore[arg-type]


def test_filter_flags_all_true() -> None:
    ff = FilterFlags(
        file_type=True,
        source_path_prefix=True,
        source_path_glob=True,
        indexed_after=True,
        indexed_before=True,
    )
    assert ff.file_type is True
    assert ff.source_path_prefix is True
    assert ff.source_path_glob is True
    assert ff.indexed_after is True
    assert ff.indexed_before is True


# ---------------------------------------------------------------------------
# TelemetryEntry.filter_flags
# ---------------------------------------------------------------------------


def test_telemetry_entry_filter_flags_default_none() -> None:
    """TelemetryEntry.filter_flags defaults to None."""
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search",
        collection="col",
        result_doc_ids=["abc"],
        latency_ms=10.0,
    )
    assert entry.filter_flags is None


def test_telemetry_entry_filter_flags_can_be_set() -> None:
    """TelemetryEntry accepts filter_flags=FilterFlags(...)."""
    ff = FilterFlags(file_type=True)
    entry = TelemetryEntry(
        query_id="test-id",
        timestamp="2026-01-01T00:00:00Z",
        endpoint="search",
        latency_ms=1.0,
        status="ok",
        collection="col",
        filter_flags=ff,
    )
    assert entry.filter_flags is not None
    assert entry.filter_flags.file_type is True


def test_telemetry_entry_rejects_query_kwarg() -> None:
    """from_search_tool_result must not accept a query parameter (privacy invariant)."""
    with pytest.raises(TypeError):
        TelemetryEntry.from_search_tool_result(  # type: ignore[call-arg]
            query="this should fail",
            endpoint="search",
            collection="col",
            result_doc_ids=[],
            latency_ms=1.0,
        )

"""Tests for TelemetryReader — file discovery and entry parsing (FEAT-039c Task 2.1)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from archon_search.telemetry.entry import TelemetryEntry
from archon_search.telemetry.reader import TelemetryReader


def _make_entry_dict(query_id: str = "abc123") -> dict:
    return {
        "query_id": query_id,
        "timestamp": "2026-05-14T12:00:00Z",
        "endpoint": "search",
        "latency_ms": 12.5,
        "status": "ok",
        "collection": "default",
        "result_count": 1,
        "result_doc_ids": ["doc-1"],
    }


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# resolve_dates
# ---------------------------------------------------------------------------


def test_resolve_dates_defaults() -> None:
    """since=None, until=None → today UTC and today-retention_days."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    fixed_today = date(2026, 5, 15)

    with patch("archon_search.telemetry.reader.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
        since, until = reader.resolve_dates(None, None)

    assert until == fixed_today
    assert since == fixed_today - timedelta(days=30)


def test_resolve_dates_clamps_since_to_retention() -> None:
    """since older than retention window is clamped to until - retention_days."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=7)
    fixed_today = date(2026, 5, 15)
    too_old = date(2026, 1, 1)  # way before the 7-day window

    with patch("archon_search.telemetry.reader.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
        since, until = reader.resolve_dates(too_old, None)

    assert until == fixed_today
    assert since == fixed_today - timedelta(days=7)


def test_resolve_dates_raises_if_since_after_until() -> None:
    """since > until after clamping → ValueError."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    # Provide explicit since > until
    with pytest.raises(ValueError, match="since must be before until"):
        reader.resolve_dates(date(2026, 5, 20), date(2026, 5, 15))


def test_resolve_dates_historical_until_uses_relative_window() -> None:
    """resolve_dates(since=None, until=date(2020, 1, 1)) with retention_days=30 → (date(2019, 12, 2), date(2020, 1, 1))."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    since, until = reader.resolve_dates(None, date(2020, 1, 1))

    assert until == date(2020, 1, 1)
    assert since == date(2020, 1, 1) - timedelta(days=30)


# ---------------------------------------------------------------------------
# files_in_range
# ---------------------------------------------------------------------------


def test_files_in_range_empty_dir() -> None:
    """Non-existent log_dir → []."""
    reader = TelemetryReader(Path("/nonexistent/path/does-not-exist"), retention_days=30)
    result = reader.files_in_range(date(2026, 5, 1), date(2026, 5, 15))
    assert result == []


def test_files_in_range_existing_empty_dir(tmp_path: Path) -> None:
    """Existing but empty log_dir → []."""
    reader = TelemetryReader(tmp_path, retention_days=30)
    result = reader.files_in_range(date(2026, 5, 1), date(2026, 5, 15))
    assert result == []


def test_files_in_range_selects_correct_dates(tmp_path: Path) -> None:
    """Files outside [since, until] are excluded."""
    # Create files: two inside range, one before, one after
    (tmp_path / "2026-05-01.jsonl").write_text("{}\n")
    (tmp_path / "2026-05-10.jsonl").write_text("{}\n")
    (tmp_path / "2026-05-15.jsonl").write_text("{}\n")
    (tmp_path / "2026-05-20.jsonl").write_text("{}\n")

    reader = TelemetryReader(tmp_path, retention_days=30)
    result = reader.files_in_range(date(2026, 5, 10), date(2026, 5, 15))

    stems = [p.stem for p in result]
    assert stems == ["2026-05-10", "2026-05-15"]


def test_files_in_range_skips_non_matching_filenames(tmp_path: Path) -> None:
    """other.jsonl, foo-bar.jsonl, notes.txt skipped; valid date file included."""
    (tmp_path / "other.jsonl").write_text("{}\n")
    (tmp_path / "foo-bar.jsonl").write_text("{}\n")
    (tmp_path / "notes.txt").write_text("{}\n")
    (tmp_path / "2026-05-14.jsonl").write_text("{}\n")

    reader = TelemetryReader(tmp_path, retention_days=30)
    result = reader.files_in_range(date(2026, 5, 1), date(2026, 5, 31))

    assert len(result) == 1
    assert result[0].stem == "2026-05-14"


def test_files_in_range_single_day_boundary(tmp_path: Path) -> None:
    """since==until==date(2026, 5, 14) with file 2026-05-14.jsonl → result contains exactly that file."""
    (tmp_path / "2026-05-13.jsonl").write_text("{}\n")
    (tmp_path / "2026-05-14.jsonl").write_text("{}\n")
    (tmp_path / "2026-05-15.jsonl").write_text("{}\n")

    reader = TelemetryReader(tmp_path, retention_days=30)
    result = reader.files_in_range(date(2026, 5, 14), date(2026, 5, 14))

    assert len(result) == 1
    assert result[0].stem == "2026-05-14"


# ---------------------------------------------------------------------------
# read_entries
# ---------------------------------------------------------------------------


def test_read_entries_empty_dir() -> None:
    """Non-existent log_dir → ([], 0)."""
    reader = TelemetryReader(Path("/nonexistent/path/does-not-exist"), retention_days=30)
    entries, skipped = reader.read_entries(date(2026, 5, 1), date(2026, 5, 15))
    assert entries == []
    assert skipped == 0


def test_read_entries_parses_valid_jsonl(tmp_path: Path) -> None:
    """Two valid JSONL lines → two TelemetryEntry objects, skipped=0."""
    _write_jsonl(
        tmp_path / "2026-05-14.jsonl",
        [_make_entry_dict("q1"), _make_entry_dict("q2")],
    )

    reader = TelemetryReader(tmp_path, retention_days=30)
    entries, skipped = reader.read_entries(date(2026, 5, 14), date(2026, 5, 14))

    assert len(entries) == 2
    assert entries[0].query_id == "q1"
    assert entries[1].query_id == "q2"
    assert skipped == 0


def test_read_entries_skips_malformed_lines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One bad JSON line → skipped=1, valid entry still returned."""
    log_file = tmp_path / "2026-05-14.jsonl"
    log_file.write_text(
        json.dumps(_make_entry_dict("q1")) + "\n"
        + "not-valid-json\n"
        + json.dumps(_make_entry_dict("q2")) + "\n",
        encoding="utf-8",
    )

    caplog.set_level(logging.WARNING, logger="archon.search")
    reader = TelemetryReader(tmp_path, retention_days=30)
    entries, skipped = reader.read_entries(date(2026, 5, 14), date(2026, 5, 14))

    assert skipped == 1
    assert len(entries) == 2
    assert any("skipping malformed" in r.getMessage() for r in caplog.records)


def test_read_entries_skips_missing_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """FileNotFoundError logged at DEBUG, not raised."""
    reader = TelemetryReader(tmp_path, retention_days=30)

    # Patch files_in_range to return a non-existent file path
    missing = tmp_path / "2026-05-14.jsonl"

    caplog.set_level(logging.DEBUG, logger="archon.search")

    with patch.object(reader, "files_in_range", return_value=[missing]):
        entries, skipped = reader.read_entries(date(2026, 5, 14), date(2026, 5, 14))

    assert entries == []
    assert skipped == 0
    assert any(r.levelno == logging.DEBUG for r in caplog.records)


def test_read_entries_skips_oserror_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """PermissionError on one file → WARNING logged, other files' entries still returned."""
    # File 1 — readable
    good_file = tmp_path / "2026-05-13.jsonl"
    _write_jsonl(good_file, [_make_entry_dict("q1")])

    # File 2 — will raise PermissionError
    bad_file = tmp_path / "2026-05-14.jsonl"
    bad_file.write_text("{}\n")  # create it, but we'll patch open

    caplog.set_level(logging.WARNING, logger="archon.search")
    reader = TelemetryReader(tmp_path, retention_days=30)

    real_open = Path.open

    def patched_open(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self == bad_file:
            raise PermissionError("permission denied")
        return real_open(self, *args, **kwargs)

    with patch.object(Path, "open", patched_open):
        with patch.object(reader, "files_in_range", return_value=[good_file, bad_file]):
            entries, skipped = reader.read_entries(date(2026, 5, 13), date(2026, 5, 14))

    assert len(entries) == 1
    assert entries[0].query_id == "q1"
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# compute_stats
# ---------------------------------------------------------------------------


def _ok_search(latency_ms: float = 10.0, collection: str = "col_a") -> TelemetryEntry:
    return TelemetryEntry(
        query_id="qid",
        timestamp="2026-05-14T12:00:00Z",
        endpoint="search",
        latency_ms=latency_ms,
        status="ok",
        collection=collection,
    )


def _ok_route(latency_ms: float = 20.0, collections: list[str] | None = None) -> TelemetryEntry:
    return TelemetryEntry(
        query_id="qid",
        timestamp="2026-05-14T12:00:00Z",
        endpoint="route",
        latency_ms=latency_ms,
        status="ok",
        collections=collections if collections is not None else ["col_a"],
    )


def _error_entry(
    endpoint: str = "search",
    error_kind: str = "timeout",
    latency_ms: float = 5.0,
) -> TelemetryEntry:
    return TelemetryEntry(
        query_id="qid",
        timestamp="2026-05-14T12:00:00Z",
        endpoint=endpoint,  # type: ignore[arg-type]
        latency_ms=latency_ms,
        status="timeout",
        error_kind=error_kind,  # type: ignore[arg-type]
    )


def test_compute_stats_total_queries() -> None:
    """len(entries) is reflected in total_queries."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    entries = [_ok_search(), _ok_search(), _ok_search()]
    since = date(2026, 5, 14)
    until = date(2026, 5, 14)

    stats = reader.compute_stats(entries, since, until, skipped_lines=0)

    assert stats["total_queries"] == 3


def test_compute_stats_success_rate_zero_queries() -> None:
    """Empty entry list → success_rate is None."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    stats = reader.compute_stats([], date(2026, 5, 14), date(2026, 5, 14), skipped_lines=0)

    assert stats["success_rate"] is None


def test_compute_stats_success_rate_formula() -> None:
    """3 ok out of 4 entries → success_rate == 0.75."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    entries = [
        _ok_search(),
        _ok_search(),
        _ok_search(),
        _error_entry(),
    ]
    stats = reader.compute_stats(entries, date(2026, 5, 14), date(2026, 5, 14), skipped_lines=0)

    assert stats["success_rate"] == pytest.approx(0.75)


def test_compute_stats_latency_nearest_rank() -> None:
    """Known dataset: [10, 20, 30, 40, 50, 60, 70, 80, 90, 100] → p50=50, p95=95 nearest-rank."""
    # 10 elements; nearest-rank: p50 idx = ceil(50/100*10)-1 = 5-1=4 → 50
    # p95 idx = ceil(95/100*10)-1 = ceil(9.5)-1 = 10-1 = 9 → 100
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    entries = [_ok_search(latency_ms=float(v)) for v in range(10, 110, 10)]
    stats = reader.compute_stats(entries, date(2026, 5, 14), date(2026, 5, 14), skipped_lines=0)

    assert stats["latency_ms"]["p50"] == pytest.approx(50.0)
    assert stats["latency_ms"]["p95"] == pytest.approx(100.0)


def test_compute_stats_latency_null_when_no_entries() -> None:
    """Zero entries → latency_ms == {p50: None, p95: None}."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    stats = reader.compute_stats([], date(2026, 5, 14), date(2026, 5, 14), skipped_lines=0)

    assert stats["latency_ms"] == {"p50": None, "p95": None}


def test_compute_stats_by_endpoint_counts() -> None:
    """search and route entries are counted in by_endpoint with correct ok/error breakdown."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    entries = [
        _ok_search(),
        _ok_search(),
        _error_entry(endpoint="search"),
        _ok_route(),
    ]
    stats = reader.compute_stats(entries, date(2026, 5, 14), date(2026, 5, 14), skipped_lines=0)

    ep = stats["by_endpoint"]
    assert ep["search"]["total"] == 3
    assert ep["search"]["ok"] == 2
    assert ep["search"]["error"] == 1
    assert ep["route"]["total"] == 1
    assert ep["route"]["ok"] == 1
    assert ep["route"]["error"] == 0


def test_compute_stats_by_collection_fan_out() -> None:
    """Route entry with collections=['A','B'] increments both A and B."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    entries = [_ok_route(collections=["A", "B"])]
    stats = reader.compute_stats(entries, date(2026, 5, 14), date(2026, 5, 14), skipped_lines=0)

    bc = stats["by_collection"]
    assert bc["A"] == {"total": 1, "ok": 1}
    assert bc["B"] == {"total": 1, "ok": 1}


def test_compute_stats_by_collection_excludes_error_entries() -> None:
    """Error entries (no collection/collections) are not counted in by_collection."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    entries = [
        _ok_search(collection="col_x"),
        _error_entry(endpoint="search"),
    ]
    stats = reader.compute_stats(entries, date(2026, 5, 14), date(2026, 5, 14), skipped_lines=0)

    bc = stats["by_collection"]
    assert "col_x" in bc
    assert bc["col_x"] == {"total": 1, "ok": 1}
    # Error entry has no collection → not in by_collection
    assert len(bc) == 1


def test_compute_stats_error_breakdown_all_keys_present() -> None:
    """All 6 ErrorKind literal values are always present, zero-filled if absent."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    stats = reader.compute_stats([], date(2026, 5, 14), date(2026, 5, 14), skipped_lines=0)

    eb = stats["error_breakdown"]
    expected_keys = {
        "empty_query",
        "slot_out_of_range",
        "timeout",
        "internal_error",
        "validation_error",
        "other",
    }
    assert set(eb.keys()) == expected_keys
    assert all(v == 0 for v in eb.values())


def test_compute_stats_invariant_error_breakdown_vs_by_endpoint() -> None:
    """sum(error_breakdown.values()) == sum(ep['error'] for ep in by_endpoint.values())."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    entries = [
        _ok_search(),
        _error_entry(endpoint="search", error_kind="timeout"),
        _error_entry(endpoint="route", error_kind="internal_error"),
    ]
    stats = reader.compute_stats(entries, date(2026, 5, 14), date(2026, 5, 14), skipped_lines=0)

    eb_total = sum(stats["error_breakdown"].values())
    ep_errors = sum(ep["error"] for ep in stats["by_endpoint"].values())
    assert eb_total == ep_errors == 2


def test_compute_stats_schema_keys() -> None:
    """Return dict contains all required top-level keys with correct types/values."""
    reader = TelemetryReader(Path("/nonexistent"), retention_days=30)
    since = date(2026, 5, 1)
    until = date(2026, 5, 14)
    stats = reader.compute_stats([_ok_search()], since, until, skipped_lines=3)

    assert stats["schema_version"] == 1
    assert stats["enabled"] is True
    assert stats["since"] == "2026-05-01"
    assert stats["until"] == "2026-05-14"
    assert stats["skipped_lines"] == 3

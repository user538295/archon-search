"""Tests for telemetry response Pydantic schemas (FEAT-039c Task 3.1)."""
from __future__ import annotations

import pytest

from archon_search.server.schemas_telemetry import (
    CollectionStats,
    DisabledResponse,
    EndpointStats,
    EntriesResponse,
    ErrorBreakdown,
    LatencyPercentiles,
    StatsResponse,
)


def test_stats_response_defaults() -> None:
    r = StatsResponse(enabled=True)
    assert r.schema_version == 1
    assert r.enabled is True
    assert r.since is None
    assert r.until is None
    assert r.total_queries == 0
    assert r.success_rate is None
    assert r.skipped_lines == 0
    assert isinstance(r.latency_ms, LatencyPercentiles)
    assert r.latency_ms == LatencyPercentiles(p50=None, p95=None)
    assert isinstance(r.by_endpoint, dict)
    assert r.by_endpoint == {}
    assert isinstance(r.by_collection, dict)
    assert r.by_collection == {}
    assert isinstance(r.error_breakdown, ErrorBreakdown)
    assert r.error_breakdown == ErrorBreakdown()


def test_error_breakdown_all_keys_default_zero() -> None:
    eb = ErrorBreakdown()
    assert eb.empty_query == 0
    assert eb.slot_out_of_range == 0
    assert eb.timeout == 0
    assert eb.internal_error == 0
    assert eb.validation_error == 0
    assert eb.other == 0


def test_disabled_response_enabled_false() -> None:
    r = DisabledResponse()
    assert r.enabled is False


def test_entries_response_fields() -> None:
    r = EntriesResponse(
        enabled=True,
        entries=[{"query_id": "abc", "endpoint": "search"}],
        next_offset=10,
        total_in_window=42,
    )
    assert r.schema_version == 1
    assert r.enabled is True
    assert r.entries == [{"query_id": "abc", "endpoint": "search"}]
    assert r.next_offset == 10
    assert r.total_in_window == 42
    assert r.skipped_lines == 0


def test_latency_percentiles_none_values() -> None:
    lp = LatencyPercentiles(p50=None, p95=None)
    assert lp.p50 is None
    assert lp.p95 is None


def test_latency_percentiles_float_values() -> None:
    lp = LatencyPercentiles(p50=12.5, p95=99.9)
    assert lp.p50 == pytest.approx(12.5)
    assert lp.p95 == pytest.approx(99.9)


def test_endpoint_stats_fields() -> None:
    es = EndpointStats(total=10, ok=8, error=2)
    assert es.total == 10
    assert es.ok == 8
    assert es.error == 2


def test_collection_stats_fields() -> None:
    cs = CollectionStats(total=5, ok=5)
    assert cs.total == 5
    assert cs.ok == 5


def test_stats_response_mutable_defaults_are_independent() -> None:
    """Each StatsResponse instance must have its own dict/model instances."""
    r1 = StatsResponse(enabled=True)
    r2 = StatsResponse(enabled=True)
    assert r1.by_endpoint is not r2.by_endpoint
    assert r1.by_collection is not r2.by_collection
    assert r1.latency_ms is not r2.latency_ms
    assert r1.error_breakdown is not r2.error_breakdown


def test_stats_response_with_data() -> None:
    r = StatsResponse(
        enabled=True,
        since="2026-01-01T00:00:00Z",
        until="2026-01-02T00:00:00Z",
        total_queries=100,
        success_rate=0.95,
        skipped_lines=2,
        latency_ms=LatencyPercentiles(p50=20.0, p95=80.0),
        by_endpoint={"search": EndpointStats(total=80, ok=76, error=4)},
        by_collection={"docs": CollectionStats(total=80, ok=76)},
        error_breakdown=ErrorBreakdown(timeout=3, other=1),
    )
    assert r.total_queries == 100
    assert r.success_rate == pytest.approx(0.95)
    assert r.latency_ms.p50 == pytest.approx(20.0)
    assert r.by_endpoint["search"].ok == 76
    assert r.by_collection["docs"].total == 80
    assert r.error_breakdown.timeout == 3
    assert r.error_breakdown.other == 1

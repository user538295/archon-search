"""Tests for TelemetryEntry factory classmethods (Task 1.4)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from archon_search.telemetry import TelemetryEntry


def _make_entry(factory_name: str) -> TelemetryEntry:
    if factory_name == "from_search_tool_result":
        return TelemetryEntry.from_search_tool_result(
            endpoint="search", collection="docs", result_doc_ids=[], latency_ms=1.0
        )
    if factory_name == "from_route_response":
        return TelemetryEntry.from_route_response(
            collections=["docs"], decomposer_invoked=False, latency_ms=1.0
        )
    if factory_name == "from_error":
        return TelemetryEntry.from_error(
            endpoint="search",
            status="internal_error",
            error_kind="other",
            latency_ms=1.0,
        )
    raise AssertionError(f"unknown factory: {factory_name}")


ALL_FACTORIES = ["from_search_tool_result", "from_route_response", "from_error"]


def test_from_search_tool_result_populates_retrieval_fields() -> None:
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search",
        collection="docs",
        result_doc_ids=["a", "b", "c"],
        latency_ms=42.5,
    )
    assert entry.endpoint == "search"
    assert entry.collection == "docs"
    assert entry.result_doc_ids == ["a", "b", "c"]
    assert entry.result_count == 3
    assert entry.status == "ok"
    assert entry.latency_ms == 42.5
    # Routing-only fields untouched
    assert entry.collections is None
    assert entry.decomposer_invoked is None
    assert entry.error_kind is None


def test_from_search_tool_result_computes_result_count_from_doc_ids() -> None:
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search_with_context",
        collection="docs",
        result_doc_ids=["x"] * 7,
        latency_ms=10.0,
    )
    assert entry.result_count == 7
    assert entry.endpoint == "search_with_context"


def test_from_search_tool_result_empty_doc_ids_yields_zero_count() -> None:
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search", collection="docs", result_doc_ids=[], latency_ms=1.0
    )
    assert entry.result_count == 0
    assert entry.result_doc_ids == []
    assert entry.status == "ok"


def test_from_route_response_populates_routing_fields() -> None:
    entry = TelemetryEntry.from_route_response(
        collections=["docs", "code"],
        decomposer_invoked=True,
        latency_ms=99.0,
    )
    assert entry.endpoint == "route"
    assert entry.collections == ["docs", "code"]
    assert entry.decomposer_invoked is True
    assert entry.status == "ok"
    assert entry.latency_ms == 99.0
    # Retrieval-only fields stay None
    assert entry.collection is None
    assert entry.result_count is None
    assert entry.result_doc_ids is None
    assert entry.error_kind is None


def test_from_error_populates_error_fields() -> None:
    entry = TelemetryEntry.from_error(
        endpoint="search",
        status="internal_error",
        error_kind="other",
        latency_ms=5.0,
    )
    assert entry.endpoint == "search"
    assert entry.status == "internal_error"
    assert entry.error_kind == "other"
    assert entry.latency_ms == 5.0
    # Cross-domain fields stay None
    assert entry.collection is None
    assert entry.result_count is None
    assert entry.result_doc_ids is None
    assert entry.collections is None
    assert entry.decomposer_invoked is None
    assert entry.truncated is None


@pytest.mark.parametrize("status", ["validation_error", "timeout", "internal_error"])
@pytest.mark.parametrize("endpoint", ["search", "search_with_context", "route"])
def test_from_error_accepts_all_non_ok_status_and_endpoints(
    status: str, endpoint: str
) -> None:
    entry = TelemetryEntry.from_error(
        endpoint=endpoint,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        error_kind="other",
        latency_ms=1.0,
    )
    assert entry.endpoint == endpoint
    assert entry.status == status


def test_from_search_tool_result_rejects_route_endpoint() -> None:
    with pytest.raises(ValueError):
        TelemetryEntry.from_search_tool_result(
            endpoint="route",  # type: ignore[arg-type]
            collection="docs",
            result_doc_ids=[],
            latency_ms=1.0,
        )


def test_from_error_rejects_ok_status() -> None:
    with pytest.raises(ValueError):
        TelemetryEntry.from_error(
            endpoint="search",
            status="ok",  # type: ignore[arg-type]
            error_kind="other",
            latency_ms=1.0,
        )


@pytest.mark.parametrize(
    "factory_name",
    ["from_search_tool_result", "from_route_response", "from_error"],
)
def test_factory_signatures_reject_raw_query_argument(factory_name: str) -> None:
    factory = getattr(TelemetryEntry, factory_name)
    params = inspect.signature(factory).parameters
    forbidden = {"query", "query_text", "body", "request"}
    assert forbidden.isdisjoint(params.keys()), (
        f"{factory_name} signature contains forbidden raw-query parameter"
    )


@pytest.mark.parametrize("factory_name", ALL_FACTORIES)
def test_factories_emit_uuid_query_id(factory_name: str) -> None:
    e1 = _make_entry(factory_name)
    e2 = _make_entry(factory_name)
    assert len(e1.query_id) == 32
    assert all(c in "0123456789abcdef" for c in e1.query_id)
    assert e1.query_id != e2.query_id


@pytest.mark.parametrize("factory_name", ALL_FACTORIES)
def test_factories_emit_utc_z_timestamp(factory_name: str) -> None:
    entry = _make_entry(factory_name)
    assert entry.timestamp.endswith("Z")
    parsed = datetime.fromisoformat(entry.timestamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    # Should round-trip back to UTC
    assert parsed.astimezone(UTC) == parsed

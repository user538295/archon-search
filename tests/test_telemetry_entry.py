"""Tests for TelemetryEntry model and factories."""
from __future__ import annotations

import inspect

import pytest

from archon_search.telemetry.entry import (
    DOCUMENTED_SCHEMA_FIELDS,
    EndpointKind,
    TelemetryEntry,
)


# ---------------------------------------------------------------------------
# EndpointKind
# ---------------------------------------------------------------------------


def test_endpoint_kind_includes_explain() -> None:
    assert "explain" in EndpointKind.__members__
    assert EndpointKind.explain == "explain"


# ---------------------------------------------------------------------------
# from_explain_result
# ---------------------------------------------------------------------------


def test_from_explain_result_returns_valid_entry() -> None:
    entry = TelemetryEntry.from_explain_result(
        collection="my-col",
        result_count=3,
        latency_ms=42.5,
    )
    assert entry.endpoint == "explain"
    assert entry.status == "ok"
    assert entry.collection == "my-col"
    assert entry.result_count == 3
    assert entry.latency_ms == pytest.approx(42.5)
    assert entry.result_doc_ids is None
    assert entry.query_id
    assert entry.timestamp


def test_from_explain_result_rejects_query_kwarg() -> None:
    """from_explain_result must not accept a query parameter."""
    with pytest.raises(TypeError):
        TelemetryEntry.from_explain_result(  # type: ignore[call-arg]
            query="this should fail",
            collection="col",
            result_count=0,
            latency_ms=1.0,
        )


def test_from_explain_result_is_keyword_only() -> None:
    """Positional arguments are not allowed."""
    with pytest.raises(TypeError):
        TelemetryEntry.from_explain_result("col", 1, 1.0)  # type: ignore[call-arg]


def test_from_explain_result_signature_has_no_query_param() -> None:
    sig = inspect.signature(TelemetryEntry.from_explain_result)
    assert "query" not in sig.parameters


def test_documented_schema_fields_equals_model_fields_after_change() -> None:
    assert DOCUMENTED_SCHEMA_FIELDS == set(TelemetryEntry.model_fields)

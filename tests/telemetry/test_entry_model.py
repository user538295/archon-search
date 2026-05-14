"""Tests for TelemetryEntry Pydantic model + schema constant (Task 1.3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from archon_search.telemetry import (
    DOCUMENTED_SCHEMA_FIELDS,
    EndpointKind,
    ErrorKind,
    Status,
    TelemetryEntry,
)


def _minimum_kwargs() -> dict:
    return {
        "query_id": "deadbeef" * 4,
        "timestamp": "2026-05-14T09:00:00Z",
        "endpoint": "search",
        "latency_ms": 12.5,
        "status": "ok",
    }


def test_entry_minimum_construction() -> None:
    entry = TelemetryEntry(**_minimum_kwargs())
    assert entry.query_id == "deadbeef" * 4
    assert entry.endpoint == "search"
    assert entry.latency_ms == 12.5
    assert entry.status == "ok"
    # All optional fields default to None
    assert entry.collection is None
    assert entry.result_count is None
    assert entry.result_doc_ids is None
    assert entry.truncated is None
    assert entry.collections is None
    assert entry.decomposer_invoked is None
    assert entry.error_kind is None


def test_entry_extra_forbid_blocks_unknown_field() -> None:
    with pytest.raises(ValidationError):
        TelemetryEntry(**_minimum_kwargs(), unknown_field="leak")


def test_entry_schema_is_exhaustive() -> None:
    assert set(TelemetryEntry.model_fields.keys()) == DOCUMENTED_SCHEMA_FIELDS


def test_documented_schema_fields_is_subset_of_model_fields() -> None:
    assert DOCUMENTED_SCHEMA_FIELDS.issubset(set(TelemetryEntry.model_fields.keys()))


def test_entry_endpoint_literal_rejects_unknown_value() -> None:
    kwargs = _minimum_kwargs()
    kwargs["endpoint"] = "weird"
    with pytest.raises(ValidationError):
        TelemetryEntry(**kwargs)


def test_entry_status_literal_rejects_unknown_value() -> None:
    kwargs = _minimum_kwargs()
    kwargs["status"] = "unknown"
    with pytest.raises(ValidationError):
        TelemetryEntry(**kwargs)


def test_entry_error_kind_literal_rejects_unknown_value() -> None:
    kwargs = _minimum_kwargs()
    kwargs["status"] = "internal_error"
    kwargs["error_kind"] = "LanceDBError"
    with pytest.raises(ValidationError):
        TelemetryEntry(**kwargs)


def test_entry_is_frozen() -> None:
    entry = TelemetryEntry(**_minimum_kwargs())
    with pytest.raises(ValidationError):
        entry.latency_ms = 99.9  # type: ignore[misc]


def test_entry_model_copy_update_works_on_frozen() -> None:
    entry = TelemetryEntry(**_minimum_kwargs())
    updated = entry.model_copy(update={"truncated": True})
    assert updated is not entry
    assert updated.truncated is True
    assert entry.truncated is None


def test_endpoint_kind_values() -> None:
    # EndpointKind is the Literal — sanity check it can be imported
    assert EndpointKind is not None
    assert Status is not None
    assert ErrorKind is not None

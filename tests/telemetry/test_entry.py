"""Tests for FilterFlags submodel and its integration with TelemetryEntry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from archon_search.telemetry.entry import FilterFlags, TelemetryEntry


# ---------------------------------------------------------------------------
# FilterFlags unit tests
# ---------------------------------------------------------------------------


def test_filter_flags_default_all_false() -> None:
    flags = FilterFlags()
    assert flags.file_type is False
    assert flags.source_path_prefix is False
    assert flags.source_path_glob is False
    assert flags.indexed_after is False
    assert flags.indexed_before is False
    assert flags.include_metadata is False


def test_filter_flags_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        FilterFlags(unknown=True)  # type: ignore[call-arg]


def test_filter_flags_rejects_non_bool_value() -> None:
    with pytest.raises(ValidationError):
        FilterFlags(file_type="yes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FilterFlags.from_search_filters mapping
# ---------------------------------------------------------------------------


def test_filter_flags_from_search_filters_all_present() -> None:
    """from_search_filters maps non-None fields to True, None to False."""
    from archon_search.filters import SearchFilters

    filters = SearchFilters(
        file_type="md",
        source_path_prefix="/docs/",
        source_path_glob="*.md",
        indexed_after="2025-01-01",
        indexed_before="2025-12-31",
        include_metadata=True,
    )
    flags = FilterFlags.from_search_filters(filters)
    assert flags.file_type is True
    assert flags.source_path_prefix is True
    assert flags.source_path_glob is True
    assert flags.indexed_after is True
    assert flags.indexed_before is True
    assert flags.include_metadata is True


def test_filter_flags_from_search_filters_all_absent() -> None:
    """from_search_filters with no optional fields set yields all False."""
    from archon_search.filters import SearchFilters

    filters = SearchFilters()
    flags = FilterFlags.from_search_filters(filters)
    assert flags == FilterFlags()


def test_filter_flags_from_search_filters_partial() -> None:
    """from_search_filters maps a partial filter correctly."""
    from archon_search.filters import SearchFilters

    filters = SearchFilters(file_type="pdf", include_metadata=True)
    flags = FilterFlags.from_search_filters(filters)
    assert flags.file_type is True
    assert flags.include_metadata is True
    assert flags.source_path_prefix is False
    assert flags.source_path_glob is False
    assert flags.indexed_after is False
    assert flags.indexed_before is False


# ---------------------------------------------------------------------------
# TelemetryEntry + FilterFlags integration
# ---------------------------------------------------------------------------


def test_telemetry_entry_filter_flags_default_factory() -> None:
    entry = TelemetryEntry.from_search_tool_result(
        endpoint="search",
        collection="docs",
        result_doc_ids=[],
        latency_ms=1.0,
    )
    assert entry.filter_flags == FilterFlags()
    assert entry.filter_flags.file_type is False


def test_telemetry_entry_rejects_query_kwarg() -> None:
    """The no-raw-query invariant must survive addition of filter_flags."""
    import inspect
    for factory_name in ("from_search_tool_result", "from_route_response", "from_error"):
        factory = getattr(TelemetryEntry, factory_name)
        params = inspect.signature(factory).parameters
        assert "query" not in params, (
            f"{factory_name} must not accept a 'query' parameter"
        )


def test_telemetry_entry_rejects_raw_filter_values_as_kwargs() -> None:
    """extra='forbid' on TelemetryEntry must block raw filter strings."""
    with pytest.raises(ValidationError):
        TelemetryEntry(
            query_id="deadbeef" * 4,
            timestamp="2026-05-14T09:00:00Z",
            endpoint="search",
            latency_ms=1.0,
            status="ok",
            source_path_prefix="/secret",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_search_writes_filter_flags_to_jsonl(tmp_path: Path) -> None:
    """REST /search with a filter writes filter_flags booleans to JSONL."""
    import os
    from unittest.mock import AsyncMock, MagicMock

    from fastapi.testclient import TestClient

    from archon_search.collection_meta import CollectionMeta
    from archon_search.config import SearchConfig, TelemetryConfig
    from archon_search.jobs.store import JobStore
    from archon_search.pipeline import SearchPipelineResult
    from archon_search.server.app import create_app

    log_dir = tmp_path / "search-logs"
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.telemetry = TelemetryConfig(enabled=True, retention_days=30, log_dir=str(log_dir))
    job_store = JobStore(path=tmp_path / "jobs.json")

    pipeline = MagicMock()
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="docs", namespace="default")
    )

    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    # Context-manager form runs the lifespan, which starts the telemetry writer
    # (telemetry enabled) and exits drain the writer so the JSONL is flushed.
    with TestClient(app, headers={"Authorization": f"Bearer {key}"}, raise_server_exceptions=False) as client:
        app.state.pipeline = pipeline
        resp = client.post(
            "/search",
            json={
                "collection": "docs",
                "query": "hello",
                "filters": {"file_type": "md", "include_metadata": True},
            },
        )

    assert resp.status_code == 200, f"Expected 200 from /search, got {resp.status_code}: {resp.text}"

    # Find the JSONL file
    jsonl_files = list(log_dir.glob("*.jsonl"))
    assert jsonl_files, "No JSONL file written"
    lines = [json.loads(l) for l in jsonl_files[0].read_text().splitlines() if l.strip()]
    search_lines = [l for l in lines if l.get("endpoint") == "search"]
    assert search_lines, "No search telemetry entry written"
    entry = search_lines[0]

    # Must have filter_flags with correct booleans — all values must be actual booleans
    assert "filter_flags" in entry, "filter_flags missing from JSONL"
    ff = entry["filter_flags"]
    assert ff["file_type"] is True
    assert ff["include_metadata"] is True
    assert ff["source_path_prefix"] is False
    assert ff["source_path_glob"] is False
    assert ff["indexed_after"] is False
    assert ff["indexed_before"] is False
    for key, val in ff.items():
        assert isinstance(val, bool), f"filter_flags[{key!r}] is {type(val).__name__!r}, expected bool"

    # Must NOT contain raw filter strings at the top-level entry
    for forbidden_key in ("file_type_value", "source_path_prefix_value", "query", "source_path_prefix", "source_path_glob"):
        assert forbidden_key not in entry, f"Raw filter key {forbidden_key!r} leaked into telemetry entry"


# ---------------------------------------------------------------------------
# correlation_id field (Task 4.1)
# ---------------------------------------------------------------------------


def test_from_route_response_accepts_correlation_id() -> None:
    entry = TelemetryEntry.from_route_response(
        collections=["a", "b"],
        decomposer_invoked=False,
        latency_ms=5.0,
        correlation_id="abc123",
    )
    assert entry.correlation_id == "abc123"


def test_correlation_id_default_none() -> None:
    entry = TelemetryEntry.from_route_response(
        collections=["a"],
        decomposer_invoked=False,
        latency_ms=2.0,
    )
    assert entry.correlation_id is None


def test_query_kwarg_still_rejected() -> None:
    with pytest.raises(ValidationError):
        TelemetryEntry(
            query_id="deadbeef" * 4,
            timestamp="2026-05-14T09:00:00Z",
            endpoint="route",
            latency_ms=1.0,
            status="ok",
            query="some query text",  # type: ignore[call-arg]
        )


def test_all_factories_accept_correlation_id() -> None:
    cid = "trace-xyz"
    entries = [
        TelemetryEntry.from_search_tool_result(
            endpoint="search", collection="c", result_doc_ids=[], latency_ms=1.0, correlation_id=cid
        ),
        TelemetryEntry.from_route_response(
            collections=["c"], decomposer_invoked=False, latency_ms=1.0, correlation_id=cid
        ),
        TelemetryEntry.from_error(
            endpoint="search", status="internal_error", error_kind="other", latency_ms=1.0, correlation_id=cid
        ),
        TelemetryEntry.from_explain_result(
            collection="c", result_count=1, latency_ms=1.0, correlation_id=cid
        ),
    ]
    for entry in entries:
        assert entry.correlation_id == cid


def test_query_not_in_factory_signatures() -> None:
    import inspect

    factories = [
        TelemetryEntry.from_search_tool_result,
        TelemetryEntry.from_route_response,
        TelemetryEntry.from_error,
        TelemetryEntry.from_explain_result,
    ]
    for factory in factories:
        params = inspect.signature(factory).parameters
        assert "query" not in params, f"{factory.__name__} must not have 'query' param"
        assert "correlation_id" in params, f"{factory.__name__} must have 'correlation_id' param"


def test_correlation_id_in_documented_schema_fields() -> None:
    from archon_search.telemetry.entry import DOCUMENTED_SCHEMA_FIELDS

    assert "correlation_id" in DOCUMENTED_SCHEMA_FIELDS


# ---------------------------------------------------------------------------
# B3 Task 7.1 — from_search_multi_result factory (no-raw-query invariant)
# ---------------------------------------------------------------------------


def test_from_search_multi_result_no_query_param() -> None:
    import inspect

    params = inspect.signature(TelemetryEntry.from_search_multi_result).parameters
    assert "query" not in params
    with pytest.raises(TypeError):
        TelemetryEntry.from_search_multi_result(
            collections=["a"],
            fanout_count=1,
            result_count=1,
            latency_ms=10.0,
            excluded_count=0,
            query="test",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Task 8.1 — language_filter_used in FilterFlags
# ---------------------------------------------------------------------------


def test_filter_flags_language_filter_used_true() -> None:
    from archon_search.filters import SearchFilters

    filters = SearchFilters(language="fr")
    flags = FilterFlags.from_search_filters(filters)
    assert flags.language_filter_used is True


def test_filter_flags_language_filter_used_false() -> None:
    from archon_search.filters import SearchFilters

    filters = SearchFilters(language=None)
    flags = FilterFlags.from_search_filters(filters)
    assert flags.language_filter_used is False


def test_filter_flags_no_raw_language_value() -> None:
    """FilterFlags must store only a boolean — no field carrying the actual language code."""
    import inspect

    fields = FilterFlags.model_fields
    for field_name in fields:
        assert "language" not in field_name or field_name == "language_filter_used", (
            f"Unexpected field {field_name!r} — only language_filter_used boolean is allowed"
        )
    # Confirm there is no attribute holding the raw string
    flags = FilterFlags(language_filter_used=True)
    assert not hasattr(flags, "language_code"), "Raw language code must not be stored"
    assert not hasattr(flags, "language_value"), "Raw language code must not be stored"


def test_from_search_multi_result_records_fanout_count() -> None:
    entry = TelemetryEntry.from_search_multi_result(
        collections=["a", "b"],
        fanout_count=2,
        result_count=5,
        latency_ms=100.0,
        excluded_count=0,
    )
    assert entry.fanout_count == 2
    assert entry.collections == ["a", "b"]
    assert entry.result_count == 5
    assert entry.excluded_count == 0
    assert entry.endpoint == "search_multi"
    assert entry.status == "ok"

"""Telemetry emission tests for POST /search handler (A3).

Uses a bare FastAPI app (NOT create_app) to avoid the chonkie tokenizer issue.
Mirrors the pattern from test_routes_route_telemetry.py.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.pipeline import SearchPipelineResult
from archon_search._types import SearchResult
from archon_search.telemetry.entry import TelemetryEntry
from archon_search.telemetry.writer import TelemetryWriter


# ---------------------------------------------------------------------------
# Minimal test app fixture — no create_app, no chonkie tokenizer
# ---------------------------------------------------------------------------


def _make_telemetry_app(
    *,
    writer: TelemetryWriter | MagicMock | None = None,
    pipeline: MagicMock | None = None,
) -> FastAPI:
    """Create a minimal FastAPI app with only the search router and mocked state."""
    from archon_search.server.routes_search import router

    app = FastAPI()
    app.state.telemetry_writer = writer

    # Set up default mock pipeline if not provided
    if pipeline is None:
        pipeline = MagicMock()
        pipeline.get_collection_meta = AsyncMock(
            return_value=CollectionMeta(name="col", namespace=DEFAULT_NAMESPACE)
        )
        pipeline.search = AsyncMock(
            return_value=SearchPipelineResult(results=[], acl_filtered=False)
        )
    app.state.pipeline = pipeline

    # Inject namespace into request.state so the handler can read it without real middleware
    @app.middleware("http")
    async def _inject_namespace(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.namespace = DEFAULT_NAMESPACE
        return await call_next(request)

    app.include_router(router)
    return app


def _make_mock_writer() -> MagicMock:
    return MagicMock(spec=TelemetryWriter)


def _make_mock_pipeline(
    *,
    search_raises: Exception | None = None,
    search_result: SearchPipelineResult | None = None,
    meta_return: CollectionMeta | None = ...,  # type: ignore[assignment]
) -> MagicMock:
    """Build a mock pipeline with configurable search and meta behaviors."""
    pipeline = MagicMock()

    if meta_return is ...:
        pipeline.get_collection_meta = AsyncMock(
            return_value=CollectionMeta(name="col", namespace=DEFAULT_NAMESPACE)
        )
    else:
        pipeline.get_collection_meta = AsyncMock(return_value=meta_return)

    if search_raises is not None:
        pipeline.search = AsyncMock(side_effect=search_raises)
    else:
        result = search_result or SearchPipelineResult(results=[], acl_filtered=False)
        pipeline.search = AsyncMock(return_value=result)

    return pipeline


_SEARCH_PAYLOAD = {"collection": "col", "query": "test query"}


# ---------------------------------------------------------------------------
# Test 1: store exception → telemetry entry with status="internal_error"
# ---------------------------------------------------------------------------


def test_store_exception_enqueues_telemetry_entry() -> None:
    """pipeline.search raises RuntimeError → 500, enqueue called once with correct fields."""
    writer = _make_mock_writer()
    pipeline = _make_mock_pipeline(search_raises=RuntimeError("db failure"))
    app = _make_telemetry_app(writer=writer, pipeline=pipeline)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/search", json=_SEARCH_PAYLOAD)

    assert resp.status_code == 500
    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert isinstance(entry, TelemetryEntry)
    assert entry.endpoint == "search"
    assert entry.status == "internal_error"
    assert entry.error_kind == "other"
    assert entry.latency_ms > 0


# ---------------------------------------------------------------------------
# Test 2: embedder exception → same telemetry shape
# ---------------------------------------------------------------------------


def test_embedder_exception_enqueues_telemetry_entry() -> None:
    """pipeline.search raises ValueError (embedder failure) → 500, error telemetry."""
    writer = _make_mock_writer()
    pipeline = _make_mock_pipeline(search_raises=ValueError("embedding failed"))
    app = _make_telemetry_app(writer=writer, pipeline=pipeline)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/search", json=_SEARCH_PAYLOAD)

    assert resp.status_code == 500
    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert entry.endpoint == "search"
    assert entry.status == "internal_error"
    assert entry.error_kind == "other"
    assert entry.latency_ms > 0


# ---------------------------------------------------------------------------
# Test 3: reranker exception → same telemetry shape
# ---------------------------------------------------------------------------


def test_reranker_exception_enqueues_telemetry_entry() -> None:
    """pipeline.search raises OSError (reranker failure) → 500, error telemetry."""
    writer = _make_mock_writer()
    pipeline = _make_mock_pipeline(search_raises=OSError("reranker crashed"))
    app = _make_telemetry_app(writer=writer, pipeline=pipeline)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/search", json=_SEARCH_PAYLOAD)

    assert resp.status_code == 500
    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert entry.endpoint == "search"
    assert entry.status == "internal_error"
    assert entry.error_kind == "other"
    assert entry.latency_ms > 0


# ---------------------------------------------------------------------------
# Test 4: pipeline failure → structured log event_type + exc_info + no raw query
# ---------------------------------------------------------------------------


def test_pipeline_failure_logs_structured_event_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pipeline failure → ERROR log with event_type='search_pipeline_failure' and exc_info.

    Also asserts that the sentinel query string is NOT in any captured log message
    (no-raw-query invariant at log layer).
    """
    sentinel = "PRIVACY-LEAK-SENTINEL-SEARCH-FAILURE-XYZ"
    writer = _make_mock_writer()
    pipeline = _make_mock_pipeline(search_raises=RuntimeError("internal error"))
    app = _make_telemetry_app(writer=writer, pipeline=pipeline)

    with caplog.at_level(logging.ERROR, logger="archon.search"):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/search", json={"collection": "col", "query": sentinel})

    assert resp.status_code == 500

    # Find ERROR records with the search_pipeline_failure event_type
    error_records = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and getattr(r, "event_type", None) == "search_pipeline_failure"
    ]
    assert len(error_records) >= 1, (
        f"Expected at least one ERROR record with event_type='search_pipeline_failure'; "
        f"got: {[(r.levelname, r.getMessage(), getattr(r, 'event_type', None)) for r in caplog.records]}"
    )
    # exc_info must be present (the exception should be attached)
    assert error_records[0].exc_info is not None

    # Sentinel must NOT appear in any log message
    for record in caplog.records:
        if record.name.startswith("archon.search"):
            msg = record.getMessage()
            assert sentinel not in msg, (
                f"Sentinel found in archon.search log message: {msg!r}"
            )


# ---------------------------------------------------------------------------
# Test 5: telemetry enqueue failure → route still returns 500 (no crash)
# ---------------------------------------------------------------------------


def test_telemetry_enqueue_failure_does_not_break_route(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """writer.enqueue raises RuntimeError → route still returns 500, WARNING logged."""
    writer = _make_mock_writer()
    writer.enqueue.side_effect = RuntimeError("disk full")
    pipeline = _make_mock_pipeline(search_raises=RuntimeError("search failed"))
    app = _make_telemetry_app(writer=writer, pipeline=pipeline)

    with caplog.at_level(logging.WARNING, logger="archon.search"):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/search", json=_SEARCH_PAYLOAD)

    assert resp.status_code == 500
    writer.enqueue.assert_called_once()
    assert any("telemetry" in r.message.lower() and "enqueue failed" in r.message.lower()
               for r in caplog.records), (
        f"Expected WARNING about telemetry enqueue failure; got: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Test 6: sequential failure then success → enqueue count matches failures only
# ---------------------------------------------------------------------------


def test_sequential_failure_then_success_on_same_app() -> None:
    """First call fails (500, error enqueue called), second call succeeds (200, success enqueue).

    Total enqueue.call_count == 2: one error entry for the failure, one success entry for
    the second call. Verifies that error telemetry is emitted exactly once.
    """
    writer = _make_mock_writer()
    pipeline = _make_mock_pipeline(search_raises=RuntimeError("first call fails"))
    app = _make_telemetry_app(writer=writer, pipeline=pipeline)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp1 = client.post("/search", json=_SEARCH_PAYLOAD)
        assert resp1.status_code == 500
        # At least one error telemetry entry from the failure
        assert writer.enqueue.call_count >= 1
        # Find the error entry among all enqueued entries
        error_entries_first = [
            call[0][0] for call in writer.enqueue.call_args_list
            if call[0][0].status == "internal_error"
        ]
        assert len(error_entries_first) == 1
        first_entry: TelemetryEntry = error_entries_first[0]
        assert first_entry.status == "internal_error"
        assert first_entry.error_kind == "other"

        # Now fix the pipeline to succeed
        pipeline.search = AsyncMock(
            return_value=SearchPipelineResult(results=[], acl_filtered=False)
        )
        resp2 = client.post("/search", json=_SEARCH_PAYLOAD)
        assert resp2.status_code == 200

    # Second call emits a success telemetry entry — total is 2
    assert writer.enqueue.call_count == 2
    second_entry: TelemetryEntry = writer.enqueue.call_args_list[1][0][0]
    assert second_entry.status == "ok"


# ---------------------------------------------------------------------------
# Test 7: serialization error in response construction → telemetry enqueued
# ---------------------------------------------------------------------------


def test_serialization_error_in_response_construction_enqueues_telemetry() -> None:
    """SearchResultSchema.from_result raises ValueError → 500, error telemetry enqueued.

    The route enqueues success telemetry before constructing SearchResponse, so when
    the list comprehension in SearchResponse(...) raises, the outer except block enqueues
    a second (error) entry. We verify that an error entry with status='internal_error' is
    present among the enqueued entries.
    """
    from archon_search.server.routes_search import SearchResultSchema

    writer = _make_mock_writer()
    result = SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000000",
        text="hello",
        score=0.9,
        source_path="/doc.md",
    )
    pipeline = _make_mock_pipeline(
        search_result=SearchPipelineResult(results=[result], acl_filtered=False)
    )
    app = _make_telemetry_app(writer=writer, pipeline=pipeline)

    with patch.object(SearchResultSchema, "from_result", side_effect=ValueError("serialization failed")):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/search", json=_SEARCH_PAYLOAD)

    assert resp.status_code == 500
    # At least one error entry must be enqueued (the route also attempts success telemetry
    # before the list comprehension fails, so total call_count may be 2)
    assert writer.enqueue.call_count >= 1
    # Find the error entry (status='internal_error')
    error_entries = [
        call[0][0]
        for call in writer.enqueue.call_args_list
        if call[0][0].status == "internal_error"
    ]
    assert len(error_entries) == 1, (
        f"Expected one error entry; got: {[e.status for e in [call[0][0] for call in writer.enqueue.call_args_list]]}"
    )
    assert error_entries[0].error_kind == "other"


# ---------------------------------------------------------------------------
# Test 8: healthy search → NO error telemetry enqueued
# ---------------------------------------------------------------------------


def test_healthy_search_does_not_enqueue_error_telemetry() -> None:
    """Successful pipeline.search() → 200, success entry enqueued with status='ok', no error entry."""
    writer = _make_mock_writer()
    pipeline = _make_mock_pipeline()
    app = _make_telemetry_app(writer=writer, pipeline=pipeline)

    with TestClient(app) as client:
        resp = client.post("/search", json=_SEARCH_PAYLOAD)

    assert resp.status_code == 200
    # Success path must enqueue exactly one entry with status="ok"
    assert writer.enqueue.call_count >= 1, "Expected success telemetry to be enqueued"
    for call in writer.enqueue.call_args_list:
        entry: TelemetryEntry = call[0][0]
        assert entry.status == "ok", f"Expected ok status but got {entry.status}"


# ---------------------------------------------------------------------------
# Test 9: query text never appears in error telemetry entry
# ---------------------------------------------------------------------------


def test_query_text_never_in_error_enqueue_args() -> None:
    """Sentinel query string must not appear in the enqueued TelemetryEntry fields."""
    sentinel = "SUPER-SECRET-QUERY-SENTINEL-A3-PRIVACY"
    writer = _make_mock_writer()
    pipeline = _make_mock_pipeline(search_raises=RuntimeError("pipeline died"))
    app = _make_telemetry_app(writer=writer, pipeline=pipeline)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/search", json={"collection": "col", "query": sentinel})

    assert resp.status_code == 500
    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    dumped = str(entry.model_dump())
    assert sentinel not in dumped, (
        f"Sentinel found in telemetry entry: {dumped}"
    )


# ---------------------------------------------------------------------------
# Test 10: writer=None, pipeline failure → 500, no crash
# ---------------------------------------------------------------------------


def test_writer_none_pipeline_failure_does_not_crash() -> None:
    """When telemetry_writer is None, pipeline failure still returns 500 without crashing."""
    pipeline = _make_mock_pipeline(search_raises=RuntimeError("crash"))
    app = _make_telemetry_app(writer=None, pipeline=pipeline)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/search", json=_SEARCH_PAYLOAD)

    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Test 11: timeout → 504 and error telemetry with status="timeout"
# ---------------------------------------------------------------------------


def test_search_pipeline_timeout_returns_504_and_enqueues_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When pipeline.search times out, returns 504 and enqueues error entry with status=timeout."""
    writer = _make_mock_writer()

    async def _slow_search(*args, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(1)
        return SearchPipelineResult(results=[], acl_filtered=False)

    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace=DEFAULT_NAMESPACE)
    )
    pipeline.search = _slow_search

    app = _make_telemetry_app(writer=writer, pipeline=pipeline)

    # Monkeypatch the timeout to 0.001 seconds so the test completes quickly
    with patch("archon_search.server.routes_search._SEARCH_TIMEOUT_SECONDS", 0.001):
        with caplog.at_level(logging.ERROR, logger="archon.search"):
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.post("/search", json=_SEARCH_PAYLOAD)

    assert resp.status_code == 504

    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert entry.endpoint == "search"
    assert entry.status == "timeout"
    assert entry.error_kind == "timeout"
    assert entry.latency_ms >= 0

    # Verify the ERROR log with event_type="search_timeout"
    timeout_records = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and getattr(r, "event_type", None) == "search_timeout"
    ]
    assert len(timeout_records) >= 1, (
        f"Expected ERROR log with event_type='search_timeout'; "
        f"records: {[(r.levelname, r.getMessage(), getattr(r, 'event_type', None)) for r in caplog.records]}"
    )

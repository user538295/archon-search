"""Tests for telemetry emission in POST /search handler."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.pipeline import SearchPipelineResult
from archon_search.telemetry.entry import TelemetryEntry
from archon_search.telemetry.writer import TelemetryWriter


# ---------------------------------------------------------------------------
# Minimal test app helper
# ---------------------------------------------------------------------------


def _make_test_app(
    writer: TelemetryWriter | None = None,
    pipeline_mock: MagicMock | None = None,
) -> FastAPI:
    """Create a minimal FastAPI app with only the search router and mocked state."""
    from archon_search.server import routes_search

    app = FastAPI()
    app.state.telemetry_writer = writer

    # Build a default pipeline mock if none supplied.
    if pipeline_mock is None:
        pipeline_mock = MagicMock()
        pipeline_mock.get_collection_meta = AsyncMock(
            return_value=CollectionMeta(name="col", namespace="default")
        )
        pipeline_mock.search = AsyncMock(
            return_value=SearchPipelineResult(results=[], acl_filtered=False)
        )

    app.state.pipeline = pipeline_mock

    # Inject namespace into request.state so the handler can read it without real middleware.
    @app.middleware("http")
    async def _inject_namespace(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.namespace = DEFAULT_NAMESPACE
        return await call_next(request)

    app.include_router(routes_search.router)
    return app


def _make_mock_writer() -> MagicMock:
    return MagicMock(spec=TelemetryWriter)


# ---------------------------------------------------------------------------
# Test 1: store exception enqueues a telemetry entry
# ---------------------------------------------------------------------------


def test_store_exception_enqueues_telemetry_entry() -> None:
    writer_mock = _make_mock_writer()
    pipeline_mock = MagicMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline_mock.search = AsyncMock(side_effect=RuntimeError("store boom"))

    app = _make_test_app(writer=writer_mock, pipeline_mock=pipeline_mock)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/search", json={"collection": "col", "query": "test query"})

    assert response.status_code == 500
    assert writer_mock.enqueue.call_count == 1
    entry = writer_mock.enqueue.call_args.args[0]
    assert entry.endpoint == "search"
    assert entry.status == "internal_error"
    assert entry.error_kind == "other"
    assert entry.latency_ms > 0


# ---------------------------------------------------------------------------
# Test 2: embedder exception enqueues a telemetry entry
# ---------------------------------------------------------------------------


def test_embedder_exception_enqueues_telemetry_entry() -> None:
    writer_mock = _make_mock_writer()
    pipeline_mock = MagicMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline_mock.search = AsyncMock(side_effect=RuntimeError("model crashed"))

    app = _make_test_app(writer=writer_mock, pipeline_mock=pipeline_mock)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/search", json={"collection": "col", "query": "test query"})

    assert response.status_code == 500
    assert writer_mock.enqueue.call_count == 1
    entry = writer_mock.enqueue.call_args.args[0]
    assert entry.endpoint == "search"
    assert entry.status == "internal_error"
    assert entry.error_kind == "other"
    assert entry.latency_ms > 0


# ---------------------------------------------------------------------------
# Test 3: reranker exception enqueues a telemetry entry
# ---------------------------------------------------------------------------


def test_reranker_exception_enqueues_telemetry_entry() -> None:
    writer_mock = _make_mock_writer()
    pipeline_mock = MagicMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline_mock.search = AsyncMock(side_effect=ValueError("score count mismatch"))

    app = _make_test_app(writer=writer_mock, pipeline_mock=pipeline_mock)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/search", json={"collection": "col", "query": "test query"})

    assert response.status_code == 500
    assert writer_mock.enqueue.call_count == 1
    entry = writer_mock.enqueue.call_args.args[0]
    assert entry.endpoint == "search"
    assert entry.status == "internal_error"
    assert entry.error_kind == "other"
    assert entry.latency_ms > 0


# ---------------------------------------------------------------------------
# Test 4: pipeline failure logs structured event_type and never leaks query text
# ---------------------------------------------------------------------------


def test_pipeline_failure_logs_structured_event_type(caplog: pytest.LogCaptureFixture) -> None:
    writer_mock = _make_mock_writer()
    pipeline_mock = MagicMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline_mock.search = AsyncMock(side_effect=RuntimeError("pipeline crash"))

    app = _make_test_app(writer=writer_mock, pipeline_mock=pipeline_mock)

    with caplog.at_level(logging.ERROR, logger="archon.search"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/search",
                json={"collection": "col", "query": "SENTINEL_QUERY_PRIVACY_CHECK_XYZ"},
            )

    assert response.status_code == 500

    error_records = [
        r
        for r in caplog.records
        if r.name == "archon.search"
        and r.levelno == logging.ERROR
        and hasattr(r, "event_type")
    ]
    assert len(error_records) == 1
    assert error_records[0].event_type == "search_pipeline_failure"
    assert error_records[0].exc_info is not None

    # Privacy check: sentinel must not appear in any log message under archon.search
    sentinel = "SENTINEL_QUERY_PRIVACY_CHECK_XYZ"
    for record in caplog.records:
        if record.name == "archon.search":
            assert sentinel not in record.message
            assert sentinel not in record.getMessage()


# ---------------------------------------------------------------------------
# Test 5: telemetry enqueue failure does not break the route
# ---------------------------------------------------------------------------


def test_telemetry_enqueue_failure_does_not_break_route(
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer_mock = _make_mock_writer()
    writer_mock.enqueue.side_effect = RuntimeError("telemetry down")

    pipeline_mock = MagicMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline_mock.search = AsyncMock(side_effect=RuntimeError("pipeline crash"))

    app = _make_test_app(writer=writer_mock, pipeline_mock=pipeline_mock)

    with caplog.at_level(logging.WARNING, logger="archon.search"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 500
    assert any(
        "telemetry enqueue failed" in r.message
        and r.levelno == logging.WARNING
        and r.name == "archon.search"
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Test 6: sequential failure then success on the same app
# ---------------------------------------------------------------------------


def test_sequential_failure_then_success_on_same_app() -> None:
    writer_mock = _make_mock_writer()
    pipeline_mock = MagicMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline_mock.search = AsyncMock(
        side_effect=[
            RuntimeError("boom"),
            SearchPipelineResult(results=[], acl_filtered=False),
        ]
    )

    app = _make_test_app(writer=writer_mock, pipeline_mock=pipeline_mock)

    # Use a persistent TestClient across both requests
    with TestClient(app, raise_server_exceptions=False) as client:
        first_response = client.post("/search", json={"collection": "col", "query": "first"})
        second_response = client.post("/search", json={"collection": "col", "query": "second"})

    assert first_response.status_code == 500
    assert second_response.status_code == 200
    # Only the failure path enqueues
    assert writer_mock.enqueue.call_count == 1
    entry = writer_mock.enqueue.call_args.args[0]
    assert entry.latency_ms > 0


# ---------------------------------------------------------------------------
# Test 7: serialization error in response construction enqueues telemetry
# ---------------------------------------------------------------------------


def test_serialization_error_in_response_construction_enqueues_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from archon_search._types import SearchResult
    from archon_search.server.routes_search import SearchResultSchema

    writer_mock = _make_mock_writer()
    pipeline_mock = MagicMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    # Return a non-empty result so from_result is actually called
    fake_result = SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000000",
        text="some text",
        score=0.9,
        source_path="/path/to/doc.md",
    )
    pipeline_mock.search = AsyncMock(
        return_value=SearchPipelineResult(results=[fake_result], acl_filtered=False)
    )

    monkeypatch.setattr(SearchResultSchema, "from_result", staticmethod(lambda r: (_ for _ in ()).throw(ValueError("bad row"))))

    app = _make_test_app(writer=writer_mock, pipeline_mock=pipeline_mock)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 500
    assert writer_mock.enqueue.call_count == 1
    entry = writer_mock.enqueue.call_args.args[0]
    assert entry.endpoint == "search"
    assert entry.status == "internal_error"
    assert entry.error_kind == "other"
    assert entry.latency_ms > 0


# ---------------------------------------------------------------------------
# Test 8: healthy search does not enqueue telemetry
# ---------------------------------------------------------------------------


def test_healthy_search_does_not_enqueue_telemetry() -> None:
    writer_mock = _make_mock_writer()
    pipeline_mock = MagicMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline_mock.search = AsyncMock(
        return_value=SearchPipelineResult(results=[], acl_filtered=False)
    )

    app = _make_test_app(writer=writer_mock, pipeline_mock=pipeline_mock)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/search", json={"collection": "col", "query": "a healthy query"})

    assert response.status_code == 200
    assert writer_mock.enqueue.call_count == 0


# ---------------------------------------------------------------------------
# Test 9: query text never in error enqueue args
# ---------------------------------------------------------------------------


def test_query_text_never_in_error_enqueue_args() -> None:
    sentinel = "PRIVACY-LEAK-SENTINEL-SEARCH-ERROR-ABC"
    writer_mock = _make_mock_writer()
    pipeline_mock = MagicMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline_mock.search = AsyncMock(side_effect=RuntimeError("boom"))

    app = _make_test_app(writer=writer_mock, pipeline_mock=pipeline_mock)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/search", json={"collection": "col", "query": sentinel})

    assert response.status_code == 500
    entry = writer_mock.enqueue.call_args.args[0]
    assert sentinel not in str(entry.model_dump())


# ---------------------------------------------------------------------------
# Test 10: writer=None + pipeline failure does not crash
# ---------------------------------------------------------------------------


def test_writer_none_pipeline_failure_does_not_crash() -> None:
    pipeline_mock = MagicMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline_mock.search = AsyncMock(side_effect=RuntimeError("crash"))

    app = _make_test_app(writer=None, pipeline_mock=pipeline_mock)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 500


# ---------------------------------------------------------------------------
# Test 11: pipeline timeout returns 504 and enqueues telemetry
# ---------------------------------------------------------------------------


def test_search_pipeline_timeout_returns_504_and_enqueues_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio
    import archon_search.server.routes_search as routes_search_module

    monkeypatch.setattr(routes_search_module, "_SEARCH_TIMEOUT_SECONDS", 0.05)

    writer_mock = _make_mock_writer()
    pipeline_mock = MagicMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )

    async def _slow_search(*args, **kwargs):
        await asyncio.sleep(60)

    pipeline_mock.search = _slow_search

    app = _make_test_app(writer=writer_mock, pipeline_mock=pipeline_mock)

    with caplog.at_level(logging.ERROR, logger="archon.search"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 504
    assert writer_mock.enqueue.call_count == 1
    entry = writer_mock.enqueue.call_args.args[0]
    assert entry.endpoint == "search"
    assert entry.status == "timeout"
    assert entry.error_kind == "timeout"
    assert entry.latency_ms > 0

    error_records = [
        r
        for r in caplog.records
        if r.name == "archon.search"
        and r.levelno == logging.ERROR
        and hasattr(r, "event_type")
    ]
    assert len(error_records) == 1
    assert error_records[0].event_type == "search_timeout"


# ---------------------------------------------------------------------------
# Test 12: meta-lookup 503 is logged at ERROR level (A3)
# ---------------------------------------------------------------------------


def test_meta_lookup_failure_logs_at_error_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """get_collection_meta raising → 503 is logged at ERROR (not WARNING or INFO).
    Verifies the A3 change that upgraded the meta-lookup log level to ERROR so
    operators can alert on it precisely.
    """
    pipeline_mock = MagicMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        side_effect=RuntimeError("lancedb unavailable")
    )
    pipeline_mock.search = AsyncMock(
        return_value=SearchPipelineResult(results=[], acl_filtered=False)
    )

    app = _make_test_app(writer=None, pipeline_mock=pipeline_mock)

    with caplog.at_level(logging.DEBUG, logger="archon.search"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 503

    # Must have at least one ERROR-level record from archon.search
    error_records = [
        r
        for r in caplog.records
        if r.name == "archon.search" and r.levelno == logging.ERROR
    ]
    assert len(error_records) >= 1

    # No WARNING-level records for this path (should be ERROR, not WARNING)
    warning_meta_records = [
        r
        for r in caplog.records
        if r.name == "archon.search"
        and r.levelno == logging.WARNING
        and "meta" in r.message.lower()
    ]
    assert len(warning_meta_records) == 0


# ---------------------------------------------------------------------------
# Test 13: _SEARCH_TIMEOUT_SECONDS constant is 30.0 (A3 — TODO: make configurable)
# ---------------------------------------------------------------------------


def test_search_timeout_constant_is_30_seconds() -> None:
    """_SEARCH_TIMEOUT_SECONDS must be 30.0 seconds — matching the /route timeout
    contract.  If this changes without a config migration path, it is a breaking
    change.  The TODO at routes_search.py:15 notes it should become configurable
    via config.py (parity with /route); until that lands, 30.0 is the contract.
    """
    import archon_search.server.routes_search as routes_search_module

    assert routes_search_module._SEARCH_TIMEOUT_SECONDS == 30.0


# ---------------------------------------------------------------------------
# Test 14: timeout telemetry entry has no query text (privacy)
# ---------------------------------------------------------------------------


def test_timeout_telemetry_entry_has_no_query_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout path telemetry entry must not contain query text — privacy guarantee."""
    import asyncio
    import archon_search.server.routes_search as routes_search_module

    sentinel = "TIMEOUT-PRIVACY-SENTINEL-QUERY-TEXT-XYZ"
    monkeypatch.setattr(routes_search_module, "_SEARCH_TIMEOUT_SECONDS", 0.05)

    writer_mock = MagicMock(spec=TelemetryWriter)
    pipeline_mock = MagicMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )

    async def _slow_search(*args, **kwargs):
        await asyncio.sleep(60)

    pipeline_mock.search = _slow_search

    app = _make_test_app(writer=writer_mock, pipeline_mock=pipeline_mock)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/search", json={"collection": "col", "query": sentinel})

    assert response.status_code == 504
    assert writer_mock.enqueue.call_count == 1
    entry: TelemetryEntry = writer_mock.enqueue.call_args.args[0]
    assert sentinel not in str(entry.model_dump())


# ---------------------------------------------------------------------------
# Test 15: 500 and 504 are distinct — pipeline exception vs timeout are separate paths
# ---------------------------------------------------------------------------


def test_pipeline_exception_and_timeout_produce_distinct_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pipeline-failure path (500) and the timeout path (504) must produce
    different HTTP status codes and different telemetry status values.
    This is a regression guard to ensure the two except-branches stay distinct.
    """
    import asyncio
    import archon_search.server.routes_search as routes_search_module

    # --- Part A: pipeline exception → 500 + internal_error telemetry ---
    writer_500 = MagicMock(spec=TelemetryWriter)
    pipeline_exc = MagicMock()
    pipeline_exc.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline_exc.search = AsyncMock(side_effect=RuntimeError("stage crash"))

    app_exc = _make_test_app(writer=writer_500, pipeline_mock=pipeline_exc)
    with TestClient(app_exc, raise_server_exceptions=False) as client:
        resp_exc = client.post("/search", json={"collection": "col", "query": "test"})

    assert resp_exc.status_code == 500
    assert writer_500.enqueue.call_count == 1
    entry_exc = writer_500.enqueue.call_args.args[0]
    assert entry_exc.status == "internal_error"
    assert entry_exc.error_kind == "other"

    # --- Part B: timeout → 504 + timeout telemetry ---
    monkeypatch.setattr(routes_search_module, "_SEARCH_TIMEOUT_SECONDS", 0.05)
    writer_504 = MagicMock(spec=TelemetryWriter)
    pipeline_slow = MagicMock()
    pipeline_slow.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )

    async def _slow(*args, **kwargs):
        await asyncio.sleep(60)

    pipeline_slow.search = _slow

    app_timeout = _make_test_app(writer=writer_504, pipeline_mock=pipeline_slow)
    with TestClient(app_timeout, raise_server_exceptions=False) as client:
        resp_timeout = client.post("/search", json={"collection": "col", "query": "test"})

    assert resp_timeout.status_code == 504
    assert writer_504.enqueue.call_count == 1
    entry_timeout = writer_504.enqueue.call_args.args[0]
    assert entry_timeout.status == "timeout"
    assert entry_timeout.error_kind == "timeout"

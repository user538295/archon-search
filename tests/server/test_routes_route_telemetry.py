"""Tests for telemetry emission in POST /route handler — FEAT-039b Task 3.4."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.telemetry.entry import TelemetryEntry
from archon_search.telemetry.writer import TelemetryWriter


# ---------------------------------------------------------------------------
# Minimal test app fixture
# ---------------------------------------------------------------------------


def _make_test_app(
    writer: TelemetryWriter | None = None,
    all_meta: list[CollectionMeta] | None = None,
) -> FastAPI:
    """Create a minimal FastAPI app with only the route router and mocked state."""
    from archon_search.config import SearchConfig
    from archon_search.server.routes_route import router

    app = FastAPI()
    app.state.config = SearchConfig()
    app.state.telemetry_writer = writer

    # Wire a mock search_store so the namespace filter can call get_all_collections_meta().
    # Namespace is injected via middleware below; all collections resolve to DEFAULT_NAMESPACE.
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=all_meta or [])
    app.state.search_store = mock_store

    # Inject namespace into request.state so the handler can read it without real middleware.
    @app.middleware("http")
    async def _inject_namespace(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.namespace = DEFAULT_NAMESPACE
        return await call_next(request)

    app.include_router(router)
    return app


def _make_mock_writer() -> MagicMock:
    return MagicMock(spec=TelemetryWriter)


# ---------------------------------------------------------------------------
# Helper: patch _build_router to return a fake router with pre_context result
# ---------------------------------------------------------------------------


class _FakeColRouter:
    """Minimal fake MultiCollectionRouter for unit tests."""

    def __init__(
        self,
        *,
        pre_context: str | None = "ctx",
        routable: list[str] | None = None,
        decomposer: bool = False,
        raise_on_get: BaseException | None = None,
    ) -> None:
        self._pre_context = pre_context
        self.last_routable_names: list[str] = routable or []
        self.decomposer_was_invoked: bool = decomposer
        self._raise = raise_on_get

    async def get_pre_context(self, **_kwargs: object) -> str | None:
        if self._raise is not None:
            raise self._raise
        return self._pre_context


# ---------------------------------------------------------------------------
# Test 1: success path → one routing entry, collections = pinned + routable
# ---------------------------------------------------------------------------


def test_route_handler_logs_entry_on_success() -> None:
    writer = _make_mock_writer()
    # col_a is pinned and belongs to DEFAULT_NAMESPACE so the namespace filter includes it.
    app = _make_test_app(writer, all_meta=[CollectionMeta(name="col_a", namespace=DEFAULT_NAMESPACE)])

    fake_router = _FakeColRouter(
        pre_context="some context",
        routable=["col_b"],
        decomposer=True,
    )

    with patch("archon_search.server.routes_route._build_router", return_value=fake_router):
        with patch("archon_search.server.routes_route.path_to_collection_name", return_value="col_a"):
            # Override pinned_collections to have one entry so col_a is pinned
            app.state.config.pinned_collections = ["/some/path"]
            with TestClient(app) as client:
                resp = client.post("/route", json={"query": "what is archon?"})

    assert resp.status_code == 200
    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert isinstance(entry, TelemetryEntry)
    assert entry.endpoint == "route"
    assert entry.status == "ok"
    assert entry.collections == ["col_a", "col_b"]
    assert entry.decomposer_invoked is True
    assert entry.latency_ms >= 0.0


# ---------------------------------------------------------------------------
# Test 2: empty query → 400 + entry error_kind="empty_query"
# ---------------------------------------------------------------------------


def test_route_handler_logs_error_entry_on_empty_query() -> None:
    writer = _make_mock_writer()
    app = _make_test_app(writer)

    with TestClient(app) as client:
        resp = client.post("/route", json={"query": ""})

    assert resp.status_code == 400
    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert entry.endpoint == "route"
    assert entry.status == "validation_error"
    assert entry.error_kind == "empty_query"
    assert entry.latency_ms >= 0.0


# ---------------------------------------------------------------------------
# Test 3: slots=0 → 400 + entry error_kind="slot_out_of_range"
# ---------------------------------------------------------------------------


def test_route_handler_logs_error_entry_on_invalid_slots() -> None:
    writer = _make_mock_writer()
    app = _make_test_app(writer)

    with TestClient(app) as client:
        resp = client.post("/route", json={"query": "hello", "slots": 0})

    assert resp.status_code == 400
    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert entry.endpoint == "route"
    assert entry.status == "validation_error"
    assert entry.error_kind == "slot_out_of_range"
    assert entry.latency_ms >= 0.0


# ---------------------------------------------------------------------------
# Test 4: routing timeout → 504 + entry error_kind="timeout"
# ---------------------------------------------------------------------------


def test_route_handler_logs_error_entry_on_timeout() -> None:
    writer = _make_mock_writer()
    app = _make_test_app(writer)

    fake_router = _FakeColRouter(raise_on_get=asyncio.TimeoutError())

    with patch("archon_search.server.routes_route._build_router", return_value=fake_router):
        with TestClient(app) as client:
            resp = client.post("/route", json={"query": "hello"})

    assert resp.status_code == 504
    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert entry.endpoint == "route"
    assert entry.status == "timeout"
    assert entry.error_kind == "timeout"
    assert entry.latency_ms >= 0.0


# ---------------------------------------------------------------------------
# Test 5: internal exception → entry error_kind="other", logger gets class name
# ---------------------------------------------------------------------------


def test_route_handler_logs_error_entry_on_internal_exception(caplog: pytest.LogCaptureFixture) -> None:
    writer = _make_mock_writer()
    app = _make_test_app(writer)

    fake_router = _FakeColRouter(raise_on_get=RuntimeError("boom"))

    with patch("archon_search.server.routes_route._build_router", return_value=fake_router):
        with caplog.at_level(logging.ERROR, logger="archon.search"):
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.post("/route", json={"query": "hello"})

    assert resp.status_code == 500
    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert entry.endpoint == "route"
    assert entry.status == "internal_error"
    assert entry.error_kind == "other"
    assert entry.latency_ms >= 0.0
    # The exception class name must appear in the log, not in the telemetry entry
    assert any("RuntimeError" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Test 6: query text NEVER in factory args
# ---------------------------------------------------------------------------


def test_route_handler_query_text_never_in_factory_args() -> None:
    writer = _make_mock_writer()
    app = _make_test_app(writer)
    sentinel = "SUPER_SECRET_QUERY_TEXT_XYZ_UNIQUE"

    fake_router = _FakeColRouter(pre_context="ctx", routable=["c"], decomposer=False)

    with patch("archon_search.server.routes_route._build_router", return_value=fake_router):
        with TestClient(app) as client:
            resp = client.post("/route", json={"query": sentinel})

    assert resp.status_code == 200
    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    dumped = str(entry.model_dump())
    assert sentinel not in dumped


# ---------------------------------------------------------------------------
# Test 7: writer=None → no exception, no enqueue call
# ---------------------------------------------------------------------------


def test_route_handler_no_entries_when_writer_none() -> None:
    app = _make_test_app(writer=None)

    fake_router = _FakeColRouter(pre_context="ctx", routable=[], decomposer=False)

    with patch("archon_search.server.routes_route._build_router", return_value=fake_router):
        with TestClient(app) as client:
            # Success path — should not raise even with no writer
            resp = client.post("/route", json={"query": "hello"})

    assert resp.status_code == 200

    # Validation error paths — should not raise with writer=None
    with TestClient(app) as client:
        resp = client.post("/route", json={"query": ""})
    assert resp.status_code == 400

    with TestClient(app) as client:
        resp = client.post("/route", json={"query": "hello", "slots": 0})
    assert resp.status_code == 400

    # Timeout path — should not raise with writer=None
    timeout_router = _FakeColRouter(raise_on_get=asyncio.TimeoutError())
    with patch("archon_search.server.routes_route._build_router", return_value=timeout_router):
        with TestClient(app) as client:
            resp = client.post("/route", json={"query": "hello"})
    assert resp.status_code == 504

    # Internal exception path — should not raise with writer=None
    crash_router = _FakeColRouter(raise_on_get=RuntimeError("crash"))
    with patch("archon_search.server.routes_route._build_router", return_value=crash_router):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/route", json={"query": "hello"})
    assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Test 8: _redact_validation fallback — unknown 400 detail maps to validation_error
# ---------------------------------------------------------------------------


def test_redact_validation_fallback_returns_validation_error() -> None:
    """_redact_validation unknown detail strings fall back to 'validation_error'."""
    from archon_search.server.routes_route import _redact_validation

    assert _redact_validation("some unexpected validation detail") == "validation_error"
    assert _redact_validation("") == "validation_error"
    assert _redact_validation("query must not be empty") == "empty_query"  # known string
    assert _redact_validation("slots must be >= 1") == "slot_out_of_range"  # known string


# ---------------------------------------------------------------------------
# Test 9: enqueue raises → response is still correct for all paths
# ---------------------------------------------------------------------------


def test_route_handler_enqueue_raises_does_not_affect_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If writer.enqueue raises on success path, the route response is unaffected."""
    writer = _make_mock_writer()
    writer.enqueue.side_effect = RuntimeError("disk full")
    app = _make_test_app(writer)

    fake_router = _FakeColRouter(pre_context="ctx", routable=["c"], decomposer=False)

    with patch("archon_search.server.routes_route._build_router", return_value=fake_router):
        with caplog.at_level(logging.WARNING, logger="archon.search"):
            with TestClient(app) as client:
                resp = client.post("/route", json={"query": "hello"})

    assert resp.status_code == 200
    writer.enqueue.assert_called_once()
    assert any("telemetry enqueue failed" in r.message for r in caplog.records)


def test_route_handler_enqueue_raises_on_timeout_still_returns_504(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Enqueue failure on timeout path must not swallow the 504."""
    writer = _make_mock_writer()
    writer.enqueue.side_effect = RuntimeError("disk full")
    app = _make_test_app(writer)

    fake_router = _FakeColRouter(raise_on_get=asyncio.TimeoutError())

    with patch("archon_search.server.routes_route._build_router", return_value=fake_router):
        with caplog.at_level(logging.WARNING, logger="archon.search"):
            with TestClient(app) as client:
                resp = client.post("/route", json={"query": "hello"})

    assert resp.status_code == 504
    writer.enqueue.assert_called_once()
    assert any("telemetry enqueue failed" in r.message for r in caplog.records)


def test_route_handler_enqueue_raises_on_exception_still_returns_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Enqueue failure on internal exception path must not swallow the 500."""
    writer = _make_mock_writer()
    writer.enqueue.side_effect = RuntimeError("disk full")
    app = _make_test_app(writer)

    fake_router = _FakeColRouter(raise_on_get=RuntimeError("crash"))

    with patch("archon_search.server.routes_route._build_router", return_value=fake_router):
        with caplog.at_level(logging.WARNING, logger="archon.search"):
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.post("/route", json={"query": "hello"})

    assert resp.status_code == 500
    writer.enqueue.assert_called_once()
    assert any("telemetry enqueue failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Test 10: query text sentinel must not appear in error-path telemetry entry
# ---------------------------------------------------------------------------


def test_route_handler_query_text_never_in_error_entry_args() -> None:
    """Query text sentinel must not appear in error-path telemetry entry fields."""
    sentinel = "PRIVACY-LEAK-SENTINEL-ROUTE-ERROR"
    writer = _make_mock_writer()
    app = _make_test_app(writer)

    fake_router = _FakeColRouter(raise_on_get=RuntimeError("internal boom"))

    with patch("archon_search.server.routes_route._build_router", return_value=fake_router):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/route", json={"query": sentinel})

    assert resp.status_code == 500
    writer.enqueue.assert_called_once()
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert sentinel not in str(entry.model_dump())


# ---------------------------------------------------------------------------
# Test 11: non-400 HTTPException → no telemetry entry, re-raises correctly
# ---------------------------------------------------------------------------


def test_route_handler_non_400_http_exception_produces_no_telemetry() -> None:
    """Non-400 HTTPExceptions are re-raised without telemetry (by design)."""
    from fastapi import HTTPException as _HTTPException

    writer = _make_mock_writer()
    app = _make_test_app(writer)

    with patch(
        "archon_search.server.routes_route._build_router",
        side_effect=_HTTPException(status_code=503, detail="service unavailable"),
    ):
        with TestClient(app) as client:
            resp = client.post("/route", json={"query": "hello"})

    assert resp.status_code == 503
    writer.enqueue.assert_not_called()

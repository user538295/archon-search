"""Tests for telemetry emission in POST /route handler — FEAT-039b Task 3.4."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, call, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from archon_search.telemetry.entry import TelemetryEntry
from archon_search.telemetry.writer import TelemetryWriter


# ---------------------------------------------------------------------------
# Minimal test app fixture
# ---------------------------------------------------------------------------


def _make_test_app(writer: TelemetryWriter | None = None) -> FastAPI:
    """Create a minimal FastAPI app with only the route router and mocked state."""
    from archon_search.config import SearchConfig
    from archon_search.server.routes_route import router

    app = FastAPI()
    app.state.config = SearchConfig()
    app.state.telemetry_writer = writer
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
    app = _make_test_app(writer)

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
    assert entry.collections is not None
    assert "col_a" in entry.collections  # pinned
    assert "col_b" in entry.collections  # routable
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
        with patch("archon_search.server.routes_route.asyncio.wait_for", side_effect=asyncio.TimeoutError()):
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

    # Also verify error paths don't raise with writer=None
    with TestClient(app) as client:
        resp = client.post("/route", json={"query": ""})
    assert resp.status_code == 400

    with TestClient(app) as client:
        resp = client.post("/route", json={"query": "hello", "slots": 0})
    assert resp.status_code == 400

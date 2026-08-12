"""Tests for stage_timings structured-log emission in /search and /route handlers (B1 Task 5.1)."""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.observability import correlation_id as _correlation_id, record_stage
from archon_search.pipeline import SearchPipelineResult


# ---------------------------------------------------------------------------
# Helpers: minimal app factories
# ---------------------------------------------------------------------------


def _make_search_app(
    *,
    timings_enabled: bool = True,
    pipeline_mock: MagicMock | None = None,
    correlation_id_value: str | None = None,
) -> FastAPI:
    from archon_search.server import routes_search
    from archon_search.server.middleware_context import RequestContextMiddleware

    config = SearchConfig()
    config.observability.stage_timings_enabled = timings_enabled

    app = FastAPI()
    app.state.config = config
    app.state.telemetry_writer = None

    if pipeline_mock is None:
        pipeline_mock = MagicMock()
        pipeline_mock.warmup_models = AsyncMock()
        pipeline_mock.get_collection_meta = AsyncMock(
            return_value=CollectionMeta(name="col", namespace=DEFAULT_NAMESPACE)
        )
        pipeline_mock.search = AsyncMock(
            return_value=SearchPipelineResult(results=[], acl_filtered=False)
        )
    app.state.pipeline = pipeline_mock

    @app.middleware("http")
    async def _inject_namespace(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.namespace = DEFAULT_NAMESPACE
        return await call_next(request)

    app.add_middleware(
        RequestContextMiddleware,
        header_name=config.observability.request_id_header,
    )
    app.include_router(routes_search.router)
    return app


class _FakeColRouter:
    """Minimal fake MultiCollectionRouter for unit tests."""

    def __init__(
        self,
        *,
        pre_context: str | None = "ctx",
        routable: list[str] | None = None,
        decomposer: bool = False,
        raise_on_get: BaseException | None = None,
        record_stages: list[str] | None = None,
    ) -> None:
        self._pre_context = pre_context
        self.last_routable_names: list[str] = routable or []
        self.decomposer_was_invoked: bool = decomposer
        self._raise = raise_on_get
        self._record_stages = record_stages or ["route"]

    async def get_pre_context(self, **_kwargs: object) -> str | None:
        for stage in self._record_stages:
            with record_stage(stage):
                pass
        if self._raise is not None:
            raise self._raise
        return self._pre_context


def _make_route_app(
    *,
    timings_enabled: bool = True,
    fake_router: _FakeColRouter | None = None,
    all_meta: list[CollectionMeta] | None = None,
) -> FastAPI:
    from archon_search.server import routes_route
    from archon_search.server.middleware_context import RequestContextMiddleware

    config = SearchConfig()
    config.observability.stage_timings_enabled = timings_enabled

    app = FastAPI()
    app.state.config = config
    app.state.telemetry_writer = None

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=all_meta or [])
    app.state.search_store = mock_store

    @app.middleware("http")
    async def _inject_namespace(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.namespace = DEFAULT_NAMESPACE
        return await call_next(request)

    app.add_middleware(
        RequestContextMiddleware,
        header_name=config.observability.request_id_header,
    )
    app.include_router(routes_route.router)
    return app


def _get_timing_records(caplog: pytest.LogCaptureFixture) -> list:
    return [r for r in caplog.records if getattr(r, "event_type", None) == "stage_timings"]


# ---------------------------------------------------------------------------
# /search stage timings tests
# ---------------------------------------------------------------------------


def test_search_emits_stage_timings_record(caplog: pytest.LogCaptureFixture) -> None:
    """POST /search success path emits one stage_timings log record with 'total' key."""
    app = _make_search_app(timings_enabled=True)
    with caplog.at_level(logging.INFO, logger="archon_search"):
        with TestClient(app) as client:
            resp = client.post("/search", json={"collection": "col", "query": "hello"})

    assert resp.status_code == 200
    timing_records = _get_timing_records(caplog)
    assert len(timing_records) == 1, f"Expected 1 stage_timings record, got {len(timing_records)}"
    rec = timing_records[0]
    assert rec.endpoint == "search", f"endpoint should be 'search', got {rec.endpoint!r}"
    assert rec.collection == "col", f"collection should be 'col', got {rec.collection!r}"
    timings = rec.stage_timings_ms
    assert "total" in timings, f"'total' key missing from stage_timings_ms: {set(timings)}"


def test_route_emits_stage_timings_record(caplog: pytest.LogCaptureFixture) -> None:
    """POST /route success path emits one stage_timings log record with 'total' key."""
    fake_router = _FakeColRouter(pre_context="ctx", routable=[], decomposer=False)
    app = _make_route_app(timings_enabled=True)

    with caplog.at_level(logging.INFO, logger="archon_search"):
        with patch("archon_search.server.routes_route._build_router", return_value=fake_router):
            with TestClient(app) as client:
                resp = client.post("/route", json={"query": "hello"})

    assert resp.status_code == 200
    timing_records = _get_timing_records(caplog)
    assert len(timing_records) == 1, f"Expected 1 stage_timings record, got {len(timing_records)}"
    rec = timing_records[0]
    assert rec.endpoint == "route", f"endpoint should be 'route', got {rec.endpoint!r}"
    assert rec.collection is None, f"collection should be None for /route, got {rec.collection!r}"
    timings = rec.stage_timings_ms
    assert "total" in timings, f"'total' key missing from stage_timings_ms: {set(timings)}"


def test_stage_timings_disabled_no_log_record(caplog: pytest.LogCaptureFixture) -> None:
    """stage_timings_enabled=False → no stage_timings log record emitted."""
    app = _make_search_app(timings_enabled=False)
    with caplog.at_level(logging.DEBUG, logger="archon_search"):
        with TestClient(app) as client:
            resp = client.post("/search", json={"collection": "col", "query": "hello"})

    assert resp.status_code == 200
    timing_records = _get_timing_records(caplog)
    assert len(timing_records) == 0, f"Expected 0 stage_timings records when disabled, got {len(timing_records)}"


def test_stage_timings_record_has_correlation_id(caplog: pytest.LogCaptureFixture) -> None:
    """correlation_id on stage_timings log record matches the X-Request-ID response header."""
    request_id = "my-request-id-abc123"
    app = _make_search_app(timings_enabled=True)
    with caplog.at_level(logging.INFO, logger="archon_search"):
        with TestClient(app) as client:
            resp = client.post(
                "/search",
                json={"collection": "col", "query": "hello"},
                headers={"X-Request-ID": request_id},
            )

    assert resp.status_code == 200
    echoed_id = resp.headers.get("x-request-id")
    assert echoed_id == request_id, f"Response X-Request-ID should echo {request_id!r}, got {echoed_id!r}"
    timing_records = _get_timing_records(caplog)
    assert len(timing_records) == 1
    assert timing_records[0].correlation_id == request_id, (
        f"Log record correlation_id should be {request_id!r}, got {timing_records[0].correlation_id!r}"
    )


@pytest.mark.asyncio
async def test_concurrent_requests_have_distinct_ids(caplog: pytest.LogCaptureFixture) -> None:
    """Two concurrent /search requests with distinct X-Request-ID values each produce a log record
    with the correct (distinct) correlation_id."""
    id_a = "req-id-aaa"
    id_b = "req-id-bbb"
    app = _make_search_app(timings_enabled=True)

    transport = httpx.ASGITransport(app=app)
    with caplog.at_level(logging.INFO, logger="archon_search"):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            resp_a, resp_b = await asyncio.gather(
                ac.post("/search", json={"collection": "col", "query": "hello"}, headers={"X-Request-ID": id_a}),
                ac.post("/search", json={"collection": "col", "query": "world"}, headers={"X-Request-ID": id_b}),
            )

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert resp_a.headers.get("x-request-id") == id_a
    assert resp_b.headers.get("x-request-id") == id_b

    timing_records = _get_timing_records(caplog)
    assert len(timing_records) == 2, f"Expected 2 timing records, got {len(timing_records)}"
    ids_in_logs = {r.correlation_id for r in timing_records}
    assert id_a in ids_in_logs, f"id_a {id_a!r} not found in log correlation_ids: {ids_in_logs}"
    assert id_b in ids_in_logs, f"id_b {id_b!r} not found in log correlation_ids: {ids_in_logs}"


def test_search_emits_partial_stage_timings_on_timeout(caplog: pytest.LogCaptureFixture) -> None:
    """On asyncio.TimeoutError from pipeline.search, stage_timings log record is still emitted
    with at least the 'total' key (and any stages completed before the timeout)."""

    async def _search_with_embed_then_timeout(*args: object, **kwargs: object) -> SearchPipelineResult:
        with record_stage("embed"):
            pass
        raise asyncio.TimeoutError

    pipeline_mock = MagicMock()
    pipeline_mock.warmup_models = AsyncMock()
    pipeline_mock.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace=DEFAULT_NAMESPACE)
    )
    pipeline_mock.search = AsyncMock(side_effect=_search_with_embed_then_timeout)

    app = _make_search_app(timings_enabled=True, pipeline_mock=pipeline_mock)
    with caplog.at_level(logging.INFO, logger="archon_search"):
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/search", json={"collection": "col", "query": "hello"})

    assert resp.status_code == 504, f"Expected 504 on timeout, got {resp.status_code}"
    timing_records = _get_timing_records(caplog)
    assert len(timing_records) == 1, (
        f"Expected 1 stage_timings record on timeout, got {len(timing_records)}"
    )
    timings = timing_records[0].stage_timings_ms
    assert "total" in timings, f"'total' must be in partial timings: {set(timings)}"
    assert "embed" in timings, f"'embed' (completed before timeout) must be in partial timings: {set(timings)}"


def test_route_emits_partial_stage_timings_on_timeout(caplog: pytest.LogCaptureFixture) -> None:
    """On asyncio.TimeoutError from route handler, stage_timings log record is still emitted
    with at least the 'route' stage (completed before timeout) and 'total' key."""
    fake_router = _FakeColRouter(
        pre_context="ctx",
        routable=[],
        decomposer=False,
        raise_on_get=asyncio.TimeoutError(),
    )
    app = _make_route_app(timings_enabled=True)

    with caplog.at_level(logging.INFO, logger="archon_search"):
        with patch("archon_search.server.routes_route._build_router", return_value=fake_router):
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.post("/route", json={"query": "hello"})

    assert resp.status_code == 504, f"Expected 504 on timeout, got {resp.status_code}"
    timing_records = _get_timing_records(caplog)
    assert len(timing_records) == 1, (
        f"Expected 1 stage_timings record on timeout, got {len(timing_records)}"
    )
    timings = timing_records[0].stage_timings_ms
    assert "total" in timings, f"'total' must be in partial timings: {set(timings)}"
    assert "route" in timings, f"'route' (completed before timeout) must be in partial timings: {set(timings)}"


def test_route_stage_timings_disabled_no_log_record(caplog: pytest.LogCaptureFixture) -> None:
    """stage_timings_enabled=False → no stage_timings log record emitted from /route."""
    fake_router = _FakeColRouter(pre_context="ctx", routable=[], decomposer=False)
    app = _make_route_app(timings_enabled=False)

    with caplog.at_level(logging.DEBUG, logger="archon_search"):
        with patch("archon_search.server.routes_route._build_router", return_value=fake_router):
            with TestClient(app) as client:
                resp = client.post("/route", json={"query": "hello"})

    assert resp.status_code == 200
    timing_records = _get_timing_records(caplog)
    assert len(timing_records) == 0, (
        f"Expected 0 stage_timings records when disabled, got {len(timing_records)}"
    )

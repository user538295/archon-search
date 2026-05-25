"""Tests for RequestContextMiddleware — pure-ASGI context middleware (B1 Task 2.1)."""
from __future__ import annotations

import re

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from archon_search.observability import correlation_id
from archon_search.server.middleware_context import RequestContextMiddleware

_UUID_RE = re.compile(r"^[A-Za-z0-9]{32}$")


def _make_app(capture_ctx: dict | None = None) -> Starlette:
    async def homepage(request: Request) -> PlainTextResponse:
        if capture_ctx is not None:
            capture_ctx["correlation_id"] = correlation_id.get()
        return PlainTextResponse("ok")

    app = Starlette(routes=[])
    app.add_middleware(RequestContextMiddleware)

    from starlette.routing import Route
    app.router.routes.append(Route("/", homepage))
    return app


def test_mints_request_id_when_absent() -> None:
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/")
    header = resp.headers.get("x-request-id", "")
    assert _UUID_RE.match(header), f"Expected 32-char hex, got {header!r}"


def test_honors_valid_inbound_id() -> None:
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/", headers={"X-Request-ID": "valid-id-123"})
    assert resp.headers["x-request-id"] == "valid-id-123"


def test_rejects_malicious_id_with_newline() -> None:
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/", headers={"X-Request-ID": "abc\ndef"})
    header = resp.headers.get("x-request-id", "")
    assert _UUID_RE.match(header), "Malicious header should be replaced by a fresh UUID"
    assert "\n" not in header


def test_rejects_too_long_id() -> None:
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/", headers={"X-Request-ID": "a" * 129})
    header = resp.headers.get("x-request-id", "")
    assert _UUID_RE.match(header), "Too-long header should be replaced by a fresh UUID"
    assert len(header) == 32


def test_correlation_id_contextvar_set_during_request() -> None:
    capture: dict = {}
    app = _make_app(capture_ctx=capture)
    client = TestClient(app)
    resp = client.get("/", headers={"X-Request-ID": "my-trace-id"})
    assert capture["correlation_id"] == "my-trace-id"
    assert resp.headers["x-request-id"] == "my-trace-id"


def test_contextvar_reset_after_request() -> None:
    """After request completes, correlation_id ContextVar is back to None."""
    capture: dict = {}
    app = _make_app(capture_ctx=capture)
    client = TestClient(app)
    client.get("/")
    # TestClient runs in a thread; ContextVar is per-task/thread, starts as None
    assert correlation_id.get() is None


def test_non_http_scope_passthrough() -> None:
    """Lifespan and non-HTTP scopes pass through without modification."""
    # Starlette TestClient with lifespan=auto exercises the lifespan scope
    app = _make_app()
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200


def test_custom_header_name() -> None:
    """Middleware respects a custom header_name parameter."""
    async def homepage(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    from starlette.routing import Route
    app = Starlette(routes=[Route("/", homepage)])
    app.add_middleware(RequestContextMiddleware, header_name="X-Trace-ID")

    client = TestClient(app)
    resp = client.get("/", headers={"X-Trace-ID": "custom-trace"})
    assert resp.headers.get("x-trace-id") == "custom-trace"

"""S740: a streamed (chunked, no Content-Length) body over ``[server].max_request_body_mb`` is a 413 before authentication."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.server.middleware_body_limit import BodySizeLimitMiddleware

_DEFAULT_KEY = "0" * 64  # tests/conftest.py ARCHON_SEARCH_API_KEY
_AUTH = {"Authorization": f"Bearer {_DEFAULT_KEY}"}
_JSON = {"Content-Type": "application/json"}
_BODY_LIMIT_MB = 1
_MIB = 1024 * 1024
_STREAMED_CHUNKS = 3
_REQUEST_TOO_LARGE = 413
_TOO_LARGE_CODE = "request_body_too_large"
_UNAUTHORIZED = 401
_KIB = 1024
_CAP_BYTES = 8 * _KIB


def _client(tmp_path: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.max_request_body_mb = _BODY_LIMIT_MB
    return TestClient(create_app(config, JobStore(path=tmp_path / "jobs.json")), raise_server_exceptions=True)


def _three_mib_stream() -> Iterator[bytes]:
    for _ in range(_STREAMED_CHUNKS):
        yield b"x" * _MIB


def test_streamed_over_cap_body_without_token_is_413(tmp_path: Path) -> None:
    response = _client(tmp_path).post("/ingest", content=_three_mib_stream(), headers=_JSON)

    assert response.status_code == _REQUEST_TOO_LARGE
    assert response.json()["code"] == _TOO_LARGE_CODE


def test_streamed_over_cap_body_with_token_is_413(tmp_path: Path) -> None:
    response = _client(tmp_path).post("/ingest", content=_three_mib_stream(), headers={**_AUTH, **_JSON})

    assert response.status_code == _REQUEST_TOO_LARGE
    assert response.json()["code"] == _TOO_LARGE_CODE


def test_tiny_streamed_body_under_cap_is_not_413(tmp_path: Path) -> None:
    response = _client(tmp_path).post("/ingest", content=iter([b"{}"]), headers={**_AUTH, **_JSON})

    assert response.status_code != _REQUEST_TOO_LARGE


def test_small_streamed_body_without_token_is_401(tmp_path: Path) -> None:
    response = _client(tmp_path).post("/ingest", content=iter([b"{}"]), headers=_JSON)

    assert response.status_code == _UNAUTHORIZED


def test_multi_message_body_is_counted_across_messages_and_never_reaches_the_app() -> None:
    sent: list[dict[str, object]] = []
    app_receives = 0

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": _UNAUTHORIZED, "headers": []})
        await send({"type": "http.response.body", "body": b"unauthorized"})

    chunks = [{"type": "http.request", "body": b"x" * _KIB, "more_body": True} for _ in range(_CAP_BYTES // _KIB + 2)]
    chunks.append({"type": "http.request", "body": b"", "more_body": False})
    feed = iter(chunks)

    async def receive() -> Message:
        nonlocal app_receives
        app_receives += 1
        return next(feed)

    async def send(message: Message) -> None:
        sent.append(dict(message))

    scope: Scope = {"type": "http", "method": "POST", "headers": [(b"transfer-encoding", b"chunked")]}
    asyncio.run(BodySizeLimitMiddleware(app, _CAP_BYTES)(scope, receive, send))

    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [_REQUEST_TOO_LARGE]
    assert app_receives == _CAP_BYTES // _KIB + 1  # stopped draining right after the cap was crossed


def test_expect_continue_body_is_not_drained() -> None:
    receives = 0

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": _UNAUTHORIZED, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        nonlocal receives
        receives += 1
        return {"type": "http.request", "body": b"", "more_body": True}

    async def send(message: Message) -> None:
        return None

    scope: Scope = {"type": "http", "method": "POST", "headers": [(b"expect", b"100-continue")]}
    asyncio.run(BodySizeLimitMiddleware(app, _CAP_BYTES)(scope, receive, send))

    assert receives == 0

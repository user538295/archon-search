"""T-2 item 4 (S12, S79): a client that aborts a search stops the work it started, so a ``claude`` child does not outlive it.

Without this, a search whose client went away kept its HyDE / RAG Fusion / graph call running to its natural end (the real
``claude`` child survived the whole call), spending the login's quota on an answer nobody reads.

The unit tests drive ``CancelOnDisconnectMiddleware`` with a scripted ASGI connection; the wire tests run it in front of a
``BaseHTTPMiddleware`` (like the application's authentication, behind which ``request.is_disconnected()`` never sees a
disconnect) on a real uvicorn server, closing a real socket mid-request and finishing a normal request. The real
application stack behind a real socket is covered by ``tests/integration/test_disconnect_cancels_provider_call_e2e.py``.

No test here waits for a clock to prove something did not happen: an absence is proved after the event loop was given
``_LOOP_TURNS`` turns to do it (every ready task runs in a turn), and every wait for something that must happen is an event
with the generous bound ``_DEADLINE_SECONDS``, so a loaded machine slows a test but cannot fail it.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from archon_search.server import disconnect
from archon_search.server.disconnect import CancelOnDisconnectMiddleware
from tests._uvicorn_server import LOOPBACK, serving

_DEADLINE_SECONDS = 30.0
_LOOP_TURNS = 50
_SEARCH = {"type": "http", "method": "POST", "path": "/search"}
_EXPLAIN = {"type": "http", "method": "POST", "path": "/explain"}
_BODY: Message = {"type": "http.request", "body": b"{}", "more_body": False}
_DISCONNECT: Message = {"type": "http.disconnect"}


async def _settle() -> None:
    """Give every task that is ready, and everything those schedule in turn, ``_LOOP_TURNS`` turns of the event loop."""
    for _ in range(_LOOP_TURNS):
        await asyncio.sleep(0)


async def _within(awaitable: Awaitable[Any]) -> Any:
    return await asyncio.wait_for(awaitable, _DEADLINE_SECONDS)


class _Connection:
    """A scripted ASGI connection: ``receive`` yields what ``push`` queued (the body first), ``send`` records."""

    def __init__(self, *, with_body: bool = True) -> None:
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()
        if with_body:
            self._inbox.put_nowait(dict(_BODY))
        self.receives = 0
        self.sent: list[Message] = []

    async def receive(self) -> Message:
        self.receives += 1
        return await self._inbox.get()

    async def send(self, message: Message) -> None:
        self.sent.append(message)

    def push(self, message: Message) -> None:
        self._inbox.put_nowait(dict(message))


def _middleware(app: Callable[..., Awaitable[None]]) -> CancelOnDisconnectMiddleware:
    return CancelOnDisconnectMiddleware(app, method="POST", paths=("/search", "/explain"))  # type: ignore[arg-type]


class _HeldApp:
    """An application that reads its body, says it is working, and parks until it is cancelled.

    Its cleanup after a cancel (like reaping a killed child) waits for ``cleanup_may_finish`` so a test decides how long
    the cleanup takes: ``cancelled`` is set the moment the cancel arrives, ``state`` says how the application ended.
    """

    def __init__(self, *, cleanup_gated: bool = False) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.cleanup_may_finish = asyncio.Event()
        if not cleanup_gated:
            self.cleanup_may_finish.set()
        self.state: dict[str, str] = {}

    async def __call__(self, scope: Scope, receive: Callable[[], Awaitable[Message]], send: object) -> None:
        await receive()  # the body, as a route reads it
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.cleanup_may_finish.wait()
            self.state["end"] = "cancelled"
            raise


@pytest.mark.parametrize("scope", [_SEARCH, _EXPLAIN], ids=["search", "explain"])
async def test_a_disconnect_cancels_the_application_and_waits_for_its_cleanup(scope: Scope) -> None:
    connection, held = _Connection(), _HeldApp(cleanup_gated=True)
    request = asyncio.create_task(_middleware(held)(scope, connection.receive, connection.send))
    await _within(held.started.wait())

    connection.push(_DISCONNECT)
    await _within(held.cancelled.wait())
    await _settle()

    assert not request.done(), "the middleware returned before the application finished its cleanup"
    held.cleanup_may_finish.set()
    await _within(request)
    assert held.state == {"end": "cancelled"}
    assert connection.sent == []


async def test_a_request_that_finishes_is_answered_untouched() -> None:
    connection = _Connection()

    async def app(scope: Scope, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        assert (await receive())["body"] == b"{}"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    await _within(_middleware(app)(_SEARCH, connection.receive, connection.send))

    assert [message["type"] for message in connection.sent] == ["http.response.start", "http.response.body"]


async def test_a_disconnect_reported_once_the_response_is_out_does_not_cancel_the_finishing_application() -> None:
    # The server reports http.disconnect as soon as the response is complete; the application may still be unwinding
    # (middleware teardown, background tasks) and must be left to finish.
    connection, ended = _Connection(), {}
    disconnect_pushed, finish = asyncio.Event(), asyncio.Event()

    async def app(scope: Scope, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        connection.push(_DISCONNECT)
        disconnect_pushed.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            ended["end"] = "cancelled"
            raise
        ended["end"] = "finished"

    request = asyncio.create_task(_middleware(app)(_SEARCH, connection.receive, connection.send))
    await _within(disconnect_pushed.wait())
    await _settle()

    assert not request.done() and ended == {}, "the finishing application was cancelled by the end-of-response disconnect"
    finish.set()
    await _within(request)
    assert ended == {"end": "finished"}


async def test_nothing_is_read_from_the_connection_before_the_application_reads() -> None:
    # The server sends ``100 Continue`` on the first read: reading ahead would answer an oversized upload's
    # ``Expect: 100-continue`` before the body-size middleware could refuse it.
    connection, go, reads_after_the_applications_first_read = _Connection(), asyncio.Event(), []

    async def app(scope: Scope, receive: Callable[[], Awaitable[Message]], send: object) -> None:
        await go.wait()
        await receive()
        reads_after_the_applications_first_read.append(connection.receives)

    request = asyncio.create_task(_middleware(app)(_SEARCH, connection.receive, connection.send))
    await _settle()
    assert connection.receives == 0

    go.set()
    await _within(request)

    assert reads_after_the_applications_first_read == [1]


async def test_the_connection_is_not_watched_while_the_body_is_still_arriving() -> None:
    connection = _Connection(with_body=False)
    connection.push({"type": "http.request", "body": b"{", "more_body": True})
    first_chunk_read, finish = asyncio.Event(), asyncio.Event()
    bodies: list[bytes] = []

    async def app(scope: Scope, receive: Callable[[], Awaitable[Message]], send: object) -> None:
        bodies.append((await receive())["body"])
        first_chunk_read.set()
        await finish.wait()
        bodies.append((await receive())["body"])

    request = asyncio.create_task(_middleware(app)(_SEARCH, connection.receive, connection.send))
    await _within(first_chunk_read.wait())
    await _settle()
    assert connection.receives == 1, "the middleware did not compete with the application for the rest of the body"

    connection.push({"type": "http.request", "body": b"}", "more_body": False})
    finish.set()
    await _within(request)

    assert bodies == [b"{", b"}"]


async def test_an_exception_of_the_application_propagates() -> None:
    connection = _Connection()

    async def app(scope: Scope, receive: object, send: object) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await _within(_middleware(app)(_SEARCH, connection.receive, connection.send))


async def test_a_failing_connection_read_cancels_the_application_and_is_raised() -> None:
    held = _HeldApp()
    reads = 0

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        if reads == 1:
            return dict(_BODY)
        await held.started.wait()
        raise ConnectionResetError("socket")

    with pytest.raises(ConnectionResetError):
        await _within(_middleware(held)(_SEARCH, receive, _Connection().send))

    assert held.state == {"end": "cancelled"}


async def test_a_connection_read_that_is_itself_cancelled_cancels_the_application_and_propagates_the_cancellation() -> None:
    held = _HeldApp()
    reads = 0

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        if reads == 1:
            return dict(_BODY)
        await held.started.wait()
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _within(_middleware(held)(_SEARCH, receive, _Connection().send))

    assert held.state == {"end": "cancelled"}


async def test_cancelling_the_request_cancels_the_application() -> None:
    connection, held = _Connection(), _HeldApp()
    request = asyncio.create_task(_middleware(held)(_SEARCH, connection.receive, connection.send))
    await _within(held.started.wait())

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await _within(request)

    assert held.state == {"end": "cancelled"}


async def test_an_application_that_fails_while_it_is_cancelled_is_logged_by_error_type_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=disconnect.__name__)
    connection, started = _Connection(), asyncio.Event()

    async def app(scope: Scope, receive: Callable[[], Awaitable[Message]], send: object) -> None:
        await receive()
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("secret-detail-of-the-cleanup") from None

    request = asyncio.create_task(_middleware(app)(_SEARCH, connection.receive, connection.send))
    await _within(started.wait())
    connection.push(_DISCONNECT)

    await _within(request)

    assert "request work raised RuntimeError while it was cancelled" in caplog.text
    assert "secret-detail-of-the-cleanup" not in caplog.text


async def test_an_application_that_swallows_the_cancel_still_lets_the_request_end() -> None:
    connection, started, swallowed = _Connection(), asyncio.Event(), asyncio.Event()

    async def app(scope: Scope, receive: Callable[[], Awaitable[Message]], send: object) -> None:
        await receive()
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            swallowed.set()  # finishes normally instead of re-raising

    request = asyncio.create_task(_middleware(app)(_SEARCH, connection.receive, connection.send))
    await _within(started.wait())
    connection.push(_DISCONNECT)

    await _within(request)

    assert swallowed.is_set()


async def test_the_cancel_is_logged_by_path_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=disconnect.__name__)
    connection, held = _Connection(), _HeldApp()
    request = asyncio.create_task(_middleware(held)(_SEARCH, connection.receive, connection.send))
    await _within(held.started.wait())

    connection.push(_DISCONNECT)
    await _within(request)

    assert "client disconnected from POST /search" in caplog.text


@pytest.mark.parametrize(
    "scope",
    [
        {"type": "http", "method": "GET", "path": "/search"},
        {"type": "http", "method": "POST", "path": "/ingest"},
        {"type": "websocket", "method": "POST", "path": "/search"},
    ],
)
async def test_other_requests_reach_the_application_with_the_servers_own_receive(scope: Scope) -> None:
    connection, seen = _Connection(), {}

    async def app(scope: Scope, receive: Callable[[], Awaitable[Message]], send: object) -> None:
        seen["receive"] = receive

    await _within(_middleware(app)(scope, connection.receive, connection.send))

    assert seen["receive"] == connection.receive


# --- the real thing: uvicorn, a BaseHTTPMiddleware, a socket that closes mid-request ---------------------------------


class _PassThrough(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        return await call_next(request)


class _Spy:
    """Outside the middleware under test: records the ``http.disconnect`` the server hands its connection reads."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self.disconnect_seen = threading.Event()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def spying_receive() -> Message:
            message = await receive()
            if message["type"] == "http.disconnect":
                self.disconnect_seen.set()
            return message

        await self._app(scope, spying_receive, send)


class _Wire:
    """The toy application of the wire tests and what its routes observed (thread-safe events: the server has its own loop)."""

    def __init__(self) -> None:
        self.search_started = threading.Event()
        self.search_cancelled = threading.Event()
        self.background_done = threading.Event()
        self.background_end = ""
        self.spy: _Spy | None = None
        self.app = self._build()

    def _build(self) -> ASGIApp:
        app = FastAPI()
        wire = self

        @app.post("/search")
        async def search(payload: dict[str, object]) -> JSONResponse:  # the real routes declare a body model: it is read
            wire.search_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                wire.search_cancelled.set()
                raise
            return JSONResponse({})

        @app.post("/explain")
        async def explain(payload: dict[str, object], background: BackgroundTasks) -> JSONResponse:
            async def after_the_response() -> None:
                assert wire.spy is not None
                try:
                    # The server answers http.disconnect as soon as the response is out; the middleware reads it and must
                    # not cancel this task. Wait until that read happened, then give the loop turns to do the wrong thing.
                    assert await asyncio.wait_for(asyncio.to_thread(wire.spy.disconnect_seen.wait, _DEADLINE_SECONDS), _DEADLINE_SECONDS)
                    await _settle()
                except asyncio.CancelledError:
                    wire.background_end = "cancelled"
                    wire.background_done.set()
                    raise
                wire.background_end = "finished"
                wire.background_done.set()

            background.add_task(after_the_response)
            return JSONResponse({"ok": True})

        app.add_middleware(_PassThrough)
        app.add_middleware(CancelOnDisconnectMiddleware, method="POST", paths=("/search", "/explain"))
        wire.spy = _Spy(app)
        return wire.spy


def _post(port: int, path: str) -> socket.socket:
    client = socket.create_connection((LOOPBACK, port))
    client.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{{}}".encode())
    client.settimeout(_DEADLINE_SECONDS)
    return client


def test_closing_the_socket_cancels_the_work_behind_a_real_server_and_an_authentication_style_middleware() -> None:
    wire = _Wire()
    with serving(wire.app) as port:
        client = _post(port, "/search")
        assert wire.search_started.wait(_DEADLINE_SECONDS), "the request never reached the work"

        client.close()

        assert wire.search_cancelled.wait(_DEADLINE_SECONDS), "closing the socket did not cancel the work"


def test_a_finished_request_keeps_its_background_work_behind_a_real_server() -> None:
    wire = _Wire()
    with serving(wire.app) as port:
        client = _post(port, "/explain")
        answer = b""
        while b'"ok"' not in answer:
            chunk = client.recv(4096)
            assert chunk, "the server closed before answering"
            answer += chunk
        client.close()

        assert wire.background_done.wait(_DEADLINE_SECONDS), "the background work never ended"

    assert wire.background_end == "finished"

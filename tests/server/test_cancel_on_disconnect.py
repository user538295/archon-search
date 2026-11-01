"""T-2 item 4 (S12, S79): a client that aborts a search stops the work it started, so a ``claude`` child does not outlive it.

Without this, a search whose client went away kept its HyDE / RAG Fusion / graph call running to its natural end (the real
``claude`` child survived the whole call), spending the login's quota on an answer nobody reads.

The unit tests drive ``CancelOnDisconnectMiddleware`` with a scripted ASGI connection; the wire tests run it in front of a
``BaseHTTPMiddleware`` (like the application's authentication, behind which ``request.is_disconnected()`` never sees a
disconnect) on a real uvicorn server, closing a real socket mid-request and finishing a normal request.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager

import pytest
import uvicorn
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message, Scope

from archon_search.server.disconnect import CancelOnDisconnectMiddleware

_NEVER_SECONDS = 60.0
_WIRE_WAIT_SECONDS = 5.0
_STARTUP_SECONDS = 10.0
_CLEANUP_SECONDS = 0.05
_SEARCH = {"type": "http", "method": "POST", "path": "/search"}
_EXPLAIN = {"type": "http", "method": "POST", "path": "/explain"}
_BODY: Message = {"type": "http.request", "body": b"{}", "more_body": False}
_DISCONNECT: Message = {"type": "http.disconnect"}


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


async def _slow_app(scope: Scope, receive: Callable[[], Awaitable[Message]], send: object, *, state: dict[str, str], started: asyncio.Event) -> None:
    await receive()  # the body, as a route reads it
    started.set()
    try:
        await asyncio.sleep(_NEVER_SECONDS)
    except asyncio.CancelledError:
        await asyncio.sleep(_CLEANUP_SECONDS)  # cleanup that takes a moment, like reaping a killed child
        state["end"] = "cancelled"
        raise


@pytest.mark.parametrize("scope", [_SEARCH, _EXPLAIN], ids=["search", "explain"])
async def test_a_disconnect_cancels_the_application_and_waits_for_its_cleanup(scope: Scope) -> None:
    connection, state, started = _Connection(), {}, asyncio.Event()

    async def app(scope: Scope, receive: Callable[[], Awaitable[Message]], send: object) -> None:
        await _slow_app(scope, receive, send, state=state, started=started)

    request = asyncio.create_task(_middleware(app)(scope, connection.receive, connection.send))
    await started.wait()

    connection.push(_DISCONNECT)
    await asyncio.wait_for(request, _WIRE_WAIT_SECONDS)

    assert state == {"end": "cancelled"}, "the application was cancelled and awaited before the middleware returned"
    assert connection.sent == []


async def test_a_request_that_finishes_is_answered_untouched() -> None:
    connection = _Connection()

    async def app(scope: Scope, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        assert (await receive())["body"] == b"{}"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    await _middleware(app)(_SEARCH, connection.receive, connection.send)

    assert [message["type"] for message in connection.sent] == ["http.response.start", "http.response.body"]


async def test_a_disconnect_reported_once_the_response_is_out_does_not_cancel_the_finishing_application() -> None:
    # The server reports http.disconnect as soon as the response is complete; the application may still be unwinding
    # (middleware teardown, background tasks) and must be left to finish.
    connection, ended = _Connection(), {}

    async def app(scope: Scope, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        connection.push(_DISCONNECT)
        try:
            await asyncio.sleep(_CLEANUP_SECONDS * 4)
        except asyncio.CancelledError:
            ended["end"] = "cancelled"
            raise
        ended["end"] = "finished"

    await _middleware(app)(_SEARCH, connection.receive, connection.send)

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
    await asyncio.sleep(_CLEANUP_SECONDS)
    assert connection.receives == 0

    go.set()
    await asyncio.wait_for(request, _WIRE_WAIT_SECONDS)

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
    await first_chunk_read.wait()
    await asyncio.sleep(_CLEANUP_SECONDS)
    assert connection.receives == 1, "the middleware did not compete with the application for the rest of the body"

    connection.push({"type": "http.request", "body": b"}", "more_body": False})
    finish.set()
    await asyncio.wait_for(request, _WIRE_WAIT_SECONDS)

    assert bodies == [b"{", b"}"]


async def test_an_exception_of_the_application_propagates() -> None:
    connection = _Connection()

    async def app(scope: Scope, receive: object, send: object) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await _middleware(app)(_SEARCH, connection.receive, connection.send)


async def test_a_failing_connection_read_cancels_the_application_and_is_raised() -> None:
    connection, state, started = _Connection(), {}, asyncio.Event()
    reads = 0

    async def receive() -> Message:
        nonlocal reads
        reads += 1
        if reads == 1:
            return dict(_BODY)
        await started.wait()
        raise ConnectionResetError("socket")

    async def app(scope: Scope, receive: Callable[[], Awaitable[Message]], send: object) -> None:
        await _slow_app(scope, receive, send, state=state, started=started)

    with pytest.raises(ConnectionResetError):
        await asyncio.wait_for(_middleware(app)(_SEARCH, receive, connection.send), _WIRE_WAIT_SECONDS)

    assert state == {"end": "cancelled"}


async def test_cancelling_the_request_cancels_the_application() -> None:
    connection, state, started = _Connection(), {}, asyncio.Event()

    async def app(scope: Scope, receive: Callable[[], Awaitable[Message]], send: object) -> None:
        await _slow_app(scope, receive, send, state=state, started=started)

    request = asyncio.create_task(_middleware(app)(_SEARCH, connection.receive, connection.send))
    await started.wait()

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert state == {"end": "cancelled"}


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

    await _middleware(app)(scope, connection.receive, connection.send)

    assert seen["receive"] == connection.receive


# --- the real thing: uvicorn, a BaseHTTPMiddleware, a socket that closes mid-request ---------------------------------


class _PassThrough(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        return await call_next(request)


def _wire_app(started: threading.Event, ended: dict[str, str]) -> FastAPI:
    app = FastAPI()

    @app.post("/search")
    async def search(payload: dict[str, object]) -> JSONResponse:  # the real routes declare a body model: the body is read
        started.set()
        try:
            await asyncio.sleep(_NEVER_SECONDS)
        except asyncio.CancelledError:
            ended["end"] = "cancelled"
            raise
        return JSONResponse({})

    @app.post("/explain")
    async def explain(payload: dict[str, object], background: BackgroundTasks) -> JSONResponse:
        async def after_the_response() -> None:
            try:
                await asyncio.sleep(_CLEANUP_SECONDS * 4)
            except asyncio.CancelledError:
                ended["background"] = "cancelled"
                raise
            ended["background"] = "finished"

        background.add_task(after_the_response)
        return JSONResponse({"ok": True})

    app.add_middleware(_PassThrough)
    app.add_middleware(CancelOnDisconnectMiddleware, method="POST", paths=("/search", "/explain"))
    return app


@contextmanager
def _serving(app: FastAPI) -> Iterator[int]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    deadline = time.monotonic() + _STARTUP_SECONDS
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    try:
        yield listener.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(_STARTUP_SECONDS)
        listener.close()


def _post(port: int, path: str) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port))
    client.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{{}}".encode())
    return client


def _wait_for(ended: dict[str, str], key: str) -> None:
    deadline = time.monotonic() + _WIRE_WAIT_SECONDS
    while key not in ended and time.monotonic() < deadline:
        time.sleep(0.02)


def test_closing_the_socket_cancels_the_work_behind_a_real_server_and_an_authentication_style_middleware() -> None:
    started, ended = threading.Event(), {}
    with _serving(_wire_app(started, ended)) as port:
        client = _post(port, "/search")
        assert started.wait(_WIRE_WAIT_SECONDS), "the request never reached the work"

        client.close()

        _wait_for(ended, "end")
    assert ended == {"end": "cancelled"}


def test_a_finished_request_keeps_its_background_work_behind_a_real_server() -> None:
    ended: dict[str, str] = {}
    with _serving(_wire_app(threading.Event(), ended)) as port:
        client = _post(port, "/explain")
        answer = b""
        while b'"ok"' not in answer:
            chunk = client.recv(4096)
            assert chunk, "the server closed before answering"
            answer += chunk
        client.close()

        _wait_for(ended, "background")
    assert ended == {"background": "finished"}

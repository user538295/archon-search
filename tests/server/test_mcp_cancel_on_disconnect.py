"""``CancelMcpToolOnDisconnectMiddleware``: a client that closes the stream of a search call cancels the call in its session.

The unit tests drive the middleware with a scripted ASGI application that behaves like the Streamable HTTP transport: it
reads the POST body, then reads the connection until the client is gone (sse-starlette's disconnect listener) and returns
without a complete response. The real MCP app behind a real socket is covered by
``tests/integration/test_disconnect_cancels_provider_call_e2e.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from starlette.types import Message, Receive, Scope, Send

from archon_search.server import mcp_disconnect
from archon_search.server.mcp import SEARCH_TOOL_NAMES
from archon_search.server.mcp_disconnect import CancelMcpToolOnDisconnectMiddleware
from tests._mcp_tools import registered_tools

_SESSION = b"0123456789abcdef"
_DEADLINE_SECONDS = 30.0
_REQUEST_BODY_SECRET = "a-query-that-must-not-reach-a-log"
_DISCONNECT: Message = {"type": "http.disconnect"}


def _scope(*, method: str = "POST", session: bytes | None = _SESSION, scope_type: str = "http") -> Scope:
    headers = [
        (b"content-type", b"application/json"),
        (b"accept", b"application/json, text/event-stream"),
        (b"content-length", b"999"),
        (b"mcp-protocol-version", b"2025-03-26"),
        (b"authorization", b"Bearer token"),
    ]
    if session is not None:
        headers.append((b"mcp-session-id", session))
    return {"type": scope_type, "method": method, "path": "/", "headers": headers}


def _call(tool: str = "search", request_id: Any = 7, **extra: Any) -> bytes:
    params = {"name": tool, "arguments": {"query": _REQUEST_BODY_SECRET}}
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params, **extra}).encode()


def _method_of(body: bytes) -> object:
    """The JSON-RPC method of *body*, or ``None`` when it is not a JSON object (the real transport would answer 400)."""
    try:
        message = json.loads(body)
    except ValueError:
        return None
    return message.get("method") if isinstance(message, dict) else None


class _Client:
    """A scripted client connection: ``receive`` yields the queued messages, a disconnect once they are gone."""

    def __init__(self, *chunks: bytes, disconnect: bool = True) -> None:
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()
        for index, chunk in enumerate(chunks):
            self._inbox.put_nowait({"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1})
        if disconnect:
            self._inbox.put_nowait(dict(_DISCONNECT))
        self.sent: list[Message] = []

    async def receive(self) -> Message:
        return await self._inbox.get()

    async def send(self, message: Message) -> None:
        self.sent.append(message)


class _Transport:
    """The application under the middleware: records every request it is handed, plays the transport's part."""

    def __init__(
        self,
        *,
        answer_before_return: bool = False,
        raises: Exception | None = None,
        fail_on_cancel: bool = False,
        partial_chunk_first: bool = False,
        parks_on_cancel: bool = False,
        writes_to_scope: bool = False,
    ) -> None:
        self.requests: list[tuple[Scope, bytes]] = []
        self.after_the_cancel_body: list[str] = []  # what a transport that reads on after the cancel's body is told next
        self.cancel_entered = asyncio.Event()
        self._answer_before_return = answer_before_return
        self._raises = raises
        self._fail_on_cancel = fail_on_cancel
        self._partial_chunk_first = partial_chunk_first
        self._parks_on_cancel = parks_on_cancel
        self._writes_to_scope = writes_to_scope

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        method = _method_of(body)
        self.requests.append((scope, body))
        if self._writes_to_scope and len(self.requests) == 1:
            scope["router_wrote"] = "endpoint"  # what a router does to the scope it is handed
        if self._raises is not None:
            raise self._raises
        if self._fail_on_cancel and len(self.requests) > 1:
            raise RuntimeError("the session is gone")
        if method == "notifications/cancelled":
            if self._parks_on_cancel:
                self.cancel_entered.set()
                await asyncio.Event().wait()
            self.after_the_cancel_body.append((await receive())["type"])
            await send({"type": "http.response.start", "status": 202, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        if self._partial_chunk_first:  # an SSE ping or a progress event goes out before the answer: the response is not complete
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b": ping", "more_body": True})
        if self._answer_before_return:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"data: {}", "more_body": False})
        while (await receive())["type"] != "http.disconnect":  # the SSE response's disconnect listener
            pass


def _middleware(transport: Callable[[Scope, Receive, Send], Awaitable[None]], tools: tuple[str, ...] = ("search", "explain")) -> CancelMcpToolOnDisconnectMiddleware:
    return CancelMcpToolOnDisconnectMiddleware(transport, tools=tools)


async def _run(middleware: CancelMcpToolOnDisconnectMiddleware, scope: Scope, client: _Client) -> None:
    await asyncio.wait_for(middleware(scope, client.receive, client.send), _DEADLINE_SECONDS)


def _cancels(transport: _Transport) -> list[dict[str, Any]]:
    return [json.loads(body) for _scope_seen, body in transport.requests[1:]]


async def test_a_disconnect_before_the_answer_sends_the_session_a_cancel_notification_for_the_request() -> None:
    transport = _Transport()

    await _run(_middleware(transport), _scope(), _Client(_call("search", 7)))

    assert len(transport.requests) == 2, "the original request, then the cancel notification"
    (cancel,) = _cancels(transport)
    assert cancel["jsonrpc"] == "2.0"
    assert cancel["method"] == "notifications/cancelled"
    assert "id" not in cancel, "a notification has no id"
    assert cancel["params"]["requestId"] == 7
    assert _REQUEST_BODY_SECRET not in json.dumps(cancel)


async def test_the_cancel_request_ends_with_the_end_of_the_connection_after_its_body() -> None:
    transport = _Transport()

    await _run(_middleware(transport), _scope(), _Client(_call("search")))

    assert transport.after_the_cancel_body == ["http.disconnect"]


async def test_the_cancel_carries_the_session_and_the_headers_the_transport_checks_and_its_own_length() -> None:
    transport = _Transport()

    await _run(_middleware(transport), _scope(), _Client(_call("explain")))

    cancel_scope, cancel_body = transport.requests[1]
    headers = dict(cancel_scope["headers"])
    assert cancel_scope["method"] == "POST"
    assert headers[b"mcp-session-id"] == _SESSION
    assert headers[b"mcp-protocol-version"] == b"2025-03-26"
    assert headers[b"content-type"] == b"application/json"
    assert headers[b"accept"] == b"application/json, text/event-stream"
    assert headers[b"content-length"] == str(len(cancel_body)).encode()
    assert [name for name, _value in cancel_scope["headers"]].count(b"content-length") == 1


@pytest.mark.parametrize("request_id", [7, "call-9", 0, "7"])
async def test_the_cancel_names_the_request_by_the_id_the_client_used(request_id: Any) -> None:
    transport = _Transport()

    await _run(_middleware(transport), _scope(), _Client(_call("search", request_id)))

    (cancel,) = _cancels(transport)
    assert cancel["params"]["requestId"] == request_id
    assert type(cancel["params"]["requestId"]) is type(request_id)


async def test_a_body_that_arrives_in_chunks_is_read_whole() -> None:
    transport = _Transport()
    body = _call("search", 11)

    await _run(_middleware(transport), _scope(), _Client(body[:20], body[20:60], body[60:]))

    (cancel,) = _cancels(transport)
    assert cancel["params"]["requestId"] == 11


async def test_an_answer_that_was_completed_is_not_cancelled() -> None:
    transport = _Transport(answer_before_return=True)
    client = _Client(_call("search"))

    await _run(_middleware(transport), _scope(), client)

    assert len(transport.requests) == 1
    assert [message["type"] for message in client.sent] == ["http.response.start", "http.response.body"]


@pytest.mark.parametrize("tool", ["ingest_file", "ingest_directory", "delete_document", "update_collection", "set_collection_description"])
async def test_a_tool_that_is_not_a_search_is_never_cancelled_by_a_disconnect(tool: str) -> None:
    transport = _Transport()

    await _run(_middleware(transport), _scope(), _Client(_call(tool)))

    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(), id="other-method"),
        pytest.param(json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "search"}}).encode(), id="notification-without-id"),
        pytest.param(json.dumps({"jsonrpc": "2.0", "id": None, "method": "tools/call", "params": {"name": "search"}}).encode(), id="null-id"),
        pytest.param(json.dumps({"jsonrpc": "2.0", "id": True, "method": "tools/call", "params": {"name": "search"}}).encode(), id="boolean-id"),
        pytest.param(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "search"}).encode(), id="params-not-an-object"),
        pytest.param(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 5}}).encode(), id="name-not-a-string"),
        pytest.param(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search"}}]).encode(), id="batch"),
        pytest.param(b"{not json", id="invalid-json"),
        pytest.param(b"", id="empty"),
    ],
)
async def test_a_body_that_is_not_one_search_tool_call_is_left_alone(body: bytes) -> None:
    transport = _Transport()

    await _run(_middleware(transport), _scope(), _Client(body))

    assert len(transport.requests) == 1


async def test_a_request_without_a_session_has_nothing_to_cancel() -> None:
    transport = _Transport()

    await _run(_middleware(transport), _scope(session=None), _Client(_call("search")))

    assert len(transport.requests) == 1


async def test_a_return_without_a_disconnect_is_not_a_cancel() -> None:
    """The transport can return for other reasons (an error answer, the session closing): only a lost client cancels."""
    transport = _Transport(answer_before_return=False)
    client = _Client(_call("search"), disconnect=False)

    async def returns_without_answer(scope: Scope, receive: Receive, send: Send) -> None:
        transport.requests.append((scope, (await receive())["body"]))

    await _run(_middleware(returns_without_answer), _scope(), client)

    assert len(transport.requests) == 1


@pytest.mark.parametrize(("extra_bytes", "cancelled"), [(0, True), (1, False)], ids=["exactly-the-limit", "one-byte-more"])
async def test_the_capture_limit_is_inclusive(monkeypatch: pytest.MonkeyPatch, extra_bytes: int, cancelled: bool) -> None:
    body = _call("search")
    monkeypatch.setattr(mcp_disconnect, "MAX_CAPTURED_BODY_BYTES", len(body) - extra_bytes)
    transport = _Transport()

    await _run(_middleware(transport), _scope(), _Client(body))

    assert (len(transport.requests) == 2) is cancelled


async def test_a_body_beyond_the_capture_limit_is_not_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_disconnect, "MAX_CAPTURED_BODY_BYTES", 10)
    transport = _Transport()

    await _run(_middleware(transport), _scope(), _Client(_call("search")))

    assert len(transport.requests) == 1


async def test_a_partial_answer_before_the_disconnect_is_still_a_cancel() -> None:
    """Real streams send ``more_body`` chunks (pings, progress) before the answer: only the last chunk completes a response."""
    transport = _Transport(partial_chunk_first=True)

    await _run(_middleware(transport), _scope(), _Client(_call("search")))

    assert len(transport.requests) == 2


async def test_the_cancel_is_built_from_the_scope_the_request_entered_with() -> None:
    """A router writes into the scope it is handed (``endpoint``, ``path_params``): the cancel must not inherit that."""
    transport = _Transport(writes_to_scope=True)

    await _run(_middleware(transport), _scope(), _Client(_call("search")))

    first_scope, cancel_scope = transport.requests[0][0], transport.requests[1][0]
    assert first_scope["router_wrote"] == "endpoint"
    assert "router_wrote" not in cancel_scope


async def test_the_cancel_does_not_carry_the_transfer_encoding_of_the_original_request() -> None:
    transport = _Transport()
    scope = _scope()
    scope["headers"].append((b"transfer-encoding", b"chunked"))

    await _run(_middleware(transport), scope, _Client(_call("search")))

    assert b"transfer-encoding" not in dict(transport.requests[1][0]["headers"])


async def test_a_cancel_the_transport_never_takes_is_given_up_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=mcp_disconnect.__name__)
    monkeypatch.setattr(mcp_disconnect, "CANCEL_DELIVERY_TIMEOUT_SECONDS", 0.01)
    transport = _Transport(parks_on_cancel=True)

    await _run(_middleware(transport), _scope(), _Client(_call("search")))

    assert transport.cancel_entered.is_set()
    assert "could not deliver the cancel" in caplog.text
    assert "TimeoutError" in caplog.text


@pytest.mark.parametrize("scope", [_scope(method="GET"), _scope(method="DELETE"), _scope(scope_type="websocket"), _scope(scope_type="lifespan")])
async def test_other_requests_reach_the_application_with_the_servers_own_receive_and_send(scope: Scope) -> None:
    seen: dict[str, Any] = {}
    client = _Client()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.update(receive=receive, send=send)

    await _middleware(app)(scope, client.receive, client.send)

    assert seen == {"receive": client.receive, "send": client.send}


async def test_an_exception_of_the_application_propagates_and_cancels_nothing() -> None:
    transport = _Transport(raises=ValueError("boom"))

    with pytest.raises(ValueError, match="boom"):
        await _run(_middleware(transport), _scope(), _Client(_call("search")))

    assert len(transport.requests) == 1


async def test_a_cancel_the_session_cannot_take_is_logged_without_content_and_does_not_fail_the_request(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=mcp_disconnect.__name__)
    transport = _Transport(fail_on_cancel=True)

    await _run(_middleware(transport), _scope(), _Client(_call("search")))

    assert "could not deliver the cancel" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "the session is gone" not in caplog.text
    assert _REQUEST_BODY_SECRET not in caplog.text
    assert _SESSION.decode() not in caplog.text


async def test_the_cancel_is_logged_with_the_tool_name_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=mcp_disconnect.__name__)

    await _run(_middleware(_Transport()), _scope(), _Client(_call("search", "id-that-is-the-clients-text")))

    assert "MCP search call" in caplog.text
    assert _REQUEST_BODY_SECRET not in caplog.text
    assert _SESSION.decode() not in caplog.text
    assert "id-that-is-the-clients-text" not in caplog.text


async def test_cancelling_the_request_task_while_the_cancel_is_delivered_propagates_the_cancellation() -> None:
    """The cancel delivery is the part that catches ``Exception``: a cancellation of the request must still get through it."""
    transport, client = _Transport(parks_on_cancel=True), _Client(_call("search"))
    request = asyncio.create_task(_middleware(transport)(_scope(), client.receive, client.send))
    await asyncio.wait_for(transport.cancel_entered.wait(), _DEADLINE_SECONDS)

    request.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, _DEADLINE_SECONDS)


def test_every_cancelled_tool_name_is_a_registered_tool() -> None:
    """A renamed search tool would silently lose its cancellation: the names must exist in the real tool list."""
    assert SEARCH_TOOL_NAMES <= set(registered_tools(None))
    assert {"search", "search_with_context", "explain"} == set(SEARCH_TOOL_NAMES)

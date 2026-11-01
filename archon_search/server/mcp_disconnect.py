"""Stop the search an MCP client abandoned by closing its stream (T-2: a ``claude`` child outlived an aborted MCP search).

An MCP ``tools/call`` is a JSON-RPC request the Streamable HTTP transport hands to the session; the tool then runs as a task
of that session, not of the HTTP request. When the client closes the connection of the POST the response stream ends, but the
tool keeps running to its natural end: a HyDE or RAG Fusion call (a real ``claude`` child) goes on spending the login's quota
for an answer nobody can read. The MCP library cancels a tool only on an explicit ``notifications/cancelled``, which a client
that crashed or lost its network never sends.

:class:`CancelMcpToolOnDisconnectMiddleware` closes that gap on the one path that matters. It wraps the transport (inside the
authentication and request-context layers, which already vouched for the request), notes the JSON-RPC request the application
reads, and when the application returns because the connection was lost before the response was complete it sends the
session the cancel notification the client never did, through the application itself: the library then cancels the tool the
way it does for a client's own notification (the ``claude`` wrapper kills and reaps its child on cancellation).

Only the tools named at construction are cancelled: the read-only searches. A disconnect never aborts a tool that writes
(``ingest_*``, ``delete_*``, ``update_collection``, the description tools): a client that sends one and drops the stream still
gets the whole operation, as it does today, and can cancel it explicitly. The MCP specification advises not to read a
disconnect as a cancel because a stream may be resumable; this server's streams are not (no event store is configured), so
the answer of an abandoned call can never be delivered and finishing it only costs money. REST ``POST /search`` and
``POST /explain`` are covered by :class:`archon_search.server.disconnect.CancelOnDisconnectMiddleware`.

Limits. A request that is not cancelled: one pipelined behind another on the same connection (uvicorn marks only the newest
request of a connection as disconnected), one whose body is larger than ``MAX_CAPTURED_BODY_BYTES`` (never parsed), one that
carries no ``mcp-session-id`` (the stateless transport of MCP protocol 2026-07-28, which cancels on disconnect itself). A
client that reuses a JSON-RPC request id while the first call with it is still running breaks MCP, which requires unique ids
per session; the cancel then names that id and the library cancels the call that holds it, as it would for the client's own
notification.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Collection
from typing import Any, Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: A ``tools/call`` body is a few hundred bytes; a body past this is not parsed (nothing is cancelled for it).
MAX_CAPTURED_BODY_BYTES: Final = 1 << 20

#: Longest wait for the application to take the cancel notification: it answers ``202`` at once, so a longer wait means a
#: stuck transport, and the already abandoned request must not be held by it.
CANCEL_DELIVERY_TIMEOUT_SECONDS: Final = 5.0

_DISCONNECT_TYPE: Final = "http.disconnect"
_SESSION_HEADER: Final = b"mcp-session-id"
_CONTENT_LENGTH_HEADER: Final = b"content-length"
_TRANSFER_ENCODING_HEADER: Final = b"transfer-encoding"
_TOOL_CALL_METHOD: Final = "tools/call"
_CANCEL_METHOD: Final = "notifications/cancelled"
_CANCEL_REASON: Final = "the client closed the connection"


class CancelMcpToolOnDisconnectMiddleware:
    """Cancels the ``tools/call`` of a *tools* member in its session when the client's connection ends before the answer."""

    def __init__(self, app: ASGIApp, *, tools: Collection[str]) -> None:
        self._app = app
        self._tools = frozenset(tools)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self._app(scope, receive, send)
            return

        entry_scope = dict(scope)  # the routers below write into the scope they are given
        body = bytearray()
        body_complete = True
        disconnected = False
        response_complete = False

        async def tracking_receive() -> Message:
            nonlocal body_complete, disconnected
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > MAX_CAPTURED_BODY_BYTES:
                    body_complete = False
                else:
                    body.extend(chunk)
            elif message["type"] == _DISCONNECT_TYPE:
                disconnected = True
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_complete
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete = True

        await self._app(scope, tracking_receive, tracking_send)

        if not disconnected or response_complete or not body_complete:
            return
        call = _tool_call(bytes(body))
        session = _header(entry_scope, _SESSION_HEADER)
        if call is None or call[0] not in self._tools or session is None:
            return
        tool, request_id = call
        logger.info("client disconnected from the MCP %s call: cancelling it", tool)
        await self._deliver_cancel(entry_scope, request_id)

    async def _deliver_cancel(self, entry_scope: Scope, request_id: int | str) -> None:
        """Post ``notifications/cancelled`` for *request_id* to the session of the original request, as its client would."""
        cancel = json.dumps(
            {"jsonrpc": "2.0", "method": _CANCEL_METHOD, "params": {"requestId": request_id, "reason": _CANCEL_REASON}}
        ).encode()
        headers = [
            (name, value)
            for name, value in entry_scope["headers"]
            if name not in (_CONTENT_LENGTH_HEADER, _TRANSFER_ENCODING_HEADER)
        ]
        headers.append((_CONTENT_LENGTH_HEADER, str(len(cancel)).encode()))
        scope = {**entry_scope, "headers": headers}
        delivered = False

        async def receive() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": _DISCONNECT_TYPE}
            delivered = True
            return {"type": "http.request", "body": cancel, "more_body": False}

        async def discard(message: Message) -> None:
            return None

        try:
            async with asyncio.timeout(CANCEL_DELIVERY_TIMEOUT_SECONDS):
                await self._app(scope, receive, discard)
        except Exception as exc:  # noqa: BLE001 — a cancel the session cannot take (it ended) must never fail the finished request
            logger.warning("could not deliver the cancel of an abandoned MCP call (%s)", type(exc).__name__)


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope["headers"]:
        if key == name:
            return value
    return None


def _tool_call(body: bytes) -> tuple[str, int | str] | None:
    """``(tool name, JSON-RPC id)`` when *body* is one ``tools/call`` request, else ``None``."""
    try:
        message: Any = json.loads(body)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("method") != _TOOL_CALL_METHOD:
        return None
    request_id, params = message.get("id"), message.get("params")
    if isinstance(request_id, bool) or not isinstance(request_id, int | str) or not isinstance(params, dict):
        return None
    name = params.get("name")
    return (name, request_id) if isinstance(name, str) else None

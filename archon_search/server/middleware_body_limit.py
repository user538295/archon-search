"""Pure-ASGI request-body size cap (SecReview #7a).

Without a cap the server buffers whatever a client sends: one inline ``documents`` ingest of a few gigabytes pins
that much memory before any validation runs. The cap is checked twice: the declared ``Content-Length`` is refused
before a single body byte is read, and the bytes actually received are counted for bodies that declare no (or a
wrong) length, such as chunked uploads.
A streamed body the application has not read when it starts to answer (authentication refuses without reading) is
counted and discarded up to the cap first, so an over-cap body is a 413 before a 401, without being buffered.

A body that outgrows the cap mid-stream is reported to the application as a client disconnect, so no framework
layer between this middleware and the route (Starlette's ``BaseHTTPMiddleware`` wraps errors raised while reading
in an ``ExceptionGroup``, FastAPI turns them into a 400) can change the outcome: whatever the application then
answers is replaced by the one sanitized 413.
"""
from __future__ import annotations

import logging
from typing import Final

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

REQUEST_TOO_LARGE_STATUS: Final[int] = 413
REQUEST_BODY_TOO_LARGE_DETAIL: Final[str] = "request body too large"
REQUEST_BODY_TOO_LARGE_CODE: Final[str] = "request_body_too_large"
_CONTENT_LENGTH: Final[bytes] = b"content-length"
_EXPECT: Final[bytes] = b"expect"
_DISCONNECT: Final[dict[str, str]] = {"type": "http.disconnect"}


def _too_large_response() -> JSONResponse:
    return JSONResponse(
        {"detail": REQUEST_BODY_TOO_LARGE_DETAIL, "code": REQUEST_BODY_TOO_LARGE_CODE},
        status_code=REQUEST_TOO_LARGE_STATUS,
    )


def _declared_length(scope: Scope) -> int | None:
    """The request's ``Content-Length``, or ``None`` when it is absent or not a non-negative integer."""
    for name, value in scope.get("headers", []):
        if name == _CONTENT_LENGTH:
            try:
                length = int(value)
            except ValueError:
                return None
            return length if length >= 0 else None
    return None


def _awaits_continue(scope: Scope) -> bool:
    """True when the client announced ``Expect: 100-continue``: it uploads only after the server reads the body."""
    return any(name == _EXPECT for name, _ in scope.get("headers", []))


class BodySizeLimitMiddleware:
    """Refuses a request whose body exceeds *max_bytes* with a sanitized 413."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self._max_bytes:
            await _too_large_response()(scope, receive, send)
            return

        received = 0
        exceeded = False
        answered = False
        body_complete = False
        # A request without a usable Content-Length (chunked) is only measured as the application reads it, and an
        # application that answers without reading (authentication) would otherwise let any amount through.
        measure_unread = declared is None and not _awaits_continue(scope)

        async def refuse() -> None:
            nonlocal answered
            if not answered:
                answered = True
                await _too_large_response()(scope, receive, send)

        async def counting_receive() -> Message:
            nonlocal received, exceeded, body_complete
            if exceeded:
                return dict(_DISCONNECT)
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    exceeded = True
                    return dict(_DISCONNECT)
                body_complete = not message.get("more_body", False)
            else:
                body_complete = True
            return message

        async def measure_rest_of_body() -> None:
            """Counts and discards the unread remainder; stops at the end of the body or once the cap is exceeded."""
            while not body_complete and not exceeded:
                await counting_receive()

        async def guarded_send(message: Message) -> None:
            nonlocal answered
            if message["type"] == "http.response.start" and measure_unread and not exceeded:
                await measure_rest_of_body()
            if exceeded:
                if message["type"] == "http.response.start":
                    await refuse()
                return
            if message["type"] == "http.response.start":
                answered = True
            await send(message)

        try:
            await self._app(scope, counting_receive, guarded_send)
        except Exception as exc:
            if not exceeded:
                raise
            logger.warning(
                "request body exceeded %d bytes and the application then raised %s; answering 413",
                self._max_bytes,
                type(exc).__name__,
                exc_info=True,
            )
        if exceeded:
            await refuse()


def install_body_size_limit(app: FastAPI, max_bytes: int) -> None:
    """Cap request bodies of *app* at *max_bytes*; the 413 carries a fixed ``{"detail", "code"}`` body."""
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)

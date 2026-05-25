"""Pure-ASGI RequestContextMiddleware — sets correlation_id ContextVar (B1)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from archon_search.observability import (
    correlation_id,
    new_correlation_id,
    sanitize_request_id,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Any


class RequestContextMiddleware:
    """Pure-ASGI middleware that mints/validates X-Request-ID and sets correlation_id ContextVar."""

    def __init__(self, app: Any, header_name: str = "X-Request-ID") -> None:
        self._app = app
        self._header_name_bytes = header_name.lower().encode()
        self._header_name_str = header_name

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        raw_id: str | None = None
        for name, value in scope.get("headers", []):
            if name.lower() == self._header_name_bytes:
                raw_id = value.decode("latin-1", errors="replace")
                break

        sanitized = sanitize_request_id(raw_id)
        request_id = sanitized if sanitized is not None else new_correlation_id()
        token = correlation_id.set(request_id)

        header_pair = (b"x-request-id", request_id.encode())
        # Use the configured header name for the response too
        response_header_bytes = self._header_name_bytes

        async def send_with_header(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers = [(n, v) for n, v in headers if n.lower() != response_header_bytes]
                headers.append((response_header_bytes, request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self._app(scope, receive, send_with_header)
        finally:
            correlation_id.reset(token)

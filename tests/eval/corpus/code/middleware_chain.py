"""Middleware chain pattern for async request processing."""
from __future__ import annotations
from typing import Any, Awaitable, Callable

Handler = Callable[[dict], Awaitable[dict]]
Middleware = Callable[[dict, Handler], Awaitable[dict]]


class MiddlewareChain:
    """Build a chain of async middlewares around a core handler."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self._middlewares: list[Middleware] = []

    def use(self, middleware: Middleware) -> "MiddlewareChain":
        self._middlewares.append(middleware)
        return self

    async def handle(self, request: dict) -> dict:
        async def call_next(req: dict, idx: int) -> dict:
            if idx >= len(self._middlewares):
                return await self._handler(req)
            mw = self._middlewares[idx]
            return await mw(req, lambda r: call_next(r, idx + 1))

        return await call_next(request, 0)

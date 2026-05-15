"""Bearer token authentication middleware for archon-search (Task 1.2)."""
from __future__ import annotations

import logging
import secrets
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("archon-search")

_EXEMPT_METHOD = "GET"
_EXEMPT_PATH = "/health"


class APIKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, api_key: str) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.method == _EXEMPT_METHOD and request.url.path == _EXEMPT_PATH:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        parts = auth_header.split(" ", 1)
        if (
            len(parts) != 2
            or parts[0] != "Bearer"
            or not secrets.compare_digest(parts[1], self._api_key)
        ):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        logger.debug("auth ok: %s %s", request.method, request.url.path)
        return await call_next(request)

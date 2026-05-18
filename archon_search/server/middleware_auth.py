"""Bearer token authentication middleware for archon-search (Task 1.2)."""
from __future__ import annotations

import logging
import secrets
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from archon_search.constants import DEFAULT_NAMESPACE, _validate_namespace

logger = logging.getLogger("archon-search")

_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})


class APIKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: object, api_key: str, namespaces: dict[str, str] | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._api_key = api_key
        self._namespaces = namespaces or {}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        parts = auth_header.split(" ", 1)
        if len(parts) != 2 or parts[0] != "Bearer":
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = parts[1]

        # Resolve namespace: iterate all entries (no early exit — prevents timing leakage)
        resolved_namespace: str | None = None
        for key_hex, ns in self._namespaces.items():
            if secrets.compare_digest(token, key_hex):
                resolved_namespace = ns  # no break

        # Fallback: check against the single default api_key
        if resolved_namespace is None and secrets.compare_digest(token, self._api_key):
            resolved_namespace = DEFAULT_NAMESPACE

        if resolved_namespace is None:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            _validate_namespace(resolved_namespace)
        except ValueError:
            logger.error("Middleware: resolved namespace %r is invalid", resolved_namespace)
            return Response(status_code=500)

        request.state.namespace = resolved_namespace
        logger.debug("auth ok: %s %s namespace=%s", request.method, request.url.path, resolved_namespace)
        return await call_next(request)

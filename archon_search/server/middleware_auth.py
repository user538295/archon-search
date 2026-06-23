"""Bearer token authentication middleware for archon-search (Task 1.2)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from archon_search.constants import DEFAULT_NAMESPACE, _validate_namespace

if TYPE_CHECKING:
    from archon_search.key_manager import KeyStore

logger = logging.getLogger(__name__)

_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/docs", "/openapi.json", "/redoc", "/ready"})


class APIKeyMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: object,
        api_key: str,
        namespaces: dict[str, str] | None = None,
        key_store: "KeyStore | None" = None,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._api_key = api_key
        self._namespaces = namespaces or {}
        self._key_store = key_store

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
        resolved_namespace: str | None = None

        # --- Managed keys (KeyStore) — checked first; early exit on first match ---
        # Token hash is computed once per request when key_store is present (not once per key).
        # hmac.compare_digest is used for constant-time comparison of equal-length
        # 64-char hex strings (SHA-256 digests).
        if self._key_store is not None:
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            for record in await self._key_store.active_keys():
                if hmac.compare_digest(token_hash, record.token_hash):
                    resolved_namespace = record.namespace
                    break  # early exit on first match (intentional; timing guarantee is per-comparison)

        # --- TOML namespace tokens — no early exit (preserves existing timing-safe design) ---
        if resolved_namespace is None:
            for key_hex, ns in self._namespaces.items():
                if secrets.compare_digest(token, key_hex):
                    resolved_namespace = ns  # no break

        # --- Legacy api_key fallback ---
        # Read the current api_key dynamically from app.state when available so
        # that POST /keys/rotate (which updates app.state.api_key) is reflected
        # immediately without a restart.  Fall back to self._api_key only when
        # app.state does not carry "api_key" at all (e.g., unit tests that build
        # the middleware without a full app).  Use `is not None` — not truthiness
        # — so an empty-string app.state.api_key does not silently reactivate the
        # stale construction-time key.
        _state_api_key = getattr(getattr(request.app, "state", None), "api_key", None)
        current_api_key: str = _state_api_key if _state_api_key is not None else self._api_key
        if resolved_namespace is None and secrets.compare_digest(token, current_api_key):
            # Rotation-revocation guard: if key_store is present, reject the token if it
            # appears as a revoked or expired record in keys.json — even if it matches
            # the current legacy api_key.  This prevents bypassing revocation via the
            # legacy fallback after key rotation.
            if self._key_store is not None:
                all_records = await self._key_store.load()
                now = datetime.now(UTC)
                is_revoked_or_expired = any(
                    hmac.compare_digest(token_hash, r.token_hash)
                    for r in all_records
                    if r.status == "revoked" or (r.expires_at is not None and r.expires_at <= now)
                )
                if not is_revoked_or_expired:
                    resolved_namespace = DEFAULT_NAMESPACE
            else:
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

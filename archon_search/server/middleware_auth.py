"""Bearer token authentication middleware for archon-search (Task 1.2)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
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

# Compiled regex for the graph viewer route — used for the ?token= middleware exemption.
# Matches exactly /graph/{collection}/view (no trailing slash, no extra segments).
_GRAPH_VIEW_RE = re.compile(r"^/graph/[^/]+/view$")

# Sentinel value returned by validate_token_and_get_namespace to indicate that
# the token was recognised but its namespace string is invalid (→ HTTP 500).
INVALID_NAMESPACE_SENTINEL = object()


async def validate_token_and_get_namespace(
    token: str,
    *,
    api_key: str,
    namespaces: dict[str, str],
    key_store: "KeyStore | None",
) -> "str | None | object":
    """Validate a raw bearer token and return the resolved namespace, or None.

    Encapsulates the full three-source, revocation-aware auth cascade:
    1. KeyStore SHA-256 lookup (managed keys) — early exit on first match.
    2. TOML namespace token compare_digest — no early exit (timing-safe design).
    3. Legacy api_key fallback with rotation-revocation guard.

    Returns:
    - The resolved namespace string on success.
    - None if authentication failed (no matching token).
    - INVALID_NAMESPACE_SENTINEL if the token matched but the namespace is invalid.

    ``token_hash`` is computed ONCE at the top of the function body so the
    revocation guard on the legacy path can reuse it without a second sha-256.
    """
    resolved_namespace: str | None = None

    # Compute once — reused by both KeyStore lookup (path 1) and revocation guard (path 3).
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    # --- 1. Managed keys (KeyStore) — checked first; early exit on first match ---
    if key_store is not None:
        for record in await key_store.active_keys():
            if hmac.compare_digest(token_hash, record.token_hash):
                resolved_namespace = record.namespace
                break  # early exit on first match

    # --- 2. TOML namespace tokens — no early exit (preserves existing timing-safe design) ---
    if resolved_namespace is None:
        for key_hex, ns in namespaces.items():
            if secrets.compare_digest(token, key_hex):
                resolved_namespace = ns  # no break

    # --- 3. Legacy api_key fallback ---
    # Use the api_key parameter directly — the caller (dispatch) is responsible for
    # passing the current dynamic value from app.state.api_key so that
    # POST /keys/rotate is reflected without a restart.
    if resolved_namespace is None and secrets.compare_digest(token, api_key):
        # Rotation-revocation guard: if key_store is present, reject the token if it
        # appears as a revoked or expired record in keys.json — even if it matches
        # the current legacy api_key.
        if key_store is not None:
            all_records = await key_store.load()
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
        return None

    try:
        _validate_namespace(resolved_namespace)
    except ValueError:
        logger.error("Middleware: resolved namespace %r is invalid", resolved_namespace)
        return INVALID_NAMESPACE_SENTINEL

    return resolved_namespace


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

        # --- Graph-view ?token= exemption ---
        # When the path matches /graph/{collection}/view AND a ?token= query param
        # is present AND no Authorization header is present, skip the header-presence
        # check so the route handler can resolve auth from the query param.
        path = request.url.path
        query_params = request.query_params
        _is_graph_view_token_path = (
            _GRAPH_VIEW_RE.match(path) is not None
            and "token" in query_params
            and "authorization" not in request.headers.keys()
        )

        if not _is_graph_view_token_path:
            if len(parts) != 2 or parts[0] != "Bearer":
                return Response(
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            token = parts[1]
        else:
            # Exempt path: token comes from the query param; the route handler
            # performs full auth via validate_token_and_get_namespace and sets
            # request.state.namespace itself.  Skip header-presence check.
            return await call_next(request)

        result = await validate_token_and_get_namespace(
            token,
            api_key=getattr(request.app.state, "api_key", self._api_key),
            namespaces=self._namespaces,
            key_store=self._key_store,
        )

        if result is INVALID_NAMESPACE_SENTINEL:
            return Response(status_code=500)

        if result is None:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        request.state.namespace = result
        logger.debug("auth ok: %s %s namespace=%s", request.method, request.url.path, result)
        return await call_next(request)

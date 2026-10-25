"""Bearer token authentication middleware for archon-search (Task 1.2)."""
from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from starlette.datastructures import State
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from archon_search.constants import (
    DEFAULT_NAMESPACE,
    VIEW_TOKEN_QUERY_PARAM,
    _validate_namespace,
)
from archon_search.key_manager import hash_token
from archon_search.server.authz import AuthIdentity
from archon_search.server.graph_view_token import GRAPH_VIEW_PATH_RE, verify_view_token

if TYPE_CHECKING:
    from archon_search.key_manager import KeyStore

logger = logging.getLogger(__name__)

# Paths served without a bearer token: liveness and readiness probes (the Docker HEALTHCHECK and
# orchestrators cannot present a key). ``/docs``, ``/redoc`` and ``/openapi.json`` are NOT here: they are
# off or authenticated unless the operator sets ``[server].docs_enabled`` (see ``DOCS_PATHS``).
_EXEMPT_PATHS: frozenset[str] = frozenset({"/health", "/ready"})

# The interactive API documentation. ``create_app`` adds these to the exempt set only when
# ``[server].docs_enabled`` is true: Swagger UI / ReDoc are browser pages that fetch ``/openapi.json``
# without credentials.
DOCS_PATHS: frozenset[str] = frozenset({"/docs", "/redoc", "/openapi.json"})

# Sentinel value returned by authenticate_token to indicate that
# the token was recognised but its namespace string is invalid (→ HTTP 500).
INVALID_NAMESPACE_SENTINEL = object()


def _constant_time_equal(a: str, b: str) -> bool:
    """Compare two strings in constant time, whatever characters they contain.

    ``hmac.compare_digest`` on two ``str`` raises ``TypeError`` for non-ASCII input; comparing the UTF-8
    bytes cannot.
    """
    return hmac.compare_digest(a.encode(), b.encode())


async def authenticate_token(
    token: str,
    *,
    api_key: str,
    namespaces: dict[str, str],
    key_store: KeyStore | None,
) -> AuthIdentity | None | object:
    """Validate a raw bearer token and return the caller's identity, or None.

    Encapsulates the full three-source, revocation-aware auth cascade:
    1. KeyStore SHA-256 lookup (managed keys) — early exit on first match; ``admin`` comes from the record.
    2. TOML namespace token compare — no early exit (timing-safe design); never admin.
    3. Legacy api_key fallback with rotation-revocation guard; the default key is always admin.

    Returns:
    - The :class:`AuthIdentity` (namespace, admin) on success.
    - None if authentication failed (no matching token) — including an empty token and any token that is not
      ASCII, which no key can be.
    - INVALID_NAMESPACE_SENTINEL if the token matched but the namespace is invalid.

    ``token_hash`` is computed ONCE at the top of the function body so the
    revocation guard on the legacy path can reuse it without a second sha-256.
    """
    if not token or not token.isascii():
        return None

    resolved_namespace: str | None = None
    admin = False

    # Compute once — reused by both KeyStore lookup (path 1) and revocation guard (path 3).
    token_hash = hash_token(token)

    # --- 1. Managed keys (KeyStore) — checked first; early exit on first match ---
    if key_store is not None:
        for record in await key_store.active_keys():
            if _constant_time_equal(token_hash, record.token_hash):
                resolved_namespace = record.namespace
                admin = record.admin
                break  # early exit on first match

    # --- 2. TOML namespace tokens — no early exit (preserves existing timing-safe design) ---
    if resolved_namespace is None:
        for key_hex, ns in namespaces.items():
            if _constant_time_equal(token, key_hex):
                resolved_namespace = ns  # no break
                admin = False

    # --- 3. Legacy api_key fallback ---
    # Use the api_key parameter directly — the caller (dispatch) is responsible for
    # passing the current dynamic value from app.state.api_key so that
    # POST /keys/rotate is reflected without a restart.
    if resolved_namespace is None and _constant_time_equal(token, api_key):
        # Rotation-revocation guard: if key_store is present, reject the token if it
        # appears as a revoked or expired record in keys.json — even if it matches
        # the current legacy api_key.
        if key_store is not None:
            all_records = await key_store.load()
            now = datetime.now(UTC)
            is_revoked_or_expired = any(
                _constant_time_equal(token_hash, r.token_hash)
                for r in all_records
                if r.status == "revoked" or (r.expires_at is not None and r.expires_at <= now)
            )
            if not is_revoked_or_expired:
                resolved_namespace = DEFAULT_NAMESPACE
                admin = True
        else:
            resolved_namespace = DEFAULT_NAMESPACE
            admin = True

    if resolved_namespace is None:
        return None

    try:
        _validate_namespace(resolved_namespace)
    except ValueError:
        logger.error("Middleware: resolved namespace %r is invalid", resolved_namespace)
        return INVALID_NAMESPACE_SENTINEL

    return AuthIdentity(namespace=resolved_namespace, admin=admin)


def _view_token_identity(request: Request, token: str) -> AuthIdentity | None:
    """Identity granted by a graph view token on a ``GET`` of the viewer page or its data, else ``None``.

    Never admin: a view token reads one collection's graph and nothing else.
    """
    if request.method != "GET":
        return None
    match = GRAPH_VIEW_PATH_RE.match(request.url.path)
    secret = getattr(request.app.state, "graph_view_secret", None)
    if match is None or secret is None:
        return None
    namespace = verify_view_token(secret, token, match.group("collection"))
    return AuthIdentity(namespace=namespace, admin=False) if namespace is not None else None


def _unauthorized() -> Response:
    return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Bearer-token auth; the legacy default key is read per request from ``<state>.api_key``.

    ``<state>`` is ``api_key_state`` when given — the mounted MCP app passes the REST app's
    ``app.state`` so both transports accept the same current default key after a rotation —
    else the serving app's own ``request.app.state``; ``api_key`` is the fallback when that
    state has no ``api_key``.

    On success it stamps ``request.state.namespace`` and ``request.state.admin`` (see
    ``archon_search.server.authz``). ``exempt_paths`` are served without a token.
    """

    def __init__(
        self,
        app: ASGIApp,
        api_key: str,
        namespaces: dict[str, str] | None = None,
        key_store: KeyStore | None = None,
        api_key_state: State | None = None,
        exempt_paths: frozenset[str] = _EXEMPT_PATHS,
    ) -> None:
        super().__init__(app)
        self._api_key = api_key
        self._namespaces = namespaces or {}
        self._key_store = key_store
        self._api_key_state = api_key_state
        self._exempt_paths = exempt_paths

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in self._exempt_paths:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        parts = auth_header.split(" ", 1)

        # --- Graph-viewer page: a view token in the query string replaces the Authorization header ---
        # (a browser navigation cannot send one); only when no Authorization header is present.
        view_token = request.query_params.get(VIEW_TOKEN_QUERY_PARAM)
        if view_token is not None and "authorization" not in request.headers:
            identity = _view_token_identity(request, view_token)
            if identity is None:
                return _unauthorized()
            return await self._forward(request, call_next, identity)

        if len(parts) != 2 or parts[0] != "Bearer":
            return _unauthorized()
        token = parts[1]

        key_state = self._api_key_state if self._api_key_state is not None else request.app.state
        result = await authenticate_token(
            token,
            api_key=getattr(key_state, "api_key", self._api_key),
            namespaces=self._namespaces,
            key_store=self._key_store,
        )

        if result is INVALID_NAMESPACE_SENTINEL:
            return Response(status_code=500)

        # A view token is also accepted as the Bearer credential of the viewer's own data fetch.
        identity = result if isinstance(result, AuthIdentity) else _view_token_identity(request, token)
        if identity is None:
            return _unauthorized()
        return await self._forward(request, call_next, identity)

    @staticmethod
    async def _forward(request: Request, call_next: Callable, identity: AuthIdentity) -> Response:
        request.state.namespace = identity.namespace
        request.state.admin = identity.admin
        logger.debug("auth ok: %s %s namespace=%s admin=%s", request.method, request.url.path, identity.namespace, identity.admin)
        return await call_next(request)

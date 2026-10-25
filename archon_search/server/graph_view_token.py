"""Short-lived, single-purpose tokens for the graph viewer (``GET /graph/{collection}/view``).

A browser cannot attach an ``Authorization`` header to a page navigation, and putting the real API
key in the URL leaks it into access logs, browser history and proxies. Instead an authenticated client
mints a *view token* (``POST /graph/{collection}/view-token``) and opens
``/graph/{collection}/view?view_token=<token>``. The token

* is an HMAC-SHA256 over ``namespace``, ``collection`` and an expiry, keyed by a per-process random
  secret (``app.state.graph_view_secret``), so it needs no server-side storage and dies on restart;
* expires after ``GRAPH_VIEW_TOKEN_TTL_SECONDS``;
* is accepted only on ``GET /graph/{collection}/view`` and ``GET /graph/{collection}`` for the one
  collection it was minted for, and only for the namespace it was minted for. It can read that
  collection's graph and nothing else: it is never an admin credential and is not recognised by any
  other route.

The viewer page embeds a freshly minted view token for its own data fetch; the real key never appears in
the page.
"""
from __future__ import annotations

import base64
import hmac
import re
import time

from archon_search.constants import GRAPH_VIEW_TOKEN_TTL_SECONDS, _validate_namespace

GRAPH_VIEW_SECRET_BYTES = 32
_TOKEN_PREFIX = "gv1"
_FIELD_SEPARATOR = "\x00"
_SEGMENT_COUNT = 3

# ``/graph/{collection}`` and ``/graph/{collection}/view``; ``cross-collection`` is a different route
# (``GET /graph/cross-collection``) that no view token may open.
GRAPH_VIEW_PATH_RE = re.compile(r"^/graph/(?!cross-collection(?:/|$))(?P<collection>[^/]+)(?:/view)?$")


def _sign(secret: bytes, payload: bytes) -> str:
    return hmac.digest(secret, payload, "sha256").hex()


def mint_view_token(secret: bytes, namespace: str, collection: str, *, now: float | None = None) -> str:
    """Return a view token for *namespace* / *collection* valid for ``GRAPH_VIEW_TOKEN_TTL_SECONDS``."""
    expires_at = int((time.time() if now is None else now) + GRAPH_VIEW_TOKEN_TTL_SECONDS)
    payload = _FIELD_SEPARATOR.join((namespace, collection, str(expires_at))).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{_TOKEN_PREFIX}.{body}.{_sign(secret, payload)}"


def verify_view_token(secret: bytes, token: str, collection: str, *, now: float | None = None) -> str | None:
    """Return the namespace *token* grants read access to for *collection*, or ``None``.

    ``None`` for anything that is not a valid, unexpired token minted by this process for exactly
    *collection*: wrong prefix, malformed, tampered, expired, other collection or an invalid namespace.
    """
    segments = token.split(".")
    if len(segments) != _SEGMENT_COUNT or segments[0] != _TOKEN_PREFIX or not token.isascii():
        return None
    _, body, signature = segments
    try:
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(secret, payload).encode(), signature.encode()):
        return None
    try:
        namespace, token_collection, expires_at = payload.decode().split(_FIELD_SEPARATOR)
        expiry = int(expires_at)
    except ValueError:
        return None
    if token_collection != collection or expiry <= (time.time() if now is None else now):
        return None
    try:
        _validate_namespace(namespace)
    except ValueError:
        return None
    return namespace

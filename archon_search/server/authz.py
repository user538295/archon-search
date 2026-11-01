"""FastAPI half of the authorization policy: reading the caller off a request and mapping refusals to HTTP.

The policy itself (``AuthIdentity``, ``ForbiddenError``, ``require_admin`` and the key rules) lives in
``archon_search.services.authz`` so REST and MCP share one transport-free implementation.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from archon_search.server.schemas import ErrorDetail
from archon_search.services.authz import (
    ADMIN_REQUIRED_DETAIL,
    AuthIdentity,
    ForbiddenError,
    require_admin,
)

# OpenAPI ``responses`` fragment for every admin-only route.
ADMIN_ONLY_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorDetail, "description": ADMIN_REQUIRED_DETAIL},
}


def identity_of(request: Request) -> AuthIdentity | None:
    """Return the identity ``APIKeyMiddleware`` stamped on *request*, or ``None`` when it did not.

    ``admin`` is ``False`` unless the middleware set it to exactly ``True``: absence never grants privilege.
    """
    namespace = getattr(request.state, "namespace", None)
    if not isinstance(namespace, str) or not namespace:
        return None
    return AuthIdentity(namespace=namespace, admin=getattr(request.state, "admin", False) is True)


async def require_admin_dependency(request: Request) -> None:
    """FastAPI dependency: ``403`` unless the request was authenticated with an admin key."""
    try:
        require_admin(identity_of(request))
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def caller_identity(request: Request) -> AuthIdentity:
    """Return the authenticated caller of a REST request; ``401`` if ``APIKeyMiddleware`` did not authenticate it."""
    identity = identity_of(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return identity

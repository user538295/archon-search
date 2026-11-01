"""Transport-free authorization policy shared by the REST routes and the MCP tools.

Authentication (``middleware_auth``) resolves a bearer token to an :class:`AuthIdentity`: the
caller's namespace and whether the key carries the ``admin`` capability. Authorization lives
here, once, so REST and MCP cannot drift apart:

* ``admin`` is required for everything that is not scoped to one namespace: key issuance across
  namespaces, default-key rotation, ``/sync``, ``/backup/trigger``, ``/maintenance/trigger``,
  ``/telemetry/*`` and ``POST /collections/`` (it appends a path to the shared
  ``archon-search.toml`` collection list, which ``/sync`` and the startup sync process for every
  namespace).
* A non-admin key may create, list and revoke *non-admin* keys of its own namespace, and nothing
  else about keys; it can never mint an admin key.

Every refusal is a :class:`ForbiddenError` carrying a fixed, sanitized detail; REST maps it to
``403 {"detail": ...}`` and MCP to ``{"error": ..., "code": "forbidden"}``. This module imports no
web framework; the request-reading and FastAPI half lives in ``archon_search.server.authz``.
"""
from __future__ import annotations

from dataclasses import dataclass

from archon_search.key_manager import KeyRecord, KeyStore

ADMIN_REQUIRED_DETAIL = "this operation requires an admin API key"
ADMIN_KEY_GRANT_DETAIL = "only an admin API key can create an admin key"
FOREIGN_NAMESPACE_DETAIL = "a non-admin API key can only manage keys in its own namespace"
FORBIDDEN_CODE = "forbidden"


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    """The authenticated caller: its namespace and whether its key is an admin key."""

    namespace: str
    admin: bool


class ForbiddenError(Exception):
    """The authenticated caller may not do this; ``str(exc)`` is a fixed, wire-safe detail."""


def require_admin(identity: AuthIdentity | None) -> None:
    """Raise :class:`ForbiddenError` unless *identity* is an admin key (``None`` = unauthenticated = refused)."""
    if identity is None or not identity.admin:
        raise ForbiddenError(ADMIN_REQUIRED_DETAIL)


def authorize_key_create(identity: AuthIdentity, *, namespace: str, admin: bool) -> None:
    """Raise :class:`ForbiddenError` unless *identity* may mint a key for *namespace* (``admin`` or not)."""
    if identity.admin:
        return
    if admin:
        raise ForbiddenError(ADMIN_KEY_GRANT_DETAIL)
    if namespace != identity.namespace:
        raise ForbiddenError(FOREIGN_NAMESPACE_DETAIL)


def authorize_key_listing_namespace(identity: AuthIdentity, namespace: str | None) -> str | None:
    """Return the namespace filter a key listing must apply for *identity*.

    An admin lists whatever it asks for (``None`` = every namespace). A non-admin is pinned to its own
    namespace; naming another one raises :class:`ForbiddenError`.
    """
    if identity.admin:
        return namespace
    if namespace is not None and namespace != identity.namespace:
        raise ForbiddenError(FOREIGN_NAMESPACE_DETAIL)
    return identity.namespace


def can_manage_key(identity: AuthIdentity, record: KeyRecord) -> bool:
    """True when *identity* may list or revoke *record*: an admin any key, a non-admin only non-admin keys of its namespace."""
    return identity.admin or (record.namespace == identity.namespace and not record.admin)


async def caller_may_revoke(key_store: KeyStore, identity: AuthIdentity, key_id: str) -> bool:
    """True when *identity* may revoke key *key_id*.

    An admin may try any id (the store answers unknown ids). A non-admin may only revoke an existing
    non-admin key of its own namespace, so an id it may not touch is indistinguishable from an unknown one.
    """
    if identity.admin:
        return True
    return any(r.id == key_id and can_manage_key(identity, r) for r in await key_store.list_keys())

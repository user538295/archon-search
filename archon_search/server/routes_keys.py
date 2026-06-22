"""Key management REST endpoints — POST /keys, GET /keys, DELETE /keys/{id} (D7 BE-4, BE-6).

POST /keys/rotate is added in BE-8.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from archon_search.constants import _validate_namespace
from archon_search.key_manager import KeyRecord, KeyStore
from archon_search.server.schemas import (
    ErrorDetail,
    KeyCreateRequest,
    KeyCreateResponse,
    KeyListResponse,
    KeyResponse,
    KeyRevokeResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["keys"])

_ERROR_401_422 = {
    401: {"model": ErrorDetail},
    422: {"model": ErrorDetail},
}

_ERROR_401_404 = {
    401: {"model": ErrorDetail},
    404: {"model": ErrorDetail},
}

# Detail message returned when the literal string "null" is passed as a key ID.
# TOML synthetic keys have id=None (Python None) and cannot be targeted via DELETE.
_TOML_KEY_HINT = (
    "This key is managed via archon-search.toml [namespaces] — "
    "remove it from the config file and restart the server."
)


def _key_record_to_response(record: KeyRecord) -> KeyResponse:
    """Convert a ``KeyRecord`` to its REST response representation."""
    return KeyResponse(
        id=record.id,
        namespace=record.namespace,
        label=record.label,
        created_at=record.created_at,
        expires_at=record.expires_at,
        status=record.status,
    )


@router.post(
    "/keys",
    status_code=201,
    response_model=KeyCreateResponse,
    responses=_ERROR_401_422,
)
async def create_key(body: KeyCreateRequest, request: Request) -> KeyCreateResponse:
    """Issue a new managed API key.

    The raw bearer token is returned exactly once in the response ``token``
    field. It is never stored on disk — only its SHA-256 hash is persisted.
    """
    # Validate namespace before any side-effects.
    try:
        _validate_namespace(body.namespace)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    key_store: KeyStore = request.app.state.key_store
    result = await key_store.create(
        ns=body.namespace,
        label=body.label,
        expires_at=body.expires_at,
    )

    return KeyCreateResponse(
        id=result["id"],  # type: ignore[arg-type]  # str narrowed from str|datetime
        token=result["token"],  # type: ignore[arg-type]
        namespace=body.namespace,
        label=body.label,
        # Use the exact created_at from the store record to prevent divergence
        # between the POST response and future GET /keys (which reads from disk).
        created_at=result["created_at"],  # type: ignore[arg-type]
        expires_at=body.expires_at,
        status="active",
    )


@router.get(
    "/keys",
    response_model=KeyListResponse,
    responses=_ERROR_401_422,
)
async def list_keys(
    request: Request,
    status: Literal["active", "revoked", "all"] = Query(default="active"),
    namespace: str | None = Query(default=None),
) -> KeyListResponse:
    """List API keys.

    By default (``status=active``) only active keys are returned; revoked keys
    are counted in ``hidden_revoked_count`` as a hint to the operator.

    TOML synthetic keys (``id=null``) are always included in the active view
    because they are always active — they can only be removed by editing
    ``archon-search.toml`` and restarting the server.

    Query parameters:

    - ``status`` — ``active`` (default), ``revoked``, or ``all``.
    - ``namespace`` — optional namespace scope applied before status filtering;
      ``hidden_revoked_count`` reflects only revoked keys within this namespace.
    """
    key_store: KeyStore = request.app.state.key_store
    all_records = await key_store.list_keys()

    # Apply optional namespace filter first so that hidden_revoked_count
    # reflects only keys hidden from THIS scoped view (not global counts).
    if namespace is not None:
        scope = [r for r in all_records if r.namespace == namespace]
    else:
        scope = all_records

    # Apply status filter within the scoped set.
    if status == "active":
        filtered = [r for r in scope if r.status == "active"]
        hidden_revoked_count = sum(1 for r in scope if r.status == "revoked")
    elif status == "revoked":
        filtered = [r for r in scope if r.status == "revoked"]
        hidden_revoked_count = 0
    else:  # "all"
        filtered = list(scope)
        hidden_revoked_count = 0

    return KeyListResponse(
        keys=[_key_record_to_response(r) for r in filtered],
        hidden_revoked_count=hidden_revoked_count,
    )


@router.delete(
    "/keys/{key_id}",
    response_model=KeyRevokeResponse,
    responses=_ERROR_401_404,
)
async def revoke_key(key_id: str, request: Request) -> KeyRevokeResponse:
    """Revoke a managed API key.

    Idempotent: calling this on an already-revoked key returns 200 (desired
    state already achieved).

    Returns 404 for unknown IDs.  TOML synthetic keys (``id=null``) cannot be
    targeted by this endpoint — the literal string ``"null"`` returns 404 with
    a hint to edit ``archon-search.toml`` instead.
    """
    key_store: KeyStore = request.app.state.key_store

    # Special-case the literal string "null" — operators may try this after
    # seeing id=null in GET /keys for TOML synthetic records.
    if key_id == "null":
        raise HTTPException(status_code=404, detail=_TOML_KEY_HINT)

    try:
        await key_store.revoke(key_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Key not found: {key_id!r}")

    return KeyRevokeResponse(id=key_id, status="revoked")

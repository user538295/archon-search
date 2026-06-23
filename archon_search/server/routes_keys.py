"""Key management REST endpoints — POST /keys, GET /keys, DELETE /keys/{id}, POST /keys/rotate (D7 BE-4, BE-6, BE-8)."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from archon_search.constants import _validate_namespace
from archon_search.key_manager import ENV_VAR, KeyRecord, KeyStore, get_key_file
from archon_search._durable_io import atomic_write_bytes
from archon_search.server.schemas import (
    ErrorDetail,
    KeyCreateRequest,
    KeyCreateResponse,
    KeyListResponse,
    KeyResponse,
    KeyRotateRequest,
    KeyRotateResponse,
    KeyRevokeResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["keys"])

# Module-level lock that serialises the full rotate sequence:
# read app.state.api_key → write .search.env → call rotate_default_key → update app.state.api_key.
# Prevents concurrent POST /keys/rotate calls from creating orphaned active keys.
_rotate_lock = asyncio.Lock()

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


_ERROR_401_409 = {
    401: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
}


@router.post(
    "/keys/rotate",
    response_model=KeyRotateResponse,
    responses=_ERROR_401_409,
)
async def rotate_key(body: KeyRotateRequest, request: Request) -> KeyRotateResponse:
    """Rotate the default API key.

    Generates a new managed API key, writes the new raw token to ``.search.env``,
    and revokes (or grace-expires) the old default key in ``keys.json``.

    Returns 409 when ``ARCHON_SEARCH_API_KEY`` env var is set — the env var
    always overrides ``.search.env`` in the running process, so rotation would
    be a silent no-op from the operator's perspective (S23).

    The ``grace_seconds`` request body field overrides the TOML
    ``[auth].rotate_grace_seconds`` config default.  When absent, the config
    default is used.  When both are 0, the old key is immediately revoked.
    """
    if os.environ.get(ENV_VAR):
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot rotate: ARCHON_SEARCH_API_KEY env var is set; "
                "unset it first and restart the server to use managed key rotation."
            ),
        )

    key_store: KeyStore = request.app.state.key_store
    config = request.app.state.config

    # Determine grace_seconds: body wins over config default.
    grace_seconds: int
    if body.grace_seconds is not None:
        grace_seconds = body.grace_seconds
    else:
        grace_seconds = config.auth.rotate_grace_seconds

    # Serialise the full rotate sequence (read → write .search.env → mutate keys.json →
    # update app.state.api_key) so concurrent POST /keys/rotate calls cannot interleave
    # and create orphaned active keys.
    async with _rotate_lock:
        # Read the current default key from app.state so the dynamic middleware
        # path and this handler always agree on the active token.
        current_token: str = request.app.state.api_key

        # Generate the new raw token here so we can write .search.env FIRST.
        # Safe write order:
        #   (a) If .search.env write fails (OSError), keys.json is never mutated
        #       and the old key remains active — the caller gets 500 and can retry.
        #   (b) If .search.env write succeeds but keys.json write fails (unlikely),
        #       .search.env has the new token but keys.json still has the old record.
        #       app.state.api_key is NOT updated (line 276 never reached).  On
        #       restart, the server reads the new token from .search.env and uses
        #       it via the legacy fallback — the operator should re-rotate to
        #       create the corresponding keys.json record.
        new_raw_token = secrets.token_hex(32)  # 64 hex chars

        # Write new token to .search.env BEFORE mutating keys.json.
        # Wrapped in asyncio.to_thread so the fsync chain (os.write → os.fsync →
        # os.replace → os.fsync(parent_dir)) does not block the event loop.
        key_file = get_key_file()
        key_file.parent.mkdir(parents=True, exist_ok=True)
        payload = f"{ENV_VAR}={new_raw_token}\n".encode()
        try:
            await asyncio.to_thread(atomic_write_bytes, key_file, payload, mode=0o600)
        except OSError as exc:
            logger.error(
                "rotate: failed to write %s — keys.json NOT modified; "
                "rotation aborted: %s",
                key_file,
                exc,
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to write .search.env — rotation aborted; keys.json unchanged.",
            )

        # Mutate keys.json with the pre-generated token (safe: .search.env already written).
        result = await key_store.rotate_default_key(
            current_token=current_token,
            grace_seconds=grace_seconds,
            new_token=new_raw_token,
        )

        new_key_id: str = result["new_key_id"]  # type: ignore[assignment]
        old_record = result["old_record"]
        # Defensive: rotate_default_key must echo back the token we passed in.
        # If this ever diverges, .search.env and keys.json would be out of sync.
        assert result["new_token"] == new_raw_token, "rotate_default_key returned unexpected token"

        # Update app.state.api_key so subsequent calls to POST /keys/rotate read the
        # correct current_token, and so the middleware's dynamic api_key lookup
        # (request.app.state.api_key) uses the new key immediately.
        request.app.state.api_key = new_raw_token

    # Log rotation event for audit trail.
    old_key_id_str = old_record.id if old_record is not None else None
    logger.info(
        "rotate: new_key_id=%s old_key_id=%s grace_seconds=%d",
        new_key_id,
        old_key_id_str,
        grace_seconds,
    )

    # Build response.
    old_key_expires_at = None
    old_key_status = None
    if old_record is not None:
        old_key_expires_at = old_record.expires_at
        old_key_status = old_record.status

    return KeyRotateResponse(
        new_key_id=new_key_id,
        token=new_raw_token,
        status="active",
        old_key_id=old_key_id_str,
        old_key_expires_at=old_key_expires_at,
        old_key_status=old_key_status,
    )

"""Key management REST endpoints — POST /keys, GET /keys, DELETE /keys/{id}, POST /keys/rotate (D7 BE-4, BE-6, BE-8)."""
from __future__ import annotations

import logging
import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from archon_search.constants import _validate_namespace
from archon_search.key_manager import (
    ENV_VAR,
    KEY_STORE_UNREADABLE_DETAIL,
    ROTATE_KEYS_JSON_FAILED_DETAIL,
    ROTATE_WRITE_FAILED_DETAIL,
    BlankCurrentKeyError,
    KeyFileWriteError,
    KeyRecord,
    KeyRotationInputError,
    KeyStore,
    KeyStoreUnreadableError,
    RotationIncompleteError,
    resolve_rotation_key_file,
    rotate_and_persist_default_key,
)
from archon_search.server.authz import (
    ADMIN_ONLY_RESPONSES,
    FOREIGN_NAMESPACE_DETAIL,
    ForbiddenError,
    authorize_key_create,
    authorize_key_listing_namespace,
    caller_identity,
    caller_may_revoke,
    can_manage_key,
    require_admin_dependency,
)
from archon_search.server.schemas import (
    ErrorDetail,
    KeyCreateRequest,
    KeyCreateResponse,
    KeyListResponse,
    KeyResponse,
    KeyRevokeResponse,
    KeyRotateRequest,
    KeyRotateResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["keys"])

# The ``responses=`` mapping FastAPI takes: status code (or ``"default"``) to a response description.
_Responses = dict[int | str, dict[str, Any]]

_ERROR_401_422: _Responses = {
    401: {"model": ErrorDetail},
    422: {"model": ErrorDetail},
}

# A non-admin key manages only non-admin keys of its own namespace (see ``archon_search.server.authz``).
_ERROR_403_KEY_SCOPE: _Responses = {
    403: {"model": ErrorDetail, "description": FOREIGN_NAMESPACE_DETAIL},
}

_ERROR_401_404: _Responses = {
    401: {"model": ErrorDetail},
    404: {"model": ErrorDetail},
}

_ERROR_500_KEY_STORE_UNREADABLE: _Responses = {
    500: {"model": ErrorDetail, "description": "keys.json could not be read; no key changes were made"},
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
        admin=record.admin,
    )


@router.post(
    "/keys",
    status_code=201,
    response_model=KeyCreateResponse,
    responses={**_ERROR_401_422, **_ERROR_403_KEY_SCOPE, **_ERROR_500_KEY_STORE_UNREADABLE},
)
async def create_key(body: KeyCreateRequest, request: Request) -> KeyCreateResponse:
    """Issue a new managed API key.

    The raw bearer token is returned exactly once in the response ``token``
    field. It is never stored on disk — only its SHA-256 hash is persisted.
    """
    try:
        authorize_key_create(caller_identity(request), namespace=body.namespace, admin=body.admin)
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # Validate namespace before any side-effects.
    try:
        _validate_namespace(body.namespace)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    key_store: KeyStore = request.app.state.key_store
    try:
        created = await key_store.create(
            ns=body.namespace,
            label=body.label,
            expires_at=body.expires_at,
            admin=body.admin,
        )
    except KeyStoreUnreadableError:
        raise HTTPException(status_code=500, detail=KEY_STORE_UNREADABLE_DETAIL)

    return KeyCreateResponse(
        id=created.id,
        token=created.token,
        namespace=body.namespace,
        label=body.label,
        # Use the exact created_at from the store record to prevent divergence
        # between the POST response and future GET /keys (which reads from disk).
        created_at=created.created_at,
        expires_at=body.expires_at,
        status="active",
        admin=body.admin,
    )


@router.get(
    "/keys",
    response_model=KeyListResponse,
    responses={**_ERROR_401_422, **_ERROR_403_KEY_SCOPE},
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
    identity = caller_identity(request)
    try:
        namespace = authorize_key_listing_namespace(identity, namespace)
    except ForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    key_store: KeyStore = request.app.state.key_store
    all_records = [r for r in await key_store.list_keys() if can_manage_key(identity, r)]

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
    responses={**_ERROR_401_404, **_ERROR_500_KEY_STORE_UNREADABLE},
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
    identity = caller_identity(request)

    # Special-case the literal string "null" — operators may try this after
    # seeing id=null in GET /keys for TOML synthetic records.
    if key_id == "null":
        raise HTTPException(status_code=404, detail=_TOML_KEY_HINT)

    # A non-admin may revoke only the non-admin keys of its own namespace. Any other id is reported as
    # unknown so a key's existence in another namespace is not revealed.
    if not await caller_may_revoke(key_store, identity, key_id):
        raise HTTPException(status_code=404, detail=f"Key not found: {key_id!r}")

    try:
        await key_store.revoke(key_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Key not found: {key_id!r}")
    except KeyStoreUnreadableError:
        raise HTTPException(status_code=500, detail=KEY_STORE_UNREADABLE_DETAIL)

    return KeyRevokeResponse(id=key_id, status="revoked")


_ERROR_401_409_500: _Responses = {
    401: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
    500: {"model": ErrorDetail, "description": "Rotation failed or incomplete; see the server log for details"},
}


@router.post(
    "/keys/rotate",
    response_model=KeyRotateResponse,
    responses={**_ERROR_401_409_500, **ADMIN_ONLY_RESPONSES},
    dependencies=[Depends(require_admin_dependency)],
)
async def rotate_key(body: KeyRotateRequest, request: Request) -> KeyRotateResponse:
    """Rotate the default API key (admin key required: it replaces the server-wide default key).

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

    async def read_current_token() -> str:
        # app.state.api_key is the token the dynamic middleware path accepts as the default key.
        return request.app.state.api_key

    def publish_new_token(new_token: str) -> None:
        # Runs inside the rotation lock, so the next rotation reads this token.
        request.app.state.api_key = new_token

    # The body's grace_seconds is already bounded by KeyRotateRequest (standard 422), so a
    # rejected input here comes from server state (a blank app.state.api_key, or an
    # unvalidated config grace): a server fault, answered 500 with its curated text.
    # If keys.json fails after .search.env was written, app.state.api_key is NOT updated, so
    # the old key stays current: recover by rotating again (REST or MCP) authenticated with
    # the old key, BEFORE any restart — a restart would make the orphaned .search.env token
    # the default key.
    try:
        result = await rotate_and_persist_default_key(
            key_store,
            key_file=resolve_rotation_key_file(),
            read_current_token=read_current_token,
            grace_seconds=grace_seconds,
            publish_new_token=publish_new_token,
        )
    except (BlankCurrentKeyError, KeyRotationInputError) as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except KeyStoreUnreadableError:
        raise HTTPException(status_code=500, detail=KEY_STORE_UNREADABLE_DETAIL)
    except KeyFileWriteError:
        raise HTTPException(status_code=500, detail=ROTATE_WRITE_FAILED_DETAIL)
    except RotationIncompleteError:
        raise HTTPException(status_code=500, detail=ROTATE_KEYS_JSON_FAILED_DETAIL)

    new_raw_token = result.new_token
    new_key_id = result.new_key_id
    old_record = result.old_record

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

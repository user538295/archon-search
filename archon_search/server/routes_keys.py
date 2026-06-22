"""Key management REST endpoints — POST /keys (D7 BE-4).

Further endpoints (GET /keys, DELETE /keys/{id}, POST /keys/rotate) are added
in subsequent tasks (BE-6, BE-8).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from archon_search.constants import _validate_namespace
from archon_search.key_manager import KeyStore
from archon_search.server.schemas import ErrorDetail, KeyCreateRequest, KeyCreateResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["keys"])

_ERROR_401_422 = {
    401: {"model": ErrorDetail},
    422: {"model": ErrorDetail},
}


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

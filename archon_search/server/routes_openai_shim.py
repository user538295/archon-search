"""GET /v1/models handler and OpenAI401Middleware for the G9 OpenAI-compatible shim.

This module is conditionally registered in ``create_app()`` when
``config.openai_shim.enabled = true``.  When disabled, no routes or
middleware are added — the existing REST surface is untouched.

Design:
- ``GET /v1/models`` delegates entirely to ``SearchPipeline.get_all_collections_meta``
  (the Use Cases layer) and maps results to ``ModelList`` (Entities).
- ``OpenAI401Middleware`` is a thin Starlette middleware that rewrites any bodyless
  401 response on a ``/v1/*`` path to the OpenAI JSON error envelope.  It must
  be added AFTER ``APIKeyMiddleware`` in Starlette LIFO ordering so it wraps
  outgoing 401 responses before they reach the client.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from archon_search.server.schemas_openai import ModelList, ModelObject, OpenAIError, OpenAIErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# The catch-all model ID — returned regardless of how many collections exist.
_CATCH_ALL_MODEL_ID = "archon-search"


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.get("/models", response_model=ModelList)
async def list_models(request: Request) -> ModelList:
    """Return one ``ModelObject`` per namespace-visible collection plus the catch-all.

    The response always contains at least one entry (the ``archon-search``
    catch-all), even when the namespace has no collections.
    """
    pipeline = request.app.state.pipeline
    ns: str = request.state.namespace

    all_meta = await pipeline.get_all_collections_meta(ns)

    data: list[ModelObject] = [
        ModelObject(id=_CATCH_ALL_MODEL_ID),
    ]
    for meta in all_meta:
        data.append(ModelObject(id=f"{_CATCH_ALL_MODEL_ID}/{meta.name}"))

    return ModelList(data=data)


# ---------------------------------------------------------------------------
# Middleware — 401 body rewrite for /v1/* paths
# ---------------------------------------------------------------------------


class OpenAI401Middleware(BaseHTTPMiddleware):
    """Rewrite bodyless 401 responses on ``/v1/*`` paths to OpenAI error shape.

    ``APIKeyMiddleware`` returns ``Response(status_code=401)`` with an empty
    body and a ``WWW-Authenticate: Bearer`` header.  Clients that speak the
    OpenAI protocol expect a JSON body on 401.  This middleware intercepts
    those responses and adds the expected body — leaving non-/v1/ responses
    and non-401 status codes untouched.

    Starlette processes middleware in LIFO order, so this middleware must be
    added AFTER ``APIKeyMiddleware`` in ``create_app()`` — it will then sit
    *outside* ``APIKeyMiddleware`` in the call stack and can see its outgoing
    401 responses.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        if response.status_code == 401 and request.url.path.startswith("/v1/"):
            error_body = OpenAIErrorResponse(
                error=OpenAIError(
                    message="Incorrect API key.",
                    type="authentication_error",
                )
            )
            return JSONResponse(
                status_code=401,
                content=error_body.model_dump(),
                headers={"WWW-Authenticate": "Bearer"},
            )

        return response

"""GET /ready — unauthenticated readiness probe."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from archon_search.server.schemas import CheckStatus, ReadinessChecks, ReadinessResponse

router = APIRouter()


@router.get(
    "/ready",
    responses={
        200: {"model": ReadinessResponse},
        503: {"model": ReadinessResponse},
    },
)
async def ready(request: Request) -> JSONResponse:
    store = request.app.state.search_store
    storage_ok = await store.ping()
    body = ReadinessResponse(
        ready=storage_ok,
        checks=ReadinessChecks(storage=CheckStatus.OK if storage_ok else CheckStatus.FAIL),
    )
    status_code = 200 if storage_ok else 503
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code)

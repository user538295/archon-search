"""GET /health endpoint."""
from __future__ import annotations

from importlib.metadata import version, PackageNotFoundError

from fastapi import APIRouter, Request

from archon_search.config import SearchConfig
from archon_search.server.routes_status import _build_mcp_status
from archon_search.server.schemas import HealthResponse

router = APIRouter()

try:
    _VERSION = version("archon-search")
except PackageNotFoundError:
    _VERSION = "dev"


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    config: SearchConfig = request.app.state.config
    return HealthResponse(
        status="running",
        version=_VERSION,
        mcp=_build_mcp_status(request, config),
    )

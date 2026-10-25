"""GET /health endpoint."""
from __future__ import annotations

from fastapi import APIRouter

from archon_search.server.schemas import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Unauthenticated liveness probe. It reveals nothing about the deployment: version and MCP bind address are in ``GET /status`` (authenticated)."""
    return HealthResponse(status="running")

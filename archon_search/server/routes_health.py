"""GET /health endpoint."""
from __future__ import annotations

from importlib.metadata import version, PackageNotFoundError

from fastapi import APIRouter

from archon_search.server.schemas import HealthResponse

router = APIRouter()

try:
    _VERSION = version("archon-search")
except PackageNotFoundError:
    _VERSION = "dev"


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="running", version=_VERSION)

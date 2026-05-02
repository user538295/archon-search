"""GET /health endpoint."""
from __future__ import annotations

from importlib.metadata import version, PackageNotFoundError

from fastapi import APIRouter

router = APIRouter()

try:
    _VERSION = version("archon-search")
except PackageNotFoundError:
    _VERSION = "dev"


@router.get("/health")
async def health() -> dict:
    return {"status": "running", "version": _VERSION}

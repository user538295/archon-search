"""GET /status endpoint — rich operator-facing service status."""
from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, Request

from archon_search.config import SearchConfig
from archon_search.progress import compute_eta_seconds
from archon_search.server.schemas import ErrorDetail, StatusCollectionEntry, StatusResponse
from archon_search.sync import path_to_collection_name

router = APIRouter()

try:
    _VERSION = version("archon-search")
except PackageNotFoundError:
    _VERSION = "dev"


@router.get("/status", response_model=StatusResponse, responses={401: {"model": ErrorDetail}})
async def status(request: Request) -> StatusResponse:
    """Return rich operator-facing status including service info and per-collection progress."""
    config: SearchConfig = request.app.state.config
    ns: str = request.state.namespace

    # Service / process fields
    pid = os.getpid()

    # Resolve which collection names belong to the caller's namespace
    search_store = request.app.state.search_store
    all_meta = await search_store.get_all_collections_meta()
    ns_names: set[str] = {m.name for m in all_meta if m.namespace == ns}

    # Load indexing state for collection progress (state_store created once in create_app)
    state_store = request.app.state.state_store
    state = state_store.read()

    collections_progress: dict = {}
    if state:
        for cname, cp in state.collections.items():
            eta = compute_eta_seconds(cp)
            collections_progress[cname] = {
                "status": str(cp.status),
                "processed_files": cp.processed_files,
                "total_files": cp.total_files,
                "error": cp.error,
                "error_count": cp.error_count,
                "eta_seconds": eta,
            }

    # Convert config paths to collection names before building the union
    config_names: set[str] = {path_to_collection_name(p) for p in config.collections}
    pinned_names: set[str] = {path_to_collection_name(p) for p in config.pinned_collections}
    all_names: set[str] = config_names | pinned_names | set(collections_progress.keys())

    # Filter to only names in the caller's namespace
    all_names &= ns_names

    collection_entries: list[StatusCollectionEntry] = []
    for name in sorted(all_names):
        progress = collections_progress.get(name)
        watching = config.watch
        collection_entries.append(
            StatusCollectionEntry(
                name=name,
                path="",  # path not yet populated from store
                doc_count=0,
                chunk_count=0,
                status=progress["status"] if progress else "not_yet_indexed",
                watching=watching,
                eta_seconds=progress["eta_seconds"] if progress else None,
                processed_files=progress["processed_files"] if progress else 0,
                total_files=progress["total_files"] if progress else 0,
                error=progress.get("error") if progress else None,
                error_count=progress["error_count"] if progress else 0,
            )
        )

    return StatusResponse(
        running=True,
        pid=pid,
        version=_VERSION,
        collections=collection_entries,
    )

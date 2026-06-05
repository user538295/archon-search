"""GET /status endpoint — rich operator-facing service status."""
from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, Request

from archon_search.config import SearchConfig
from archon_search.progress import compute_eta_seconds
from archon_search.server.readiness import collect_readiness
from archon_search.server.schemas import ErrorDetail, StatusCollectionEntry, StatusResponse
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
    ns_meta = [m for m in all_meta if m.namespace == ns]
    ns_names: set[str] = {m.name for m in ns_meta}
    meta_by_name = {m.name: m for m in ns_meta}

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

    # ns_names (from the store) is the authoritative, namespace-scoped set of collections.
    # Config/pinned paths without a store meta row are not yet indexed and won't appear here.
    all_names: set[str] = ns_names

    collection_entries: list[StatusCollectionEntry] = []
    for name in sorted(all_names):
        progress = collections_progress.get(name)
        watching = config.watch
        col_meta = meta_by_name.get(name)

        # C2: warn when multilingual mode is on but untagged legacy chunks exist
        warning: str | None = None
        if config.multilingual:
            untagged = await search_store.count_untagged_language_chunks(name)
            if untagged > 0:
                warning = "multilingual=true but collection contains untagged chunks; re-ingest required"

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
                needs_reindex=col_meta.needs_reindex if col_meta else False,
                warning=warning,
            )
        )

    readiness = await collect_readiness(request.app.state, state)
    return StatusResponse(
        running=True,
        pid=pid,
        version=_VERSION,
        collections=collection_entries,
        readiness=readiness,
    )

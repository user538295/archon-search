"""GET/POST/DELETE /collections/* endpoints — collection management."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from archon_search.config import SearchConfig, save_config
from archon_search.jobs.model import job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.server.routes_jobs import IngestRequest, _default_ingest_task
from archon_search.sync import path_to_collection_name

logger = logging.getLogger("archon-search")

router = APIRouter(prefix="/collections")


# ---------------------------------------------------------------------------
# Request / response helpers
# ---------------------------------------------------------------------------


class AddCollectionRequest(BaseModel):
    path: str


# ---------------------------------------------------------------------------
# Collection registry helpers
# ---------------------------------------------------------------------------


def _all_collection_paths(config: SearchConfig) -> dict[str, str]:
    """Return {name: resolved_path} for all known paths (collections + pinned)."""
    result: dict[str, str] = {}
    all_paths = list(dict.fromkeys(config.collections + config.pinned_collections))
    for p in all_paths:
        name = path_to_collection_name(p)
        resolved = str(Path(p).expanduser().resolve())
        result[name] = resolved
    return result


def _collection_status(config: SearchConfig, state_store, name: str) -> str:
    """Return the indexing status string for a collection."""
    try:
        state = state_store.read()
        if state and name in state.collections:
            return str(state.collections[name].status)
    except Exception:  # noqa: BLE001
        pass
    return "not_yet_indexed"


def _maybe_save_config(config: SearchConfig, request: Request) -> None:
    """Persist config to disk if config_path is set on app.state (graceful degradation)."""
    config_path = getattr(request.app.state, "config_path", None)
    if config_path is not None:
        save_config(config, config_path)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/", response_model=None)
def list_collections(request: Request) -> JSONResponse:
    """List all known collections with basic metadata."""
    config: SearchConfig = request.app.state.config
    state_store = request.app.state.state_store

    path_to_name = _all_collection_paths(config)
    result = []
    for name, resolved in path_to_name.items():
        status = _collection_status(config, state_store, name)
        entry = {
            "name": name,
            "path": resolved,
            "description": "",
            "doc_count": 0,
            "chunk_count": 0,
            "status": status,
        }
        result.append(entry)

    return JSONResponse(content=result)


@router.post("/", status_code=202, response_model=None)
async def add_collection(body: AddCollectionRequest, request: Request) -> JSONResponse:
    """Add a new collection: persist config + enqueue ingest. Returns 202 + IngestJob."""
    config: SearchConfig = request.app.state.config
    store: JobStore = request.app.state.job_store

    resolved = str(Path(body.path).expanduser().resolve())

    # Fix 2: dedup check against resolved paths from both lists
    existing_resolved = {
        str(Path(p).expanduser().resolve())
        for p in config.collections + config.pinned_collections
    }
    if resolved in existing_resolved:
        return JSONResponse({"detail": "collection already registered"}, status_code=409)

    config.collections.append(resolved)
    _maybe_save_config(config, request)

    job = store.create()
    ingest_body = IngestRequest(collection=path_to_collection_name(resolved), path=resolved)
    task = asyncio.create_task(_default_ingest_task(job.job_id, store, ingest_body))
    request.app.state._background_tasks.add(task)
    task.add_done_callback(request.app.state._background_tasks.discard)

    return JSONResponse(content=job_to_dict(job), status_code=202)


@router.delete("/{name}", response_model=None)
async def remove_collection(name: str, request: Request) -> JSONResponse:
    """Remove a collection: delete config entry and drop LanceDB data."""
    config: SearchConfig = request.app.state.config

    path_to_name = _all_collection_paths(config)
    if name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    resolved = path_to_name[name]

    # Resolve config lists for comparison
    collections_resolved = [str(Path(p).expanduser().resolve()) for p in config.collections]
    pinned_resolved = [str(Path(p).expanduser().resolve()) for p in config.pinned_collections]

    in_collections = resolved in collections_resolved
    in_pinned = resolved in pinned_resolved

    # Pinned-only: path is in pinned but NOT in collections — reject
    if in_pinned and not in_collections:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Collection {name!r} is pinned-only; remove it from "
                "'pinned_collections' in config before deleting."
            ),
        )

    # Remove from collections list (by original path values)
    if in_collections:
        config.collections = [
            p for p in config.collections
            if str(Path(p).expanduser().resolve()) != resolved
        ]

    # Remove from pinned_collections list if also present there
    if in_pinned:
        config.pinned_collections = [
            p for p in config.pinned_collections
            if str(Path(p).expanduser().resolve()) != resolved
        ]

    _maybe_save_config(config, request)

    # Best-effort: drop LanceDB table if store is available
    search_store = getattr(request.app.state, "search_store", None)
    if search_store is not None:
        try:
            await search_store.drop_collection(name)
        except (KeyError, RuntimeError):
            pass  # table doesn't exist — that's fine

    return JSONResponse(content={"name": name, "deleted": True})


@router.get("/{name}", response_model=None)
def get_collection_info(name: str, request: Request) -> JSONResponse:
    """Return CollectionDetail for a single collection. 404 if not found."""
    config: SearchConfig = request.app.state.config
    state_store = request.app.state.state_store

    path_to_name = _all_collection_paths(config)
    if name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    resolved = path_to_name[name]
    status = _collection_status(config, state_store, name)

    # Pull extra detail from state if available
    embedding_model = config.embedding_model
    centroid_present = False
    last_indexed: str | None = None
    try:
        state = state_store.read()
        if state and name in state.collections:
            cp = state.collections[name]
            last_indexed = getattr(cp, "completed_at", None)
    except Exception:  # noqa: BLE001
        pass

    data = {
        "name": name,
        "path": resolved,
        "description": "",
        "doc_count": 0,
        "chunk_count": 0,
        "status": status,
        "embedding_model": embedding_model,
        "centroid_present": centroid_present,
        "last_indexed": last_indexed,
    }
    return JSONResponse(content=data)


@router.post("/{name}/reindex", status_code=202, response_model=None)
async def reindex_collection(name: str, request: Request) -> JSONResponse:
    """Start a reindex job for an existing collection. 404 if not found."""
    config: SearchConfig = request.app.state.config
    store: JobStore = request.app.state.job_store

    path_to_name = _all_collection_paths(config)
    if name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    resolved = path_to_name[name]

    job = store.create()
    ingest_body = IngestRequest(collection=name, path=resolved)
    task = asyncio.create_task(_default_ingest_task(job.job_id, store, ingest_body))
    request.app.state._background_tasks.add(task)
    task.add_done_callback(request.app.state._background_tasks.discard)

    return JSONResponse(content=job_to_dict(job), status_code=202)

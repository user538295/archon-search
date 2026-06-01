"""GET/POST/DELETE /collections/* endpoints — collection management."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import math

from archon_search._path_safety import PathUnsafeError, validate_ingest_path
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig, save_config
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.model import job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.model_validation import ModelValidationError, validate_embedding_model
from archon_search.server._ingest_lock import acquire_collection_lock_or_503
from archon_search.server._ingested_by import parse_ingested_by_header
from archon_search.server.routes_jobs import IngestRequest, _default_ingest_task, _default_ingest_task_with_lock
from archon_search.server.schemas import CollectionDetail, CollectionSummary, DeleteResponse, ErrorDetail, JobResponse, PatchCollectionBody
from archon_search.store import StoreBusyError
from archon_search.sync import path_to_collection_name
from archon_search.types import JobStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collections")


# ---------------------------------------------------------------------------
# Request / response helpers
# ---------------------------------------------------------------------------


class AddCollectionRequest(BaseModel):
    path: str
    embedding_model: str | None = None


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


@router.get("/", response_model=list[CollectionSummary], responses={401: {"model": ErrorDetail}})
async def list_collections(request: Request) -> list[CollectionSummary]:
    """List all known collections with basic metadata."""
    config: SearchConfig = request.app.state.config
    state_store = request.app.state.state_store
    search_store = request.app.state.search_store
    ns: str = request.state.namespace

    all_meta = await search_store.get_all_collections_meta()
    ns_names = {m.name for m in all_meta if m.namespace == ns}
    meta_by_name = {m.name: m for m in all_meta}

    path_to_name = _all_collection_paths(config)
    result = []
    for name, resolved in path_to_name.items():
        if name in ns_names:
            namespace = meta_by_name[name].namespace
        elif name not in meta_by_name and ns == DEFAULT_NAMESPACE:
            namespace = DEFAULT_NAMESPACE
        else:
            continue
        status = _collection_status(config, state_store, name)
        col_meta = meta_by_name.get(name)
        result.append(CollectionSummary(
            name=name,
            path=resolved,
            description="",
            doc_count=0,
            chunk_count=0,
            namespace=namespace,
            status=status,
            active_embedding_model=(col_meta.active_embedding_model or config.embedding_model) if col_meta is not None else config.embedding_model,
            needs_reindex=col_meta.needs_reindex if col_meta is not None else False,
        ))

    return result


_ERROR_401 = {401: {"model": ErrorDetail}}
_ERROR_401_404 = {401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}}
_ERROR_401_409 = {401: {"model": ErrorDetail}, 409: {"model": ErrorDetail}}
_ERROR_400_401_409 = {
    400: {"model": ErrorDetail, "description": "Ingest path failed safety validation"},
    401: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
}
_ERROR_400_401_409_503 = {
    400: {"model": ErrorDetail, "description": "Ingest path failed safety validation"},
    401: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
    503: {"description": "Store busy — reindex in progress"},
}


@router.post("/", status_code=202, response_model=JobResponse, responses={**_ERROR_400_401_409_503, 422: {"model": ErrorDetail}})
async def add_collection(body: AddCollectionRequest, request: Request) -> JobResponse | JSONResponse:
    """Add a new collection: persist config + enqueue ingest. Returns 202 + IngestJob."""
    config: SearchConfig = request.app.state.config
    store: JobStore = request.app.state.job_store
    search_store = request.app.state.search_store
    ns: str = request.state.namespace

    # Validate embedding_model early, before any side-effects
    if body.embedding_model is not None:
        try:
            await validate_embedding_model(body.embedding_model)
        except ModelValidationError as e:
            raise HTTPException(status_code=422, detail=str(e))

    try:
        resolved = str(validate_ingest_path(body.path))
    except PathUnsafeError as e:
        raise HTTPException(status_code=400, detail=f"path is unsafe: {e.reason}")

    # Dedup check against resolved paths from both lists
    existing_resolved = {
        str(Path(p).expanduser().resolve())
        for p in config.collections + config.pinned_collections
    }
    if resolved in existing_resolved:
        return JSONResponse({"detail": "collection already registered"}, status_code=409)

    # Global name uniqueness check across all namespaces
    collection_name = path_to_collection_name(resolved)
    all_meta = await search_store.get_all_collections_meta()
    if any(m.name == collection_name for m in all_meta):
        return JSONResponse({"detail": "collection name already registered"}, status_code=409)

    config.collections.append(resolved)
    _maybe_save_config(config, request)

    # Write stub meta — update_collection_meta acquires the per-collection lock internally.
    # StoreBusyError → 503; ValueError → 409 TOCTOU race; other → 500.
    active_model = body.embedding_model if body.embedding_model is not None else config.embedding_model
    try:
        await search_store.update_collection_meta(
            CollectionMeta(
                name=collection_name,
                namespace=ns,
                active_embedding_model=active_model,
                pending_embedding_model=None,
                needs_reindex=False,
                reindex_job_id=None,
            )
        )
    except StoreBusyError as e:
        config.collections.remove(resolved)
        _maybe_save_config(config, request)
        retry_after = str(math.ceil(e.timeout_s))
        return JSONResponse(
            {"error": "store_busy", "detail": "reindex in progress; retry after Retry-After seconds"},
            status_code=503,
            headers={"Retry-After": retry_after},
        )
    except ValueError:
        # TOCTOU race: name claimed by another namespace between check and write
        config.collections.remove(resolved)
        _maybe_save_config(config, request)
        return JSONResponse({"detail": "collection name already registered"}, status_code=409)
    except Exception:
        config.collections.remove(resolved)
        try:
            _maybe_save_config(config, request)
        except Exception:
            logger.exception("Failed to rollback config after stub meta write failure")
        return JSONResponse({"detail": "internal error"}, status_code=500)

    # Pre-acquire the per-collection lock for the ingest task.
    lock_result = await acquire_collection_lock_or_503(search_store, collection_name)
    if isinstance(lock_result, JSONResponse):
        # Lock busy for ingest task — roll back config and meta to leave clean state.
        config.collections.remove(resolved)
        try:
            _maybe_save_config(config, request)
        except Exception:
            logger.exception("Failed to rollback config after ingest-lock timeout")
        try:
            await search_store.delete_collection_meta(collection_name, ns)
        except Exception:
            logger.exception("Failed to rollback meta after ingest-lock timeout")
        return lock_result

    ingested_by = parse_ingested_by_header(request.headers.get("X-Ingested-By"))
    try:
        job = store.create(namespace=ns)
    except OSError:
        if isinstance(lock_result, asyncio.Lock) and lock_result.locked():
            lock_result.release()
        return JSONResponse({"detail": "internal error"}, status_code=500)
    ingest_body = IngestRequest(
        collection=collection_name, path=resolved, ingested_by=ingested_by
    )

    pipeline = getattr(request.app.state, "pipeline", None)
    embedder_cache = getattr(request.app.state, "embedder_cache", None)
    config_state = getattr(request.app.state, "config", None)

    if lock_result is not None:
        task = asyncio.create_task(
            _default_ingest_task_with_lock(
                job.job_id, store, ingest_body, namespace=ns, held_lock=lock_result,
                search_store=search_store, embedder_cache=embedder_cache, pipeline=pipeline, config=config_state,
            )
        )
    else:
        task = asyncio.create_task(
            _default_ingest_task(
                job.job_id, store, ingest_body, namespace=ns,
                search_store=search_store, embedder_cache=embedder_cache, pipeline=pipeline, config=config_state,
            )
        )
    request.app.state._background_tasks.add(task)
    task.add_done_callback(request.app.state._background_tasks.discard)

    return JobResponse(**job_to_dict(job))


@router.delete("/{name}", response_model=DeleteResponse, responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}})
async def remove_collection(name: str, request: Request) -> DeleteResponse | JSONResponse:
    """Remove a collection: delete config entry and drop LanceDB data."""
    config: SearchConfig = request.app.state.config
    search_store = getattr(request.app.state, "search_store", None)
    ns: str = request.state.namespace

    path_to_name = _all_collection_paths(config)
    if name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    # Namespace check: meta row must exist and belong to the caller's namespace
    if search_store is not None:
        meta = await search_store.get_collection_meta(name, namespace=ns)
        if meta is None:
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

    # Drop LanceDB table and meta row
    if search_store is not None:
        try:
            await search_store.drop_collection(name)
        except (KeyError, RuntimeError):
            pass  # table doesn't exist — that's fine
        await search_store.delete_collection_meta(name, ns)

    return DeleteResponse(name=name, deleted=True)


@router.get("/{name}", response_model=CollectionDetail, responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}})
async def get_collection_info(name: str, request: Request) -> CollectionDetail:
    """Return CollectionDetail for a single collection. 404 if not found."""
    config: SearchConfig = request.app.state.config
    state_store = request.app.state.state_store
    ns: str = request.state.namespace

    path_to_name = _all_collection_paths(config)
    if name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    # Namespace gate: 404 for cross-namespace access; also fetches meta for centroid/namespace.
    search_store = getattr(request.app.state, "search_store", None)
    meta = None
    if search_store is not None:
        meta = await search_store.get_collection_meta(name, namespace=ns)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    resolved = path_to_name[name]
    status = _collection_status(config, state_store, name)

    # Pull extra detail from state if available
    last_indexed: str | None = None
    try:
        state = state_store.read()
        if state and name in state.collections:
            cp = state.collections[name]
            last_indexed = getattr(cp, "completed_at", None)
    except Exception:  # noqa: BLE001
        pass

    # Fetch real doc_count and ACL stats from search store; reuse already-fetched meta for centroid.
    doc_count = 0
    centroid_present = bool(meta is not None and meta.centroid)
    acl_protected = 0
    acl_open = 0
    if search_store is not None:
        try:
            doc_count = await search_store.count_documents(name)
        except Exception:  # noqa: BLE001
            doc_count = 0
        try:
            acl_protected, acl_open = await search_store.get_acl_stats(name)
        except Exception:  # noqa: BLE001
            acl_protected, acl_open = 0, 0

    data = {
        "name": name,
        "path": resolved,
        "description": "",
        "doc_count": doc_count,
        "chunk_count": 0,
        "status": status,
        "active_embedding_model": (meta.active_embedding_model or config.embedding_model) if meta is not None else config.embedding_model,
        "pending_embedding_model": meta.pending_embedding_model if meta is not None else None,
        "needs_reindex": meta.needs_reindex if meta is not None else False,
        "reindex_job_id": meta.reindex_job_id if meta is not None else None,
        "centroid_present": centroid_present,
        "last_indexed": last_indexed,
        "namespace": meta.namespace if meta is not None else DEFAULT_NAMESPACE,
        "acl_protected_count": acl_protected,
        "acl_open_count": acl_open,
    }
    return CollectionDetail(**data)


_ERROR_401_404_409_422 = {
    401: {"model": ErrorDetail},
    404: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
    422: {"model": ErrorDetail},
}


@router.patch("/{name}", response_model=CollectionDetail, responses=_ERROR_401_404_409_422)
async def patch_collection(name: str, body: PatchCollectionBody, request: Request) -> CollectionDetail | JSONResponse:
    """Update the embedding model for a collection. Triggers reindex if needed."""
    config: SearchConfig = request.app.state.config
    search_store = request.app.state.search_store
    job_store: JobStore = request.app.state.job_store
    ns: str = request.state.namespace

    # 404 if collection not in config
    path_to_name = _all_collection_paths(config)
    if name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    # 404 if meta not found for this namespace
    meta = await search_store.get_collection_meta(name, namespace=ns)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    # Validate embedding model — 422 on ModelValidationError
    try:
        new_dim = await validate_embedding_model(body.embedding_model)
    except ModelValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Dimension mismatch guard
    stored_dim = await search_store.get_stored_vector_dimension(name, namespace=ns)
    if stored_dim is not None and stored_dim != new_dim:
        raise HTTPException(
            status_code=422,
            detail=(
                f"model dimension mismatch: current vectors are {stored_dim}-dim, "
                f"new model produces {new_dim}-dim; delete and recreate collection to change dimensions"
            ),
        )

    # 409 guard: check if reindex job is still active
    stale_cleared = False
    if meta.reindex_job_id is not None:
        job = job_store.get(meta.reindex_job_id)
        if job is not None and job.status in (JobStatus.RUNNING, JobStatus.PENDING):
            return JSONResponse(
                {"detail": "reindex in progress; wait for job to complete before changing embedding model"},
                status_code=409,
            )
        # Stale (DONE/FAILED/CANCELLED) — clear it (must persist below)
        meta.reindex_job_id = None
        stale_cleared = True

    requested = body.embedding_model
    active = meta.active_embedding_model
    pending = meta.pending_embedding_model

    # State machine
    if active == requested and pending is None:
        # (a) no-op — persist only if stale reindex_job_id was cleared
        if stale_cleared:
            await search_store.update_collection_meta(meta)
    elif pending == requested:
        # (a') no-op — persist only if stale reindex_job_id was cleared
        if stale_cleared:
            await search_store.update_collection_meta(meta)
    elif pending is not None and active == requested:
        # (c) revert: clear pending and any stale reindex_job_id
        meta.pending_embedding_model = None
        meta.needs_reindex = False
        meta.reindex_job_id = None
        await search_store.update_collection_meta(meta)
    else:
        # (b) or (d): new model requested
        chunk_count = await search_store.count_chunks(name, namespace=ns)
        if chunk_count > 0:
            meta.pending_embedding_model = requested
            meta.needs_reindex = True
        else:
            meta.active_embedding_model = requested
            meta.pending_embedding_model = None
            meta.needs_reindex = False
            meta.reindex_job_id = None
        await search_store.update_collection_meta(meta)

    # Build CollectionDetail response
    state_store = request.app.state.state_store
    resolved = path_to_name[name]
    status = _collection_status(config, state_store, name)

    last_indexed: str | None = None
    try:
        state = state_store.read()
        if state and name in state.collections:
            cp = state.collections[name]
            last_indexed = getattr(cp, "completed_at", None)
    except Exception:  # noqa: BLE001
        pass

    doc_count = 0
    centroid_present = bool(meta.centroid)
    acl_protected = 0
    acl_open = 0
    try:
        doc_count = await search_store.count_documents(name)
    except Exception:  # noqa: BLE001
        doc_count = 0
    try:
        acl_protected, acl_open = await search_store.get_acl_stats(name)
    except Exception:  # noqa: BLE001
        acl_protected, acl_open = 0, 0

    return CollectionDetail(
        name=name,
        path=resolved,
        description="",
        doc_count=doc_count,
        chunk_count=0,
        status=status,
        active_embedding_model=meta.active_embedding_model or config.embedding_model,
        pending_embedding_model=meta.pending_embedding_model,
        needs_reindex=meta.needs_reindex,
        reindex_job_id=meta.reindex_job_id,
        centroid_present=centroid_present,
        last_indexed=last_indexed,
        namespace=meta.namespace,
        acl_protected_count=acl_protected,
        acl_open_count=acl_open,
    )


@router.post("/{name}/reindex", status_code=202, response_model=JobResponse, responses=_ERROR_401_404)
async def reindex_collection(name: str, request: Request) -> JobResponse | JSONResponse:
    """Start a reindex job for an existing collection. 404 if not found."""
    config: SearchConfig = request.app.state.config
    store: JobStore = request.app.state.job_store
    search_store = request.app.state.search_store
    ns: str = request.state.namespace

    path_to_name = _all_collection_paths(config)
    if name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    meta = await search_store.get_collection_meta(name, namespace=ns)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    resolved = path_to_name[name]

    ingested_by = parse_ingested_by_header(request.headers.get("X-Ingested-By"))
    try:
        job = store.create(namespace=ns)
    except OSError:
        return JSONResponse({"detail": "internal error"}, status_code=500)
    ingest_body = IngestRequest(collection=name, path=resolved, ingested_by=ingested_by)
    pipeline = getattr(request.app.state, "pipeline", None)
    embedder_cache = getattr(request.app.state, "embedder_cache", None)
    config_state = getattr(request.app.state, "config", None)
    task = asyncio.create_task(
        _default_ingest_task(
            job.job_id, store, ingest_body, namespace=ns,
            search_store=search_store, embedder_cache=embedder_cache, pipeline=pipeline, config=config_state,
        )
    )
    request.app.state._background_tasks.add(task)
    task.add_done_callback(request.app.state._background_tasks.discard)

    return JobResponse(**job_to_dict(job))

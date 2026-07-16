"""GET/POST/DELETE /collections/* endpoints — collection management."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
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
from archon_search.server.routes_jobs import IngestRequest, _default_ingest_task, _default_ingest_task_with_lock, _reindex_task
from archon_search.server.schemas import CollectionDetail, CollectionSummary, DeleteResponse, DocumentInfoItem, DocumentListResponse, ErrorDetail, ExpiringChunkItem, ExpiringChunksResponse, JobResponse, MigrateInPlaceResponse, MigrateRequest, MigrationPendingResponse, MigrationSpecSchema, PatchCollectionBody
from archon_search.store import STORE_SCHEMA_VERSION, StoreBusyError
from archon_search.sync import path_to_collection_name
from archon_search.types import JobStatus, MigrationJob, MigrationKind, MigrationSpec

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
        # Live count, not the maintained meta.chunk_count: the cached value drifts
        # (delete subtracts a vector count, TTL-expiry pruning never decrements it).
        # A metaless entry is a configured-but-not-yet-indexed collection → 0; this
        # also avoids surfacing an orphaned table's rows to a caller that has no meta
        # row for it. count_chunks is table-wide (its namespace arg is ignored by the
        # store), but one collection name maps to exactly one namespace, so the count
        # is namespace-isolated in practice.
        chunk_count = 0
        if col_meta is not None:
            try:
                chunk_count = await search_store.count_chunks(name, namespace=namespace)
            except Exception:  # noqa: BLE001 — one bad collection must not 500 the whole list
                chunk_count = 0
        result.append(CollectionSummary(
            name=name,
            path=resolved,
            description="",
            # doc_count intentionally 0 — populating it here is bug-025 (out of scope).
            doc_count=0,
            chunk_count=chunk_count,
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
                schema_version=STORE_SCHEMA_VERSION,
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
    chunk_count = 0
    if search_store is not None:
        try:
            doc_count = await search_store.count_documents(name)
        except Exception:  # noqa: BLE001
            doc_count = 0
        try:
            chunk_count = await search_store.count_chunks(name, namespace=ns)
        except Exception:  # noqa: BLE001
            chunk_count = 0
        try:
            acl_protected, acl_open = await search_store.get_acl_stats(name)
        except Exception:  # noqa: BLE001
            acl_protected, acl_open = 0, 0

    data = {
        "name": name,
        "path": resolved,
        "description": "",
        "doc_count": doc_count,
        "chunk_count": chunk_count,
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


@router.get("/{name}/documents", response_model=DocumentListResponse, responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}})
async def list_collection_documents(
    name: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> DocumentListResponse:
    """List documents in a collection with cursor-based pagination (E0c BE-6).

    - ``limit``: 1–200, default 50.
    - ``cursor``: opaque cursor from the previous response's ``next_cursor``.
      A cursor referencing a deleted document silently resumes from the next
      sort position — no 4xx is raised.
    - Returns 404 when the collection does not exist in the caller's namespace.
    - Returns 422 when ``limit`` is outside [1, 200].
    """
    ns: str = request.state.namespace
    search_store = request.app.state.search_store

    meta = await search_store.get_collection_meta(name, namespace=ns)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    pipeline = request.app.state.pipeline
    items, next_cursor, total = await pipeline.list_documents(
        name, limit, cursor=cursor, namespace=ns
    )
    return DocumentListResponse(
        items=[
            DocumentInfoItem(
                doc_id=doc.doc_id,
                source_path=doc.source_path,
                chunk_count=doc.chunk_count,
                indexed_at=doc.indexed_at,
                scopes=doc.scopes,
            )
            for doc in items
        ],
        next_cursor=next_cursor,
        total=total,
    )


_SECONDS_PER_HOUR: int = 3_600


@router.get(
    "/{name}/expiring",
    response_model=ExpiringChunksResponse,
    responses={**_ERROR_401_404, 422: {"model": ErrorDetail}},
)
async def get_expiring_chunks(
    name: str,
    request: Request,
    within_hours: int = Query(ge=1, le=8760),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> ExpiringChunksResponse:
    """List chunks expiring within *within_hours* hours (E2a BE-4).

    - ``within_hours``: 1–8760 (1 hour to 1 year); required.
    - ``limit``: 1–200, default 50.
    - ``cursor``: opaque cursor from a previous response's ``next_cursor``.
    - Returns 404 when the collection does not exist in the caller's namespace.
    - Already-expired chunks are excluded; chunks with no expiry are excluded.
    """
    ns: str = request.state.namespace
    search_store = request.app.state.search_store

    meta = await search_store.get_collection_meta(name, namespace=ns)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    within_seconds = within_hours * _SECONDS_PER_HOUR
    items_raw, next_cursor = await search_store.query_expiring_chunks(
        name, ns, within_seconds, limit, cursor
    )
    items = [ExpiringChunkItem(**row) for row in items_raw]
    return ExpiringChunksResponse(items=items, next_cursor=next_cursor, page_count=len(items))


_ERROR_401_404_409_422 = {
    401: {"model": ErrorDetail},
    404: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
    422: {"model": ErrorDetail},
}


@router.patch("/{name}", response_model=CollectionDetail, responses=_ERROR_401_404_409_422)
async def patch_collection(name: str, body: PatchCollectionBody, request: Request) -> CollectionDetail | JSONResponse:
    """Update a collection's embedding model and/or default TTL. Triggers reindex when the model changes."""
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

    # Determine which fields were explicitly provided in the request body.
    payload = body.model_dump(exclude_unset=True)

    # --- Embedding model logic (only when field is explicitly set AND non-null) ---
    stale_cleared = False
    if "embedding_model" in payload and body.embedding_model is not None:
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
    elif "embedding_model" not in payload:
        # No embedding_model field at all — check for stale reindex_job_id (for TTL-only path)
        if meta.reindex_job_id is not None:
            job = job_store.get(meta.reindex_job_id)
            if job is None or job.status not in (JobStatus.RUNNING, JobStatus.PENDING):
                meta.reindex_job_id = None
                stale_cleared = True

    # --- default_ttl_seconds (explicit null = clear; absent = no change) ---
    if "default_ttl_seconds" in payload:
        meta = dataclasses.replace(meta, default_ttl_seconds=body.default_ttl_seconds)
        await search_store.update_collection_meta(meta)
    elif stale_cleared and "embedding_model" not in payload:
        # Persist stale-cleared reindex_job_id even when no other field changed
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
    chunk_count = 0
    centroid_present = bool(meta.centroid)
    acl_protected = 0
    acl_open = 0
    try:
        doc_count = await search_store.count_documents(name)
    except Exception:  # noqa: BLE001
        doc_count = 0
    try:
        chunk_count = await search_store.count_chunks(name, namespace=ns)
    except Exception:  # noqa: BLE001
        chunk_count = 0
    try:
        acl_protected, acl_open = await search_store.get_acl_stats(name)
    except Exception:  # noqa: BLE001
        acl_protected, acl_open = 0, 0

    return CollectionDetail(
        name=name,
        path=resolved,
        description="",
        doc_count=doc_count,
        chunk_count=chunk_count,
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

    # 409 guard: reject if an active reindex is already in progress
    if meta.reindex_job_id is not None:
        existing_job = store.get(meta.reindex_job_id)
        if existing_job is not None and existing_job.status in {JobStatus.RUNNING, JobStatus.PENDING}:
            return JSONResponse({"detail": "reindex already in progress"}, status_code=409)
        # Stale: job missing or terminal — clear and proceed
        meta.reindex_job_id = None

    try:
        job = store.create_reindex(namespace=ns, target_embedding_model=meta.pending_embedding_model)
    except OSError:
        return JSONResponse({"detail": "internal error"}, status_code=500)

    meta.reindex_job_id = job.job_id
    try:
        await search_store.update_collection_meta(meta)
    except Exception:
        store.update(job.job_id, JobStatus.FAILED)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    embedder_cache = getattr(request.app.state, "embedder_cache", None)
    pipeline_obj = getattr(request.app.state, "pipeline", None)
    if embedder_cache is None or pipeline_obj is None:
        store.update(job.job_id, JobStatus.FAILED)
        return JSONResponse({"detail": "service not ready"}, status_code=503)

    task = asyncio.create_task(
        _reindex_task(
            job_id=job.job_id,
            store=search_store,
            job_store=store,
            embedder_cache=embedder_cache,
            pipeline=pipeline_obj,
            collection=name,
            namespace=ns,
            collection_path=Path(resolved),
        )
    )
    request.app.state._background_tasks.add(task)
    task.add_done_callback(request.app.state._background_tasks.discard)

    return JobResponse(**job_to_dict(job))


async def _migration_task(
    job: MigrationJob,
    job_store: JobStore,
    search_store: object,
    spec: MigrationSpec | None = None,
) -> None:
    """Coroutine that drives a single REWRITE MigrationJob to completion.

    The caller is responsible for ensuring the job is already in RUNNING state
    before invoking this coroutine.  Both the route handler and the scheduler
    dispatch function satisfy this contract:

    - **Route path**: ``migrate_collection`` calls
      ``job_store.transition({QUEUED}, RUNNING)`` immediately after creating
      the job, then passes the already-RUNNING job to ``asyncio.create_task``.
    - **Scheduler path**: ``JobScheduler._tick()`` calls
      ``store.transition({QUEUED}, RUNNING)`` before calling ``dispatch_fn``,
      which creates this task.

    Lifecycle (caller sets RUNNING → this task drives to terminal):
      1. Resolve spec from ``pending_migrations()`` when ``spec`` is ``None``
         (scheduler resume path).
      2. call ``apply_rewrite_migration`` with ``progress_cb``
      3. Transition to DONE with ``result.migrated_chunks`` on success.
      4. Transition to FAILED with error message on any exception.

    When ``spec`` is ``None`` (scheduler resume path), the pending migrations
    list is fetched from the store and the first REWRITE spec is used.
    """
    job_id = job.job_id

    def _progress_cb(processed: int, total: int, phase: str) -> None:
        try:
            job_store.update_progress(job_id, processed, total, phase)
        except (KeyError, OSError):
            logger.warning("_migration_task: could not update progress for job %s", job_id)

    try:
        if spec is None:
            pending = await search_store.pending_migrations(job.collection, job.namespace)  # type: ignore[attr-defined]
            rewrite_pending = [s for s in pending if s.kind == MigrationKind.REWRITE]
            if not rewrite_pending:
                raise RuntimeError(
                    f"MigrationJob {job_id}: no pending REWRITE migration found for "
                    f"collection {job.collection!r}"
                )
            spec = rewrite_pending[0]

        migrated = await search_store.apply_rewrite_migration(  # type: ignore[attr-defined]
            job.collection,
            job.namespace,
            spec,
            progress_cb=_progress_cb,
        )
        job_store.update(
            job_id,
            status=JobStatus.DONE,
            result={"migrated_chunks": migrated},
            migrations_applied=[spec.name],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("_migration_task: job %s failed", job_id)
        try:
            job_store.update(job_id, status=JobStatus.FAILED, error=str(exc))
        except (KeyError, OSError):
            logger.error("_migration_task: could not persist FAILED status for job %s", job_id)


@router.get("/{name}/migrations/pending", response_model=MigrationPendingResponse, responses=_ERROR_401_404)
async def get_migrations_pending(name: str, request: Request) -> MigrationPendingResponse:
    """Return the list of pending migrations for a collection.

    Returns 404 if the collection is not found in the caller's namespace.
    Returns an empty ``pending`` list when the collection schema is current.
    """
    config: SearchConfig = request.app.state.config
    search_store = request.app.state.search_store
    ns: str = request.state.namespace

    path_to_name = _all_collection_paths(config)
    if name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    meta = await search_store.get_collection_meta(name, namespace=ns)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    pending = await search_store.pending_migrations(name, ns)
    return MigrationPendingResponse(
        collection=name,
        pending=[
            MigrationSpecSchema(
                name=spec.name,
                kind=spec.kind.value,
                description=spec.description,
                introduced_at=spec.introduced_at,
            )
            for spec in pending
        ],
        schema_version=meta.schema_version,
    )


_MIGRATE_ERROR_RESPONSES = {
    401: {"model": ErrorDetail},
    404: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
    422: {"model": ErrorDetail},
    500: {"model": ErrorDetail},
}


@router.post(
    "/{name}/migrate",
    response_model=MigrateInPlaceResponse | MigrationPendingResponse | JobResponse,
    responses=_MIGRATE_ERROR_RESPONSES,
)
async def migrate_collection(
    name: str, body: MigrateRequest, request: Request
) -> MigrateInPlaceResponse | MigrationPendingResponse | JobResponse | JSONResponse:
    """Apply pending migrations or report pending list (dry-run).

    **dry_run=true:** returns the same ``MigrationPendingResponse`` body as
    ``GET /migrations/pending`` without applying anything.

    **In-place-only migrations:** applies synchronously, returns 200 with
    ``{migrations_applied: [...]}``.  No ``MigrationJob`` is created.

    **Rewrite migrations:** requires ``backup_confirmed: true``; creates a
    ``MigrationJob``, transitions it immediately to RUNNING, and returns 202
    with ``{job_id, status: "RUNNING"}``.  The job is transitioned before the
    task is spawned so the scheduler never sees it in QUEUED state.

    **export_rebuild migrations:** always returns 422 — D3 does not execute
    export_rebuild; operators must re-ingest manually.

    Returns 404 if the collection is not found in the caller's namespace.
    Returns 409 if a ReindexJob is active for the same collection.
    Returns 422 if rewrite/export_rebuild migrations are pending without
    ``backup_confirmed: true``, or if export_rebuild migrations are present.
    """
    config: SearchConfig = request.app.state.config
    search_store = request.app.state.search_store
    job_store: JobStore = request.app.state.job_store
    ns: str = request.state.namespace

    # Gate 1: config-path check (matches all sibling /{name} routes).
    path_to_name = _all_collection_paths(config)
    if name not in path_to_name:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    # Gate 2: namespace-scoped meta check.
    meta = await search_store.get_collection_meta(name, namespace=ns)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Collection {name!r} not found")

    pending = await search_store.pending_migrations(name, ns)

    # dry_run: return pending list, no side effects.
    if body.dry_run:
        return MigrationPendingResponse(
            collection=name,
            pending=[
                MigrationSpecSchema(
                    name=spec.name,
                    kind=spec.kind.value,
                    description=spec.description,
                    introduced_at=spec.introduced_at,
                )
                for spec in pending
            ],
            schema_version=meta.schema_version,
        )

    # Classify pending specs by kind.
    in_place_specs = [s for s in pending if s.kind == MigrationKind.IN_PLACE]
    rewrite_specs = [s for s in pending if s.kind == MigrationKind.REWRITE]
    export_rebuild_specs = [s for s in pending if s.kind == MigrationKind.EXPORT_REBUILD]

    # Gate: export_rebuild migrations cannot be executed by D3.
    if export_rebuild_specs:
        names = ", ".join(s.name for s in export_rebuild_specs)
        raise HTTPException(
            status_code=422,
            detail=(
                f"export_rebuild migrations cannot be applied automatically: {names}. "
                "Operators must re-ingest the collection manually (D5)."
            ),
        )

    # Gate: rewrite migrations require backup_confirmed.
    if rewrite_specs and not body.backup_confirmed:
        raise HTTPException(
            status_code=422,
            detail=(
                "backup_confirmed: true is required before applying rewrite migrations. "
                "Confirm you have a backup of the collection before proceeding."
            ),
        )

    # Rewrite path: create a MigrationJob and dispatch asynchronously.
    if rewrite_specs:
        # Gate: reject if a ReindexJob is active for this collection.
        if meta.reindex_job_id is not None:
            existing_reindex = job_store.get(meta.reindex_job_id)
            if existing_reindex is not None and existing_reindex.status in {
                JobStatus.RUNNING, JobStatus.QUEUED, JobStatus.PENDING
            }:
                return JSONResponse(
                    {"detail": "reindex in progress; wait for reindex job to complete before migrating"},
                    status_code=409,
                )

        # Apply in-place migrations before queuing the rewrite job.
        if in_place_specs:
            try:
                await search_store.apply_in_place_migrations(name, ns, in_place_specs)
            except Exception:
                logger.exception("apply_in_place_migrations failed before rewrite dispatch for collection %r", name)
                return JSONResponse({"detail": "migration failed: internal error"}, status_code=500)

        # Create the MigrationJob and immediately transition it to RUNNING.
        # The route dispatches the task directly (not via the scheduler), so the
        # job must NOT sit in QUEUED state — otherwise the scheduler would pick it
        # up on its next tick and dispatch a second _migration_task for the same job.
        try:
            migration_job = job_store.create_migration(
                collection=name,
                kind=MigrationKind.REWRITE,
                backup_confirmed=body.backup_confirmed,
                namespace=ns,
            )
        except OSError:
            return JSONResponse({"detail": "internal error"}, status_code=500)

        # Transition to RUNNING before spawning the task so the scheduler never
        # sees this job in QUEUED state (scheduler only promotes QUEUED → RUNNING).
        running_job = job_store.transition(migration_job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
        if running_job is None:
            # Should not happen (we just created this job), but guard defensively.
            logger.error("_migration_task route: failed to transition job %s to RUNNING", migration_job.job_id)
            return JSONResponse({"detail": "internal error"}, status_code=500)

        # Use the first rewrite spec (D3 scope: one rewrite migration at a time).
        spec = rewrite_specs[0]

        task = asyncio.create_task(
            _migration_task(
                job=running_job,
                job_store=job_store,
                search_store=search_store,
                spec=spec,
            )
        )
        request.app.state._background_tasks.add(task)
        task.add_done_callback(request.app.state._background_tasks.discard)

        return JSONResponse(
            job_to_dict(running_job),
            status_code=202,
        )

    # In-place-only path: apply synchronously.
    try:
        await search_store.apply_in_place_migrations(name, ns, in_place_specs)
    except Exception:
        logger.exception("apply_in_place_migrations failed for collection %r", name)
        return JSONResponse({"detail": "migration failed: internal error"}, status_code=500)

    return MigrateInPlaceResponse(
        migrations_applied=[s.name for s in in_place_specs],
    )

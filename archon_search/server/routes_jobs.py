"""POST /ingest, GET /jobs, GET /jobs/{job_id}, DELETE /jobs/{job_id} endpoints."""
from __future__ import annotations

import asyncio
import logging
import math
import os
from pathlib import Path
from typing import Any, Callable, Awaitable

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from archon_search._path_safety import PathUnsafeError, validate_ingest_path
from archon_search._types import IngestError, _file_exceeds_limit
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.embedder_cache import EmbedderCache
from archon_search.jobs.model import IngestJob, JobStatus, job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.server._ingest_lock import acquire_collection_lock_or_503
from archon_search.server._ingested_by import parse_ingested_by_header
from archon_search.server.schemas import ErrorDetail, JobListResponse, JobResponse
from archon_search.types import DeleteJob, ExportJob, ImportJob, MigrationJob, ReindexJob

logger = logging.getLogger(__name__)

router = APIRouter()

# Terminal statuses — DELETE is idempotent for these
_TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.FAILED, JobStatus.FAILED_EXPIRED, JobStatus.CANCELLED}
# Active statuses — DELETE sets CANCELLING
_ACTIVE_STATUSES = {JobStatus.RUNNING, JobStatus.PENDING}


# Inclusive valid range for chunk_ttl_seconds.
_TTL_MIN: int = 1
_TTL_MAX: int = 2**31 - 1
# Limits for chunk_scopes.
_SCOPES_MAX_ITEMS: int = 100
_SCOPE_MAX_LEN: int = 255


class IngestRequest(BaseModel):
    collection: str
    path: str | None = None
    documents: list[dict[str, Any]] | None = None
    ingested_by: str = "http"
    # E2a BE-4: optional per-request TTL and scope tags forwarded to the pipeline.
    chunk_ttl_seconds: int | None = None
    chunk_scopes: list[str] | None = None

    @field_validator("collection")
    @classmethod
    def collection_must_be_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("collection must be non-empty")
        return v

    @field_validator("chunk_ttl_seconds")
    @classmethod
    def chunk_ttl_seconds_must_be_positive(cls, v: int | None) -> int | None:
        if v is not None and not (_TTL_MIN <= v <= _TTL_MAX):
            raise ValueError(
                f"chunk_ttl_seconds must be in [{_TTL_MIN}, {_TTL_MAX}], got {v}"
            )
        return v

    @field_validator("chunk_scopes")
    @classmethod
    def chunk_scopes_must_be_valid(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        if len(v) > _SCOPES_MAX_ITEMS:
            raise ValueError(
                f"chunk_scopes must have at most {_SCOPES_MAX_ITEMS} items, got {len(v)}"
            )
        for item in v:
            if not (1 <= len(item) <= _SCOPE_MAX_LEN):
                raise ValueError(
                    f"each scope string must be 1–{_SCOPE_MAX_LEN} chars; got {len(item)!r}"
                )
        return v



async def _run_pipeline(
    job_id: str,
    store: JobStore,
    body: IngestRequest,
    pipeline_fn: Callable[..., Awaitable[None]] | None,
    namespace: str = DEFAULT_NAMESPACE,
    locked_by_caller: bool = False,
) -> None:
    """Run the ingest pipeline (real or stub). Raises on failure."""
    if pipeline_fn is not None:
        kwargs: dict = {"namespace": namespace}
        if locked_by_caller:
            kwargs["locked_by_caller"] = True
        await pipeline_fn(job_id, store, body, **kwargs)
    else:
        # Stub: succeed immediately
        await asyncio.sleep(0)


async def _dispatch_ingest(
    body: IngestRequest,
    namespace: str,
    search_store: Any,
    embedder_cache: "EmbedderCache",
    pipeline: Any,
    config: Any,
) -> tuple[list[str], list[dict]]:
    """Resolve per-collection embedder and dispatch to pipeline.ingest_file / ingest_directory.

    Returns a tuple of:
    - warnings: flat list of warning strings from all IngestResult objects
    - file_results: list of per-file dicts with ``doc_id``, ``status``, and optional ``code``
      (non-empty only when at least one file has a non-None code, e.g. ``"file_too_large"``)
    """
    meta = await search_store.get_collection_meta(body.collection, namespace)
    active_model = (meta.active_embedding_model if meta else "") or config.embedding_model
    embedder = await embedder_cache.get_or_load(active_model)

    if body.path is not None:
        p = Path(body.path)
        if p.is_file():
            result = await pipeline.ingest_file(
                p, body.collection, embedder=embedder, namespace=namespace,
                ingested_by=body.ingested_by,
                chunk_ttl_seconds=body.chunk_ttl_seconds,
                chunk_scopes=body.chunk_scopes,
            )
            file_results = []
            if result.code is not None:
                file_results.append({"doc_id": result.doc_id, "status": result.status, "code": result.code})
            return list(result.warnings), file_results
        elif p.is_dir():
            results = await pipeline.ingest_directory(
                p, body.collection, embedder=embedder, namespace=namespace,
                ingested_by=body.ingested_by,
                chunk_ttl_seconds=body.chunk_ttl_seconds,
                chunk_scopes=body.chunk_scopes,
            )
            warnings = [w for r in results for w in r.warnings]
            file_results = [
                {"doc_id": r.doc_id, "status": r.status, "code": r.code}
                for r in results
                if r.code is not None
            ]
            return warnings, file_results
        else:
            raise FileNotFoundError(f"path does not exist or is not a file/directory: {body.path}")
    elif body.documents is not None:
        if hasattr(pipeline, "ingest_documents"):
            await pipeline.ingest_documents(
                body.documents, body.collection, embedder=embedder, namespace=namespace, ingested_by=body.ingested_by
            )
        else:
            logger.warning("pipeline has no ingest_documents method; skipping documents ingest for collection %s", body.collection)
    return [], []


async def _default_ingest_task(
    job_id: str,
    store: JobStore,
    body: IngestRequest,
    namespace: str = DEFAULT_NAMESPACE,
    pipeline_fn: Callable[..., Awaitable[None]] | None = None,
    *,
    search_store: Any = None,
    embedder_cache: "EmbedderCache | None" = None,
    pipeline: Any = None,
    config: Any = None,
) -> None:
    """Lifecycle wrapper: PENDING → RUNNING → DONE/FAILED/CANCELLED."""
    try:
        store.update(job_id, status=JobStatus.RUNNING)
        ingest_warnings: list[str] = []
        ingest_file_results: list[dict] = []
        if pipeline_fn is None and search_store is not None and embedder_cache is not None and pipeline is not None and config is not None:
            ingest_warnings, ingest_file_results = await _dispatch_ingest(body, namespace, search_store, embedder_cache, pipeline, config)
        else:
            await _run_pipeline(job_id, store, body, pipeline_fn, namespace=namespace)
        # Check for cancellation before marking DONE
        job = store.get(job_id)
        if job and job.status == JobStatus.CANCELLING:
            store.update(job_id, status=JobStatus.CANCELLED)
            return
        result_dict: dict = {"warnings": ingest_warnings}
        if ingest_file_results:
            result_dict["file_results"] = ingest_file_results
        store.update(job_id, status=JobStatus.DONE, result=result_dict)
    except asyncio.CancelledError:
        try:
            store.update(job_id, status=JobStatus.CANCELLED)
        except (KeyError, OSError):
            logger.error("background ingest: could not persist CANCELLED status for job %s", job_id)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ingest task %s failed", job_id)
        try:
            store.update(job_id, status=JobStatus.FAILED, error=str(exc))
        except (KeyError, OSError):
            logger.error("background ingest: could not persist FAILED status for job %s", job_id)


async def _default_ingest_task_with_lock(
    job_id: str,
    store: JobStore,
    body: IngestRequest,
    namespace: str = DEFAULT_NAMESPACE,
    pipeline_fn: Callable[..., Awaitable[None]] | None = None,
    held_lock: "asyncio.Lock | None" = None,
    *,
    search_store: Any = None,
    embedder_cache: "EmbedderCache | None" = None,
    pipeline: Any = None,
    config: Any = None,
) -> None:
    """Lifecycle wrapper that releases held_lock in try/finally on success, failure, and cancellation.

    The held_lock was pre-acquired by the request handler; passing locked_by_caller=True
    to _run_pipeline signals the pipeline not to re-acquire the same lock.
    """
    try:
        store.update(job_id, status=JobStatus.RUNNING)
        ingest_warnings: list[str] = []
        ingest_file_results: list[dict] = []
        if pipeline_fn is None and search_store is not None and embedder_cache is not None and pipeline is not None and config is not None:
            # Release the pre-acquired lock before dispatch: pipeline.ingest_file /
            # ingest_directory will acquire it internally. Holding it here would
            # deadlock since asyncio.Lock is not reentrant.
            if held_lock is not None and held_lock.locked():
                held_lock.release()
            ingest_warnings, ingest_file_results = await _dispatch_ingest(body, namespace, search_store, embedder_cache, pipeline, config)
        else:
            await _run_pipeline(
                job_id, store, body, pipeline_fn, namespace=namespace, locked_by_caller=True
            )
        job = store.get(job_id)
        if job and job.status == JobStatus.CANCELLING:
            store.update(job_id, status=JobStatus.CANCELLED)
            return
        result_dict: dict = {"warnings": ingest_warnings}
        if ingest_file_results:
            result_dict["file_results"] = ingest_file_results
        store.update(job_id, status=JobStatus.DONE, result=result_dict)
    except asyncio.CancelledError:
        try:
            store.update(job_id, status=JobStatus.CANCELLED)
        except KeyError:
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ingest task %s failed", job_id)
        try:
            store.update(job_id, status=JobStatus.FAILED, error=str(exc))
        except KeyError:
            pass
    finally:
        # Release the pre-acquired lock regardless of outcome or cancellation.
        if held_lock is not None and held_lock.locked():
            held_lock.release()


async def _reindex_task(
    job_id: str,
    store: Any,
    job_store: JobStore,
    embedder_cache: EmbedderCache,
    pipeline: Any,
    collection: str,
    namespace: str,
    collection_path: Path | None,
) -> None:
    """Lifecycle wrapper for reindex: resolves embedder, ingests, promotes model on success.

    Never raises — catches all exceptions.
    """
    # --- Guards ---
    job = job_store.get(job_id)
    if job is None:
        meta = await store.get_collection_meta(collection, namespace)
        if meta is not None:
            meta.reindex_job_id = None
            await store.update_collection_meta(meta)
        logger.error("_reindex_task: job %s not found", job_id)
        return

    if not isinstance(job, ReindexJob):
        logger.error("_reindex_task: job %s is not a ReindexJob (got %s)", job_id, type(job))
        job_store.update(job_id, status=JobStatus.FAILED, error="not a ReindexJob")
        meta = await store.get_collection_meta(collection, namespace)
        if meta is not None:
            meta.reindex_job_id = None
            await store.update_collection_meta(meta)
        return

    if job.status in (JobStatus.CANCELLING, JobStatus.CANCELLED):
        meta = await store.get_collection_meta(collection, namespace)
        if meta is not None:
            meta.reindex_job_id = None
            await store.update_collection_meta(meta)
        return

    # --- Step 1 & 2: resolve embedder ---
    target_model = job.target_embedding_model
    try:
        if target_model is not None:
            embedder = await embedder_cache.get_or_load(target_model)
        else:
            meta = await store.get_collection_meta(collection, namespace)
            active_model = meta.active_embedding_model if meta is not None else ""
            embedder = await embedder_cache.get_or_load(active_model)
    except Exception as exc:  # noqa: BLE001
        logger.exception("_reindex_task: embedder resolution failed for job %s", job_id)
        meta = await store.get_collection_meta(collection, namespace)
        if meta is not None:
            meta.reindex_job_id = None
            await store.update_collection_meta(meta)
        job_store.update(job_id, status=JobStatus.FAILED, error=str(exc))
        return

    # --- Step 3: mark RUNNING ---
    job_store.update(job_id, status=JobStatus.RUNNING)

    # --- Step 4: ingest ---
    ingest_error: Exception | None = None
    cancelled: bool = False
    # A meta-only collection (e.g. created by a single-file POST /ingest) has no
    # configured source directory to re-scan; the route passes collection_path=None.
    # Skip the directory scan entirely — otherwise Path("") would coerce to "." and
    # walk the entire server CWD. The reindex becomes a data-only no-op that still
    # completes DONE, so `collection reindex --wait` exits 0 with a message.
    if collection_path is None:
        logger.info(
            "_reindex_task: collection %r has no configured source directory; "
            "skipping directory re-scan (no-op reindex) for job %s",
            collection, job_id,
        )
    else:
        try:
            await pipeline.ingest_directory(
                collection_path,
                collection,
                embedder=embedder,
                namespace=namespace,
                ingested_by="reindex",
                collection_root=collection_path,
            )
        except asyncio.CancelledError:
            cancelled = True
        except Exception as exc:  # noqa: BLE001
            ingest_error = exc

    if cancelled or ingest_error is not None:
        logger.exception("_reindex_task: ingest failed for job %s", job_id) if ingest_error else None
        # Step 6: failure path — do NOT touch active_embedding_model
        try:
            meta = await store.get_collection_meta(collection, namespace)
            if meta is not None:
                meta.reindex_job_id = None
                await store.update_collection_meta(meta)
        except Exception:  # noqa: BLE001
            logger.exception("_reindex_task: failed to clear reindex_job_id for job %s", job_id)
        try:
            if cancelled:
                job_store.update(job_id, status=JobStatus.CANCELLED)
            else:
                job_store.update(job_id, status=JobStatus.FAILED, error=str(ingest_error))
        except Exception:  # noqa: BLE001
            logger.exception("_reindex_task: failed to update job status for job %s", job_id)
        return

    # Check for cancellation between ingest and DONE (mirrors _default_ingest_task)
    current_job = job_store.get(job_id)
    if current_job and current_job.status == JobStatus.CANCELLING:
        try:
            meta = await store.get_collection_meta(collection, namespace)
            if meta is not None:
                meta.reindex_job_id = None
                await store.update_collection_meta(meta)
        except Exception:  # noqa: BLE001
            logger.exception("_reindex_task: failed to clear reindex_job_id on cancel for job %s", job_id)
        job_store.update(job_id, status=JobStatus.CANCELLED)
        return

    # --- Step 5: success path — promote model ---
    try:
        meta = await store.get_collection_meta(collection, namespace)
        if meta is not None:
            # Only promote the model when the chunks were actually re-embedded.
            # A source-less no-op reindex (collection_path is None) skipped the
            # scan, so promoting active_embedding_model would advertise a model the
            # stored vectors were never produced with — silent search corruption.
            # Leave pending_embedding_model/needs_reindex intact in that case.
            if target_model is not None and collection_path is not None:
                # Model-change path
                meta.active_embedding_model = target_model
                meta.reindex_job_id = None
                pending = meta.pending_embedding_model
                if pending == target_model:
                    meta.pending_embedding_model = None
                    meta.needs_reindex = False
                elif pending is not None:
                    # Different pending set concurrently — leave pending and needs_reindex as-is
                    pass
                else:
                    # pending is None
                    meta.needs_reindex = False
            else:
                # Data-only path — only clear reindex_job_id
                meta.reindex_job_id = None
            await store.update_collection_meta(meta)
        else:
            logger.warning("_reindex_task: collection meta not found at success for job %s", job_id)
    except Exception:  # noqa: BLE001
        logger.exception("_reindex_task: failed to update collection meta for job %s", job_id)

    try:
        job_store.update(job_id, status=JobStatus.DONE)
    except Exception:  # noqa: BLE001
        logger.exception("_reindex_task: failed to mark job done for job %s", job_id)


_ERROR_401 = {401: {"model": ErrorDetail}}
_ERROR_400_401 = {
    400: {"model": ErrorDetail, "description": "Ingest path failed safety validation"},
    401: {"model": ErrorDetail},
}
_ERROR_400_401_503 = {
    400: {"model": ErrorDetail, "description": "Ingest path failed safety validation"},
    401: {"model": ErrorDetail},
    413: {"model": ErrorDetail, "description": "File size exceeds configured max_file_mb limit"},
    503: {"description": "Store busy — reindex in progress"},
}


@router.post("/ingest", status_code=202, response_model=JobResponse, responses=_ERROR_400_401_503)
async def ingest(body: IngestRequest, request: Request) -> JobResponse | JSONResponse:
    store: JobStore = request.app.state.job_store
    pipeline_fn: Callable[..., Awaitable[None]] | None = getattr(
        request.app.state, "ingest_pipeline", None
    )
    # Populate ingested_by from HTTP header (normalized at boundary).
    body.ingested_by = parse_ingested_by_header(request.headers.get("X-Ingested-By"))
    if body.path is not None:
        try:
            body.path = str(validate_ingest_path(body.path))
        except PathUnsafeError as e:
            raise HTTPException(status_code=400, detail=f"path is unsafe: {e.reason}")
    ns = request.state.namespace

    # Synchronous 413 pre-check — only for single-file paths, before job creation.
    # Skipped for directory paths and `documents` payloads (no filesystem path).
    if body.path is not None:
        p = Path(body.path)
        if p.is_file():
            config: SearchConfig = request.app.state.config
            max_file_mb: int = config.ingest.max_file_mb
            try:
                exceeds = _file_exceeds_limit(p, max_file_mb)
            except OSError:
                exceeds = False  # File disappeared between is_file() and _file_exceeds_limit(); let the job handle it.
            if exceeds:
                try:
                    file_size_mb = math.ceil(os.path.getsize(p) / (1024 * 1024))
                except OSError:
                    file_size_mb = max_file_mb + 1  # File size unknown but known to exceed limit.
                err = IngestError(file_size_mb=file_size_mb, limit_mb=max_file_mb)
                raise HTTPException(
                    status_code=413, detail={"code": err.code, "message": err.message}
                )

    try:
        job = store.create(namespace=ns, collection=body.collection, path=body.path or "")
    except OSError:
        return JSONResponse({"detail": "internal error"}, status_code=500)

    # Pre-acquire the per-collection lock to return 503 synchronously on contention.
    search_store = getattr(request.app.state, "search_store", None)
    lock_result = await acquire_collection_lock_or_503(search_store, body.collection)
    if isinstance(lock_result, JSONResponse):
        return lock_result

    pipeline = getattr(request.app.state, "pipeline", None)
    embedder_cache = getattr(request.app.state, "embedder_cache", None)
    config = getattr(request.app.state, "config", None)

    if lock_result is not None:
        task = asyncio.create_task(
            _default_ingest_task_with_lock(
                job.job_id, store, body, namespace=ns, pipeline_fn=pipeline_fn, held_lock=lock_result,
                search_store=search_store, embedder_cache=embedder_cache, pipeline=pipeline, config=config,
            )
        )
    else:
        task = asyncio.create_task(
            _default_ingest_task(
                job.job_id, store, body, namespace=ns, pipeline_fn=pipeline_fn,
                search_store=search_store, embedder_cache=embedder_cache, pipeline=pipeline, config=config,
            )
        )
    request.app.state._background_tasks.add(task)
    task.add_done_callback(request.app.state._background_tasks.discard)
    return JobResponse(**job_to_dict(job))


_KIND_TYPE_MAP: dict[str, type] = {
    "ingest": IngestJob,
    "reindex": ReindexJob,
    "delete": DeleteJob,
    "export": ExportJob,
    "import": ImportJob,
    "migration": MigrationJob,
}


@router.get("/jobs", response_model=JobListResponse, responses={401: {"model": ErrorDetail}})
async def list_jobs(
    request: Request,
    status: list[str] = Query(default=[]),
    kind: list[str] = Query(default=[]),
    source: list[str] = Query(default=[]),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> JobListResponse:
    store: JobStore = request.app.state.job_store
    namespace: str = request.state.namespace

    # Filter by namespace first
    jobs = [j for j in store.list() if j.namespace == namespace]

    # Filter by status (case-insensitive comparison via enum value)
    if status:
        status_upper = {s.upper() for s in status}
        jobs = [j for j in jobs if j.status.value in status_upper]

    # Filter by source. Since D5-BE-7, all IngestJob subclasses (including base
    # IngestJob, ReindexJob, DeleteJob) have source="user" by default, so
    # ?source=user returns all job types. ExportJob/ImportJob/MigrationJob
    # carry source="user" or source="backup" (not "maintenance" — only IngestJob
    # base, ReindexJob, and DeleteJob can carry source="maintenance").
    if source:
        source_set = {s.lower() for s in source}
        jobs = [j for j in jobs if j.source in source_set]

    # Filter by kind using exact type matching (not isinstance, since IngestJob is base class)
    if kind:
        kind_lower = {k.lower() for k in kind}
        kind_types = {_KIND_TYPE_MAP[k] for k in kind_lower if k in _KIND_TYPE_MAP}
        jobs = [j for j in jobs if type(j) in kind_types]

    # Sort by created_at descending (newest first)
    jobs.sort(key=lambda j: j.created_at, reverse=True)

    total = len(jobs)

    # Cursor pagination: find cursor position in sorted list
    if cursor is not None:
        cursor_index = next(
            (i for i, j in enumerate(jobs) if j.job_id == cursor), None
        )
        if cursor_index is not None:
            jobs = jobs[cursor_index + 1:]
        # If cursor not found, return from the start

    page = jobs[:limit]
    next_cursor = page[-1].job_id if len(jobs) > limit else None

    return JobListResponse(
        items=[JobResponse(**job_to_dict(j)) for j in page],
        next_cursor=next_cursor,
        total=total,
    )


@router.get("/jobs/{job_id}", response_model=JobResponse, responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}})
async def get_job(job_id: str, request: Request) -> JobResponse:
    store: JobStore = request.app.state.job_store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.namespace != request.state.namespace:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse(**job_to_dict(job))


@router.post(
    "/jobs/{job_id}/resume",
    status_code=202,
    response_model=JobResponse,
    responses={
        202: {"model": JobResponse},
        401: {"model": ErrorDetail},
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail},
        422: {"model": ErrorDetail},
    },
)
async def resume_job(job_id: str, request: Request) -> JobResponse | JSONResponse:
    """Transition a FAILED export, import, or migration job back to QUEUED so the scheduler can retry it."""
    store: JobStore = request.app.state.job_store
    job = store.get(job_id)

    # 404 if missing or from a different namespace
    if job is None or job.namespace != request.state.namespace:
        return JSONResponse({"error": "not_found"}, status_code=404)

    # Only bulk jobs (ExportJob, ImportJob, MigrationJob) support resume
    if not isinstance(job, (ExportJob, ImportJob, MigrationJob)):
        return JSONResponse(
            {"error": "job_not_resumable", "reason": "only export, import, and migration jobs support resume"},
            status_code=409,
        )

    # Job must be in FAILED state
    if job.status != JobStatus.FAILED:
        return JSONResponse(
            {"error": "job_not_failed", "current_status": job.status.value},
            status_code=409,
        )

    # Validate that the required file(s) still exist
    if isinstance(job, ExportJob):
        # tmp file missing AND there is a checkpoint → can't resume from where we left off
        if job.progress is not None and not Path(job.tmp_path).exists():
            return JSONResponse({"error": "source_not_found"}, status_code=422)
    elif isinstance(job, ImportJob):
        if not Path(job.archive_path).exists():
            return JSONResponse({"error": "source_not_found"}, status_code=422)

    # Atomically transition FAILED → QUEUED
    updated = store.transition(job_id, {JobStatus.FAILED}, JobStatus.QUEUED)
    if updated is None:
        # Race: status changed between our check and transition
        job = store.get(job_id)
        if job is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(
            {"error": "job_not_failed", "current_status": job.status.value},
            status_code=409,
        )

    return JSONResponse(job_to_dict(updated), status_code=202)


@router.delete(
    "/jobs/{job_id}",
    response_model=JobResponse,
    responses={
        200: {"model": JobResponse},
        202: {"model": JobResponse},
        401: {"model": ErrorDetail},
        404: {"model": ErrorDetail},
    },
)
async def delete_job(job_id: str, request: Request, response: Response) -> JobResponse | JSONResponse:
    store: JobStore = request.app.state.job_store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.namespace != request.state.namespace:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in _TERMINAL_STATUSES:
        return JobResponse(**job_to_dict(job))
    if job.status in _ACTIVE_STATUSES:
        # Use transition() to avoid TOCTOU race: only updates if still active
        try:
            updated = store.transition(job.job_id, _ACTIVE_STATUSES, JobStatus.CANCELLING)
        except OSError:
            return JSONResponse({"detail": "internal error"}, status_code=500)
        if updated is None:
            # Race: job became terminal between get() and transition() — idempotent 200
            job = store.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="Job not found")
            return JobResponse(**job_to_dict(job))
        response.status_code = 202
        return JobResponse(**job_to_dict(updated))
    elif job.status == JobStatus.CANCELLING:
        response.status_code = 202
        return JobResponse(**job_to_dict(job))
    else:
        logger.error("DELETE /jobs/%s: unknown status %s", job_id, job.status)
        return JSONResponse(
            content={"detail": f"Unknown job status: {job.status}"},
            status_code=500,
        )

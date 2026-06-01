"""POST /ingest, GET /jobs/{job_id}, DELETE /jobs/{job_id} endpoints."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Awaitable

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from archon_search._path_safety import PathUnsafeError, validate_ingest_path
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.embedder_cache import EmbedderCache
from archon_search.jobs.model import IngestJob, JobStatus, job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.server._ingest_lock import acquire_collection_lock_or_503
from archon_search.server._ingested_by import parse_ingested_by_header
from archon_search.server.schemas import ErrorDetail, JobResponse
from archon_search.types import ReindexJob

logger = logging.getLogger(__name__)

router = APIRouter()

# Terminal statuses — DELETE is idempotent for these
_TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}
# Active statuses — DELETE sets CANCELLING
_ACTIVE_STATUSES = {JobStatus.RUNNING, JobStatus.PENDING}


class IngestRequest(BaseModel):
    collection: str
    path: str | None = None
    documents: list[dict[str, Any]] | None = None
    ingested_by: str = "http"

    @field_validator("collection")
    @classmethod
    def collection_must_be_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("collection must be non-empty")
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
) -> None:
    """Resolve per-collection embedder and dispatch to pipeline.ingest_file / ingest_directory."""
    meta = await search_store.get_collection_meta(body.collection, namespace)
    active_model = (meta.active_embedding_model if meta else "") or config.embedding_model
    embedder = await embedder_cache.get_or_load(active_model)

    if body.path is not None:
        p = Path(body.path)
        if p.is_file():
            await pipeline.ingest_file(
                p, body.collection, embedder=embedder, namespace=namespace, ingested_by=body.ingested_by
            )
        elif p.is_dir():
            await pipeline.ingest_directory(
                p, body.collection, embedder=embedder, namespace=namespace, ingested_by=body.ingested_by
            )
        else:
            raise FileNotFoundError(f"path does not exist or is not a file/directory: {body.path}")
    elif body.documents is not None:
        if hasattr(pipeline, "ingest_documents"):
            await pipeline.ingest_documents(
                body.documents, body.collection, embedder=embedder, namespace=namespace, ingested_by=body.ingested_by
            )
        else:
            logger.warning("pipeline has no ingest_documents method; skipping documents ingest for collection %s", body.collection)


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
        if pipeline_fn is None and search_store is not None and embedder_cache is not None and pipeline is not None and config is not None:
            await _dispatch_ingest(body, namespace, search_store, embedder_cache, pipeline, config)
        else:
            await _run_pipeline(job_id, store, body, pipeline_fn, namespace=namespace)
        # Check for cancellation before marking DONE
        job = store.get(job_id)
        if job and job.status == JobStatus.CANCELLING:
            store.update(job_id, status=JobStatus.CANCELLED)
            return
        store.update(job_id, status=JobStatus.DONE)
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
        if pipeline_fn is None and search_store is not None and embedder_cache is not None and pipeline is not None and config is not None:
            # Release the pre-acquired lock before dispatch: pipeline.ingest_file /
            # ingest_directory will acquire it internally. Holding it here would
            # deadlock since asyncio.Lock is not reentrant.
            if held_lock is not None and held_lock.locked():
                held_lock.release()
            await _dispatch_ingest(body, namespace, search_store, embedder_cache, pipeline, config)
        else:
            await _run_pipeline(
                job_id, store, body, pipeline_fn, namespace=namespace, locked_by_caller=True
            )
        job = store.get(job_id)
        if job and job.status == JobStatus.CANCELLING:
            store.update(job_id, status=JobStatus.CANCELLED)
            return
        store.update(job_id, status=JobStatus.DONE)
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
    collection_path: Path,
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
    try:
        await pipeline.ingest_directory(
            collection_path,
            collection,
            embedder=embedder,
            namespace=namespace,
            ingested_by="reindex",
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
            if target_model is not None:
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
    try:
        job = store.create(namespace=ns)
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


@router.get("/jobs/{job_id}", response_model=JobResponse, responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}})
async def get_job(job_id: str, request: Request) -> JobResponse:
    store: JobStore = request.app.state.job_store
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.namespace != request.state.namespace:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse(**job_to_dict(job))


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

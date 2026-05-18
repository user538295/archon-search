"""POST /ingest, GET /jobs/{job_id}, DELETE /jobs/{job_id} endpoints."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.model import IngestJob, JobStatus, job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.server.schemas import ErrorDetail, JobResponse

logger = logging.getLogger("archon-search")

router = APIRouter()

# Terminal statuses — DELETE is idempotent for these
_TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}
# Active statuses — DELETE sets CANCELLING
_ACTIVE_STATUSES = {JobStatus.RUNNING, JobStatus.PENDING}


class IngestRequest(BaseModel):
    collection: str
    path: str | None = None
    documents: list[dict[str, Any]] | None = None
    ingested_by: str = "archon-search-cli"

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
) -> None:
    """Run the ingest pipeline (real or stub). Raises on failure."""
    if pipeline_fn is not None:
        await pipeline_fn(job_id, store, body, namespace=namespace)
    else:
        # Stub: succeed immediately
        await asyncio.sleep(0)


async def _default_ingest_task(
    job_id: str,
    store: JobStore,
    body: IngestRequest,
    namespace: str = DEFAULT_NAMESPACE,
    pipeline_fn: Callable[..., Awaitable[None]] | None = None,
) -> None:
    """Lifecycle wrapper: PENDING → RUNNING → DONE/FAILED/CANCELLED."""
    try:
        store.update(job_id, status=JobStatus.RUNNING)
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
        except KeyError:
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ingest task %s failed", job_id)
        try:
            store.update(job_id, status=JobStatus.FAILED, error=str(exc))
        except KeyError:
            pass


_ERROR_401 = {401: {"model": ErrorDetail}}


@router.post("/ingest", status_code=202, response_model=JobResponse, responses=_ERROR_401)
async def ingest(body: IngestRequest, request: Request) -> JobResponse:
    store: JobStore = request.app.state.job_store
    pipeline_fn: Callable[..., Awaitable[None]] | None = getattr(
        request.app.state, "ingest_pipeline", None
    )
    # Populate ingested_by from HTTP header if present
    ingested_by = request.headers.get("X-Ingested-By", "archon-search-cli")
    body.ingested_by = ingested_by
    ns = request.state.namespace
    job = store.create(namespace=ns)
    task = asyncio.create_task(_default_ingest_task(job.job_id, store, body, namespace=ns, pipeline_fn=pipeline_fn))
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
        updated = store.transition(job.job_id, _ACTIVE_STATUSES, JobStatus.CANCELLING)
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

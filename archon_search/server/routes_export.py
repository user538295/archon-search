"""POST /collections/{name}/export endpoint and _export_task() worker.

Task 4.1: export worker + prerequisite list_chunks_raw() in SearchStore.
Task 4.2: POST /collections/{name}/export REST endpoint.
"""
from __future__ import annotations

import base64
import importlib.metadata
import logging
import struct
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from archon_search._path_safety import PathUnsafeError, validate_export_path
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.export_archive import EXPORT_SCHEMA_VERSION, ExportArchiveWriter
from archon_search.jobs.model import job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.paths import get_data_dir
from archon_search.server.schemas import ErrorDetail, JobResponse
from archon_search.store import SearchStore
from archon_search.types import ExportJob, JobStatus

logger = logging.getLogger(__name__)

router = APIRouter()

_ERROR_400_401_404 = {
    400: {"model": ErrorDetail, "description": "Path unsafe or invalid request"},
    401: {"model": ErrorDetail},
    404: {"model": ErrorDetail},
}


def _encode_vector(floats: list[float]) -> str:
    """Encode a list of floats as base64 little-endian float32 bytes."""
    packed = struct.pack(f"<{len(floats)}f", *floats)
    return base64.standard_b64encode(packed).decode("ascii")


async def _export_task(
    job: ExportJob,
    store: JobStore,
    search_store: SearchStore,
    config: SearchConfig,
) -> None:
    """Export a collection to a .tar.gz archive.

    Worker lifecycle:
      1. reading phase: count total chunks
      2. writing phase: stream all chunks to temp JSONL; checkpoint progress periodically
      3. packaging phase: finalize tar.gz archive
      4. mark DONE on success; FAILED on any exception; CANCELLED if CANCELLING detected
    """
    job_id = job.job_id
    try:
        archive_path = Path(job.output_path)
        tmp_path = Path(job.tmp_path)
        # Ensure parent directory exists
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        # --- Phase: reading — count total docs ---
        store.update_progress(job_id, 0, 0, "reading")
        total = await search_store.count_chunks(job.collection, job.namespace)

        # --- Phase: writing — stream chunks to temp JSONL ---
        store.update_progress(job_id, 0, total, "writing")
        writer = ExportArchiveWriter(tmp_path)
        with writer:
            async for raw in search_store.list_chunks_raw(job.collection, job.namespace):
                # Encode vector as base64 little-endian float32
                vector_floats = raw.get("vector") or []
                raw_doc = dict(raw)
                raw_doc["vector"] = _encode_vector(vector_floats)
                writer.write_doc(raw_doc)

                # Checkpoint progress every checkpoint_interval docs
                if writer.lines_written % config.jobs.checkpoint_interval == 0:
                    store.update_progress(job_id, writer.lines_written, total, "writing")

                # Cancellation check
                current = store.get(job_id)
                if current is not None and current.status == JobStatus.CANCELLING:
                    writer.cleanup()
                    store.update(job_id, status=JobStatus.CANCELLED)
                    return

            # --- Phase: packaging — build the tar.gz ---
            store.update_progress(job_id, writer.lines_written, total, "packaging")

            # Build manifest
            try:
                archon_version = importlib.metadata.version("archon-search")
            except importlib.metadata.PackageNotFoundError:
                archon_version = "dev"

            meta = await search_store.get_collection_meta(job.collection, job.namespace)
            active_model = meta.active_embedding_model if meta else ""
            description = meta.description if meta else ""

            manifest = {
                "archon_search_version": archon_version,
                "schema_version": EXPORT_SCHEMA_VERSION,
                "collection": job.collection,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "doc_count": writer.lines_written,
                "active_embedding_model": active_model,
                "description": description,
            }
            writer.finalize(manifest, archive_path)

        store.update(job_id, status=JobStatus.DONE, result={"archive_path": str(archive_path)})

    except Exception as exc:  # noqa: BLE001
        logger.exception("_export_task: job %s failed", job_id)
        try:
            store.update(job_id, status=JobStatus.FAILED, error=str(exc))
        except (KeyError, OSError):
            logger.error("_export_task: could not persist FAILED status for job %s", job_id)


# ---------------------------------------------------------------------------
# POST /collections/{name}/export
# ---------------------------------------------------------------------------


class ExportRequest(BaseModel):
    output_path: str = ""


@router.post(
    "/{name}/export",
    status_code=202,
    response_model=JobResponse,
    responses=_ERROR_400_401_404,
)
async def export_collection(
    name: str,
    body: ExportRequest,
    request: Request,
) -> JobResponse | JSONResponse:
    """Enqueue an export job for the named collection.

    Returns 202 with a JobResponse (status=QUEUED). The job is dispatched by
    the scheduler when a slot is available.
    """
    store: JobStore = request.app.state.job_store
    search_store: SearchStore = request.app.state.search_store
    ns: str = request.state.namespace

    # Resolve output path — default to get_data_dir() / "exports"
    exports_dir = get_data_dir() / "exports"
    raw_output = body.output_path if body.output_path else str(exports_dir)

    # Validate the output directory path
    try:
        resolved_dir = validate_export_path(raw_output, [get_data_dir()])
    except PathUnsafeError as exc:
        return JSONResponse({"error": "path_unsafe", "reason": exc.reason}, status_code=400)

    # Verify collection exists
    meta = await search_store.get_collection_meta(name, ns)
    if meta is None:
        return JSONResponse({"error": "not_found", "detail": f"Collection {name!r} not found"}, status_code=404)

    # Compute paths for the job
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    job_uuid = str(uuid4())
    archive_path = resolved_dir / f"{name}-{timestamp}.tar.gz"
    tmp_path = resolved_dir / f".export-{job_uuid}.jsonl.tmp"

    job = store.create_export(
        collection=name,
        output_path=str(archive_path),
        tmp_path=str(tmp_path),
        namespace=ns,
    )

    return JSONResponse(job_to_dict(job), status_code=202)

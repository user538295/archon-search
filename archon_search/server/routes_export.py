"""POST /collections/{name}/export and POST /collections/{name}/import endpoints.

Task 4.1: export worker + prerequisite list_chunks_raw() in SearchStore.
Task 4.2: POST /collections/{name}/export REST endpoint.
Task 5.1: _import_task() worker.
Task 5.2: POST /collections/{name}/import REST endpoint.
"""
from __future__ import annotations

import base64
import importlib.metadata
import json
import logging
import struct
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from archon_search._path_safety import PathUnsafeError, validate_archive_members, validate_export_path
from archon_search._types import ChunkRecord
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.embedder_cache import EMBEDDER_NOT_READY_DETAIL, EmbedderCache, EmbedderNotReadyError
from archon_search.jobs.export_archive import (
    EXPORT_SCHEMA_VERSION,
    ExportArchiveWriter,
    ImportArchiveReader,
    get_lancedb_version,
)
from archon_search.jobs.model import job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.paths import get_data_dir
from archon_search.pipeline import SearchPipeline
from archon_search.server.schemas import ErrorDetail, JobResponse
from archon_search.store import SearchStore
from archon_search.types import ExportJob, ImportJob, JobStatus

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
                # D2-1.4: record the LanceDB version this archive was produced
                # against. Null on PackageNotFoundError.
                "lancedb_version": get_lancedb_version(),
            }
            writer.finalize(manifest, archive_path)

        store.update(job_id, status=JobStatus.DONE, result={"archive_path": str(archive_path)})

    except Exception as exc:  # noqa: BLE001
        logger.exception("_export_task: job %s failed", job_id)
        try:
            store.update(job_id, status=JobStatus.FAILED, error=str(exc))
        except (KeyError, OSError):
            logger.error("_export_task: could not persist FAILED status for job %s", job_id)


async def _import_task(
    job: ImportJob,
    store: JobStore,
    search_store: SearchStore,
    pipeline: SearchPipeline,
    embedder_cache: EmbedderCache,
    config: SearchConfig,
) -> None:
    """Import a collection from a .tar.gz archive.

    Worker lifecycle:
      1. validating phase: read manifest, check schema_version and embedding model
      2. collection exists check: fail or drop+recreate depending on force_overwrite
      3. ingesting phase: stream docs from archive into the store; checkpoint progress
      4. indexing phase: rebuild FTS index and recompute collection meta
      5. mark DONE on success; FAILED on any exception; CANCELLED if CANCELLING detected
    """
    job_id = job.job_id
    try:
        # --- Phase: validating ---
        # Read checkpoint BEFORE overwriting progress — needed to detect resume path.
        initial_job = store.get(job_id)
        initial_progress = initial_job.progress if initial_job is not None else None
        is_resume = (
            initial_progress is not None
            and initial_progress.get("processed", 0) > 0
        )

        store.update_progress(job_id, 0, 0, "validating")
        reader = ImportArchiveReader(Path(job.archive_path))
        manifest = reader.read_manifest()

        # Schema version check (bypassable)
        if not job.ignore_schema_version and manifest["schema_version"] != EXPORT_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version mismatch: archive has {manifest['schema_version']!r}, "
                f"server expects {EXPORT_SCHEMA_VERSION!r}; use ignore_schema_version=true to bypass"
            )

        # Embedding model check (NOT bypassable)
        archive_model = manifest["active_embedding_model"]
        server_model = config.embedding_model
        if archive_model != server_model:
            raise ValueError(
                f"embedding model mismatch: archive has {archive_model!r}, "
                f"server is configured with {server_model!r}; re-export with a matching model"
            )

        store.update_progress(job_id, 0, manifest["doc_count"], "validating")

        # --- Collection exists guard ---
        # Check both the meta table and the underlying LanceDB table for existence.
        existing_meta = await search_store.get_collection_meta(job.collection, job.namespace)
        existing_chunk_count = await search_store.count_chunks(job.collection, job.namespace)
        collection_exists = existing_meta is not None or existing_chunk_count > 0

        # On resume (progress checkpoint from previous run), skip the exists guard —
        # the collection was already partially created in the first run.
        if collection_exists and not is_resume:
            if not job.force_overwrite:
                raise ValueError(
                    f"collection {job.collection!r} already exists; use force_overwrite=true to overwrite"
                )
            # Drop and recreate
            try:
                await search_store.drop_collection(job.collection)
            except (KeyError, RuntimeError):
                pass  # table may not exist in LanceDB; that's fine
            if existing_meta is not None:
                await search_store.delete_collection_meta(job.collection, job.namespace)

        # --- Phase: ingesting ---
        # Use initial_progress for checkpoint skip — the store progress was overwritten
        # by the "validating" update_progress call above.
        skip = initial_progress["processed"] if initial_progress else 0

        processed = skip  # start from checkpoint
        skipped = 0
        batch: list[ChunkRecord] = []

        # Determine embedding dimension from the first valid document in the
        # archive (needed to create the LanceDB table before ingesting).
        # When on_error="skip", corrupt JSON lines are skipped internally by
        # iter_docs so a corrupt first line does not abort the job.
        embedding_dim: int | None = None
        if manifest["doc_count"] > 0:
            for first_doc in reader.iter_docs(skip=0, on_error=job.on_error):
                try:
                    b64_first = first_doc["vector"]
                    raw_first = base64.standard_b64decode(b64_first)
                    embedding_dim = len(raw_first) // 4
                except Exception:  # noqa: BLE001
                    continue  # try next doc for dimension
                break  # found a valid dimension

        # Ensure the LanceDB table exists before we ingest into it.
        # For non-empty archives: dimension is derived from the first vector.
        # For empty archives: nothing to ingest; skip ensure_collection.
        if embedding_dim is not None:
            await search_store.ensure_collection(job.collection, embedding_dim)

        doc_iter = reader.iter_docs(skip=skip, on_error=job.on_error)
        for doc in doc_iter:

            # Decode vector from base64 little-endian float32
            try:
                b64_str = doc["vector"]
                raw = base64.standard_b64decode(b64_str)
                floats = list(struct.unpack(f"<{len(raw) // 4}f", raw))
            except Exception as exc:  # noqa: BLE001
                if job.on_error == "skip":
                    skipped += 1
                    logger.warning("_import_task: skipping corrupt doc (vector decode): %s", exc)
                    continue
                raise ValueError(f"corrupt doc (vector): {exc}") from exc

            try:
                raw_meta = doc.get("metadata", {})
                # metadata may be JSON-encoded string (from list_chunks_raw) or already a dict
                if isinstance(raw_meta, str):
                    raw_meta = json.loads(raw_meta) if raw_meta else {}
                chunk = ChunkRecord(
                    doc_id=doc["doc_id"],
                    chunk_id=doc["chunk_id"],
                    text=doc["text"],
                    vector=floats,
                    source_path=doc["source_path"],
                    indexed_at=doc["indexed_at"],
                    file_type=doc.get("file_type", ""),
                    language=doc.get("language", ""),
                    metadata=raw_meta,
                    acl=doc.get("acl"),
                    custom_score=doc.get("custom_score"),
                    ingested_by=doc.get("ingested_by", "import"),  # type: ignore[arg-type]
                    updated_at=doc.get("updated_at", ""),
                    expires_at=doc.get("expires_at"),
                    scopes=doc.get("scopes"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                if job.on_error == "skip":
                    skipped += 1
                    logger.warning("_import_task: skipping malformed doc: %s", exc)
                    continue
                raise ValueError(f"malformed doc: {exc}") from exc

            batch.append(chunk)

            if len(batch) >= config.jobs.checkpoint_interval:
                await search_store.ingest_chunks(
                    job.collection, batch, namespace=job.namespace
                )
                processed += len(batch)
                batch = []
                store.update_progress(job_id, processed, manifest["doc_count"], "ingesting")

                # Cancellation check
                current = store.get(job_id)
                if current is not None and current.status == JobStatus.CANCELLING:
                    store.update(job_id, status=JobStatus.CANCELLED)
                    return

        # Flush remaining batch
        if batch:
            await search_store.ingest_chunks(
                job.collection, batch, namespace=job.namespace
            )
            processed += len(batch)

        # --- Phase: indexing ---
        store.update_progress(job_id, processed, manifest["doc_count"], "indexing")

        if embedding_dim is not None:
            # Non-empty archive: rebuild FTS index and recompute centroid.
            detected_language = manifest.get("language", "")
            await search_store.rebuild_fts_index(job.collection, language=detected_language)

            global_embedder = await embedder_cache.get_or_load(manifest["active_embedding_model"])
            await pipeline.recompute_collection_meta(
                job.collection, global_embedder, namespace=job.namespace, force=True
            )
        else:
            # Empty archive: table was never created; nothing to index or recompute.
            logger.info("_import_task: archive for %r is empty; skipping FTS rebuild and centroid recompute", job.collection)

        # Total skipped = JSON-parse errors (handled by iter_docs) + vector/doc
        # errors (handled locally in the while loop above).
        total_skipped = skipped + reader.skipped_lines

        store.update(
            job_id,
            status=JobStatus.DONE,
            result={
                "imported": processed,
                "skipped": total_skipped,
                "total_in_archive": manifest["doc_count"],
            },
        )

    except EmbedderNotReadyError as exc:
        # Retryable, not a genuine failure: ImportJob supports manual resume via
        # POST /jobs/{id}/resume while status==FAILED (routes_jobs.py), so leaving
        # this job FAILED with a clean, non-leaking message is sufficient.
        logger.warning("_import_task: embedder not ready for job %s — %s", job_id, exc)
        try:
            store.update(job_id, status=JobStatus.FAILED, error=EMBEDDER_NOT_READY_DETAIL)
        except (KeyError, OSError):
            logger.error("_import_task: could not persist FAILED status for job %s", job_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("_import_task: job %s failed", job_id)
        try:
            store.update(job_id, status=JobStatus.FAILED, error=str(exc))
        except (KeyError, OSError):
            logger.error("_import_task: could not persist FAILED status for job %s", job_id)


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


# ---------------------------------------------------------------------------
# POST /collections/{name}/import
# ---------------------------------------------------------------------------


class ImportRequest(BaseModel):
    path: str
    force_overwrite: bool = False
    ignore_schema_version: bool = False
    on_error: str = "fail"

    @field_validator("on_error")
    @classmethod
    def _validate_on_error(cls, v: str) -> str:
        if v not in {"fail", "skip"}:
            raise ValueError("on_error must be 'fail' or 'skip'")
        return v


@router.post(
    "/{name}/import",
    status_code=202,
    response_model=JobResponse,
    responses={
        400: {"model": ErrorDetail, "description": "Path unsafe or invalid request"},
        401: {"model": ErrorDetail},
        404: {"model": ErrorDetail},
        409: {"model": ErrorDetail, "description": "Collection already exists"},
        422: {"model": ErrorDetail, "description": "Archive invalid or schema/model mismatch"},
    },
)
async def import_collection(
    name: str,
    body: ImportRequest,
    request: Request,
) -> JobResponse | JSONResponse:
    """Enqueue an import job for the named collection.

    Returns 202 with a JobResponse (status=QUEUED). The job is dispatched by
    the scheduler when a slot is available.
    """
    store: JobStore = request.app.state.job_store
    search_store: SearchStore = request.app.state.search_store
    ns: str = request.state.namespace
    config: SearchConfig = request.app.state.config

    # Step 1: Validate the archive path is within the allowed data directory
    try:
        validate_export_path(body.path, [get_data_dir()])
    except PathUnsafeError as exc:
        return JSONResponse({"error": "path_unsafe", "reason": exc.reason}, status_code=400)

    # Step 2: Verify archive file exists
    archive_path = Path(body.path)
    if not archive_path.exists():
        return JSONResponse(
            {"error": "archive_not_found", "detail": f"Archive not found: {body.path}"},
            status_code=422,
        )

    # Step 3: Pre-validate archive members (zip-slip guard)
    try:
        with tarfile.open(archive_path, "r:gz") as tf:
            validate_archive_members(tf)
    except PathUnsafeError as exc:
        return JSONResponse(
            {"error": "unsafe_archive", "reason": exc.reason},
            status_code=422,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"error": "invalid_archive", "detail": str(exc)},
            status_code=422,
        )

    # Step 4: Read manifest to early-detect embedding model mismatch
    try:
        reader = ImportArchiveReader(archive_path)
        manifest = reader.read_manifest()
    except (ValueError, PathUnsafeError) as exc:
        return JSONResponse(
            {"error": "invalid_manifest", "detail": str(exc)},
            status_code=422,
        )

    archive_model = manifest.get("active_embedding_model", "")
    server_model = config.embedding_model
    if archive_model != server_model:
        return JSONResponse(
            {
                "error": "embedding_model_mismatch",
                "detail": (
                    f"archive uses model {archive_model!r}; "
                    f"server is configured with {server_model!r}"
                ),
            },
            status_code=422,
        )

    # Step 5: Check collection existence vs force_overwrite
    existing_meta = await search_store.get_collection_meta(name, ns)
    if existing_meta is not None and not body.force_overwrite:
        return JSONResponse(
            {"error": "collection_exists", "detail": f"Collection {name!r} already exists; use force_overwrite=true to overwrite"},
            status_code=409,
        )

    # Step 6: Check schema_version mismatch (bypassable)
    archive_schema = manifest.get("schema_version")
    if not body.ignore_schema_version and archive_schema != EXPORT_SCHEMA_VERSION:
        return JSONResponse(
            {
                "error": "schema_version_mismatch",
                "detail": (
                    f"archive has schema_version={archive_schema!r}; "
                    f"server expects {EXPORT_SCHEMA_VERSION!r}; "
                    "use ignore_schema_version=true to bypass"
                ),
            },
            status_code=422,
        )

    # Step 7: Create the import job (status=QUEUED)
    job = store.create_import(
        collection=name,
        archive_path=body.path,
        force_overwrite=body.force_overwrite,
        ignore_schema_version=body.ignore_schema_version,
        on_error=body.on_error,
        namespace=ns,
    )

    # Step 8: Return 202 with the job response
    return JSONResponse(job_to_dict(job), status_code=202)

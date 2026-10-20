"""POST /collections/{name}/export and POST /collections/{name}/import endpoints.

Task 4.1: export worker + prerequisite list_chunks_raw() in SearchStore.
Task 4.2: POST /collections/{name}/export REST endpoint.
Task 5.1: _import_task() worker.
Task 5.2: POST /collections/{name}/import REST endpoint.
"""
from __future__ import annotations

import base64
import contextlib
import importlib.metadata
import json
import logging
import math
import struct
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from archon_search._path_safety import PathUnsafeError, validate_archive_members, validate_export_path
from archon_search._types import ChunkRecord
from archon_search.config import SearchConfig
from archon_search.embedder_cache import EMBEDDER_NOT_READY_DETAIL, EmbedderCache, EmbedderNotReadyError
from archon_search.jobs.export_archive import (
    EXPORT_SCHEMA_VERSION,
    INVALID_ARCHIVE_DETAIL,
    ArchiveRejectedError,
    ExportArchiveWriter,
    ImportArchiveReader,
    get_lancedb_version,
    manifest_error_detail,
    quote_archive_value,
)
from archon_search.jobs.model import job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.paths import get_data_dir
from archon_search.pipeline import SearchPipeline
from archon_search.server._job_warmup import await_models_warm
from archon_search.server.schemas import ErrorDetail, JobResponse
from archon_search.store import SearchStore, normalize_ingested_by, validate_chunk_record
from archon_search.types import ExportJob, ImportJob, IngestJob, JobStatus

logger = logging.getLogger(__name__)

# Sanitized job ``error`` for an export/import that failed unexpectedly (store,
# filesystem or library errors, whose text can embed paths and internals). The
# original exception is logged server-side.
EXPORT_FAILED_DETAIL = "export failed; see the server log for details"
IMPORT_FAILED_DETAIL = "import failed; see the server log for details"

router = APIRouter()

# Byte width of one archived vector component (little-endian float32).
_FLOAT32_BYTES = struct.calcsize("<f")

_ERROR_400_401_404: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorDetail, "description": "Path unsafe or invalid request"},
    401: {"model": ErrorDetail},
    404: {"model": ErrorDetail},
}


def _encode_vector(floats: list[float]) -> str:
    """Encode a list of floats as base64 little-endian float32 bytes."""
    packed = struct.pack(f"<{len(floats)}f", *floats)
    return base64.standard_b64encode(packed).decode("ascii")


def _utf8_encodable(value: str) -> str:
    """Return *value*; raise ``UnicodeEncodeError`` (a ``ValueError``) if it holds a lone surrogate.

    JSON can spell one (``"\\ud800"``), but pyarrow fails a whole batch holding one in any string column or list item.
    """
    value.encode("utf-8")
    return value


def _archived_required_str(doc: dict[str, Any], field: str) -> str | None:
    """Return the archive doc's required *field*, checked as ``_archived_optional_str`` checks it (a null included).

    Raises:
        KeyError: the field is absent.
    """
    if field not in doc:
        raise KeyError(field)
    return _archived_optional_str(doc, field)


def _archived_optional_str(doc: dict[str, Any], field: str) -> str | None:
    """Return the archive doc's *field* if it is a UTF-8-encodable string or null (or absent).

    These checks cover the column types ``store.validate_chunk_record`` leaves to its callers: pyarrow stringifies a
    non-str value (a number, a bool, a list) in a string column only when every row of the batch holds one there, and
    fails a batch mixing it with str values or holding a dict. An archive's other docs hold strings, so such a doc
    would fail its whole batch inside ``ingest_chunks``, where ``on_error="skip"`` can no longer skip the one doc.
    A null is stored as ``ingest_chunks`` stores it: ``""`` for ``file_type`` / ``language`` (substituted by
    ``_decode_chunk``), the row's ``indexed_at`` for ``updated_at``, null for the other fields.

    Raises:
        TypeError: the value is neither a string nor null.
        UnicodeEncodeError: a string holding a lone surrogate (see ``_utf8_encodable``).
    """
    value = _archived_str_allowing_surrogates(doc, field)
    return value if value is None else _utf8_encodable(value)


def _archived_str_allowing_surrogates(doc: dict[str, Any], field: str) -> str | None:
    """Return the archive doc's *field* if it is a string or null (or absent); raise ``TypeError`` otherwise.

    Unlike ``_archived_optional_str`` it passes a lone surrogate through: for ``ingested_by``, which
    ``normalize_ingested_by`` maps to ``"cli"`` (as it does a null or any other unknown value) before pyarrow sees it.
    """
    value = doc.get(field)
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"{field} must be a string or null")


def _archived_optional_str_list(doc: dict[str, Any], field: str) -> list[str] | None:
    """Return the archive doc's *field* if it is a list of UTF-8-encodable strings or null.

    Checked per doc because pyarrow would otherwise accept a bare string for a
    ``list<utf8>`` column (splitting it into characters) or fail later, inside
    ``ingest_chunks``, where ``on_error="skip"`` can no longer skip the one doc.

    Raises:
        TypeError: the value is neither a list of strings nor null.
        UnicodeEncodeError: an item holding a lone surrogate (see ``_utf8_encodable``).
    """
    value = doc.get(field)
    if value is None:
        return value
    if not (isinstance(value, list) and all(isinstance(item, str) for item in value)):
        raise TypeError(f"{field} must be a list of strings or null")
    return [_utf8_encodable(item) for item in value]


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

    except Exception:  # noqa: BLE001
        logger.exception("_export_task: job %s failed", job_id)
        try:
            store.update(job_id, status=JobStatus.FAILED, error=EXPORT_FAILED_DETAIL)
        except (KeyError, OSError):
            logger.error("_export_task: could not persist FAILED status for job %s", job_id)


@dataclass(frozen=True)
class _ResumePoint:
    """Where an import run starts: from scratch, or continuing a previous run of the same job."""

    is_resume: bool
    processed: int  # docs previous runs stored
    total: int  # the checkpoint's progress total, reported until the manifest is read
    skip: int  # archive lines previous runs consumed (stored or skipped): the iter_docs skip position


_FRESH_START = _ResumePoint(is_resume=False, processed=0, total=0, skip=0)


def _resume_point(job: IngestJob | None) -> _ResumePoint:
    """Read the resume point a previous run of an import left on its stored *job*.

    A run resumes when a previous one claimed the collection (it may have committed rows before
    its first checkpoint) or checkpointed docs (``processed > 0``: also a FAILED job persisted before
    the claim existed). It continues after the last archive line consumed, not after ``processed``
    lines: lines skipped before the checkpoint would otherwise be re-read as docs and appended a
    second time. ``max()`` covers a checkpoint written without ``lines_consumed``.
    A resume continues into the collection only while ``_owns_collection`` says it is still the one
    that run wrote — never once another import's claim revoked the job's (``collection_claim_revoked``).
    """
    if not isinstance(job, ImportJob):
        return _FRESH_START
    progress = job.progress or {}
    processed = progress.get("processed", 0)
    if not (job.collection_claimed or processed > 0):
        return _FRESH_START
    return _ResumePoint(
        is_resume=True,
        processed=processed,
        total=progress.get("total", 0),
        skip=max(job.lines_consumed, processed),
    )


@dataclass
class _ImportCounters:
    """One import run's counts, on top of the resume point it started from.

    Every archive line read is stored (``stored``) or skipped — before the resume point, by the importer
    (``skipped``), or by the reader (its ``skipped_lines``) — so the resume position and the result's totals
    follow from these counts.
    """

    resume: _ResumePoint
    stored: int = 0  # docs this run stored (or found already stored by a failed run)
    skipped: int = 0  # docs this run's decoding skipped (on_error="skip")

    @property
    def processed(self) -> int:
        """Docs stored by this run and the runs before it."""
        return self.resume.processed + self.stored

    def lines_consumed(self, reader_skipped_lines: int) -> int:
        """Archive lines read so far, stored or skipped: the iter_docs skip position a resume continues from."""
        return self.resume.skip + self.stored + self.skipped + reader_skipped_lines

    def total_skipped(self, reader_skipped_lines: int) -> int:
        """Archive lines skipped across all runs: every consumed line that was not stored."""
        return self.lines_consumed(reader_skipped_lines) - self.processed


def _validate_manifest(manifest: dict[str, Any], job: ImportJob, config: SearchConfig) -> None:
    """Reject an archive whose schema version (unless bypassed) or embedding model the server cannot import."""
    if not job.ignore_schema_version and manifest["schema_version"] != EXPORT_SCHEMA_VERSION:
        raise ArchiveRejectedError(
            f"schema_version mismatch: archive has {quote_archive_value(manifest['schema_version'])}, "
            f"server expects {EXPORT_SCHEMA_VERSION!r}; use ignore_schema_version=true to bypass"
        )
    # Not bypassable: the archived vectors are only valid for the model that produced them.
    archive_model = manifest["active_embedding_model"]
    server_model = config.embedding_model
    if archive_model != server_model:
        raise ArchiveRejectedError(
            f"embedding model mismatch: archive has {quote_archive_value(archive_model)}, "
            f"server is configured with {server_model!r}; re-export with a matching model"
        )


class _VectorDimensionError(ArchiveRejectedError):
    """An archived vector whose length differs from the archive's first valid doc's.

    It fails the import even under ``on_error="skip"``: the first valid doc sets the dimension, so were that doc the
    corrupt one, skipping would pass over every other doc and still end DONE.
    """


def _decode_vector(doc: dict[str, Any], line_number: int, embedding_dim: int | None) -> list[float]:
    """Decode the archived *doc*'s base64 little-endian float32 vector.

    Raises:
        ArchiveRejectedError: naming *line_number* when the vector is missing, not base64-encoded float32, or
            empty (a decode error is chained as its cause), or when a component is NaN, which LanceDB refuses for the
            whole batch (infinities it stores).
        _VectorDimensionError: naming *line_number* when, given *embedding_dim*, the vector has another length —
            checked before NaN components, so such a vector fails the job even when it also holds a NaN.
    """
    corrupt_vector = f"corrupt doc (vector) at line {line_number}: missing or not base64-encoded float32"
    try:
        raw = base64.standard_b64decode(doc["vector"])
        floats = list(struct.unpack(f"<{len(raw) // _FLOAT32_BYTES}f", raw))
    # KeyError: no vector; TypeError: not a string; ValueError: not base64 (binascii.Error) or not ASCII;
    # struct.error: a byte length that is not a multiple of a float32's.
    except (KeyError, TypeError, ValueError, struct.error) as exc:
        raise ArchiveRejectedError(corrupt_vector) from exc
    if not floats:
        raise ArchiveRejectedError(corrupt_vector)
    if embedding_dim is not None and len(floats) != embedding_dim:
        raise _VectorDimensionError(
            f"corrupt doc (vector) at line {line_number}: its dimension differs from the archive's first valid document"
        )
    if any(math.isnan(component) for component in floats):
        raise ArchiveRejectedError(f"corrupt doc (vector) at line {line_number}: a component is NaN")
    return floats


def _decode_chunk(doc: dict[str, Any], line_number: int, *, embedding_dim: int | None = None) -> ChunkRecord:
    """Decode one archived *doc* into a ``ChunkRecord`` that ``SearchStore.ingest_chunks`` accepts.

    Raises:
        ArchiveRejectedError: naming *line_number* when the vector does not decode (see ``_decode_vector``), or
            when a required field is missing or wrongly typed — every value ``validate_chunk_record`` refuses,
            and every non-str or not UTF-8-encodable value in a string column (see ``_archived_optional_str``)
            included; a decode error is chained as its cause.
        _VectorDimensionError: when, given *embedding_dim*, the vector has another length.
    """
    floats = _decode_vector(doc, line_number, embedding_dim)
    try:
        raw_meta = doc.get("metadata", {})
        # metadata may be JSON-encoded string (from list_chunks_raw) or already a dict
        if isinstance(raw_meta, str):
            raw_meta = json.loads(raw_meta) if raw_meta else {}
        chunk = ChunkRecord(
            doc_id=_archived_required_str(doc, "doc_id"),
            chunk_id=doc["chunk_id"],
            text=_archived_required_str(doc, "text"),
            vector=floats,
            source_path=_archived_required_str(doc, "source_path"),
            indexed_at=_archived_required_str(doc, "indexed_at"),
            file_type=_archived_optional_str(doc, "file_type") or "",
            language=_archived_optional_str(doc, "language") or "",
            metadata=raw_meta,
            acl=_archived_optional_str_list(doc, "acl"),
            custom_score=doc.get("custom_score"),
            # Older exports wrote "" for a null column; legacy / unknown values normalize as on every read.
            ingested_by=normalize_ingested_by(_archived_str_allowing_surrogates(doc, "ingested_by")),
            updated_at=_archived_optional_str(doc, "updated_at") or "",
            expires_at=doc.get("expires_at"),
            scopes=_archived_optional_str_list(doc, "scopes"),
            # Absent from archives exported before ACL provenance was included.
            acl_source=_archived_optional_str(doc, "acl_source"),
            acl_sidecar_path=_archived_optional_str(doc, "acl_sidecar_path"),
            acl_warning=_archived_optional_str_list(doc, "acl_warning") or [],
        )
        # ingest_chunks' own per-chunk checks, made here so it never rejects a decoded doc's whole batch and
        # on_error="skip" can skip this one doc.
        validate_chunk_record(chunk)
    # KeyError: a required field is absent; TypeError: a wrongly typed field; ValueError: malformed metadata JSON, a
    # lone surrogate in a string (UnicodeEncodeError) or a value validate_chunk_record refuses; OverflowError: a
    # custom_score int too large for a float or an expires_at before year 1 in UTC; RecursionError: metadata JSON
    # nested past the parser's limit.
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ArchiveRejectedError(
            f"malformed doc at line {line_number}: a required field is missing or has the wrong type"
        ) from exc
    return chunk


def _first_valid_chunk(reader: ImportArchiveReader, on_error: str) -> ChunkRecord | None:
    """Return the archive's first doc that decodes, or ``None`` when it has none.

    Scans with the ingest loop's *on_error* semantics: under ``"skip"`` a corrupt line or doc is passed over
    (``iter_docs`` logs a corrupt line here too; the ingest loop counts both and logs a bad doc when it
    reaches them); otherwise it raises ``ArchiveRejectedError``.
    """
    # closing(): the scan stops early, so close the generator (and its archive) now, not when it is collected.
    with contextlib.closing(reader.iter_docs(skip=0, on_error=on_error)) as docs:
        for doc in docs:
            try:
                return _decode_chunk(doc, reader.line_number)
            except ArchiveRejectedError:
                if on_error != "skip":
                    raise
    return None


async def _owns_collection(
    job: ImportJob, store: JobStore, search_store: SearchStore, first_chunk: ChunkRecord | None,
) -> bool:
    """Return whether *job*'s collection is still the one a previous run of the job wrote.

    It is while no other import has claimed the collection since (which revokes this job's claim:
    ``collection_claim_revoked`` on the stored job) and the archive's first valid doc (*first_chunk*) is stored in
    it as archived — the same ``chunk_id`` and ``indexed_at`` — even after other rows were deleted from it since
    (e.g. by maintenance). The ``chunk_id`` alone would not do: it derives from the source path and chunk index, so
    re-ingesting the archived files stores it again, with a new ``indexed_at``. The stored doc alone would not do
    either: another import of the same archive, or of a newer one from the same source, stores that very row.

    Limitation (accepted): when that very row is gone (TTL prune, orphan cleanup, a delete) or rewritten (its
    ``indexed_at`` normalized), nothing marks the collection as this job's, so the resume is refused. The refusal
    never writes into an unverified collection; a new import with ``force_overwrite`` recovers.
    """
    if first_chunk is None:
        return False
    stored = await search_store.get_chunks_by_ids(job.collection, [first_chunk.chunk_id])
    # Read after the await, so a claim another import made meanwhile is seen.
    stored_job = store.get(job.job_id)
    if isinstance(stored_job, ImportJob) and stored_job.collection_claim_revoked:
        return False
    return any(row["indexed_at"] == first_chunk.indexed_at for row in stored)


async def _ensure_collection_absent(
    job: ImportJob, search_store: SearchStore, *, refusal: str, may_drop: bool,
) -> None:
    """Refuse *job*'s collection when it exists, or drop it when *may_drop*.

    Raises:
        ArchiveRejectedError: with *refusal* as its message, when the collection exists and *may_drop* is false.
    """
    # Check both the meta table and the underlying LanceDB table for existence.
    existing_meta = await search_store.get_collection_meta(job.collection, job.namespace)
    if existing_meta is None and await search_store.count_chunks(job.collection, job.namespace) == 0:
        return
    if not may_drop:
        raise ArchiveRejectedError(refusal)
    try:
        await search_store.drop_collection(job.collection)
    except (KeyError, RuntimeError):
        pass  # table may not exist in LanceDB; that's fine
    if existing_meta is not None:
        await search_store.delete_collection_meta(job.collection, job.namespace)


def _claim_collection(job: ImportJob, store: JobStore, resume: _ResumePoint, doc_count: int) -> None:
    """Claim *job*'s collection right before its run's first ingest write, revoking every other import's claim.

    This run is about to write the collection, so a resume of any other import of it (any namespace — the chunk
    table is shared — and any status) must never continue into it again. The others are revoked first, each with a
    plain ``store.update`` that leaves its progress alone: a crash in between leaves this job's previous claim as it
    was. This job's claim is one ``update_progress`` write, so it lands together with its checkpoint; it also clears
    this job's own revocation, and resets its checkpoint when restarting from scratch.
    """
    for other in store.list():
        if (
            isinstance(other, ImportJob)
            and other.job_id != job.job_id
            and other.collection == job.collection
            and not other.collection_claim_revoked
        ):
            with contextlib.suppress(KeyError):  # evicted since store.list(): no claim left to revoke
                store.update(other.job_id, collection_claim_revoked=True)
    store.update_progress(
        job.job_id, resume.processed, doc_count, "validating",
        collection_claimed=True, collection_claim_revoked=False, lines_consumed=resume.skip,
    )


async def _store_batch_reporting_overlap(
    search_store: SearchStore,
    collection: str,
    namespace: str,
    batch: list[ChunkRecord],
    *,
    may_overlap: bool,
) -> bool:
    """Store *batch*; return whether the next batch may still overlap chunks a failed previous run stored.

    A failed run may commit batches past its checkpoint, so while *may_overlap* the chunks already stored are
    dropped from *batch* first. Commits follow archive order: once a batch holds no stored chunk, no later
    batch does, and this returns ``False`` from then on.

    Limitation (accepted): a resume run with a smaller ``checkpoint_interval`` than the failed run, whose first
    resumed batch's overlap rows were deleted since, stops checking early and can re-append the rest of that
    one old batch. Checking every batch instead would scan the table once per batch (``chunk_id`` is unindexed).
    """
    if may_overlap:
        stored = await search_store.stored_chunk_ids(collection, [chunk.chunk_id for chunk in batch])
        batch = [chunk for chunk in batch if chunk.chunk_id not in stored]
        may_overlap = bool(stored)
    await search_store.ingest_chunks(collection, batch, namespace=namespace)
    return may_overlap


async def _ingest_docs(
    job: ImportJob,
    store: JobStore,
    search_store: SearchStore,
    reader: ImportArchiveReader,
    resume: _ResumePoint,
    *,
    embedding_dim: int | None,
    doc_count: int,
    config: SearchConfig,
) -> _ImportCounters | None:
    """Stream the archive's docs after *resume*'s skip position into the collection, checkpointing each batch.

    Returns this run's counts, or ``None`` when a ``DELETE /jobs/{id}`` cancelled the job (now ``CANCELLED``).
    """
    counters = _ImportCounters(resume)
    may_overlap = resume.is_resume  # a resume re-reads batches a failed run may have committed past its checkpoint
    batch: list[ChunkRecord] = []
    for doc in reader.iter_docs(skip=resume.skip, on_error=job.on_error):
        try:
            chunk = _decode_chunk(doc, reader.line_number, embedding_dim=embedding_dim)
        except _VectorDimensionError:
            raise  # never skipped: see _VectorDimensionError
        except ArchiveRejectedError as exc:
            if job.on_error != "skip":
                raise  # logged once, with its cause chained, by _import_task's ArchiveRejectedError handler
            counters.skipped += 1
            logger.warning("_ingest_docs: skipping %s: %s", exc, exc.__cause__ or "no decode error")
            continue

        batch.append(chunk)

        if len(batch) >= config.jobs.checkpoint_interval:
            may_overlap = await _store_batch_reporting_overlap(
                search_store, job.collection, job.namespace, batch, may_overlap=may_overlap,
            )
            counters.stored += len(batch)
            batch = []
            store.update_progress(
                job.job_id, counters.processed, doc_count, "ingesting",
                lines_consumed=counters.lines_consumed(reader.skipped_lines),
            )

            # Cancellation check
            current = store.get(job.job_id)
            if current is not None and current.status == JobStatus.CANCELLING:
                store.update(job.job_id, status=JobStatus.CANCELLED)
                return None

    # Flush remaining batch
    if batch:
        await _store_batch_reporting_overlap(
            search_store, job.collection, job.namespace, batch, may_overlap=may_overlap,
        )
        counters.stored += len(batch)
    return counters


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
      2. collection exists check, then find the archive's first valid doc (it sets the embedding dimension):
         - a fresh run refuses an existing collection, or drops it under force_overwrite, before that scan;
         - a resume finds that doc first and continues into the collection while it still owns it
           (``_owns_collection``). Otherwise it restarts from scratch when the collection is gone, and refuses
           it when it exists — never dropping it, even under force_overwrite: it may now hold another
           import's data, which only a new import may overwrite.
         The run then claims the collection, revoking every other import's claim of it (``_claim_collection``).
      3. ingesting phase: stream docs from archive into the store; checkpoint progress
      4. indexing phase: rebuild FTS index, recompute collection meta, warm the models
      5. mark DONE on success; FAILED on any exception; CANCELLED if CANCELLING detected

    A cancelled task (only the lifespan shutdown cancels it) leaves the job RUNNING on
    purpose: crash recovery turns it into FAILED ``process_restart``, which
    ``POST /jobs/{id}/resume`` accepts — a CANCELLED job could not be resumed.
    """
    job_id = job.job_id
    try:
        # --- Phase: validating ---
        # The validating writes carry a resumed run's checkpoint forward, so a run that fails
        # before its first new checkpoint leaves that checkpoint intact for the next resume.
        resume = _resume_point(store.get(job_id))
        store.update_progress(job_id, resume.processed, resume.total, "validating")
        reader = ImportArchiveReader(Path(job.archive_path))
        manifest = reader.read_manifest()
        _validate_manifest(manifest, job, config)
        doc_count = manifest["doc_count"]
        store.update_progress(job_id, resume.processed, doc_count, "validating")

        # --- Collection exists guard ---
        # A fresh run refuses or drops an existing collection before reading the archive's docs.
        if not resume.is_resume:
            await _ensure_collection_absent(
                job, search_store,
                refusal=f"collection {job.collection!r} already exists; use force_overwrite=true to overwrite",
                may_drop=job.force_overwrite,
            )
        # The first valid doc sets the embedding dimension (needed to create the LanceDB table before
        # ingesting) and identifies the collection a previous run of this job wrote.
        first_chunk = _first_valid_chunk(reader, job.on_error) if doc_count > 0 else None
        embedding_dim = len(first_chunk.vector) if first_chunk is not None else None
        if resume.is_resume and not await _owns_collection(job, store, search_store, first_chunk):
            logger.warning(
                "_import_task: job %s cannot verify it wrote collection %r; restarting from scratch unless it exists",
                job_id, job.collection,
            )
            await _ensure_collection_absent(
                job, search_store,
                refusal=(
                    f"collection {job.collection!r} already exists and was not written by this import; "
                    "start a new import with force_overwrite=true"
                ),
                may_drop=False,
            )
            resume = _FRESH_START

        # Claim the collection right before the first ingest write to it, so a resume after a crash ahead
        # of the first checkpoint continues into it, while a run rejected earlier claims nothing.
        _claim_collection(job, store, resume, doc_count)

        # Ensure the LanceDB table exists before we ingest into it.
        # For empty archives: nothing to ingest; skip ensure_collection.
        if embedding_dim is not None:
            await search_store.ensure_collection(job.collection, embedding_dim)

        # --- Phase: ingesting ---
        counters = await _ingest_docs(
            job, store, search_store, reader, resume, embedding_dim=embedding_dim, doc_count=doc_count, config=config,
        )
        if counters is None:
            return  # cancelled

        # --- Phase: indexing ---
        # Records the lines consumed since the last checkpoint, so a resume after an
        # indexing-phase failure re-reads none of them.
        store.update_progress(
            job_id, counters.processed, doc_count, "indexing",
            lines_consumed=counters.lines_consumed(reader.skipped_lines),
        )

        if embedding_dim is not None:
            # Non-empty archive: rebuild FTS index and recompute centroid.
            detected_language = manifest.get("language", "")
            await search_store.rebuild_fts_index(job.collection, language=detected_language)

            collection_embedder = await embedder_cache.get_or_load(manifest["active_embedding_model"])
            await pipeline.recompute_collection_meta(
                job.collection, collection_embedder, namespace=job.namespace, force=True
            )
            # get_or_load returns a lazy embedder, and an import takes its vectors from
            # the archive, so nothing here builds it: without this the first search
            # after DONE finds it cold and runs FTS-only, dropping every vector-only
            # hit (S288 (import)).
            await await_models_warm(pipeline, job_id, collection_embedder)
        else:
            # Empty archive: table was never created; nothing to index or recompute.
            logger.info("_import_task: archive for %r is empty; skipping FTS rebuild and centroid recompute", job.collection)

        # A DELETE /jobs/{id} may have landed during the indexing phase (the warm-up
        # can take minutes on a cold server); honour it rather than overwrite it.
        current = store.get(job_id)
        if current is not None and current.status == JobStatus.CANCELLING:
            store.update(job_id, status=JobStatus.CANCELLED)
            return

        store.update(
            job_id,
            status=JobStatus.DONE,
            result={
                "imported": counters.processed,
                # Lines skipped before a resumed checkpoint + JSON-parse errors (iter_docs) + doc decode errors.
                "skipped": counters.total_skipped(reader.skipped_lines),
                "total_in_archive": doc_count,
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
    except (ArchiveRejectedError, PathUnsafeError) as exc:
        # A refusal phrased by archon-search itself (an ArchiveRejectedError message or a
        # PathUnsafeError reason code): safe to report.
        logger.warning("_import_task: job %s rejected the archive: %s", job_id, exc, exc_info=True)
        try:
            store.update(job_id, status=JobStatus.FAILED, error=str(exc))
        except (KeyError, OSError):
            logger.error("_import_task: could not persist FAILED status for job %s", job_id)
    except Exception:  # noqa: BLE001
        logger.exception("_import_task: job %s failed", job_id)
        try:
            store.update(job_id, status=JobStatus.FAILED, error=IMPORT_FAILED_DETAIL)
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

    # Step 1: Validate the archive path is within the allowed data directory, or
    # within the configured [backup].output_dir — the backup job writes archives
    # straight to that directory and it may legitimately sit off-tree (see
    # Documentation/OperatorGuide/40_backup_restore_disaster_recovery.md), so an
    # archive this product wrote itself must remain restorable.
    allowed_dirs = [get_data_dir()]
    if config.backup.output_dir:
        allowed_dirs.append(Path(config.backup.output_dir))
    try:
        validated_path = validate_export_path(body.path, allowed_dirs)
    except PathUnsafeError as exc:
        return JSONResponse({"error": "path_unsafe", "reason": exc.reason}, status_code=400)

    # Step 2: Verify archive file exists. Use the resolved, already-validated Path
    # returned by validate_export_path() above — not a fresh Path(body.path) — so every
    # later filesystem operation acts on the exact path that was checked against
    # allowed_dirs.
    archive_path = validated_path
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
        # Full original is logged server-side; the wire detail is a fixed, sanitized
        # message — tarfile/gzip errors can embed the archive's own byte content.
        logger.warning("import archive %s could not be opened: %s", archive_path, exc, exc_info=exc)
        return JSONResponse(
            {"error": "invalid_archive", "detail": INVALID_ARCHIVE_DETAIL},
            status_code=422,
        )

    # Step 4: Read manifest to early-detect embedding model mismatch
    try:
        reader = ImportArchiveReader(archive_path)
        manifest = reader.read_manifest()
    except (ValueError, PathUnsafeError) as exc:
        return JSONResponse(
            {"error": "invalid_manifest", "detail": manifest_error_detail(exc, archive_path)},
            status_code=422,
        )

    archive_model = manifest.get("active_embedding_model", "")
    server_model = config.embedding_model
    if archive_model != server_model:
        return JSONResponse(
            {
                "error": "embedding_model_mismatch",
                "detail": (
                    f"archive uses model {quote_archive_value(archive_model)}; "
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
                    f"archive has schema_version={quote_archive_value(archive_schema)}; "
                    f"server expects {EXPORT_SCHEMA_VERSION!r}; "
                    "use ignore_schema_version=true to bypass"
                ),
            },
            status_code=422,
        )

    # Step 7: Create the import job (status=QUEUED)
    job = store.create_import(
        collection=name,
        archive_path=str(archive_path),
        force_overwrite=body.force_overwrite,
        ignore_schema_version=body.ignore_schema_version,
        on_error=body.on_error,
        namespace=ns,
    )

    # Step 8: Return 202 with the job response
    return JSONResponse(job_to_dict(job), status_code=202)

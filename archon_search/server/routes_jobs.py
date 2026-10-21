"""POST /ingest, GET /jobs, GET /jobs/{job_id}, DELETE /jobs/{job_id} endpoints."""
from __future__ import annotations

import asyncio
import logging
import math
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Final

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, PrivateAttr, field_validator

from archon_search._path_safety import PathUnsafeError, validate_ingest_path
from archon_search._types import IngestError, _file_exceeds_limit
from archon_search.config import SearchConfig, resolve_active_model
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.embedder_cache import EMBEDDER_NOT_READY_DETAIL, EmbedderCache, EmbedderNotReadyError
from archon_search.jobs.model import IngestJob, JobStatus, job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.server._ingest_lock import acquire_collection_lock_or_503
from archon_search.server._job_warmup import await_models_warm
from archon_search.server._ingested_by import parse_ingested_by_header
from archon_search.server.schemas import CodedErrorDetail, ErrorDetail, JobListResponse, JobResponse
from archon_search.types import DeleteJob, ExportJob, ImportJob, MigrationJob, ReindexJob, SyncJob

if TYPE_CHECKING:
    from archon_search.store import SearchStore

logger = logging.getLogger(__name__)

router = APIRouter()

# Terminal statuses — DELETE is idempotent for these
_TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.FAILED, JobStatus.FAILED_EXPIRED, JobStatus.CANCELLED}
# Active statuses — DELETE sets CANCELLING
_ACTIVE_STATUSES = {JobStatus.RUNNING, JobStatus.PENDING}

# Sanitized, wire-facing ``detail`` for DELETE /jobs/{id} on any non-terminal
# SyncJob — the startup sync, a manual POST /sync, or a legacy QUEUED/PENDING
# row (CLAUDE.md hard invariant: no str(exc)/internals in a wire-facing detail
# field). A sync is non-cancellable: nothing interrupts the task that runs it,
# so transitioning the row to CANCELLING would be silently ignored by the sync
# itself AND would arm the (sticky, since fix-brief-C item 1) crash-loop guard
# on the next boot — CANCELLING is one of jobs/store.py's _CRASH_STATUSES.
_SYNC_JOB_NOT_CANCELLABLE_DETAIL = "a sync cannot be cancelled"

#: Wire-facing job ``error`` (served by GET /jobs) for an unexpected ingest / reindex failure; the
#: exception text can embed server paths and library internals, so it is logged only.
INGEST_FAILED_DETAIL = "ingest failed; see the server log for details"
REINDEX_FAILED_DETAIL = "reindex failed; see the server log for details"
#: Wire-facing body of a sanitized 500 whose cause is logged only.
INTERNAL_ERROR_DETAIL = "internal error"
#: Wire-facing job ``error`` for a legacy QUEUED/PENDING community-rebuild / metadata-reindex / sync job (an older
#: server created those QUEUED first; nothing schedules any of these kinds) that a newer request of its route
#: supersedes (POST /graph/{collection}/rebuild-communities, POST /collections/{name}/reindex-metadata, POST /sync).
JOB_SUPERSEDED_DETAIL = "never started; superseded by a newer request"
#: Log-line prefixes naming the writer of a best-effort job status write (see ``_persist_job_status``).
_INGEST_LOG_CONTEXT = "background ingest"
_REINDEX_LOG_CONTEXT = "_reindex_task"
_REINDEX_COLLECTION_LOG_CONTEXT = "reindex_collection"

# ``500`` ``detail`` when a job-store write (POST /ingest job creation, DELETE /jobs/{id}
# cancel transition) fails: an OSError names the job file path, so the cause is logged
# with its traceback, never sent.
JOB_STORE_WRITE_FAILED_DETAIL: Final[str] = INTERNAL_ERROR_DETAIL
# Prefix of the not-found job ``error``; the full text is ``f"{prefix}: {path}"``.
INGEST_PATH_NOT_FOUND_PREFIX = "path does not exist or is not a file/directory"


class IngestPathNotFoundError(FileNotFoundError):
    """An ingest ``path`` names neither a file nor a directory on the server host.

    Its message is curated wire text that echoes the caller's ``path`` exactly as
    submitted — never the server's ``~`` expansion or symlink resolution of it — so
    both ingest tasks record it verbatim as the job ``error`` instead of the
    catch-all's INGEST_FAILED_DETAIL.
    """

    def __init__(self, submitted_path: str) -> None:
        super().__init__(f"{INGEST_PATH_NOT_FOUND_PREFIX}: {submitted_path}")


# Inclusive valid range for chunk_ttl_seconds.
_TTL_MIN: int = 1
_TTL_MAX: int = 2**31 - 1
# Limits for chunk_scopes.
_SCOPES_MAX_ITEMS: int = 100
_SCOPE_MAX_LEN: int = 255


class IngestDocument(BaseModel):
    """One document whose text is sent inline rather than read from the server host.

    ``source_path`` is the document's logical identity: it must be an absolute,
    traversal-free path but need not name a file on the server, and it seeds the
    document id, so re-posting the same ``source_path`` replaces that document.
    """

    text: str
    source_path: str


class IngestRequest(BaseModel):
    collection: str
    path: str | None = None
    documents: list[IngestDocument] | None = None
    ingested_by: str = "http"
    # E2a BE-4: optional per-request TTL and scope tags forwarded to the pipeline.
    chunk_ttl_seconds: int | None = None
    chunk_scopes: list[str] | None = None
    # ``path`` as the caller sent it, set by with_resolved_path() for wire-facing error text.
    _submitted_path: str | None = PrivateAttr(default=None)

    @property
    def submitted_path(self) -> str:
        """The caller's raw ``path``; equals ``path`` on a request never passed through with_resolved_path().

        Raises ValueError on a request without a ``path`` (the documents mode), so
        error text can never echo a missing path as ``"None"``.
        """
        if self._submitted_path is not None:
            return self._submitted_path
        if self.path is None:
            raise ValueError("IngestRequest has no path")
        return self.path

    def with_resolved_path(self, resolved: str) -> IngestRequest:
        """A copy whose ``path`` is *resolved* — validate_ingest_path()'s result — keeping this ``path`` as submitted.

        Takes the already-validated path rather than validating again, so a handler
        validates exactly once and the copy runs on the path it registered.
        """
        copy = self.model_copy(update={"path": resolved})
        copy._submitted_path = self.submitted_path
        return copy

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
    """Resolve per-collection embedder and dispatch to the pipeline ingest for this body.

    Three targets: ``ingest_file`` and ``ingest_directory`` for a ``path`` body,
    ``ingest_documents`` for an inline ``documents`` body.

    Returns a tuple of:
    - warnings: flat list of warning strings from all IngestResult objects
    - file_results: list of per-file dicts with ``doc_id``, ``status``, and optional ``code``
      (non-empty only when at least one file has a non-None code, e.g. ``"file_too_large"``)
    """
    meta = await search_store.get_collection_meta(body.collection, namespace)
    active_model = resolve_active_model(meta, config)
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
            # Write a stub meta row so the collection appears in GET /collections/
            # with docs=0 after the job transitions to FAILED — mirrors the guarantee
            # documented in 50_ingestion_and_collections.md:113 for POST /collections/.
            if meta is None:
                from archon_search.collection_meta import CollectionMeta  # noqa: PLC0415
                from archon_search.store import STORE_SCHEMA_VERSION  # noqa: PLC0415
                try:
                    await search_store.update_collection_meta(CollectionMeta(
                        name=body.collection,
                        namespace=namespace,
                        active_embedding_model=active_model,
                        schema_version=STORE_SCHEMA_VERSION,
                    ))
                except Exception:
                    logger.warning(
                        "_dispatch_ingest: could not write stub meta for %r on path-not-found",
                        body.collection,
                        exc_info=True,
                    )
            # Echo the caller's raw string: body.path may be the server's ~ / symlink resolution.
            raise IngestPathNotFoundError(body.submitted_path)
    elif body.documents is not None:
        results = await pipeline.ingest_documents(
            [{"text": d.text, "source_path": d.source_path} for d in body.documents],
            body.collection,
            embedder=embedder,
            namespace=namespace,
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
    return [], []


def _persist_job_status(job_store: JobStore, job_id: str, status: JobStatus, *, context: str, **fields: object) -> None:
    """Best-effort job status write: any failure is logged with its traceback, never raised.

    That covers a persist failure (OSError, or a JSON-serialization TypeError/ValueError from
    ``JobStore._write_atomic``) and, defensively, a KeyError for a job no longer in the store.
    *context* names the writer in the log line (``_INGEST_LOG_CONTEXT``, ``_REINDEX_LOG_CONTEXT``, ...).
    """
    try:
        job_store.update(job_id, status=status, **fields)
    except Exception:  # noqa: BLE001 — best-effort status write; logged with traceback
        logger.error("%s: could not persist %s status for job %s", context, status.value, job_id, exc_info=True)


def _cancel_requested(job_store: JobStore, job_id: str) -> bool:
    """True when a DELETE /jobs/{id} has moved the job to CANCELLING and its task has not completed the cancel yet."""
    job = job_store.get(job_id)
    return job is not None and job.status == JobStatus.CANCELLING


def _finish_requested_cancel(job_store: JobStore, job_id: str, *, context: str) -> bool:
    """If a cancel is pending (CANCELLING), write CANCELLED and return True; else False.

    A DELETE /jobs/{id} moves an active job to CANCELLING and leaves the rest to the job's task; a task that will
    run no (further) work completes the cancel here. The write is best-effort (a failure is logged, and True is
    still returned); any other status, or a missing job, is left untouched.
    """
    if not _cancel_requested(job_store, job_id):
        return False
    _persist_job_status(job_store, job_id, JobStatus.CANCELLED, context=context)
    return True


def _log_refused_claim(job_id: str, job: IngestJob | None, *, context: str) -> None:
    """Log why a PENDING -> RUNNING claim refused for a reason other than a requested cancel runs nothing.

    Already CANCELLED is where a cancel would leave the job anyway, so it is logged at DEBUG only; a missing job or
    any other status (RUNNING from a second task for the same job, or terminal) is unexpected and logged as WARNING.
    """
    level = logging.DEBUG if job is not None and job.status == JobStatus.CANCELLED else logging.WARNING
    status = "missing" if job is None else job.status.value
    logger.log(level, "%s: job %s not claimed as RUNNING (status %s); nothing run", context, job_id, status)


def _complete_refused_claim(job_store: JobStore, job_id: str, *, context: str) -> None:
    """A task's PENDING -> RUNNING claim was refused, so it runs nothing: complete a requested cancel, else log why."""
    if not _finish_requested_cancel(job_store, job_id, context=context):
        _log_refused_claim(job_id, job_store.get(job_id), context=context)


def _claim_ingest(store: JobStore, job_id: str) -> bool:
    """Claim an ingest job RUNNING by compare-and-set from PENDING; False (nothing to run) when the claim is refused.

    Unlike ``_claim_reindex``, a failed persist of the claim raises (the job stays PENDING): the ingest task's
    own exception handler then writes it FAILED.
    """
    if store.transition(job_id, {JobStatus.PENDING}, JobStatus.RUNNING) is not None:
        return True
    _complete_refused_claim(store, job_id, context=_INGEST_LOG_CONTEXT)
    return False


def _persist_final_ingest_status(
    store: JobStore, job_id: str, ingest_warnings: list[str], ingest_file_results: list[dict]
) -> None:
    """Write a successful ingest's terminal status: CANCELLED when cancellation was requested meanwhile, else DONE."""
    if _finish_requested_cancel(store, job_id, context=_INGEST_LOG_CONTEXT):
        return
    result_dict: dict = {"warnings": ingest_warnings}
    if ingest_file_results:
        result_dict["file_results"] = ingest_file_results
    _persist_job_status(store, job_id, JobStatus.DONE, context=_INGEST_LOG_CONTEXT, result=result_dict)


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
    """Lifecycle wrapper: PENDING → RUNNING → DONE/FAILED/CANCELLED.

    RUNNING is claimed by compare-and-set from PENDING: a job cancelled before the task starts ends CANCELLED
    without running, and a failed persist of the claim (the job stays PENDING) is an ingest failure → FAILED.
    """
    try:
        if not _claim_ingest(store, job_id):
            return
        ingest_warnings: list[str] = []
        ingest_file_results: list[dict] = []
        if pipeline_fn is None and search_store is not None and embedder_cache is not None and pipeline is not None and config is not None:
            ingest_warnings, ingest_file_results = await _dispatch_ingest(body, namespace, search_store, embedder_cache, pipeline, config)
        else:
            await _run_pipeline(job_id, store, body, pipeline_fn, namespace=namespace)
        await await_models_warm(pipeline, job_id)
    except asyncio.CancelledError:
        _persist_job_status(store, job_id, JobStatus.CANCELLED, context=_INGEST_LOG_CONTEXT)
        raise
    except EmbedderNotReadyError as exc:
        # Not a genuine failure: FAILED with a clean message. MaintenanceLoop._run_failed_ingest_retry
        # re-enqueues a FAILED base IngestJob as a new PENDING job; nothing in this module runs that job.
        logger.warning("Ingest task %s: embedder not ready — %s", job_id, exc)
        _persist_job_status(store, job_id, JobStatus.FAILED, context=_INGEST_LOG_CONTEXT, error=EMBEDDER_NOT_READY_DETAIL)
    except IngestPathNotFoundError as exc:
        # Curated text echoing only the caller's own path: safe as the job error.
        logger.warning("Ingest task %s failed: %s", job_id, exc)
        _persist_job_status(store, job_id, JobStatus.FAILED, context=_INGEST_LOG_CONTEXT, error=str(exc))
    except Exception:  # noqa: BLE001
        logger.exception("Ingest task %s failed", job_id)
        _persist_job_status(store, job_id, JobStatus.FAILED, context=_INGEST_LOG_CONTEXT, error=INGEST_FAILED_DETAIL)
    else:
        # Outside the ``try``: failing to persist a successful ingest's terminal status is no ingest
        # failure — it is logged, never reported FAILED (which MaintenanceLoop would then retry).
        _persist_final_ingest_status(store, job_id, ingest_warnings, ingest_file_results)


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
    to _run_pipeline signals the pipeline not to re-acquire the same lock. RUNNING is claimed
    as in _default_ingest_task.
    """
    # The lock while this task still owns it; None once released early, so the ``finally``
    # never releases an acquisition another holder (e.g. a DELETE) made in between.
    owned_lock = held_lock
    try:
        if not _claim_ingest(store, job_id):
            return
        ingest_warnings: list[str] = []
        ingest_file_results: list[dict] = []
        if pipeline_fn is None and search_store is not None and embedder_cache is not None and pipeline is not None and config is not None:
            # Release the pre-acquired lock before dispatch: pipeline.ingest_file /
            # ingest_directory will acquire it internally. Holding it here would
            # deadlock since asyncio.Lock is not reentrant.
            if owned_lock is not None and owned_lock.locked():
                owned_lock.release()
            owned_lock = None
            ingest_warnings, ingest_file_results = await _dispatch_ingest(body, namespace, search_store, embedder_cache, pipeline, config)
        else:
            await _run_pipeline(
                job_id, store, body, pipeline_fn, namespace=namespace, locked_by_caller=True
            )
        await await_models_warm(pipeline, job_id)
    except asyncio.CancelledError:
        _persist_job_status(store, job_id, JobStatus.CANCELLED, context=_INGEST_LOG_CONTEXT)
        raise
    except EmbedderNotReadyError as exc:
        # Not a genuine failure — see the sibling handler in _default_ingest_task.
        logger.warning("Ingest task %s: embedder not ready — %s", job_id, exc)
        _persist_job_status(store, job_id, JobStatus.FAILED, context=_INGEST_LOG_CONTEXT, error=EMBEDDER_NOT_READY_DETAIL)
    except IngestPathNotFoundError as exc:
        # Curated text — see the sibling handler in _default_ingest_task.
        logger.warning("Ingest task %s failed: %s", job_id, exc)
        _persist_job_status(store, job_id, JobStatus.FAILED, context=_INGEST_LOG_CONTEXT, error=str(exc))
    except Exception:  # noqa: BLE001
        logger.exception("Ingest task %s failed", job_id)
        _persist_job_status(store, job_id, JobStatus.FAILED, context=_INGEST_LOG_CONTEXT, error=INGEST_FAILED_DETAIL)
    else:
        # Outside the ``try`` — see the sibling ``else`` in _default_ingest_task.
        _persist_final_ingest_status(store, job_id, ingest_warnings, ingest_file_results)
    finally:
        # Release the pre-acquired lock regardless of outcome or cancellation — unless already released.
        if owned_lock is not None and owned_lock.locked():
            owned_lock.release()


async def _clear_reindex_job_id(search_store: SearchStore, collection: str, namespace: str, job_id: str) -> None:
    """Best-effort: clear the collection's ``reindex_job_id`` if it still names *job_id*; a failure is logged, never raised.

    A pointer to another job is left alone: once this job is CANCELLING, the reindex route's 409 guard treats it as
    stale, so a newer reindex may already own the collection.
    """
    try:
        meta = await search_store.get_collection_meta(collection, namespace)
        if meta is not None and meta.reindex_job_id == job_id:
            meta.reindex_job_id = None
            await search_store.update_collection_meta(meta)
    except Exception:  # noqa: BLE001
        logger.exception("_reindex_task: failed to clear reindex_job_id for job %s", job_id)


async def _end_reindex_cancelled_or_failed(
    search_store: SearchStore, job_store: JobStore, collection: str, namespace: str, job_id: str, *,
    error: str | None = None,
) -> None:
    """End a reindex that runs nothing more: clear the collection's ``reindex_job_id``, THEN write the job CANCELLED
    when a DELETE requested a cancel (CANCELLING), else FAILED with *error*.

    ``error=None`` never writes FAILED: only a requested cancel is completed (the job is already terminal, or the
    caller has checked that a cancel is pending). The clear comes first because it rewrites the whole meta row: a
    client that waits for the terminal status and then starts a new reindex or PATCH must not have its row clobbered
    by this task's stale write. Both steps are best-effort (failures are logged, never raised).
    """
    await _clear_reindex_job_id(search_store, collection, namespace, job_id)
    if _finish_requested_cancel(job_store, job_id, context=_REINDEX_LOG_CONTEXT) or error is None:
        return
    _persist_job_status(job_store, job_id, JobStatus.FAILED, context=_REINDEX_LOG_CONTEXT, error=error)


async def _claim_reindex(
    search_store: SearchStore, job_store: JobStore, collection: str, namespace: str, job_id: str
) -> bool:
    """Claim a reindex job RUNNING by compare-and-set from PENDING; on False the caller runs nothing more.

    Unlike ``_claim_ingest``, a failed persist of the claim is handled here and never raised: it leaves the job
    PENDING, which nothing would ever run, so the job is ended FAILED — or CANCELLED, when a DELETE landed meanwhile
    (``_end_reindex_cancelled_or_failed``). A refused claim ends the same way with no FAILED write: a requested
    cancel is completed, any other status is logged (``_log_refused_claim``). A RUNNING job is the exception — owned
    by another task, so its pointer names a live job and still drives the reindex/PATCH 409 guards: it is only logged.
    """
    try:
        if job_store.transition(job_id, {JobStatus.PENDING}, JobStatus.RUNNING) is not None:
            return True
    except Exception:  # noqa: BLE001 — the claim did not happen; handled below, never raised
        logger.exception("_reindex_task: failed to persist job %s as RUNNING", job_id)
        await _end_reindex_cancelled_or_failed(
            search_store, job_store, collection, namespace, job_id, error=REINDEX_FAILED_DETAIL
        )
        return False
    job = job_store.get(job_id)
    if job is None or job.status != JobStatus.CANCELLING:
        _log_refused_claim(job_id, job, context=_REINDEX_LOG_CONTEXT)
    if job is None or job.status != JobStatus.RUNNING:
        await _end_reindex_cancelled_or_failed(search_store, job_store, collection, namespace, job_id)
    return False


async def _reindex_task(
    job_id: str,
    store: Any,
    job_store: JobStore,
    embedder_cache: EmbedderCache,
    pipeline: Any,
    collection: str,
    namespace: str,
    collection_path: Path | None,
    config: SearchConfig,
) -> None:
    """Lifecycle wrapper for reindex: resolves embedder, ingests, promotes model on success.

    Embedder-resolution and ingest failures (a cancelled ingest included), collection-meta read/write
    failures, and job-status write failures (see ``_persist_job_status``) are logged, never raised. Not
    caught: ``asyncio.CancelledError`` outside the ingest step (e.g. while the embedder loads).

    RUNNING is claimed by compare-and-set from PENDING after the embedder loads: a job cancelled before
    the claim ends CANCELLED without any ingest or model promotion (also when the embedder load then fails);
    a failed persist of the claim → FAILED, or CANCELLED when a DELETE landed meanwhile. A cancel landing
    during the model promotion ends CANCELLED with the model promoted (the vectors were re-embedded).

    On every non-success exit the collection's ``reindex_job_id`` is cleared (only while it names this job)
    BEFORE the terminal status is written (``_end_reindex_cancelled_or_failed`` has why); a non-cancel failure
    before any work ran (embedder load, "not a ReindexJob", claim persist) ends CANCELLED instead of FAILED when a
    DELETE requested a cancel meanwhile; a failed ingest ends FAILED regardless (the work's outcome wins).
    """
    # --- Guards ---
    job = job_store.get(job_id)
    if job is None:
        await _clear_reindex_job_id(store, collection, namespace, job_id)
        logger.error("_reindex_task: job %s not found", job_id)
        return

    if not isinstance(job, ReindexJob):
        logger.error("_reindex_task: job %s is not a ReindexJob (got %s)", job_id, type(job))
        await _end_reindex_cancelled_or_failed(store, job_store, collection, namespace, job_id, error="not a ReindexJob")
        return

    if job.status in (JobStatus.CANCELLING, JobStatus.CANCELLED):
        await _end_reindex_cancelled_or_failed(store, job_store, collection, namespace, job_id)
        return

    # --- Step 1 & 2: resolve embedder ---
    target_model = job.target_embedding_model
    try:
        if target_model is not None:
            embedder = await embedder_cache.get_or_load(target_model)
        else:
            meta = await store.get_collection_meta(collection, namespace)
            embedder = await embedder_cache.get_or_load(resolve_active_model(meta, config))
    except EmbedderNotReadyError as exc:
        # Unlike _default_ingest_task's IngestJob, a FAILED ReindexJob is not
        # auto-retried by MaintenanceLoop._run_failed_ingest_retry (exact
        # ``type(job) is IngestJob`` only) — an operator must manually re-run
        # `collection reindex`. That gap predates this fix and is out of scope
        # here; this only sanitizes the error message (never str(exc) on the wire).
        logger.warning("_reindex_task: embedder not ready for job %s — %s", job_id, exc)
        await _end_reindex_cancelled_or_failed(
            store, job_store, collection, namespace, job_id, error=EMBEDDER_NOT_READY_DETAIL
        )
        return
    except Exception:  # noqa: BLE001
        logger.exception("_reindex_task: embedder resolution failed for job %s", job_id)
        await _end_reindex_cancelled_or_failed(store, job_store, collection, namespace, job_id, error=REINDEX_FAILED_DETAIL)
        return

    # --- Step 3: claim RUNNING (a DELETE may have landed during the embedder load) ---
    if not await _claim_reindex(store, job_store, collection, namespace, job_id):
        return

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
        if ingest_error is not None:
            # Outside the ``except`` block, so the exception is passed explicitly for the traceback.
            logger.error("_reindex_task: ingest failed for job %s", job_id, exc_info=ingest_error)
        # Step 6: failure path — do NOT touch active_embedding_model
        # The work itself ended, so its outcome wins over a pending cancel (as in the ingest tasks): CANCELLED
        # when the task was cancelled (a DELETE, or shutdown while still RUNNING), FAILED when the ingest failed —
        # so this exit cannot use _end_reindex_cancelled_or_failed; same clear-first order.
        await _clear_reindex_job_id(store, collection, namespace, job_id)
        if cancelled:
            _persist_job_status(job_store, job_id, JobStatus.CANCELLED, context=_REINDEX_LOG_CONTEXT)
        else:
            _persist_job_status(job_store, job_id, JobStatus.FAILED, context=_REINDEX_LOG_CONTEXT, error=REINDEX_FAILED_DETAIL)
        return

    # Check for cancellation between ingest and DONE (mirrors _persist_final_ingest_status): skip the promotion
    if _cancel_requested(job_store, job_id):
        await _end_reindex_cancelled_or_failed(store, job_store, collection, namespace, job_id)
        return

    # --- Step 5: success path — promote model ---
    try:
        meta = await store.get_collection_meta(collection, namespace)
        if meta is not None:
            if meta.reindex_job_id == job_id:  # another job's pointer is left alone (see _clear_reindex_job_id)
                meta.reindex_job_id = None
            # Only promote the model when the chunks were actually re-embedded.
            # A source-less no-op reindex (collection_path is None) skipped the
            # scan, so promoting active_embedding_model would advertise a model the
            # stored vectors were never produced with — silent search corruption.
            # Leave pending_embedding_model/needs_reindex intact in that case.
            if target_model is not None and collection_path is not None:
                # Model-change path
                meta.active_embedding_model = target_model
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
            await store.update_collection_meta(meta)
        else:
            logger.warning("_reindex_task: collection meta not found at success for job %s", job_id)
    except Exception:  # noqa: BLE001
        logger.exception("_reindex_task: failed to update collection meta for job %s", job_id)

    # A DELETE may have landed during the promotion awaits: the model is promoted (the vectors were re-embedded with
    # it), so CANCELLED is truthful. No await between this check and the write, so no DELETE can slip in between.
    if not _finish_requested_cancel(job_store, job_id, context=_REINDEX_LOG_CONTEXT):
        _persist_job_status(job_store, job_id, JobStatus.DONE, context=_REINDEX_LOG_CONTEXT)


_ERROR_401 = {401: {"model": ErrorDetail}}
_ERROR_400_401 = {
    400: {"model": ErrorDetail, "description": "Ingest path failed safety validation"},
    401: {"model": ErrorDetail},
}
_INGEST_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorDetail, "description": "Ingest path failed safety validation"},
    401: {"model": ErrorDetail},
    413: {
        "model": CodedErrorDetail,
        "description": "A single-file path or an inline document exceeds the configured max_file_mb limit",
    },
    500: {"model": ErrorDetail, "description": f'Creating the ingest job failed (body {{"detail": "{INTERNAL_ERROR_DETAIL}"}})'},
    503: {"model": ErrorDetail, "description": "Store busy — reindex in progress"},
}


@router.post("/ingest", status_code=202, response_model=JobResponse, responses=_INGEST_ERRORS)
async def ingest(body: IngestRequest, request: Request) -> JobResponse | JSONResponse:
    store: JobStore = request.app.state.job_store
    pipeline_fn: Callable[..., Awaitable[None]] | None = getattr(
        request.app.state, "ingest_pipeline", None
    )
    # Populate ingested_by from HTTP header (normalized at boundary).
    body.ingested_by = parse_ingested_by_header(request.headers.get("X-Ingested-By"))
    if body.path is not None:
        try:
            body = body.with_resolved_path(str(validate_ingest_path(body.path)))
        except PathUnsafeError as e:
            raise HTTPException(status_code=400, detail=f"path is unsafe: {e.reason}")
    # S293: `documents[].source_path` is client input that reaches the same pipeline,
    # so it gets the same path-safety rule as `body.path`.
    if body.documents is not None:
        for index, document in enumerate(body.documents):
            try:
                document.source_path = str(validate_ingest_path(document.source_path))
            except PathUnsafeError as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"documents[{index}].source_path is unsafe: {e.reason}",
                )
    ns = request.state.namespace

    # Synchronous 413 pre-check for a single-file path, before job creation. A directory
    # path is not checked here; inline `documents` get their own per-document check below.
    if body.path is not None:
        p = Path(body.path)
        if p.is_file():
            max_file_mb: int = request.app.state.config.ingest.max_file_mb
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

    # S293: the same operator cap for the inline mode — `max_file_mb` is per document
    # here, since one inline document is what one file is in the path mode. Without it
    # a single authenticated request pins arbitrary memory in the chunker and embedder.
    if body.documents is not None:
        documents_config: SearchConfig | None = getattr(request.app.state, "config", None)
        max_inline_mb: int = documents_config.ingest.max_file_mb if documents_config else 0
        if max_inline_mb > 0:
            limit_bytes = max_inline_mb * 1024 * 1024
            for document in body.documents:
                size_bytes = len(document.text.encode("utf-8"))
                if size_bytes > limit_bytes:
                    err = IngestError(
                        file_size_mb=math.ceil(size_bytes / (1024 * 1024)),
                        limit_mb=max_inline_mb,
                    )
                    raise HTTPException(
                        status_code=413, detail={"code": err.code, "message": err.message}
                    )

    # Pre-acquire the per-collection lock to return 503 synchronously on contention —
    # before creating the job, so a 503 leaves no PENDING job that nothing ever runs.
    search_store = getattr(request.app.state, "search_store", None)
    lock_result = await acquire_collection_lock_or_503(search_store, body.collection)
    if isinstance(lock_result, JSONResponse):
        return lock_result

    try:
        job = store.create(namespace=ns, collection=body.collection, path=body.path or "")
    except Exception:  # noqa: BLE001 — any failure maps to one sanitized 500 and releases the lock
        logger.exception("Failed to create the ingest job for collection %s", body.collection)
        if lock_result is not None and lock_result.locked():
            lock_result.release()
        return JSONResponse({"detail": INTERNAL_ERROR_DETAIL}, status_code=500)

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
    "sync": SyncJob,
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
        500: {"model": ErrorDetail, "description": f'Persisting the resume failed (body {{"detail": "{INTERNAL_ERROR_DETAIL}"}})'},
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
    try:
        updated = store.transition(job_id, {JobStatus.FAILED}, JobStatus.QUEUED)
    except Exception:  # noqa: BLE001 — the job stays FAILED; any failure maps to one sanitized 500
        logger.exception("Failed to persist the resume of job %s", job_id)
        return JSONResponse({"detail": INTERNAL_ERROR_DETAIL}, status_code=500)
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
        409: {
            "model": ErrorDetail,
            "description": f'The job is a non-terminal sync, which cannot be cancelled (body {{"detail": "{_SYNC_JOB_NOT_CANCELLABLE_DETAIL}"}})',
        },
        500: {
            "model": ErrorDetail,
            "description": (
                f'Persisting the cancel failed (body {{"detail": "{INTERNAL_ERROR_DETAIL}"}}), or the job is in a '
                'status DELETE does not handle, e.g. QUEUED (body {"detail": "Unknown job status: <status>"})'
            ),
        },
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
    if isinstance(job, SyncJob):
        return JSONResponse({"detail": _SYNC_JOB_NOT_CANCELLABLE_DETAIL}, status_code=409)
    if job.status in _ACTIVE_STATUSES:
        # Use transition() to avoid TOCTOU race: only updates if still active
        try:
            updated = store.transition(job.job_id, _ACTIVE_STATUSES, JobStatus.CANCELLING)
        except Exception:  # noqa: BLE001 — the job keeps its status; any failure maps to one sanitized 500
            logger.exception("Failed to persist the cancel of job %s", job_id)
            return JSONResponse({"detail": INTERNAL_ERROR_DETAIL}, status_code=500)
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
            content={"detail": f"Unknown job status: {job.status.value}"},
            status_code=500,
        )

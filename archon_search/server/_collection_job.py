"""Create a collection's job, and record it on the collection's meta row, under the collection lock.

Shared by POST /collections/{name}/reindex, POST /collections/{name}/reindex-metadata and
POST /graph/{collection}/rebuild-communities: each creates a job that writes its collection, and none holds the
collection lock for the job's whole run — a reindex or community rebuild never holds it throughout, and a metadata
reindex (``SearchStore.reindex_metadata`` holds it for its whole scan) clears its meta pointer after releasing it. So
DELETE /collections/{name} refuses while such a job is active, checking the job store under that lock
(``JobStore.find_active_collection_job``). Doing every step under the same lock means a
DELETE either drops the collection first (the route answers 404, creating nothing) or sees the job and refuses; and
two concurrent requests cannot both pass the route's duplicate-job guard, since each evaluates it on a meta row read
under the lock, after any earlier request wrote its job's pointer — except a job recording no pointer
(``pointer_field=None``: a dry-run metadata reindex), which the guard never sees, so concurrent dry runs each create
their job.
"""
from __future__ import annotations

import dataclasses
import logging
from http import HTTPStatus
from collections.abc import Callable, Set
from typing import TYPE_CHECKING, Literal

from fastapi.responses import JSONResponse

from archon_search.server._ingest_lock import acquire_collection_lock_or_503
from archon_search.server.routes_jobs import INTERNAL_ERROR_DETAIL, JOB_SUPERSEDED_DETAIL, _persist_job_status
from archon_search.store import CollectionLockNotHeldError
from archon_search.types import IngestJob, JobStatus

if TYPE_CHECKING:
    from archon_search.collection_meta import CollectionMeta
    from archon_search.jobs.store import JobStore
    from archon_search.store import SearchStore

logger = logging.getLogger(__name__)

#: 503 detail of these routes while the server is not ready (no search store: no meta row, lock or job can be used).
SERVICE_NOT_READY_DETAIL = "service not ready"
#: The ``CollectionMeta`` field pointing at a collection's active job of one kind (its routes' 409 guard reads it).
MetaJobPointer = Literal["reindex_job_id", "metadata_reindex_job_id", "community_rebuild_job_id"]
#: Statuses of a legacy metadata-reindex or community-rebuild row: an older server created these jobs QUEUED first,
#: and nothing schedules either kind, so such a row never runs. ``JobStore._load`` already fails a QUEUED one at
#: startup, so QUEUED here is a defensive leftover.
_LEGACY_NEVER_RUN_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.PENDING})


def refuse_active_job_or_fail_legacy(
    job_store: JobStore,
    pointed_job_id: str | None,
    *,
    active_statuses: Set[JobStatus],
    conflict_detail: str,
    log_context: str,
) -> JSONResponse | None:
    """The 409 *conflict_detail* while the meta pointer *pointed_job_id* names a job in *active_statuses*, else
    ``None``. Side effect: a legacy QUEUED/PENDING job it names is first marked FAILED ``JOB_SUPERSEDED_DETAIL``.

    The duplicate guard of POST /collections/{name}/reindex-metadata and POST /graph/{collection}/rebuild-communities,
    whose routes create their job RUNNING and run it themselves. A pointer naming a missing or terminal job is stale:
    ``None``, and the new job's id overwrites it — this lazy clear unwedges a collection whose task never cleared it
    (a crash mid-run: ``JobStore._load`` fails the job but never touches ``CollectionMeta``). A legacy QUEUED/PENDING
    row is stale too; marking it FAILED (best-effort, logged under *log_context*) keeps it from staying non-terminal,
    and so never evicted, forever.
    """
    if pointed_job_id is None:
        return None
    existing_job = job_store.get(pointed_job_id)
    if existing_job is None:
        return None
    if existing_job.status in active_statuses:
        return JSONResponse({"detail": conflict_detail}, status_code=409)
    if existing_job.status in _LEGACY_NEVER_RUN_STATUSES:
        _persist_job_status(job_store, existing_job.job_id, JobStatus.FAILED, context=log_context, error=JOB_SUPERSEDED_DETAIL)
    return None


async def _meta_or_refusal(
    search_store: SearchStore,
    collection: str,
    namespace: str,
    not_found_detail: str,
    refuse_duplicate: Callable[[CollectionMeta], JSONResponse | None],
) -> CollectionMeta | JSONResponse:
    """*collection*'s meta row in *namespace*, or the response refusing the job: 404 *not_found_detail* without a
    row, else *refuse_duplicate*'s response (a 409) when it gives one."""
    meta = await search_store.get_collection_meta(collection, namespace=namespace)
    if meta is None:
        return JSONResponse({"detail": not_found_detail}, status_code=404)
    refusal = refuse_duplicate(meta)
    return meta if refusal is None else refusal


async def create_collection_job[J: IngestJob](
    search_store: SearchStore,
    collection: str,
    namespace: str,
    *,
    job_store: JobStore,
    log_context: str,
    not_found_detail: str,
    refuse_duplicate: Callable[[CollectionMeta], JSONResponse | None],
    create_job: Callable[[CollectionMeta], J],
    pointer_field: MetaJobPointer | None,
    on_pointer_write_failure: Callable[[J, Exception], JSONResponse] | None = None,
) -> J | JSONResponse:
    """Create *collection*'s job under ``lock_for(collection)`` and return it, or the response refusing it.

    1. Without the lock, the meta row in *namespace* is read: 404 *not_found_detail* without one, and
       *refuse_duplicate*'s 409 when it refuses that row — at once, even while another write holds the lock (a
       metadata reindex holds it for its whole run).
    2. Under the lock, the row is read and checked again: a DELETE may have dropped the collection meanwhile (404;
       the lock entry is then discarded after the release, as DELETE's own discard kept it while this call waited on
       it), or a concurrent request may have recorded its job (409).
    3. *create_job*: any failure is logged and answered with the sanitized 500 — no job exists.
    4. Unless *pointer_field* is ``None``, the new job's id is written into that meta field
       (``update_collection_meta(..., _locked_by_caller=True)``: the held lock is not reentrant). A failed write
       returns *on_pointer_write_failure*'s response, given the job and the exception; without one the failure is
       logged at WARNING with its traceback and the job is returned (best-effort pointer). A
       ``CollectionLockNotHeldError`` is a programming error, not a failed write: the job, which no task will run, is
       marked FAILED ``INTERNAL_ERROR_DETAIL`` in *job_store* (best-effort) — else it would block DELETE until a
       restart — and the error propagates.

    Every refusal is returned as a response. A lock-wait timeout returns the lock helper's 503 ``store_busy``. The
    lock is always released.
    """
    guarded = await _meta_or_refusal(search_store, collection, namespace, not_found_detail, refuse_duplicate)
    if isinstance(guarded, JSONResponse):
        return guarded
    lock = await acquire_collection_lock_or_503(search_store, collection)
    if isinstance(lock, JSONResponse):
        return lock
    collection_gone = False
    try:
        meta = await _meta_or_refusal(search_store, collection, namespace, not_found_detail, refuse_duplicate)
        if isinstance(meta, JSONResponse):
            collection_gone = meta.status_code == HTTPStatus.NOT_FOUND
            return meta
        try:
            job = create_job(meta)
        except Exception:  # noqa: BLE001 — no job exists; any failure maps to one sanitized 500
            logger.exception("%s: failed to create the job for collection %r", log_context, collection)
            return JSONResponse({"detail": INTERNAL_ERROR_DETAIL}, status_code=500)
        if pointer_field is not None:
            # Built outside the try below: a bad field name is a bug that must surface, not a failed write.
            recorded = dataclasses.replace(meta, **{pointer_field: job.job_id})
            try:
                await search_store.update_collection_meta(recorded, _locked_by_caller=True)
            except CollectionLockNotHeldError:
                # A bug (the lock above is held), never a failed write to answer or log away; no task runs the job.
                _persist_job_status(job_store, job.job_id, JobStatus.FAILED, context=log_context, error=INTERNAL_ERROR_DETAIL)
                raise
            except Exception as exc:  # noqa: BLE001 — the caller's policy decides; no store failure escapes as a raw 500
                if on_pointer_write_failure is not None:
                    return on_pointer_write_failure(job, exc)
                logger.warning(
                    "%s: failed to persist %s for job %s", log_context, pointer_field, job.job_id, exc_info=True
                )
        return job
    finally:
        lock.release()
        if collection_gone:
            # Only after the release (discard_lock keeps an entry still held or waited on).
            search_store.discard_lock(collection)

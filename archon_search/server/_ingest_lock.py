"""Pre-acquire per-collection ingest lock for REST handlers.

Shared lock-acquisition logic of POST /ingest, POST /collections/, DELETE /collections/{name},
POST /collections/{name}/reindex, POST /collections/{name}/reindex-metadata and
POST /graph/{collection}/rebuild-communities (the last three through ``_collection_job.create_collection_job``,
which creates their job and its meta pointer under the lock).
On timeout it builds a 503 with a ``Retry-After`` header and the body
``{"detail": "collection is busy with another write; retry after Retry-After seconds", "code": "store_busy"}``
(:data:`STORE_BUSY_DETAIL`: the lock holder may be any write — an ingest, a DELETE, a job's creation, a reindex —
so the detail names none of them):
every caller but POST /collections/ returns that response unchanged, while
POST /collections/ rolls back its config entry and stub meta row and answers with its own
body ``{"detail": "store busy; retry in N seconds"}`` (N = the same ``Retry-After`` value).
:func:`store_busy_503` also builds DELETE /collections/{name}'s 503 refusal while a job — or a write that creates no job,
registered in ``SearchStore.collection_writer`` (:data:`COLLECTION_WRITER_IN_PROGRESS_DETAIL`) — may still be writing to
the collection; :func:`collection_job_in_progress_detail` words the job refusal, and the one of an import's
``force_overwrite`` drop (``routes_export._ensure_collection_absent``), which fails the import job instead of answering a response.
"""
from __future__ import annotations

import asyncio
import math

from fastapi.responses import JSONResponse

import archon_search.constants as _constants
from archon_search.types import IngestJob

#: Wire ``code`` of the 503s built by :func:`store_busy_503`: the lock-timeout refusal of every
#: :func:`acquire_collection_lock_or_503` caller, and DELETE /collections/{name}'s job-in-progress refusal.
#: POST /collections/ answers its own busy 503 body without a ``code``.
STORE_BUSY_CODE = "store_busy"
#: ``detail`` of the lock-timeout 503 of every :func:`acquire_collection_lock_or_503` caller. Names no operation
#: because the write holding the lock is unknown and need not be a reindex.
STORE_BUSY_DETAIL = "collection is busy with another write; retry after Retry-After seconds"
#: OpenAPI description of the 503 :func:`acquire_collection_lock_or_503` answers when the lock wait times out.
STORE_BUSY_503_DESCRIPTION = "Store busy (code store_busy): the collection lock was not acquired in time"
#: Wire-facing detail when the job that may still write the collection belongs to another namespace than the
#: caller's: its job id is not disclosed.
COLLECTION_JOB_IN_PROGRESS_DETAIL = "a job is in progress on this collection; retry after it completes"
#: Wire-facing detail when that job belongs to the caller's namespace (``str.format`` with ``job_id``).
COLLECTION_JOB_IN_PROGRESS_DETAIL_TEMPLATE = "job {job_id} is in progress; retry after it completes"
#: Wire-facing detail when no job but a write that creates none — an MCP ``ingest_file`` / ``ingest_directory`` or a
#: collection sync (startup, ``POST /sync``, filesystem watcher), registered in ``SearchStore.collection_writer`` — may
#: still write the collection.
COLLECTION_WRITER_IN_PROGRESS_DETAIL = (
    "an MCP ingest or a collection sync is in progress on this collection; retry after it completes"
)


def collection_job_in_progress_detail(job: IngestJob, namespace: str) -> str:
    """The detail refusing a write to the collection of *job*, which may still write it, for a caller in *namespace*.

    The job id appears only when *job* belongs to *namespace*.
    """
    if job.namespace == namespace:
        return COLLECTION_JOB_IN_PROGRESS_DETAIL_TEMPLATE.format(job_id=job.job_id)
    return COLLECTION_JOB_IN_PROGRESS_DETAIL


def store_busy_503(detail: str) -> JSONResponse:
    """Build the 503 ``store_busy`` response refusing a write because the collection is busy.

    ``Retry-After`` is a polling hint equal to the lock-wait bound ``INGEST_LOCK_TIMEOUT_S`` (read live,
    so a patched value applies), not an estimate of how long the blocking write still runs.
    """
    return JSONResponse(
        {"detail": detail, "code": STORE_BUSY_CODE},
        status_code=503,
        headers={"Retry-After": str(math.ceil(_constants.INGEST_LOCK_TIMEOUT_S))},
    )


async def acquire_collection_lock_or_503(
    store,
    collection_name: str,
):
    """Pre-acquire the per-collection ingest lock.

    Returns:
        The acquired asyncio.Lock on success.
        A 503 JSONResponse on acquisition timeout.
        None when store is unavailable (handler proceeds best-effort).
    """
    if store is None:
        return None

    lock = store.lock_for(collection_name)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=_constants.INGEST_LOCK_TIMEOUT_S)
    except asyncio.TimeoutError:
        return store_busy_503(STORE_BUSY_DETAIL)
    return lock

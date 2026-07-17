"""POST /sync — trigger a full synchronisation of all server-configured collections.

Mirrors ``routes_maintenance.py`` file structure. The sync job follows the
QUEUED -> RUNNING pre-transition pattern from ``routes_graph.py`` (rebuild_communities):

  1. Check ``app.state.sync_lock.locked()``. If held: 409 (TOCTOU-free — no await between check and acquire in the single event loop).
  2. If locked: 409 "sync already in progress".
  3. Create a SyncJob (QUEUED) in the job store.
  4. Transition QUEUED -> RUNNING before spawning the task.
  5. Spawn ``_sync_task`` as a background task (lock still held).
  6. Return 202 with the RUNNING job body.

``_sync_task`` releases the lock in a ``finally`` block so any sync() exception
(``OSError``, ``KeyError``, or any other) always frees the lock (S23).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from archon_search.jobs.store import JobStore
    from archon_search.sync import SearchCollectionSync
    from archon_search.types import SyncJob

from archon_search.jobs.model import job_to_dict
from archon_search.server.schemas import ErrorDetail, JobResponse
from archon_search.types import JobStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sync"])

_SYNC_ERROR_RESPONSES = {
    401: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
}


async def _sync_task(
    job: "SyncJob",
    job_store: "JobStore",
    collection_sync: "SearchCollectionSync",
    collections: list[str],
    lock: asyncio.Lock,
) -> None:
    """Drive a SyncJob from RUNNING to DONE or FAILED.

    The caller is responsible for transitioning the job to RUNNING before
    invoking this coroutine. The ``sync_lock`` is released in the ``finally``
    block — regardless of whether sync() raises — so a second POST /sync
    always finds the lock free after this task exits (S23).
    """
    job_id = job.job_id
    try:
        result = await collection_sync.sync(collections)
        job_store.update(
            job_id,
            status=JobStatus.DONE,
            result={
                "added": result.added,
                "removed": result.removed,
                "unchanged": result.unchanged,
                "errors": result.errors,
                "skipped": result.skipped,
                "updated": result.updated,
            },
        )
    except Exception as exc:
        logger.exception("_sync_task: job %s failed", job_id)
        try:
            job_store.update(job_id, status=JobStatus.FAILED, error=str(exc))
        except (KeyError, OSError):
            logger.error(
                "_sync_task: could not persist FAILED status for job %s", job_id
            )
    finally:
        lock.release()


@router.post(
    "/sync",
    name="trigger_sync",
    status_code=202,
    response_model=JobResponse,
    responses=_SYNC_ERROR_RESPONSES,
)
async def trigger_sync(request: Request) -> JSONResponse:
    """Enqueue an async full-collection sync — CSP120 BE-3.

    Syncs ALL server-configured collections (pinned_collections + collections)
    via ``SearchCollectionSync.sync()``. No request body needed (C2).

    Returns:
    - 202: JobResponse-shaped body with status RUNNING.
    - 401: Missing or invalid Bearer token.
    - 409: A sync is already in progress.
    """
    job_store: "JobStore" = request.app.state.job_store
    collection_sync: "SearchCollectionSync" = request.app.state.collection_sync
    config = request.app.state.config
    lock: asyncio.Lock = request.app.state.sync_lock

    # Non-blocking acquire: check lock.locked() then acquire immediately.
    # In a single-threaded async event loop, there is no TOCTOU window between
    # lock.locked() and lock.acquire() because no other coroutine can run
    # between two non-await expressions.
    if lock.locked():
        return JSONResponse(
            {"detail": "sync already in progress"},
            status_code=409,
        )
    await lock.acquire()

    # Lock is now held by this coroutine. From here on the lock is released
    # only inside _sync_task's finally block (so a task failure still frees it).
    try:
        job = job_store.create_sync(namespace=request.state.namespace)
    except OSError:
        lock.release()
        return JSONResponse({"detail": "internal error"}, status_code=500)

    # Transition QUEUED -> RUNNING before spawning (202 body reports RUNNING, not QUEUED).
    running_job = job_store.transition(job.job_id, {JobStatus.QUEUED}, JobStatus.RUNNING)
    if running_job is None:
        logger.error("trigger_sync: failed to transition job %s to RUNNING", job.job_id)
        lock.release()
        return JSONResponse({"detail": "internal error"}, status_code=500)

    # Build the collections list: pinned_collections + collections (mirrors old CLI sync.py).
    all_collections = list(config.pinned_collections) + list(config.collections)

    try:
        task = asyncio.create_task(
            _sync_task(
                job=running_job,
                job_store=job_store,
                collection_sync=collection_sync,
                collections=all_collections,
                lock=lock,
            )
        )
        request.app.state._background_tasks.add(task)
        task.add_done_callback(request.app.state._background_tasks.discard)
    except Exception:
        lock.release()
        logger.error("trigger_sync: failed to spawn _sync_task for job %s", running_job.job_id)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    return JSONResponse(job_to_dict(running_job), status_code=202)

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

Exemption: ``app.state.sync_lock`` only serialises this route and the lifespan's
startup sync (``app.py``) — a watcher-driven sync (``app.py``'s ``_watch_callback``
-> ``collection_sync.sync_collection()``) does not take the lock and can run
concurrently with either.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.datastructures import State

    from archon_search.jobs.store import JobStore
    from archon_search.sync import SearchCollectionSync
    from archon_search.types import SyncJob

from archon_search.jobs.model import job_to_dict
from archon_search.server.schemas import ErrorDetail, JobResponse, StartupSyncResult
from archon_search.sync_suppression import clear_sync_suppressed_sentinel
from archon_search.types import JobStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sync"])

_SYNC_ERROR_RESPONSES = {
    401: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
}

# Sanitized, wire-facing ``error`` value for a SyncJob that fails via _sync_task —
# it is readable through GET /jobs, so no exception text goes in here (CLAUDE.md:
# "Never put str(exc) in a wire-facing detail or job error field"). The
# logger.exception() call at the raise site keeps the detail for operators.
_SYNC_TASK_ERROR_FAILED = "sync failed"


def _clear_degraded_sync_state(app_state: "State") -> None:
    """Clear sticky startup-sync degradation after a clean manual sync.

    A clean ``POST /sync`` (no exception, no per-collection errors) is the
    sanctioned resume path for BOTH degraded states the startup sync can leave
    behind: ``SUPPRESSED`` (the crash-loop guard fired) and ``FAILED`` (the
    startup sync itself failed or timed out). Clearing is symmetric on
    purpose — ``sync_result`` and ``_startup_sync_failed`` always move
    together, never one without the other, because ``GET /ready`` reads
    ``_startup_sync_failed`` and ``GET /status`` reads ``sync_result``: leaving
    them out of sync would make the two endpoints contradict each other.

    The sticky sentinel (``sync_suppression.py``) is the durable half of the
    ``SUPPRESSED`` state, so it is cleared here too — unconditionally
    (``unlink(missing_ok=True)`` internally), since a stale sentinel from a
    ``FAILED``-only prior state is harmless to attempt-clear.

    No-op when ``sync_result`` is anything else (``None``, ``PENDING``,
    ``DONE``, or absent) — ``sync_result`` is the *startup* sync's record, and
    only these two degraded states have a sanctioned resume.
    """
    if getattr(app_state, "sync_result", None) in (
        StartupSyncResult.SUPPRESSED,
        StartupSyncResult.FAILED,
    ):
        app_state.sync_result = StartupSyncResult.DONE
        app_state._startup_sync_failed = False
    # Outside the branch on purpose. The boot guard is gated on ``all_cols``
    # (``app.py``), so a server with no collections configured never reaches
    # SUPPRESSED — a sentinel left behind by an earlier configuration would
    # otherwise be unclearable by the sanctioned resume path and would suppress
    # the first boot after collections are added back. ``unlink(missing_ok=True)``
    # makes this a no-op when there is nothing to clear.
    clear_sync_suppressed_sentinel()


async def _sync_task(
    job: "SyncJob",
    job_store: "JobStore",
    collection_sync: "SearchCollectionSync",
    collections: list[str],
    lock: asyncio.Lock,
    app_state: "State",
) -> None:
    """Drive a SyncJob from RUNNING to DONE, FAILED, or CANCELLED.

    The caller is responsible for transitioning the job to RUNNING before
    invoking this coroutine. The ``sync_lock`` is released in the ``finally``
    block — regardless of how the task exits — so a second POST /sync always
    finds the lock free after this task exits (S23).

    ``asyncio.CancelledError`` is caught explicitly, before ``except
    Exception`` (which does not catch it — it is a ``BaseException``): an
    ordinary clean shutdown during a manual sync must record ``CANCELLED``,
    not leave the job ``RUNNING`` forever. A ``RUNNING`` row left behind by a
    plain shutdown would be rewritten to ``FAILED / "process_restart"`` on the
    next boot — a false crash marker that arms the (sticky, since fix-brief-C
    item 1) crash-loop guard for a shutdown that was never a crash.

    See ``_clear_degraded_sync_state`` for the ``app_state`` write on a clean
    sync.
    """
    job_id = job.job_id
    try:
        result = await collection_sync.sync(collections)
        if not result.errors:
            _clear_degraded_sync_state(app_state)
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
    except asyncio.CancelledError:
        try:
            job_store.update(job_id, status=JobStatus.CANCELLED)
        except (KeyError, OSError):
            logger.error(
                "_sync_task: could not persist CANCELLED status for job %s", job_id
            )
        raise
    except Exception:
        logger.exception("_sync_task: job %s failed", job_id)
        try:
            job_store.update(job_id, status=JobStatus.FAILED, error=_SYNC_TASK_ERROR_FAILED)
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
                app_state=request.app.state,
            )
        )
        request.app.state._background_tasks.add(task)
        task.add_done_callback(request.app.state._background_tasks.discard)
    except Exception:
        lock.release()
        logger.error("trigger_sync: failed to spawn _sync_task for job %s", running_job.job_id)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    return JSONResponse(job_to_dict(running_job), status_code=202)

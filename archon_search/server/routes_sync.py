"""POST /sync — trigger a full synchronisation of all server-configured collections.

Mirrors ``routes_maintenance.py`` file structure. The route:

  1. Takes ``app.state.sync_lock`` without waiting (``_try_acquire``), else
     answers 409 "sync already in progress".
  2. Marks every legacy QUEUED/PENDING SyncJob FAILED (best-effort): older
     servers created SyncJobs QUEUED first, and nothing schedules one, so such
     a row would never run and never be evicted. DELETE cannot clear it either:
     it answers 409 for any non-terminal SyncJob (``routes_jobs.delete_job``).
  3. Creates the SyncJob directly RUNNING in one write, like
     ``rebuild_communities`` (``routes_graph.py``) — never QUEUED.
  4. Spawns ``_sync_task`` as a background task (lock still held).
  5. Returns 202 with the RUNNING job body.

Any exception (``Exception``) in step 3 or 4 releases the lock and answers the
sanitized 500. A spawn failure first marks the already-saved job FAILED
(best-effort, ``_persist_job_status``), so no RUNNING job is left without a task.

Once the task is spawned, ``_sync_task`` releases the lock in a ``finally``
block, so any sync() exception (``OSError``, ``KeyError``, or any other) always
frees the lock (S23).

Exemption: ``app.state.sync_lock`` only serialises this route and the lifespan's
startup sync (``app.py``) — a watcher-driven sync (``app.py``'s ``_watch_callback``
-> ``collection_sync.sync_collection()``) does not take the lock and can run
concurrently with either.

A sync creates no collection-scoped job, so it guards ``DELETE /collections/{name}`` itself: ``sync()`` registers in
``SearchStore.collection_writer`` around each collection's write and skips a collection the live config no longer
holds (``SearchCollectionSync._write_collection``); it also refuses to drop a collection gone from the config while a
job or writer may still write it (``SearchCollectionSync._drop_if_idle``).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.datastructures import State

    from archon_search.jobs.store import JobStore
    from archon_search.sync import SearchCollectionSync

from archon_search.jobs.model import job_to_dict
from archon_search.server.authz import ADMIN_ONLY_RESPONSES, require_admin_dependency
from archon_search.server.routes_jobs import (
    INTERNAL_ERROR_DETAIL,
    JOB_SUPERSEDED_DETAIL,
    _persist_job_status,
)
from archon_search.server.schemas import ErrorDetail, JobResponse, StartupSyncResult
from archon_search.sync_suppression import clear_sync_suppressed_sentinel
from archon_search.types import JobStatus, SyncJob

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sync"])

_SYNC_ERROR_RESPONSES = {
    401: {"model": ErrorDetail},
    409: {"model": ErrorDetail},
    500: {
        "model": ErrorDetail,
        "description": f'Creating or starting the sync job failed (body {{"detail": "{INTERNAL_ERROR_DETAIL}"}})',
    },
}

# Sanitized, wire-facing ``error`` value for a SyncJob that fails — in _sync_task,
# or in trigger_sync when the task cannot be spawned. It is readable through
# GET /jobs, so no exception text goes in here (CLAUDE.md: "Never put str(exc) in
# a wire-facing detail or job error field"). The logger.exception() call at each
# failure site keeps the detail for operators.
_SYNC_FAILED_DETAIL = "sync failed"
#: Log-line prefixes naming the writer of a best-effort job status write (see ``_persist_job_status``).
_SYNC_TASK_LOG_CONTEXT = "_sync_task"
_TRIGGER_SYNC_LOG_CONTEXT = "trigger_sync"
#: SyncJob statuses only an older server wrote (it created SyncJobs QUEUED first); nothing runs such a job.
#: ``JobStore._load`` already fails a QUEUED one at startup, so QUEUED here is a defensive leftover.
_LEGACY_SYNC_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.PENDING})


async def _try_acquire(lock: asyncio.Lock) -> bool:
    """Acquire *lock* only if that needs no wait; return whether it was acquired.

    ``locked()`` alone is not enough: a released lock is handed to its first queued
    waiter, so right after such a release ``locked()`` is False yet ``acquire()``
    queues. This relies on two asyncio behaviours: ``asyncio.Lock.acquire()`` takes
    a free, waiter-less lock on a fast path that never suspends, and
    ``asyncio.timeout(0)`` fires only via ``loop.call_soon``, i.e. after the body
    suspends. So the fast path always succeeds, and a queued acquire is cancelled;
    the cancelled acquire leaves the lock to the queued waiter.

    Not exact: in the same loop pass a handover to an acquire of our own that is
    still queued can make this return False although the lock ends up free. The
    lock stays consistent (the cancelled acquire passes it on, or leaves it free),
    so the caller's 409 is spurious only, and a retry succeeds.
    """
    try:
        async with asyncio.timeout(0):
            await lock.acquire()
    except TimeoutError:
        return False
    return True


def _supersede_legacy_sync_jobs(job_store: JobStore) -> None:
    """Mark every legacy QUEUED/PENDING SyncJob FAILED (best-effort), so none stays non-terminal forever.

    A RUNNING SyncJob is never touched: the startup sync creates its job RUNNING before it waits for the lock.
    """
    for job in job_store.list():
        if isinstance(job, SyncJob) and job.status in _LEGACY_SYNC_STATUSES:
            _persist_job_status(
                job_store, job.job_id, JobStatus.FAILED, context=_TRIGGER_SYNC_LOG_CONTEXT, error=JOB_SUPERSEDED_DETAIL
            )


def _clear_degraded_sync_state(app_state: State) -> None:
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
    job: SyncJob,
    job_store: JobStore,
    collection_sync: SearchCollectionSync,
    collections: list[str],
    lock: asyncio.Lock,
    app_state: State,
) -> None:
    """Drive a SyncJob from RUNNING to DONE, FAILED, or CANCELLED.

    The caller is responsible for creating the job RUNNING before invoking
    this coroutine. The ``sync_lock`` is released in the ``finally``
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

    Every terminal status write is best-effort (``_persist_job_status``): a
    failed write is logged and suppressed, so it never escapes this
    fire-and-forget task, never replaces a ``CancelledError``, and a failed
    ``DONE`` write never turns a successful sync into ``FAILED``.
    """
    job_id = job.job_id
    try:
        result = await collection_sync.sync(collections)
        if not result.errors:
            _clear_degraded_sync_state(app_state)
    except asyncio.CancelledError:
        _persist_job_status(job_store, job_id, JobStatus.CANCELLED, context=_SYNC_TASK_LOG_CONTEXT)
        raise
    except Exception:
        logger.exception("_sync_task: job %s failed", job_id)
        _persist_job_status(
            job_store, job_id, JobStatus.FAILED, context=_SYNC_TASK_LOG_CONTEXT, error=_SYNC_FAILED_DETAIL
        )
    else:
        _persist_job_status(
            job_store,
            job_id,
            JobStatus.DONE,
            context=_SYNC_TASK_LOG_CONTEXT,
            result={
                "added": result.added,
                "removed": result.removed,
                "unchanged": result.unchanged,
                "errors": result.errors,
                "skipped": result.skipped,
                "updated": result.updated,
            },
        )
    finally:
        lock.release()


def _spawn_sync_task(
    job: SyncJob,
    job_store: JobStore,
    collection_sync: SearchCollectionSync,
    collections: list[str],
    lock: asyncio.Lock,
    app_state: State,
) -> bool:
    """Spawn and track ``_sync_task`` for *job*, which then owns the held *lock*; return whether it was spawned.

    On a spawn failure (any ``Exception``) roll back instead and return ``False``: the
    coroutine never started, so its ``finally`` never runs — close it (no "never
    awaited" warning), mark the already-saved job FAILED (best-effort, so no RUNNING
    job is left without a task), and release *lock* here.
    """
    sync_coro = _sync_task(
        job=job,
        job_store=job_store,
        collection_sync=collection_sync,
        collections=collections,
        lock=lock,
        app_state=app_state,
    )
    try:
        task = asyncio.create_task(sync_coro)
    except Exception:
        sync_coro.close()
        logger.exception("trigger_sync: failed to spawn _sync_task for job %s", job.job_id)
        _persist_job_status(
            job_store, job.job_id, JobStatus.FAILED, context=_TRIGGER_SYNC_LOG_CONTEXT, error=_SYNC_FAILED_DETAIL
        )
        lock.release()
        return False
    app_state._background_tasks.add(task)
    task.add_done_callback(app_state._background_tasks.discard)
    return True


@router.post(
    "/sync",
    name="trigger_sync",
    status_code=202,
    response_model=JobResponse,
    responses={**_SYNC_ERROR_RESPONSES, **ADMIN_ONLY_RESPONSES},
    dependencies=[Depends(require_admin_dependency)],
)
async def trigger_sync(request: Request) -> JSONResponse:
    """Start an async full-collection sync — CSP120 BE-3.

    Syncs ALL server-configured collections (pinned_collections + collections)
    via ``SearchCollectionSync.sync()``. No request body needed (C2).

    Returns:
    - 202: JobResponse-shaped body with status RUNNING.
    - 401: Missing or invalid Bearer token.
    - 409: A sync is already in progress.
    - 500: The job could not be saved or its task could not be started
      (sanitized ``{"detail": "internal error"}``); a retry is accepted (no 409).
    """
    job_store: JobStore = request.app.state.job_store
    collection_sync: SearchCollectionSync = request.app.state.collection_sync
    config = request.app.state.config
    lock: asyncio.Lock = request.app.state.sync_lock

    if not await _try_acquire(lock):
        return JSONResponse(
            {"detail": "sync already in progress"},
            status_code=409,
        )

    # Lock is now held by this coroutine. Every early return below releases it;
    # once the task is spawned, _sync_task's finally block releases it.
    _supersede_legacy_sync_jobs(job_store)
    # Create the job RUNNING in one write (the 202 body reports RUNNING): nothing
    # schedules a SyncJob, so a QUEUED one would never run.
    try:
        running_job = job_store.create_sync(namespace=request.state.namespace)
    except Exception:
        logger.exception("trigger_sync: failed to persist a SyncJob")
        lock.release()
        return JSONResponse({"detail": INTERNAL_ERROR_DETAIL}, status_code=500)

    # Build the collections list: pinned_collections + collections (mirrors old CLI sync.py).
    all_collections = list(config.pinned_collections) + list(config.collections)
    spawned = _spawn_sync_task(
        job=running_job,
        job_store=job_store,
        collection_sync=collection_sync,
        collections=all_collections,
        lock=lock,
        app_state=request.app.state,
    )
    if not spawned:
        return JSONResponse({"detail": INTERNAL_ERROR_DETAIL}, status_code=500)
    return JSONResponse(job_to_dict(running_job), status_code=202)

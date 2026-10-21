"""Pre-acquire per-collection ingest lock for REST handlers.

Shared lock-acquisition logic of POST /ingest, POST /collections/, DELETE /collections/{name},
POST /collections/{name}/reindex, POST /collections/{name}/reindex-metadata and
POST /graph/{collection}/rebuild-communities (the last three through ``_collection_job.create_collection_job``,
which creates their job and its meta pointer under the lock).
On timeout it builds a 503 with a ``Retry-After`` header and the body
``{"detail": "reindex in progress; retry after Retry-After seconds", "code": "store_busy"}``:
every caller but POST /collections/ returns that response unchanged, while
POST /collections/ rolls back its config entry and stub meta row and answers with its own
body ``{"detail": "store busy; retry in N seconds"}`` (N = the same ``Retry-After`` value).
:func:`store_busy_503` also builds DELETE /collections/{name}'s 503 refusal while a job may still be writing to the collection.
"""
from __future__ import annotations

import asyncio
import math

from fastapi.responses import JSONResponse

import archon_search.constants as _constants

#: Wire ``code`` of the 503s built by :func:`store_busy_503`: the lock-timeout refusal of every
#: :func:`acquire_collection_lock_or_503` caller, and DELETE /collections/{name}'s job-in-progress refusal.
#: POST /collections/ answers its own busy 503 body without a ``code``.
STORE_BUSY_CODE = "store_busy"
#: OpenAPI description of the 503 :func:`acquire_collection_lock_or_503` answers when the lock wait times out.
STORE_BUSY_503_DESCRIPTION = "Store busy (code store_busy): the collection lock was not acquired in time"


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
        return store_busy_503("reindex in progress; retry after Retry-After seconds")
    return lock

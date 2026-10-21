"""Pre-acquire per-collection ingest lock for REST handlers.

Shared lock-acquisition logic of POST /ingest, POST /collections/ and DELETE /collections/{name}.
On timeout it builds a 503 with a ``Retry-After`` header and the body
``{"detail": "reindex in progress; retry after Retry-After seconds", "code": "store_busy"}``:
POST /ingest and DELETE /collections/{name} return that response unchanged, while
POST /collections/ rolls back its config entry and stub meta row and answers with its own
body ``{"detail": "store busy; retry in N seconds"}`` (N = the same ``Retry-After`` value).
:func:`store_busy_503` also builds DELETE /collections/{name}'s 503 refusal while a base ingest job is still writing to the collection.
"""
from __future__ import annotations

import asyncio
import math

from fastapi.responses import JSONResponse

import archon_search.constants as _constants

#: Wire ``code`` of the 503s built by :func:`store_busy_503`: POST /ingest's and DELETE /collections/{name}'s
#: busy-collection refusals. POST /collections/ answers its own busy 503 body without a ``code``.
STORE_BUSY_CODE = "store_busy"


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

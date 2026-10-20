"""Pre-acquire per-collection ingest lock for REST handlers.

Shared lock-acquisition logic of POST /ingest, POST /collections/ and DELETE /collections/{name}.
On timeout it builds a 503 with a ``Retry-After`` header and the body
``{"detail": "reindex in progress; retry after Retry-After seconds", "code": "store_busy"}``:
POST /ingest and DELETE /collections/{name} return that response unchanged, while
POST /collections/ rolls back its config entry and stub meta row and answers with its own
body ``{"detail": "store busy; retry in N seconds"}`` (N = the same ``Retry-After`` value).
"""
from __future__ import annotations

import asyncio
import math

from fastapi.responses import JSONResponse

import archon_search.constants as _constants


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
        retry_after = str(math.ceil(_constants.INGEST_LOCK_TIMEOUT_S))
        return JSONResponse(
            {
                "detail": "reindex in progress; retry after Retry-After seconds",
                "code": "store_busy",
            },
            status_code=503,
            headers={"Retry-After": retry_after},
        )
    return lock

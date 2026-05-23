"""Pre-acquire per-collection ingest lock for REST handlers.

Factored out so both POST /ingest and POST /collections/ share the same
lock-acquisition logic and 503 response shape.
"""
from __future__ import annotations

import asyncio
import math

from fastapi.responses import JSONResponse

import archon_search.constants as _constants

# Module-level alias so tests can monkeypatch this value to speed up timeouts.
# The Retry-After header always reflects the canonical constant value.
INGEST_LOCK_TIMEOUT_S = _constants.INGEST_LOCK_TIMEOUT_S


async def acquire_collection_lock_or_503(
    store,
    collection_name: str,
):
    """Pre-acquire the per-collection ingest lock.

    Returns:
        The acquired asyncio.Lock on success.
        A 503 JSONResponse on acquisition timeout (Retry-After uses the canonical constant).
        None when store is unavailable (handler proceeds best-effort).
    """
    if store is None:
        return None

    lock = store._lock_for(collection_name)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)
    except asyncio.TimeoutError:
        # Retry-After always uses the canonical constant so clients get a stable hint.
        retry_after = str(math.ceil(_constants.INGEST_LOCK_TIMEOUT_S))
        return JSONResponse(
            {
                "error": "store_busy",
                "detail": "reindex in progress; retry after Retry-After seconds",
            },
            status_code=503,
            headers={"Retry-After": retry_after},
        )
    return lock

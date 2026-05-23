"""_ingest_lock — helper for pre-acquiring per-collection locks in ingest handlers (A5c).

Provides ``acquire_collection_lock_or_503`` which tries to acquire the
per-collection lock from ``SearchStore._lock_for(collection)`` within
``INGEST_LOCK_TIMEOUT_S`` seconds.  Returns the acquired lock on success,
a 503 ``JSONResponse`` on timeout (caller returns it directly), or ``None``
when the store is unavailable (test/stub paths — proceed without locking).
"""
from __future__ import annotations

import asyncio
import math
from typing import Union

from fastapi.responses import JSONResponse

from archon_search.constants import INGEST_LOCK_TIMEOUT_S

INGEST_LOCK_RETRY_AFTER = str(math.ceil(INGEST_LOCK_TIMEOUT_S))


async def acquire_collection_lock_or_503(
    search_store: object,
    collection: str,
) -> Union[asyncio.Lock, JSONResponse, None]:
    """Try to pre-acquire the per-collection lock for *collection*.

    Returns:
      - The acquired ``asyncio.Lock`` on success (caller releases it in a finally).
      - A 503 ``JSONResponse`` with ``Retry-After`` header on timeout.
      - ``None`` when *search_store* is ``None`` or has no ``_lock_for`` method
        (test/stub paths) — handler treats as best-effort and proceeds without lock.
    """
    if search_store is None or not hasattr(search_store, "_lock_for"):
        return None

    lock: asyncio.Lock = search_store._lock_for(collection)  # type: ignore[union-attr]
    try:
        await asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)
    except asyncio.TimeoutError:
        return JSONResponse(
            content={
                "error": "store_busy",
                "detail": "reindex in progress; retry after Retry-After seconds",
            },
            status_code=503,
            headers={"Retry-After": INGEST_LOCK_RETRY_AFTER},
        )
    return lock

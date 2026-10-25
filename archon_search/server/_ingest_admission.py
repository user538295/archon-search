"""Admission cap on the ingest jobs a REST handler starts (``[ingest].max_concurrent_tasks``).

``POST /ingest`` and ``POST /collections/`` each start a background task that parses, embeds and writes. The jobs are
not queued: a request that would exceed the cap is refused with ``503`` ``ingest_busy`` + ``Retry-After`` before it
creates a job, takes a collection lock or touches the config (the convention of every other retryable 503 here).

A request :meth:`IngestAdmission.reserve`\\ s a slot, then either hands it to the task it starts
(:func:`~archon_search.server.routes_jobs._track_background_task` releases it when the task ends, however it ends) or
releases it when it returns without starting one.
"""
from __future__ import annotations

import math
from typing import Any

from fastapi.responses import JSONResponse

import archon_search.constants as _constants

INGEST_BUSY_CODE = "ingest_busy"
INGEST_BUSY_DETAIL = "too many ingest jobs are running; retry after Retry-After seconds"
INGEST_BUSY_503_DESCRIPTION = "Ingest busy (code ingest_busy): [ingest].max_concurrent_tasks jobs are already running"


class IngestSlot:
    """One reserved slot; :meth:`release` is idempotent."""

    def __init__(self, admission: IngestAdmission) -> None:
        self._admission: IngestAdmission | None = admission
        self.attached = False

    def release(self) -> None:
        if self._admission is not None:
            self._admission._active -= 1
            self._admission = None

    def release_unless_attached(self) -> None:
        """Release a slot no task took over (the request ended without starting one)."""
        if not self.attached:
            self.release()


class IngestAdmission:
    def __init__(self) -> None:
        self._active = 0

    def reserve(self, limit: int) -> IngestSlot | None:
        """A slot while fewer than *limit* are held, else ``None``. Synchronous, so reservations never interleave."""
        if self._active >= limit:
            return None
        self._active += 1
        return IngestSlot(self)


def reserve_ingest_slot(app_state: Any) -> IngestSlot | None:
    """Reserve a slot on *app_state*'s admission (created on first use); the cap is read live from its config."""
    admission: IngestAdmission | None = getattr(app_state, "ingest_admission", None)
    if admission is None:
        admission = IngestAdmission()
        app_state.ingest_admission = admission
    config = getattr(app_state, "config", None)
    limit = config.ingest.max_concurrent_tasks if config is not None else _constants.DEFAULT_MAX_CONCURRENT_INGEST_TASKS
    return admission.reserve(limit)


def ingest_busy_503() -> JSONResponse:
    """The 503 refusing an ingest request while the cap is reached; ``Retry-After`` is the lock-wait polling hint."""
    return JSONResponse(
        {"detail": INGEST_BUSY_DETAIL, "code": INGEST_BUSY_CODE},
        status_code=503,
        headers={"Retry-After": str(math.ceil(_constants.INGEST_LOCK_TIMEOUT_S))},
    )

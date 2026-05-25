"""Per-request correlation IDs and per-stage latency recording (B1)."""
from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Generator

logger = logging.getLogger("archon.search")

# ---------------------------------------------------------------------------
# ContextVars
# ---------------------------------------------------------------------------

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_stage_recorder: ContextVar[StageRecorder | None] = ContextVar("_stage_recorder", default=None)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def sanitize_request_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    if _REQUEST_ID_RE.fullmatch(raw):
        return raw
    return None


# ---------------------------------------------------------------------------
# StageRecorder
# ---------------------------------------------------------------------------


class StageRecorder:
    """Accumulates per-stage perf_counter() timings as lists."""

    def __init__(self) -> None:
        self._timings: dict[str, list[float]] = {}

    def record(self, name: str, elapsed_ms: float) -> None:
        if name not in self._timings:
            self._timings[name] = []
        self._timings[name].append(elapsed_ms)
        if len(self._timings[name]) > 1:
            logger.debug("StageRecorder: stage %r recorded more than once", name)

    @property
    def stage_timings_ms(self) -> dict[str, float]:
        return {k: v[-1] for k, v in self._timings.items()}

    @property
    def stage_sums_ms(self) -> dict[str, float]:
        return {k: sum(vs) for k, vs in self._timings.items()}


# ---------------------------------------------------------------------------
# Context managers
# ---------------------------------------------------------------------------


@contextmanager
def record_stage(name: str) -> Generator[None, None, None]:
    recorder = _stage_recorder.get()
    if recorder is None:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        recorder.record(name, (time.perf_counter() - t0) * 1000.0)


@contextmanager
def bind_stage_recorder() -> Generator[StageRecorder, None, None]:
    recorder = StageRecorder()
    token = _stage_recorder.set(recorder)
    try:
        yield recorder
    finally:
        _stage_recorder.reset(token)

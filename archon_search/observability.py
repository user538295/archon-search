"""Per-request correlation IDs and per-stage latency recording (B1)."""
from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Generator

logger = logging.getLogger(__name__)

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
        # (start, end) perf_counter() spans of paused_stage_recorder() blocks.
        self._pauses: list[tuple[float, float]] = []

    def record_pause(self, started_at: float, ended_at: float) -> None:
        """Record a :func:`paused_stage_recorder` block's span (``perf_counter()`` values)."""
        self._pauses.append((started_at, ended_at))

    def record_total(self, started_at: float) -> None:
        """Record ``total`` as the wall time since *started_at* (a ``perf_counter()`` value).

        The part of every paused span that falls after *started_at* is subtracted, so a
        paused block counts as if it had run outside the timed window; a pause that
        ended before *started_at* takes nothing off.
        """
        now = time.perf_counter()
        paused = sum(max(0.0, end - max(start, started_at)) for start, end in self._pauses)
        self.record("total", (now - started_at - paused) * 1000.0)

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
def paused_stage_recorder() -> Generator[None, None, None]:
    """Run a block that is not a retrieval stage off the bound recorder.

    Nothing inside the block is recorded (HyDE's hypothesis ``embed`` included), and
    its wall time is excluded from :meth:`StageRecorder.record_total`. A no-op when
    no recorder is bound.
    """
    recorder = _stage_recorder.get()
    if recorder is None:
        yield
        return
    token = _stage_recorder.set(None)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _stage_recorder.reset(token)
        recorder.record_pause(t0, time.perf_counter())


@contextmanager
def bind_stage_recorder() -> Generator[StageRecorder, None, None]:
    recorder = StageRecorder()
    token = _stage_recorder.set(recorder)
    try:
        yield recorder
    finally:
        _stage_recorder.reset(token)

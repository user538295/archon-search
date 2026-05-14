"""TelemetryWriter — async-safe JSONL writer for FEAT-039b Task 2.1.

Provides a bounded-queue producer/consumer pair: hot-path callers invoke the
synchronous `enqueue()`, and a single background drain task serializes entries
and appends them to a daily UTC-dated `.jsonl` file in `log_dir`.

Design constraints:
- `enqueue()` is intentionally synchronous (`def`, not `async def`). The
  full-queue drop-and-replace dance (`get_nowait` → `task_done` → `put_nowait`)
  must be atomic with respect to other tasks — no `await` may be interleaved.
- The `task_done()` on the drop path is required so `queue.join()` stays
  balanced; without it, `drain_and_stop()` would hang on shutdown.
- On unexpected exceptions, the drain task crashes and the failure is
  observable via `task.exception()`. Subsequent `enqueue()` calls silently
  drop into the queue with no consumer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from archon_search.telemetry.entry import TelemetryEntry

_logger = logging.getLogger("archon.search")

# Rate-limit window for warnings (one warning per kind per window).
_WARN_WINDOW_S = 60.0


class TelemetryWriter:
    """Append-only JSONL writer with bounded queue and graceful drain."""

    def __init__(
        self,
        log_dir: Path,
        *,
        queue_size: int = 1024,
        drain_timeout_s: float = 2.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._log_dir = log_dir
        self._drain_timeout_s = drain_timeout_s
        self._clock = clock
        self._queue: asyncio.Queue[TelemetryEntry] = asyncio.Queue(maxsize=queue_size)
        self._stopped: bool = False
        self._task: asyncio.Task[None] | None = None
        self._dir_ensured: bool = False
        # Rate-limit state: kind → last-emit monotonic timestamp.
        self._warn_last: dict[str, float] = {}

    # -- public API ------------------------------------------------------

    def enqueue(self, entry: TelemetryEntry) -> None:
        """Hot-path entry into the writer queue.

        MUST remain synchronous. The full-queue branch performs a
        drop-oldest/task_done/put_new sequence that must be atomic; inserting
        an `await` between any of these calls would race with the drain loop.
        """
        if self._stopped:
            self._warn_rate_limited(
                "after-stop", "telemetry: enqueue after stop dropped entry"
            )
            return
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:  # pragma: no cover — defensive
                pass
            try:
                self._queue.put_nowait(entry)
            except asyncio.QueueFull:  # pragma: no cover — defensive
                pass
            self._warn_rate_limited(
                "dropped", "telemetry: queue full, dropped oldest entry"
            )

    async def start(self) -> asyncio.Task[None]:
        """Create and return the background drain task."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="telemetry-writer")
        return self._task

    async def drain_and_stop(self) -> None:
        """Idempotent: flush pending entries (bounded by drain_timeout_s) and stop."""
        if self._stopped and self._task is None:
            return
        self._stopped = True
        if self._task is None:
            return
        try:
            await asyncio.wait_for(
                self._queue.join(), timeout=self._drain_timeout_s
            )
        except TimeoutError:
            unfinished = self._queue.qsize()
            _logger.warning(
                "telemetry: drain timed out with %d unfinished entries", unfinished
            )
        # Cancel the drain task in either case.
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        # If the task crashed with a non-cancellation exception, surface it.
        if self._task.done() and not self._task.cancelled():
            exc = self._task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                _logger.warning("telemetry: drain task crashed: %r", exc)
        self._task = None

    # -- internals -------------------------------------------------------

    async def _run(self) -> None:
        while True:
            entry = await self._queue.get()
            try:
                now = self._clock()
                payload = self._serialize(entry)
                self._append(now, payload)
            except (OSError, ValueError) as exc:
                self._warn_rate_limited(
                    "io-error", f"telemetry: write failed ({exc!r}); dropping entry"
                )
            finally:
                self._queue.task_done()

    def _serialize(self, entry: TelemetryEntry) -> bytes:
        return (
            json.dumps(entry.model_dump(exclude_none=True), separators=(",", ":"))
            + "\n"
        ).encode("utf-8")

    def _file_for(self, when: datetime) -> Path:
        return self._log_dir / f"{when.date().isoformat()}.jsonl"

    def _append(self, when: datetime, payload: bytes) -> None:
        if not self._dir_ensured:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            self._dir_ensured = True
        path = self._file_for(when)
        with path.open("ab") as fh:
            fh.write(payload)

    def _warn_rate_limited(self, kind: str, message: str) -> None:
        now = time.monotonic()
        last = self._warn_last.get(kind, 0.0)
        if now - last >= _WARN_WINDOW_S:
            self._warn_last[kind] = now
            _logger.warning(message)

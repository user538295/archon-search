"""TelemetryWriter — async-safe JSONL writer.

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
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from archon_search.telemetry.entry import TelemetryEntry

_logger = logging.getLogger("archon.search")

# Maximum serialized byte size for a single JSONL entry.
MAX_ENTRY_BYTES = 8192

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
        # Persistent per-date file descriptor (rotate-only fsync per ADR-06).
        self._fd: int | None = None
        self._fd_date: str | None = None
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
        # The drain task has stopped, so no concurrent _append can touch the
        # fd. Flush + close the persistent fd best-effort (telemetry is
        # best-effort on shutdown) and always clear it.
        if self._fd is not None:
            try:
                os.fsync(self._fd)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._task = None

    # -- internals -------------------------------------------------------

    async def _run(self) -> None:
        while True:
            entry = await self._queue.get()
            try:
                now = self._clock()
                entry = self._truncate_to_fit(entry)
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
        current_date = when.date().isoformat()
        if self._fd_date != current_date:
            if self._fd is not None:
                # Rotation: fsync the old date's fd BEFORE closing it.
                try:
                    os.fsync(self._fd)
                except OSError:
                    # A failed rotation fsync must not leak the fd or wedge
                    # state. Best-effort close, clear state, and re-raise so
                    # _run's except (OSError, ValueError) swallows it; the next
                    # _append reopens lazily on the current date.
                    try:
                        os.close(self._fd)
                    except OSError:
                        pass
                    self._fd = None
                    self._fd_date = None
                    raise
                os.close(self._fd)
            # Rotate-only fsync per ADR-06; the persistent fd is the durability
            # boundary, so the O_CREAT open below is intentionally not per-line
            # synced (durable-write lint carve-out justified by that contract).
            self._fd = os.open(  # noqa: durable-write
                str(self._file_for(when)),
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                0o644,
            )
            self._fd_date = current_date
        os.write(self._fd, payload)

    def _truncate_to_fit(
        self, entry: TelemetryEntry, limit_bytes: int = MAX_ENTRY_BYTES
    ) -> TelemetryEntry:
        """Return entry unchanged if it fits; otherwise binary-search the largest
        result_doc_ids prefix that still serializes within limit_bytes.

        Raises ValueError if even an empty result_doc_ids list exceeds limit_bytes.
        """
        if len(self._serialize(entry)) <= limit_bytes:
            return entry

        # If there are no result_doc_ids to truncate, we cannot reduce the size.
        if entry.result_doc_ids is None:
            raise ValueError(
                "entry exceeds MAX_ENTRY_BYTES and has no result_doc_ids to truncate"
            )

        # Check whether common fields alone fit with empty doc_ids.
        base = entry.model_copy(update={"result_doc_ids": [], "truncated": True})
        if len(self._serialize(base)) > limit_bytes:
            raise ValueError(
                "entry exceeds MAX_ENTRY_BYTES even with empty result_doc_ids"
            )

        # Binary search for the largest prefix of result_doc_ids that fits.
        ids = entry.result_doc_ids or []
        lo, hi = 0, len(ids)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            candidate = entry.model_copy(
                update={"result_doc_ids": ids[:mid], "truncated": True}
            )
            if len(self._serialize(candidate)) <= limit_bytes:
                lo = mid
            else:
                hi = mid - 1

        return entry.model_copy(
            update={"result_doc_ids": ids[:lo], "truncated": True}
        )

    def _warn_rate_limited(self, kind: str, message: str) -> None:
        now = time.monotonic()
        last = self._warn_last.get(kind, 0.0)
        if now - last >= _WARN_WINDOW_S:
            self._warn_last[kind] = now
            _logger.warning(message)

"""Single-flight warm-up of a lazily loaded model, shared by ``Embedder`` and ``Reranker``.

Both model backends load their weights on first use. A request that pays that cost inside its
own timeout budget fails with a 504, so callers warm the model first, outside the budget.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: Ceiling on one warm-up build; past it the wait raises, the build itself keeps running.
WARMUP_TIMEOUT_SECONDS = 300.0


class WarmupTimeout(Exception):
    """The warm-up build outran ``WARMUP_TIMEOUT_SECONDS`` (the wait stopped, not the build).

    Deliberately not a ``TimeoutError``: callers use that to mean "I gave up waiting", which is a
    different condition from the build blowing its own ceiling.
    """


class WarmupCoordinator:
    """Builds one model at most once at a time, however many callers ask.

    One shared task does the build and every caller only awaits it through ``asyncio.shield``:

    - Concurrent callers join the in-flight build instead of each starting another.
    - A caller that bounds its own wait and gives up cancels nothing but that wait. The build
      keeps running and warms the model for later callers. ``asyncio.to_thread`` workers are not
      cancellable, so a build that outruns the ceiling also runs to completion in its thread
      (and, being non-daemon, delays executor shutdown).
    - A failed build is not remembered: the task is dropped once settled, so the next caller
      starts a fresh one.
    - The in-flight task belongs to one event loop. When the running loop differs from the one
      that started it, the stale task is cancelled and replaced.

    The build is raced against ``WARMUP_TIMEOUT_SECONDS`` (read on every build, so it is
    patchable); only that ceiling raises *timeout_error*.
    """

    def __init__(
        self,
        *,
        subject: str,
        is_warm: Callable[[], bool],
        build: Callable[[], Awaitable[object]],
        timeout_error: type[WarmupTimeout],
    ) -> None:
        """*subject* names the model in the timeout message; *build* forces it to load."""
        self._subject = subject
        self._is_warm = is_warm
        self._build = build
        self._timeout_error = timeout_error
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._waiters = 0

    async def warmup(self) -> None:
        """Load the model now, off the request path; a no-op once warm.

        Raises:
            WarmupTimeout: (the *timeout_error* class) if the build outruns the ceiling.
            Exception: whatever the build raised; the next call retries.
        """
        if self._is_warm():
            return
        self._drop_task_of_another_loop()
        task = self._task
        if task is None or task.done():
            task = asyncio.ensure_future(self._run_build())
            task.add_done_callback(self._on_build_done)
            self._task = task
        self._waiters += 1
        try:
            await asyncio.shield(task)
        finally:
            self._waiters -= 1

    def _drop_task_of_another_loop(self) -> None:
        """Cancel and forget an in-flight build started on a different event loop."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            stale = self._task
            if stale is not None and not stale.done():
                stale.cancel()
            self._task = None
            self._loop = loop

    async def _run_build(self) -> None:
        """Run the build under the ceiling. Never cancelled by a caller."""
        build = asyncio.ensure_future(self._build())
        try:
            await asyncio.wait_for(build, timeout=WARMUP_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            # Since 3.11 asyncio.TimeoutError *is* the builtin TimeoutError, so a socket
            # ETIMEDOUT raised inside the build arrives here looking exactly like the ceiling
            # expiring. Only the ceiling cancels the build.
            if not build.cancelled():
                raise
            raise self._timeout_error(f"{self._subject} warm-up exceeded {WARMUP_TIMEOUT_SECONDS}s") from exc

    def _on_build_done(self, task: asyncio.Task[None]) -> None:
        """Drop the settled task; log a failure no waiter is left to receive.

        A build whose every waiter was cancelled has no one left to retrieve its exception. It is
        retrieved and logged here, so the failure is visible and asyncio does not warn "Task
        exception was never retrieved". When a waiter is still parked, it receives the exception.
        """
        if self._task is task:
            self._task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and self._waiters == 0:
            logger.warning(
                "%s warm-up failed after every caller stopped waiting: %s",
                self._subject,
                type(exc).__name__,
                exc_info=exc,
            )

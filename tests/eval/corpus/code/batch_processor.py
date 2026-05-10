"""Async batch processor: accumulate items and flush when full or timeout expires."""
from __future__ import annotations
import asyncio
from typing import Any, Callable, Coroutine


class BatchProcessor:
    """Collect items; flush them in a single batch call."""

    def __init__(
        self,
        flush_fn: Callable[[list[Any]], Coroutine[Any, Any, None]],
        max_size: int = 100,
        flush_interval: float = 1.0,
    ) -> None:
        self._flush_fn = flush_fn
        self._max_size = max_size
        self._interval = flush_interval
        self._queue: list[Any] = []
        self._task: asyncio.Task | None = None

    async def add(self, item: Any) -> None:
        self._queue.append(item)
        if len(self._queue) >= self._max_size:
            await self._flush()

    async def _flush(self) -> None:
        if not self._queue:
            return
        batch, self._queue = self._queue, []
        await self._flush_fn(batch)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._periodic_flush())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self._flush()

    async def _periodic_flush(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._flush()

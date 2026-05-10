"""Priority task queue backed by asyncio.PriorityQueue."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


@dataclass(order=True)
class Task:
    priority: int
    name: str = field(compare=False)
    fn: Callable[..., Coroutine[Any, Any, None]] = field(compare=False)
    args: tuple = field(default_factory=tuple, compare=False)


class TaskQueue:
    """Async priority queue; lower priority number = higher urgency."""

    def __init__(self, workers: int = 4) -> None:
        self._q: asyncio.PriorityQueue[Task] = asyncio.PriorityQueue()
        self._workers = workers
        self._tasks: list[asyncio.Task] = []

    async def enqueue(self, task: Task) -> None:
        await self._q.put(task)

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._worker()) for _ in range(self._workers)
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _worker(self) -> None:
        while True:
            task = await self._q.get()
            try:
                await task.fn(*task.args)
            finally:
                self._q.task_done()

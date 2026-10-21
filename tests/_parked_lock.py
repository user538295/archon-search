"""Helpers for tests that park a maintenance retry task on a held collection lock."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Set

import pytest

#: Event-loop iterations ``until_parked`` yields before it fails the test.
_PARK_ITERATIONS = 100


def parked_waiters(lock: asyncio.Lock) -> int:
    """How many tasks wait on *lock* (``asyncio.Lock._waiters``, the stdlib bookkeeping ``SearchStore.discard_lock``
    also reads)."""
    return len(lock._waiters or ())


async def until_parked(lock: asyncio.Lock) -> None:
    """Yield to the loop until a task waits on *lock*; fail if none does within a bounded number of iterations."""
    for _ in range(_PARK_ITERATIONS):
        if parked_waiters(lock):
            return
        await asyncio.sleep(0)
    pytest.fail("the retry task never waited on the collection lock")


@contextlib.asynccontextmanager
async def reaping(tasks: Set[asyncio.Task[object]]) -> AsyncIterator[None]:
    """On exit — a failed assertion included — cancel and await whichever of *tasks* is still running, so no parked
    task outlives its test."""
    try:
        yield
    finally:
        leftover = [task for task in tasks if not task.done()]
        for task in leftover:
            task.cancel()
        await asyncio.gather(*leftover, return_exceptions=True)

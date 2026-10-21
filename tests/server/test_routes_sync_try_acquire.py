"""Unit tests for ``routes_sync._try_acquire`` and the route's 409 on a sync_lock handover — pure asyncio, no app.

sync_lock handover: asyncio.Lock hands a released lock to a queued waiter (the lifespan's startup sync waits for
it via ``async with``), so right after such a release ``locked()`` is False yet ``acquire()`` queues. POST /sync
used to wait there for the whole startup sync, then run a redundant sync; it must answer 409. The HTTP-level
handover test lives in ``test_csp120_be3_sync_route.py``.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from archon_search.server.routes_sync import _try_acquire, trigger_sync

#: Bound on any wait in these tests: a regression shows up as a timeout, never as a hang.
_WAIT_TIMEOUT_SECONDS = 5.0


async def _queue_waiter_and_release(lock: asyncio.Lock) -> tuple[asyncio.Task, asyncio.Event, asyncio.Event]:
    """Hold *lock*, queue a task that waits for it, then release it: the lock is free, its waiter still queued.

    Returns ``(waiter_task, waiter_acquired, let_waiter_finish)``; set the last one to let the waiter release.
    """
    await lock.acquire()
    waiter_acquired = asyncio.Event()
    let_waiter_finish = asyncio.Event()

    async def _waiter() -> None:
        async with lock:
            waiter_acquired.set()
            await let_waiter_finish.wait()

    waiter = asyncio.create_task(_waiter())
    await asyncio.sleep(0)  # the waiter runs up to its queued acquire()
    lock.release()
    assert not lock.locked(), "test setup is wrong — the released lock must read free while its waiter is queued"
    return waiter, waiter_acquired, let_waiter_finish


async def test_try_acquire_takes_a_free_lock() -> None:
    lock = asyncio.Lock()

    assert await _try_acquire(lock) is True
    assert lock.locked()


async def test_try_acquire_refuses_a_held_lock() -> None:
    lock = asyncio.Lock()
    await lock.acquire()

    assert await _try_acquire(lock) is False
    assert lock.locked(), "a refused try-acquire must leave the holder's lock held"


async def test_try_acquire_refuses_a_lock_handed_to_a_queued_waiter_and_the_waiter_still_gets_it() -> None:
    lock = asyncio.Lock()
    waiter, waiter_acquired, let_waiter_finish = await _queue_waiter_and_release(lock)

    try:
        assert await asyncio.wait_for(_try_acquire(lock), _WAIT_TIMEOUT_SECONDS) is False
        await asyncio.wait_for(waiter_acquired.wait(), _WAIT_TIMEOUT_SECONDS)
        assert lock.locked(), "the queued waiter must hold the lock after the refused try-acquire"
    finally:
        let_waiter_finish.set()
        await asyncio.wait_for(waiter, _WAIT_TIMEOUT_SECONDS)
    assert not lock.locked()


async def test_trigger_sync_answers_409_while_the_lock_is_handed_to_a_queued_waiter() -> None:
    """The route answers 409 at once instead of queueing behind the waiter (the startup sync)."""
    lock = asyncio.Lock()
    waiter, waiter_acquired, let_waiter_finish = await _queue_waiter_and_release(lock)
    job_store = MagicMock()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                job_store=job_store,
                collection_sync=MagicMock(),
                config=SimpleNamespace(pinned_collections=[], collections=[]),
                sync_lock=lock,
                _background_tasks=set(),
            )
        ),
        state=SimpleNamespace(namespace="default"),
    )

    try:
        resp = await asyncio.wait_for(trigger_sync(request), _WAIT_TIMEOUT_SECONDS)
        assert resp.status_code == 409
        assert json.loads(resp.body) == {"detail": "sync already in progress"}
        assert job_store.method_calls == [], "nothing may touch the job store before the lock is taken"
        await asyncio.wait_for(waiter_acquired.wait(), _WAIT_TIMEOUT_SECONDS)
    finally:
        let_waiter_finish.set()
        await asyncio.wait_for(waiter, _WAIT_TIMEOUT_SECONDS)

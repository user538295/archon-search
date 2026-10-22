"""``SearchStore.collection_writer`` / ``has_active_writer``: the registry of writers that create no job.

An MCP ``ingest_file`` / ``ingest_directory`` and a filesystem-watcher sync write a collection without a job, so
``JobStore.find_active_collection_job`` never sees them. ``DELETE /collections/{name}`` consults this registry, under
the collection lock, next to the job store (Documentation/Architecture/530_technical_debt_refactoring_roadmap.md,
CON-12). Registering takes the lock, so a DELETE holding it either runs before the writer registers or sees it.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from archon_search.store import SearchStore, StoreBusyError
from tests._parked_lock import reaping, until_parked

_COLLECTION = "docs"
#: Lock wait the timeout test patches in, short enough to keep the test fast.
_SHORT_LOCK_TIMEOUT_S = 0.05
_WRITERS_OF_ONE_COLLECTION = 2


@pytest.fixture
def store(tmp_path: Path) -> SearchStore:
    return SearchStore(tmp_path / "db")


async def test_a_collection_has_no_active_writer_until_one_registers(store: SearchStore) -> None:
    assert not store.has_active_writer(_COLLECTION)


async def test_collection_writer_is_registered_for_the_whole_block_and_released_after(store: SearchStore) -> None:
    async with store.collection_writer(_COLLECTION):
        assert store.has_active_writer(_COLLECTION)
        await asyncio.sleep(0)
        assert store.has_active_writer(_COLLECTION)

    assert not store.has_active_writer(_COLLECTION)


async def test_collection_writer_is_per_collection(store: SearchStore) -> None:
    async with store.collection_writer(_COLLECTION):
        assert not store.has_active_writer("other")


async def test_collection_writer_does_not_hold_the_collection_lock_inside_the_block(store: SearchStore) -> None:
    """The pipeline takes the (non-reentrant) lock per store write, so the block must run lock-free."""
    async with store.collection_writer(_COLLECTION):
        assert not store.lock_for(_COLLECTION).locked()


async def test_collection_writer_stays_active_until_the_last_of_several_writers_ends(store: SearchStore) -> None:
    first = store.collection_writer(_COLLECTION)
    second = store.collection_writer(_COLLECTION)
    await first.__aenter__()
    await second.__aenter__()
    assert store.has_active_writer(_COLLECTION)

    await first.__aexit__(None, None, None)
    assert store.has_active_writer(_COLLECTION), "one writer is still running"

    await second.__aexit__(None, None, None)
    assert not store.has_active_writer(_COLLECTION)


async def test_collection_writer_is_released_when_the_block_raises(store: SearchStore) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with store.collection_writer(_COLLECTION):
            raise RuntimeError("boom")

    assert not store.has_active_writer(_COLLECTION)


async def test_collection_writer_is_released_when_the_block_is_cancelled(store: SearchStore) -> None:
    inside = asyncio.Event()

    async def writer() -> None:
        async with store.collection_writer(_COLLECTION):
            inside.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(writer())
    async with reaping({task}):
        await asyncio.wait_for(inside.wait(), timeout=_SHORT_LOCK_TIMEOUT_S * 100)
        assert store.has_active_writer(_COLLECTION)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert not store.has_active_writer(_COLLECTION)


async def test_collection_writer_registers_only_after_the_lock_holder_releases(store: SearchStore) -> None:
    """A DELETE holds the lock for its check and drop: a writer starting meanwhile waits, so it is never registered
    between the DELETE's check and its drop."""
    lock = store.lock_for(_COLLECTION)
    await lock.acquire()
    entered = asyncio.Event()

    async def writer() -> None:
        async with store.collection_writer(_COLLECTION):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(writer())
    async with reaping({task}):
        await until_parked(lock)
        assert not store.has_active_writer(_COLLECTION), "registered while the lock holder was still checking"

        lock.release()
        await asyncio.wait_for(entered.wait(), timeout=_SHORT_LOCK_TIMEOUT_S * 100)
        assert store.has_active_writer(_COLLECTION)


async def test_collection_writer_raises_store_busy_and_registers_nothing_when_the_lock_stays_held(
    store: SearchStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _SHORT_LOCK_TIMEOUT_S)
    lock = store.lock_for(_COLLECTION)
    await lock.acquire()
    try:
        with pytest.raises(StoreBusyError) as excinfo:
            async with store.collection_writer(_COLLECTION):
                pytest.fail("the block must not run without the registration")
    finally:
        lock.release()

    assert excinfo.value.timeout_s == _SHORT_LOCK_TIMEOUT_S
    assert not store.has_active_writer(_COLLECTION)


@pytest.mark.parametrize("invalid_name", ["", "../escape", "has space", "x" * 65])
async def test_collection_writer_rejects_an_invalid_name_without_creating_a_lock_entry(
    store: SearchStore, invalid_name: str
) -> None:
    """A tool argument must not grow the lock map or the registry with arbitrary names."""
    with pytest.raises(ValueError, match="Invalid collection name"):
        async with store.collection_writer(invalid_name):
            pytest.fail("the block must not run for an invalid name")

    assert not store.has_active_writer(invalid_name)
    assert invalid_name not in store._collection_locks


async def test_writers_of_one_collection_are_counted_not_merged(store: SearchStore) -> None:
    async with store.collection_writer(_COLLECTION), store.collection_writer(_COLLECTION):
        assert store.has_active_writer(_COLLECTION)
        assert store._active_writers[_COLLECTION] == _WRITERS_OF_ONE_COLLECTION
    assert _COLLECTION not in store._active_writers, "no entry outlives its last writer"

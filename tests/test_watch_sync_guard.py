"""The filesystem watcher's sync of a collection is a registered writer and ignores a collection removed from the config.

``SearchCollectionSync.sync_collection`` creates no job, so ``DELETE /collections/{name}`` cannot see a running watcher
sync in the job store; it consults ``SearchStore.has_active_writer`` (CON-12). The watcher also keeps its startup
snapshot of ``{collection: path}`` after a DELETE removes the path from the config: a change fired afterwards would have
the sync re-create the dropped collection from whatever the stale indexing state calls changed, so it is skipped.
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from archon_search.config import SearchConfig
from archon_search.server.app import _sync_watched_collection
from archon_search.server.schemas import StartupSyncResult
from archon_search.store import SearchStore, StoreBusyError

_COLLECTION = "docs"
_SHORT_LOCK_TIMEOUT_S = 0.05
_BOOM = "sync blew up"


class _RecordingSync:
    """Stand-in for ``SearchCollectionSync`` that records what the writer registry said during each call."""

    def __init__(self, store: SearchStore, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, Path]] = []
        self.active_during_call: list[bool] = []
        self._store = store
        self._error = error

    async def sync_collection(self, collection_name: str, source_path: Path) -> None:
        self.calls.append((collection_name, source_path))
        self.active_during_call.append(self._store.has_active_writer(collection_name))
        if self._error is not None:
            raise self._error


def _app(
    store: SearchStore,
    sync: _RecordingSync,
    *,
    collections: list[str],
    pinned: list[str] | None = None,
    sync_result: StartupSyncResult | None = None,
) -> Any:
    config = SearchConfig()
    config.collections = collections
    config.pinned_collections = pinned or []
    return SimpleNamespace(
        state=SimpleNamespace(config=config, search_store=store, collection_sync=sync, sync_result=sync_result)
    )


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / _COLLECTION
    path.mkdir()
    return path.resolve()


@pytest.fixture
def store(tmp_path: Path) -> SearchStore:
    return SearchStore(tmp_path / "db")


async def test_the_sync_runs_as_a_registered_writer_that_is_released_after(store: SearchStore, source: Path) -> None:
    sync = _RecordingSync(store)

    await _sync_watched_collection(_app(store, sync, collections=[str(source)]), _COLLECTION, str(source))

    assert sync.calls == [(_COLLECTION, source)]
    assert sync.active_during_call == [True]
    assert not store.has_active_writer(_COLLECTION)


async def test_a_pinned_collection_is_still_configured(store: SearchStore, source: Path) -> None:
    sync = _RecordingSync(store)

    await _sync_watched_collection(
        _app(store, sync, collections=[], pinned=[str(source)]), _COLLECTION, str(source)
    )

    assert sync.calls == [(_COLLECTION, source)]


async def test_a_configured_path_is_compared_resolved(store: SearchStore, source: Path) -> None:
    """The watcher snapshot holds resolved paths; the config may hold any spelling of the same directory."""
    sync = _RecordingSync(store)
    unresolved = str(source.parent / "elsewhere" / ".." / source.name)

    await _sync_watched_collection(_app(store, sync, collections=[unresolved]), _COLLECTION, str(source))

    assert sync.calls == [(_COLLECTION, source)]


async def test_a_collection_removed_from_the_config_is_not_synced(
    store: SearchStore, source: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sync = _RecordingSync(store)

    with caplog.at_level(logging.INFO, logger="archon_search.server.app"):
        await _sync_watched_collection(_app(store, sync, collections=[]), _COLLECTION, str(source))

    assert sync.calls == []
    assert not store.has_active_writer(_COLLECTION)
    assert any(
        _COLLECTION in record.getMessage() and "no longer configured" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


async def test_a_suppressed_startup_sync_skips_the_watcher_before_registering(store: SearchStore, source: Path) -> None:
    sync = _RecordingSync(store)

    await _sync_watched_collection(
        _app(store, sync, collections=[str(source)], sync_result=StartupSyncResult.SUPPRESSED),
        _COLLECTION,
        str(source),
    )

    assert sync.calls == []
    assert _COLLECTION not in store._collection_locks, "the collection lock was touched, so a writer registered"


async def test_the_registration_is_released_when_the_sync_raises(store: SearchStore, source: Path) -> None:
    sync = _RecordingSync(store, error=RuntimeError(_BOOM))

    with pytest.raises(RuntimeError, match=_BOOM):
        await _sync_watched_collection(_app(store, sync, collections=[str(source)]), _COLLECTION, str(source))

    assert sync.active_during_call == [True]
    assert not store.has_active_writer(_COLLECTION)


async def test_the_sync_does_not_run_when_the_collection_lock_is_not_acquired(
    store: SearchStore, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _SHORT_LOCK_TIMEOUT_S)
    sync = _RecordingSync(store)
    lock = store.lock_for(_COLLECTION)
    await lock.acquire()
    try:
        with pytest.raises(StoreBusyError):
            await _sync_watched_collection(_app(store, sync, collections=[str(source)]), _COLLECTION, str(source))
    finally:
        lock.release()

    assert sync.calls == []
    assert not store.has_active_writer(_COLLECTION)

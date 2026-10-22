"""CON-7: ``SearchCollectionSync.sync`` is a registered writer of each collection it writes, and guards its own drops.

A running sync names no collection to ``JobStore.find_active_collection_job``, yet it writes every collection of the
list it started with. It therefore registers in ``SearchStore.collection_writer`` around each collection's write (not for
the whole sync), skips a collection ``DELETE /collections/{name}`` removed from the live config before it got there, and
never drops a collection gone from the config while a job or a registered writer may still write it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import CollectionInfo, IngestResult
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
from archon_search.store import SearchStore
from archon_search.sync import SearchCollectionSync, collection_sync_failed_detail

_SHORT_LOCK_TIMEOUT_S = 0.05
_STEP_TIMEOUT_S = 10.0
_FIRST, _SECOND = "alpha", "beta"
_SITES = ("new", "resume", "update")


def _pipeline(tmp_path: Path, existing: tuple[str, ...] = ()) -> MagicMock:
    """A pipeline over a real (unconnected) ``SearchStore`` — the writer registry and locks are the real ones."""
    store = SearchStore(tmp_path / "db")
    store.list_collections = AsyncMock(
        return_value=[CollectionInfo(name=name, doc_count=0, chunk_count=0) for name in existing]
    )
    store.get_all_collections_meta = AsyncMock(return_value=[])
    store.get_collection_meta = AsyncMock(return_value=None)
    store.drop_collection = AsyncMock()
    store.optimize_fts = AsyncMock()
    store.rebuild_fts_index = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value=None)
    pipeline = MagicMock()
    pipeline.store = store
    pipeline.ingest_directory = AsyncMock(return_value=[])
    pipeline.ingest_file = AsyncMock(return_value=IngestResult(doc_id="d0", chunks_created=1, status="ok"))
    pipeline.delete_by_source_path = AsyncMock()
    return pipeline


def _write_manifest(tmp_path: Path, manifest: dict[str, str]) -> None:
    (tmp_path / "db").mkdir(parents=True, exist_ok=True)
    (tmp_path / "db" / "sync_manifest.json").write_text(json.dumps(manifest))


def _read_manifest(tmp_path: Path) -> dict[str, str]:
    return json.loads((tmp_path / "db" / "sync_manifest.json").read_text())


def _source(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    (path / "doc.md").write_text(f"# {name}\n\nsome text\n")
    return path.resolve()


@dataclass
class _Site:
    """One of the three places ``sync()`` writes a collection, with the pipeline call that writes it."""

    syncer: SearchCollectionSync
    pipeline: MagicMock
    sources: dict[str, Path]
    configured: list[str]
    write_call: AsyncMock


def _site(tmp_path: Path, site: str, *, job_store: Any = None) -> _Site:
    """A sync of ``alpha`` and ``beta`` whose first write goes through the *site* named (new / resume / update)."""
    sources = {name: _source(tmp_path, name) for name in (_FIRST, _SECOND)}
    existing = () if site == "new" else (_FIRST, _SECOND)
    pipeline = _pipeline(tmp_path, existing)
    state_store = IndexingStateStore(tmp_path / "state")
    for name in existing:
        state_store.update_collection(
            name,
            CollectionProgress(
                status=IndexingStatus.PENDING if site == "resume" else IndexingStatus.DONE,
                total_files=0,
                indexed_chunk_size=0,
            ),
        )
    configured = [str(path) for path in sources.values()]
    syncer = SearchCollectionSync(
        pipeline,
        state_store=state_store,
        job_store=job_store,
        configured_paths=lambda: list(configured),
    )
    write_call = pipeline.ingest_file if site == "update" else pipeline.ingest_directory
    return _Site(syncer, pipeline, sources, configured, write_call)


@pytest.mark.parametrize("site", _SITES)
async def test_a_collection_is_a_registered_writer_for_exactly_its_own_write(tmp_path: Path, site: str) -> None:
    """The registration spans one collection's write, and no other collection's: a DELETE of any other stays possible."""
    world = _site(tmp_path, site)
    store: SearchStore = world.pipeline.store
    seen: list[tuple[str, set[str]]] = []

    async def record(_path: Path, collection: str, *_args: Any, **_kwargs: Any) -> Any:
        seen.append((collection, set(store._active_writers)))
        return world.write_call.return_value

    world.write_call.side_effect = record

    result = await world.syncer.sync(world.configured)

    assert seen, "the sync wrote nothing: test setup"
    assert {collection for collection, _ in seen} == {_FIRST, _SECOND}
    assert [writers for _, writers in seen] == [{collection} for collection, _ in seen]
    assert not store._active_writers, "a registration outlived the sync"
    assert result.errors == []


@pytest.mark.parametrize("site", _SITES)
async def test_a_cancelled_sync_releases_its_registration(tmp_path: Path, site: str) -> None:
    world = _site(tmp_path, site)
    store: SearchStore = world.pipeline.store
    started, never = asyncio.Event(), asyncio.Event()

    async def block(*_args: Any, **_kwargs: Any) -> Any:
        started.set()
        await never.wait()

    world.write_call.side_effect = block
    sync = asyncio.create_task(world.syncer.sync(world.configured))
    await asyncio.wait_for(started.wait(), _STEP_TIMEOUT_S)
    assert store.has_active_writer(_FIRST)

    sync.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sync

    assert not store._active_writers


@pytest.mark.parametrize("site", _SITES)
async def test_a_collection_removed_from_the_config_before_the_sync_reaches_it_is_not_written(
    tmp_path: Path, site: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A DELETE mid-sync edits the live config under the collection lock: the sync must not re-create the collection."""
    world = _site(tmp_path, site)
    written: list[str] = []

    async def write_first_then_get_second_deleted(_path: Path, collection: str, *_args: Any, **_kwargs: Any) -> Any:
        written.append(collection)
        world.configured.remove(str(world.sources[_SECOND]))
        return world.write_call.return_value

    world.write_call.side_effect = write_first_then_get_second_deleted

    with caplog.at_level(logging.INFO, logger="archon_search.sync"):
        result = await world.syncer.sync(world.configured + [str(world.sources[_SECOND])])

    assert written == [_FIRST]
    assert not world.pipeline.store._active_writers
    reported = {*result.added, *result.updated, *result.unchanged, *result.errors, *result.skipped}
    assert not any(_SECOND in entry for entry in reported), f"the deleted collection was reported: {result}"
    assert _SECOND not in _read_manifest(tmp_path)
    assert _FIRST in _read_manifest(tmp_path)
    assert any(
        _SECOND in record.getMessage() and "no longer configured" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


async def test_a_configured_path_is_compared_resolved(tmp_path: Path) -> None:
    """The sync's own list and the config may spell one directory differently (``~``, ``..``)."""
    world = _site(tmp_path, "new")
    world.configured[:] = [f"{world.sources[name].parent}/../{world.sources[name].parent.name}/{name}" for name in world.sources]

    result = await world.syncer.sync([str(path) for path in world.sources.values()])

    assert sorted(result.added) == [_FIRST, _SECOND]
    assert result.errors == []


async def test_a_collection_with_an_invalid_name_is_written_unregistered_and_fails_as_before(tmp_path: Path) -> None:
    """No such collection can exist for a DELETE to drop: the sync neither registers it nor aborts, and the pipeline's
    own rejection of the name stays that one collection's error."""
    too_long = "x" * 70
    source = tmp_path / too_long
    source.mkdir()
    (source / "doc.md").write_text("# doc\n")
    pipeline = _pipeline(tmp_path)
    pipeline.ingest_directory.side_effect = ValueError("Invalid collection name")
    syncer = SearchCollectionSync(pipeline, configured_paths=lambda: [str(source)])

    result = await syncer.sync([str(source)])

    pipeline.ingest_directory.assert_awaited_once()
    assert result.errors == [collection_sync_failed_detail(too_long)]
    assert not pipeline.store._active_writers
    assert too_long not in pipeline.store._collection_locks, "a lock entry was created for an invalid name"


async def test_a_collection_whose_lock_is_not_acquired_is_an_error_and_the_others_are_still_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _SHORT_LOCK_TIMEOUT_S)
    world = _site(tmp_path, "new")
    store: SearchStore = world.pipeline.store

    with caplog.at_level(logging.WARNING, logger="archon_search.sync"):
        async with store.lock_for(_FIRST):
            result = await world.syncer.sync(world.configured)

    assert result.added == [_SECOND]
    assert len(result.errors) == 1 and _FIRST in result.errors[0]
    assert [call.args[1] for call in world.pipeline.ingest_directory.call_args_list] == [_SECOND]
    assert _FIRST not in _read_manifest(tmp_path), "a collection that was not synced must not become managed"
    assert any(_FIRST in record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING)


# --- the sync's own drop of a collection gone from the config ---------------------------------------------------


_OLD = "oldcol"


def _drop_world(tmp_path: Path, *, job_store: Any = None) -> tuple[SearchCollectionSync, MagicMock]:
    _write_manifest(tmp_path, {_OLD: "/some/old/path"})
    pipeline = _pipeline(tmp_path, existing=(_OLD,))
    return SearchCollectionSync(pipeline, job_store=job_store), pipeline


async def test_the_drop_runs_under_the_collection_lock_like_delete_does(tmp_path: Path) -> None:
    syncer, pipeline = _drop_world(tmp_path)
    store: SearchStore = pipeline.store
    lock_held_during_drop: list[bool] = []

    async def drop(name: str, **kwargs: Any) -> None:
        lock_held_during_drop.append(store.lock_for(name).locked())
        assert kwargs == {"_locked_by_caller": True}

    store.drop_collection.side_effect = drop

    result = await syncer.sync([])

    assert lock_held_during_drop == [True]
    assert result.removed == [_OLD]
    assert result.errors == []
    assert not store.lock_for(_OLD).locked(), "the drop left the collection lock held"
    assert _OLD not in _read_manifest(tmp_path)


async def test_a_collection_with_an_active_writer_is_not_dropped_and_is_retried_by_the_next_sync(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    syncer, pipeline = _drop_world(tmp_path)
    store: SearchStore = pipeline.store

    with caplog.at_level(logging.WARNING, logger="archon_search.sync"):
        async with store.collection_writer(_OLD):
            refused = await syncer.sync([])

    store.drop_collection.assert_not_called()
    assert refused.removed == []
    assert len(refused.errors) == 1 and _OLD in refused.errors[0]
    assert any(_OLD in record.getMessage() for record in caplog.records if record.levelno == logging.WARNING)
    assert _read_manifest(tmp_path) == {_OLD: "/some/old/path"}, "the refused drop must stay managed to be retried"

    retried = await syncer.sync([])

    store.drop_collection.assert_awaited_once()
    assert retried.removed == [_OLD]
    assert retried.errors == []
    assert _read_manifest(tmp_path) == {}


async def test_a_collection_with_an_active_job_is_not_dropped(tmp_path: Path) -> None:
    job_store = MagicMock()
    job_store.find_active_collection_job.return_value = MagicMock(job_id="j-1")
    syncer, pipeline = _drop_world(tmp_path, job_store=job_store)

    result = await syncer.sync([])

    job_store.find_active_collection_job.assert_called_once_with(_OLD, DEFAULT_NAMESPACE)
    pipeline.store.drop_collection.assert_not_called()
    assert result.removed == []
    assert len(result.errors) == 1 and _OLD in result.errors[0]
    assert _read_manifest(tmp_path) == {_OLD: "/some/old/path"}
    assert not pipeline.store.lock_for(_OLD).locked()


async def test_a_collection_whose_lock_is_not_acquired_is_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _SHORT_LOCK_TIMEOUT_S)
    syncer, pipeline = _drop_world(tmp_path)

    async with pipeline.store.lock_for(_OLD):
        result = await syncer.sync([])

    pipeline.store.drop_collection.assert_not_called()
    assert len(result.errors) == 1 and _OLD in result.errors[0]
    assert _read_manifest(tmp_path) == {_OLD: "/some/old/path"}


async def test_a_collection_missing_from_the_store_is_still_reported_by_the_drop(tmp_path: Path) -> None:
    syncer, pipeline = _drop_world(tmp_path)
    pipeline.store.drop_collection.side_effect = KeyError(_OLD)

    result = await syncer.sync([])

    assert result.removed == []
    assert result.errors == [f"Collection {_OLD!r} in manifest but not in LanceDB; skipping drop"]
    assert not pipeline.store.lock_for(_OLD).locked()

"""BE-20 (C3, C11): a whole-row metadata writer carries the locked fields.

Each writer below used to read the metadata row outside the collection lock and write it back, so a description write
that landed in between (a manual set, a generation, a clear) was overwritten with the old description, source, status
and routing vector. Now the row is read after the lock is acquired.

Every case parks the writer on the collection lock, changes the description while the test holds the lock, then lets
the writer run: the description written in between must survive the writer's own change.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.store import STORE_SCHEMA_VERSION, SearchStore
from archon_search.types import MigrationKind, MigrationSpec
from tests._parked_lock import parked_waiters, reaping

pytestmark = pytest.mark.integration

_NAME = "docs"
_NAMESPACE = "default"
_MANUAL_TEXT = "written while the writer waited"
_MANUAL_VECTOR = [0.5, 0.25]
_STORED_MODEL = "model-a"
_TARGET_MODEL = "model-b"

#: Seeds the collection for one writer and returns the ``CollectionMeta`` field the writer must clear (or ``None``
#: when it changes another field) with the coroutine factory that runs the writer.
Prepared = tuple[str | None, Callable[[], Awaitable[object]]]
Prepare = Callable[[SearchStore, JobStore, Path], Awaitable[Prepared]]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SearchStore]:
    search_store = SearchStore(tmp_path / "db")
    await search_store.connect()
    await search_store.update_collection_meta(
        CollectionMeta(
            name=_NAME,
            description="generated before",
            description_source="generated",
            description_status="ok",
            description_embedding=[1.0, 2.0],
            active_embedding_model=_STORED_MODEL,
            pending_embedding_model=_TARGET_MODEL,
            needs_reindex=True,
            schema_version=STORE_SCHEMA_VERSION,
        )
    )
    yield search_store
    await search_store.disconnect()


async def _stored(store: SearchStore) -> CollectionMeta:
    row = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert row is not None
    return row


async def _point_at(store: SearchStore, **pointers: str) -> None:
    await store.update_collection_meta(replace(await _stored(store), **pointers))


async def _clear_reindex_pointer(store: SearchStore, job_store: JobStore, tmp_path: Path) -> Prepared:
    from archon_search.server.routes_jobs import _clear_reindex_job_id

    await _point_at(store, reindex_job_id="job-1")
    return "reindex_job_id", lambda: _clear_reindex_job_id(store, _NAME, _NAMESPACE, "job-1")


async def _reindex(
    store: SearchStore, job_store: JobStore, tmp_path: Path, target_model: str | None
) -> Prepared:
    from archon_search.server.routes_jobs import _reindex_task

    job = job_store.create_reindex(_NAME, _NAMESPACE, target_embedding_model=target_model)
    await _point_at(store, reindex_job_id=job.job_id)
    pipeline = MagicMock()
    pipeline.ingest_directory = AsyncMock(return_value=[])
    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(return_value=MagicMock())
    return "reindex_job_id", lambda: _reindex_task(
        job_id=job.job_id,
        store=store,
        job_store=job_store,
        embedder_cache=embedder_cache,
        pipeline=pipeline,
        collection=_NAME,
        namespace=_NAMESPACE,
        collection_path=tmp_path,
        config=SearchConfig(),
    )


async def _metadata_reindex_pointer(store: SearchStore, job_store: JobStore, tmp_path: Path) -> Prepared:
    from archon_search.server.routes_collections import _reindex_metadata_task

    job = job_store.create_metadata_reindex(_NAME, _NAMESPACE)
    await _point_at(store, metadata_reindex_job_id=job.job_id)

    async def run() -> None:
        # The reindex itself fails: the pointer is cleared in the task's ``finally`` either way.
        with patch.object(store, "reindex_metadata", AsyncMock(side_effect=RuntimeError("scan failed"))):
            await _reindex_metadata_task(job, job_store, store, dry_run=False, normalize_timestamps=False)

    return "metadata_reindex_job_id", run


async def _community_rebuild_pointer(store: SearchStore, job_store: JobStore, tmp_path: Path) -> Prepared:
    from archon_search.server.routes_graph import _community_rebuild_task

    job = job_store.create_community_rebuild(_NAME, _NAMESPACE)
    await _point_at(store, community_rebuild_job_id=job.job_id)

    async def run() -> None:
        with patch("archon_search.server.routes_graph.CommunityBuilder") as builder_class:
            builder_class.return_value.build = AsyncMock(return_value=[])
            await _community_rebuild_task(
                job=job, job_store=job_store, graph_store=object(), graph_config=object(), search_store=store
            )

    return "community_rebuild_job_id", run


async def _reindex_promoting_the_model(store: SearchStore, job_store: JobStore, tmp_path: Path) -> Prepared:
    return await _reindex(store, job_store, tmp_path, _TARGET_MODEL)


async def _reindex_without_a_model_change(store: SearchStore, job_store: JobStore, tmp_path: Path) -> Prepared:
    return await _reindex(store, job_store, tmp_path, None)


_SITES: dict[str, Prepare] = {
    "clear_reindex_job_id": _clear_reindex_pointer,
    "reindex_task_data_only": _reindex_without_a_model_change,
    "reindex_metadata_task": _metadata_reindex_pointer,
    "community_rebuild_task": _community_rebuild_pointer,
}


_QUEUE_WAIT_SECONDS = 10.0
_QUEUE_POLL_SECONDS = 0.005


async def _until_queued(lock: asyncio.Lock) -> None:
    """Wait until a writer queues on *lock*. Unlike ``until_parked`` this polls in time, not in event-loop turns,
    because a writer that reads the row first (the old behaviour) awaits real LanceDB I/O before it queues."""
    async with asyncio.timeout(_QUEUE_WAIT_SECONDS):
        while not parked_waiters(lock):
            await asyncio.sleep(_QUEUE_POLL_SECONDS)


@pytest.mark.parametrize("site", list(_SITES), ids=list(_SITES))
async def test_writer_carries_a_description_written_while_it_waited_for_the_lock(
    store: SearchStore, tmp_path: Path, site: str
) -> None:
    pointer, run = await _SITES[site](store, JobStore(path=tmp_path / "jobs.json"), tmp_path)
    lock = store.lock_for(_NAME)
    await lock.acquire()  # a description writer holds the collection while the metadata writer queues behind it
    task = asyncio.create_task(run())
    async with reaping({task}):
        await _until_queued(lock)
        db = store._require_connected()
        row = await store._do_read_meta_unlocked(db, _NAME, namespace=_NAMESPACE)
        assert row is not None
        await store._do_write_meta_unlocked(
            db,
            _NAME,
            replace(
                row,
                description=_MANUAL_TEXT,
                description_source="manual",
                description_status="ok",
                description_embedding=list(_MANUAL_VECTOR),
            ),
        )
        lock.release()
        await task

    final = await _stored(store)
    assert final.description == _MANUAL_TEXT
    assert final.description_source == "manual"
    assert final.description_status == "ok"
    assert final.description_embedding == _MANUAL_VECTOR
    assert pointer is not None and getattr(final, pointer) is None, "the writer's own change still landed"


async def test_reindex_promotion_promotes_the_model_and_drops_the_vector_of_the_old_model(
    store: SearchStore, tmp_path: Path
) -> None:
    """The description vector belongs to the model it was computed with: once the collection moves to another model
    it must not route against it. The description text itself is kept."""
    pointer, run = await _reindex_promoting_the_model(store, JobStore(path=tmp_path / "jobs.json"), tmp_path)

    await run()

    final = await _stored(store)
    assert (final.active_embedding_model, final.pending_embedding_model) == (_TARGET_MODEL, None)
    assert final.needs_reindex is False and getattr(final, pointer or "") is None
    assert final.description == "generated before"
    assert final.description_embedding is None
    assert final.needs_recompute is True, "flagged so the next recompute can repair the vector"


async def test_ingest_stub_meta_is_not_written_over_a_row_created_meanwhile(
    store: SearchStore, tmp_path: Path
) -> None:
    """The stub row for an ingest of a missing path is written only while the row is still absent under the lock: a
    concurrent ingest that created the collection since the first read keeps its row."""
    from archon_search.server.routes_jobs import (
        IngestPathNotFoundError,
        IngestRequest,
        _dispatch_ingest,
    )

    new_name = "fresh"
    body = IngestRequest(collection=new_name, path=str(tmp_path / "does-not-exist"))
    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(return_value=MagicMock())
    lock = store.lock_for(new_name)
    await lock.acquire()
    task = asyncio.create_task(_dispatch_ingest(body, _NAMESPACE, store, embedder_cache, MagicMock(), SearchConfig()))
    async with reaping({task}):
        await _until_queued(lock)
        db = store._require_connected()
        await store._do_write_meta_unlocked(
            db, new_name, CollectionMeta(name=new_name, description=_MANUAL_TEXT, doc_count=7, schema_version=1)
        )
        lock.release()
        with pytest.raises(IngestPathNotFoundError):
            await task

    row = await store.get_collection_meta(new_name, _NAMESPACE)
    assert row is not None
    assert (row.description, row.doc_count) == (_MANUAL_TEXT, 7)


async def test_ingest_stub_meta_is_written_when_the_collection_is_still_absent(
    store: SearchStore, tmp_path: Path
) -> None:
    from archon_search.server.routes_jobs import (
        IngestPathNotFoundError,
        IngestRequest,
        _dispatch_ingest,
    )

    body = IngestRequest(collection="fresh", path=str(tmp_path / "does-not-exist"))
    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(return_value=MagicMock())

    with pytest.raises(IngestPathNotFoundError):
        await _dispatch_ingest(body, _NAMESPACE, store, embedder_cache, MagicMock(), SearchConfig())

    row = await store.get_collection_meta("fresh", _NAMESPACE)
    assert row is not None and row.doc_count == 0 and row.description is None


async def test_apply_rewrite_migration_reads_the_row_it_writes_back_under_the_lock(
    store: SearchStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The schema-version stamp after a rewrite migration is a whole-row write: the row it writes back must be read
    while the collection lock is held (the rewrite itself held the lock, but the stamp used to run after releasing)."""
    await store.ensure_collection(_NAME, 2)
    await store.update_collection_meta(replace(await _stored(store), schema_version=0))
    monkeypatch.setattr(store, "noop_rewrite", AsyncMock(return_value=[]), raising=False)
    lock = store.lock_for(_NAME)
    reads_with_lock_held: list[bool] = []
    real_read = store.get_collection_meta

    async def recording_read(name: str, namespace: str = _NAMESPACE) -> CollectionMeta | None:
        reads_with_lock_held.append(lock.locked())
        return await real_read(name, namespace)

    spec = MigrationSpec(name="noop_rewrite", kind=MigrationKind.REWRITE, description="no chunks", introduced_at=1)
    monkeypatch.setattr(store, "get_collection_meta", recording_read)

    await store.apply_rewrite_migration(_NAME, _NAMESPACE, spec)

    assert reads_with_lock_held == [True]
    final = await real_read(_NAME, _NAMESPACE)
    assert final is not None and final.schema_version == STORE_SCHEMA_VERSION
    assert final.description == "generated before"

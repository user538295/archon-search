"""CON-7: a ``DELETE /collections/{name}`` racing a running sync cannot be undone by it, and the sync cannot drop a
collection a job or writer may still write.

Real ``SearchStore`` / ``SearchPipeline`` / ``SearchCollectionSync`` as ``create_app`` wires them; the sync is started the
way the server starts it — ``POST /sync`` and the startup sync — and DELETE goes over ASGI.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.pipeline import SearchPipeline
from archon_search.server._ingest_lock import COLLECTION_WRITER_IN_PROGRESS_DETAIL, STORE_BUSY_CODE
from archon_search.server.app import _run_startup_sync, create_app
from archon_search.store import SearchStore
from archon_search.sync import path_to_collection_name
from archon_search.types import JobStatus
from tests.integration.conftest import make_real_pipeline

pytestmark = pytest.mark.integration

_STEP_TIMEOUT_S = 10.0
_DOC_TEXT = "# Title\n\nSome searchable content for the CON-7 race test.\n" * 4
_ENTRY_POINTS = ("post_sync", "startup_sync")


@dataclass
class _World:
    app: Any
    store: SearchStore
    pipeline: SearchPipeline
    sources: dict[str, Path]
    first_write_started: asyncio.Event
    release_writer: asyncio.Event
    db: Path

    @property
    def first(self) -> str:
        return min(self.sources)

    @property
    def second(self) -> str:
        return max(self.sources)

    async def exists(self, name: str) -> bool:
        """Whether the collection's table or meta row is in the store."""
        listed = {info.name for info in await self.store.list_collections()}
        return name in listed or await self.store.get_collection_meta(name) is not None

    def client(self) -> httpx.AsyncClient:
        key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {key}"},
        )

    async def start_sync(self, entry_point: str, client: httpx.AsyncClient) -> Awaitable[Any]:
        """Start a sync the way the server does; return an awaitable that settles when it ends, with its result."""
        if entry_point == "startup_sync":
            cols = list(self.app.state.config.pinned_collections) + list(self.app.state.config.collections)
            task = asyncio.create_task(_run_startup_sync(self.app.state.job_store, self.app.state, cols))
            self.app.state._background_tasks.add(task)
            return self._startup_result(task)
        accepted = await client.post("/sync")
        assert accepted.status_code == 202, accepted.text
        return self._job_result(accepted.json()["job_id"])

    async def _startup_result(self, task: asyncio.Task[None]) -> dict[str, Any]:
        await task
        job = next(job for job in self.app.state.job_store.list() if job.kind == "sync")
        assert job.status == JobStatus.DONE, job
        return job.result

    async def _job_result(self, job_id: str) -> dict[str, Any]:
        while (job := self.app.state.job_store.get(job_id)).status == JobStatus.RUNNING:
            await asyncio.sleep(0.01)
        assert job.status == JobStatus.DONE, job
        return job.result


@pytest.fixture
async def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_World]:
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    sources: dict[str, Path] = {}
    for dir_name in ("alpha_docs", "beta_docs"):
        source = tmp_path / dir_name
        source.mkdir()
        (source / "a.md").write_text(_DOC_TEXT)
        sources[path_to_collection_name(str(source))] = source.resolve()

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.collections = [str(path) for path in sources.values()]
    app = create_app(cfg, JobStore(path=tmp_path / "jobs.json"))
    app.state.search_store = store
    # The app's own sync, as create_app wired it (its job store and live-config reader), over the real store.
    app.state.collection_sync._pipeline = pipeline

    started, release = asyncio.Event(), asyncio.Event()
    real_ingest_file = pipeline.ingest_file
    first_call = True

    async def gated_ingest_file(*args: Any, **kwargs: Any) -> Any:
        nonlocal first_call
        if first_call:
            first_call = False
            started.set()
            await release.wait()
        return await real_ingest_file(*args, **kwargs)

    monkeypatch.setattr(pipeline, "ingest_file", gated_ingest_file)
    built = _World(app, store, pipeline, sources, started, release, tmp_path / "db")
    try:
        yield built
    finally:
        release.set()
        await store.disconnect()


async def _within_timeout(awaitable: Awaitable[Any]) -> Any:
    return await asyncio.wait_for(awaitable, timeout=_STEP_TIMEOUT_S)


@pytest.mark.parametrize("entry_point", _ENTRY_POINTS)
async def test_delete_is_refused_for_the_collection_a_sync_is_writing_and_works_once_it_has_been_synced(
    world: _World, entry_point: str
) -> None:
    async with world.client() as client:
        settled = await world.start_sync(entry_point, client)
        await _within_timeout(world.first_write_started.wait())

        refused = await _within_timeout(client.delete(f"/collections/{world.first}"))

        assert refused.status_code == 503, f"DELETE ran mid-sync: {refused.status_code} {refused.text}"
        assert refused.json() == {"detail": COLLECTION_WRITER_IN_PROGRESS_DETAIL, "code": STORE_BUSY_CODE}

        world.release_writer.set()
        result = await _within_timeout(settled)
        assert sorted(result["added"]) == sorted(world.sources)
        assert result["errors"] == []
        deleted = await _within_timeout(client.delete(f"/collections/{world.first}"))
        assert deleted.status_code == 200, deleted.text
        assert not await world.exists(world.first)


@pytest.mark.parametrize("entry_point", _ENTRY_POINTS)
async def test_a_collection_deleted_before_the_sync_reaches_it_is_not_re_created(
    world: _World, entry_point: str
) -> None:
    """DELETE of a collection the running sync has not yet reached succeeds, and the sync then leaves it alone."""
    async with world.client() as client:
        settled = await world.start_sync(entry_point, client)
        await _within_timeout(world.first_write_started.wait())

        deleted = await _within_timeout(client.delete(f"/collections/{world.second}"))
        assert deleted.status_code == 200, f"DELETE of a collection the sync is not writing: {deleted.text}"

        world.release_writer.set()
        result = await _within_timeout(settled)

        assert result["added"] == [world.first]
        assert result["errors"] == []
        assert world.second not in result["unchanged"] + result["updated"]
        assert await world.exists(world.first)
        assert not await world.exists(world.second), "the sync re-created the collection DELETE removed"
        assert world.second not in json.loads((world.db / "sync_manifest.json").read_text())


@pytest.mark.parametrize("entry_point", _ENTRY_POINTS)
async def test_delete_works_while_no_sync_runs(world: _World, entry_point: str) -> None:
    world.release_writer.set()
    async with world.client() as client:
        await _within_timeout(await world.start_sync(entry_point, client))

        deleted = await _within_timeout(client.delete(f"/collections/{world.first}"))

        assert deleted.status_code == 200, deleted.text
        assert not await world.exists(world.first)


async def test_the_sync_does_not_drop_a_collection_gone_from_the_config_while_a_writer_is_active(
    world: _World,
) -> None:
    """The sync's own drop honours the guard DELETE honours; once the writer ends, the next sync drops it."""
    world.release_writer.set()
    async with world.client() as client:
        await _within_timeout(await world.start_sync("post_sync", client))
        assert await world.exists(world.first)
        world.app.state.config.collections = [str(world.sources[world.second])]

        async with world.store.collection_writer(world.first):
            refused = await _within_timeout(await world.start_sync("post_sync", client))

        assert refused["removed"] == []
        assert len(refused["errors"]) == 1 and world.first in refused["errors"][0]
        assert await world.exists(world.first), "the sync dropped a collection a writer was still writing"

        dropped = await _within_timeout(await world.start_sync("post_sync", client))

        assert dropped["removed"] == [world.first]
        assert dropped["errors"] == []
        assert world.first not in {info.name for info in await world.store.list_collections()}


async def test_the_sync_does_not_drop_a_collection_gone_from_the_config_while_a_job_may_write_it(
    world: _World,
) -> None:
    """The app's sync consults the app's job store (``find_active_collection_job``), as DELETE does."""
    world.release_writer.set()
    async with world.client() as client:
        await _within_timeout(await world.start_sync("post_sync", client))
        world.app.state.config.collections = [str(world.sources[world.second])]
        job = world.app.state.job_store.create(collection=world.first)
        world.app.state.job_store.update(job.job_id, status=JobStatus.RUNNING)

        refused = await _within_timeout(await world.start_sync("post_sync", client))

        assert refused["removed"] == []
        assert len(refused["errors"]) == 1 and world.first in refused["errors"][0]
        assert await world.exists(world.first), "the sync dropped a collection a running job may still write"

        world.app.state.job_store.update(job.job_id, status=JobStatus.DONE)
        dropped = await _within_timeout(await world.start_sync("post_sync", client))

        assert dropped["removed"] == [world.first]
        assert dropped["errors"] == []

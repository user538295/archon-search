"""CON-12: a ``DELETE /collections/{name}`` racing a writer that creates no job cannot be undone by that writer.

Real ``SearchStore`` + ``SearchPipeline`` and the real DELETE route; the writer is paused mid-write by a gate on
``pipeline.ingest_file`` (the same call ``ingest_directory`` makes per file), so each interleaving is deterministic.
Before the fix the DELETE answered 200 and the paused writer then re-created the table and meta row it had dropped,
because ``find_active_collection_job`` cannot see an MCP ingest or a watcher sync (neither creates a job).
"""
from __future__ import annotations

import asyncio
import math
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from archon_search.config import SearchConfig
from archon_search.constants import INGEST_LOCK_TIMEOUT_S
from archon_search.jobs.store import JobStore
from archon_search.pipeline import SearchPipeline
from archon_search.server._ingest_lock import (
    COLLECTION_WRITER_IN_PROGRESS_DETAIL,
    STORE_BUSY_CODE,
)
from archon_search.server.app import create_app
from archon_search.store import SearchStore
from archon_search.sync import path_to_collection_name
from tests._fastmcp_stub import stub_mcp_tool
from tests.integration.conftest import make_real_pipeline

pytestmark = pytest.mark.integration

_STEP_TIMEOUT_S = 10.0
_EMBEDDING_DIM = 4
_DOC_TEXT = "# Title\n\nSome searchable content for the CON-12 race test.\n" * 4
_TOOL_ARGUMENTS: dict[str, Callable[[Path], dict[str, str]]] = {
    "ingest_file": lambda corpus: {"path": str(corpus / "b.md")},
    "ingest_directory": lambda corpus: {"path": str(corpus)},
}


@dataclass
class _World:
    app: Any
    store: SearchStore
    pipeline: SearchPipeline
    corpus: Path
    name: str
    first_write_started: asyncio.Event
    release_writer: asyncio.Event

    async def exists(self) -> bool:
        """Whether the collection's table or meta row is in the store."""
        listed = {info.name for info in await self.store.list_collections()}
        return self.name in listed or await self.store.get_collection_meta(self.name) is not None

    def client(self) -> httpx.AsyncClient:
        key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {key}"},
        )


@pytest.fixture
async def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_World]:
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for file_name in ("a.md", "b.md"):
        (corpus / file_name).write_text(_DOC_TEXT)
    name = path_to_collection_name(str(corpus))

    seeded = await pipeline.ingest_file(corpus / "a.md", name, embedder=pipeline._global_embedder, collection_root=corpus)
    assert seeded.status == "ok", seeded
    await pipeline.recompute_collection_meta(name, pipeline._global_embedder, force=True)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    cfg.collections = [str(corpus)]
    app = create_app(cfg, JobStore(path=tmp_path / "jobs.json"))
    app.state.search_store = store

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
    built = _World(app, store, pipeline, corpus, name, started, release)
    assert await built.exists(), "test setup: the seeded collection must exist"
    try:
        yield built
    finally:
        release.set()
        await store.disconnect()


async def _within_timeout(awaitable: Awaitable[Any]) -> Any:
    return await asyncio.wait_for(awaitable, timeout=_STEP_TIMEOUT_S)


@pytest.mark.parametrize("tool_name", sorted(_TOOL_ARGUMENTS))
async def test_delete_is_refused_while_an_mcp_ingest_is_writing_the_collection_and_works_once_it_ends(
    world: _World, tool_name: str
) -> None:
    tool = stub_mcp_tool(tool_name, world.pipeline, world.name)
    ingest = asyncio.create_task(tool(collection=world.name, **_TOOL_ARGUMENTS[tool_name](world.corpus)))
    try:
        await _within_timeout(world.first_write_started.wait())
        async with world.client() as client:
            refused = await _within_timeout(client.delete(f"/collections/{world.name}"))

            assert refused.status_code == 503, f"DELETE ran mid-ingest: {refused.status_code} {refused.text}"
            assert refused.json() == {"detail": COLLECTION_WRITER_IN_PROGRESS_DETAIL, "code": STORE_BUSY_CODE}
            assert refused.headers["Retry-After"] == str(math.ceil(INGEST_LOCK_TIMEOUT_S))
            assert await world.exists(), "the refused DELETE dropped the collection"
            assert world.app.state.config.collections == [str(world.corpus)], "the refused DELETE edited the config"

            world.release_writer.set()
            await _within_timeout(ingest)
            assert await world.exists()
            assert not world.store.has_active_writer(world.name)

            deleted = await _within_timeout(client.delete(f"/collections/{world.name}"))

            assert deleted.status_code == 200, deleted.text
            assert deleted.json() == {"name": world.name, "deleted": True}
            assert not await world.exists(), "the idle DELETE left the collection behind"
    finally:
        world.release_writer.set()
        ingest.cancel()
        await asyncio.gather(ingest, return_exceptions=True)


async def test_a_cancelled_mcp_ingest_stops_blocking_delete(world: _World) -> None:
    tool = stub_mcp_tool("ingest_directory", world.pipeline, world.name)
    ingest = asyncio.create_task(tool(collection=world.name, path=str(world.corpus)))
    try:
        await _within_timeout(world.first_write_started.wait())
        ingest.cancel()
        await asyncio.gather(ingest, return_exceptions=True)

        async with world.client() as client:
            deleted = await _within_timeout(client.delete(f"/collections/{world.name}"))

        assert deleted.status_code == 200, deleted.text
        assert not await world.exists()
    finally:
        world.release_writer.set()
        ingest.cancel()
        await asyncio.gather(ingest, return_exceptions=True)


async def test_a_watcher_sync_blocks_delete_while_it_runs_and_is_ignored_once_the_collection_is_deleted(
    world: _World,
) -> None:
    """The watcher keeps its startup snapshot after a DELETE: a change fired later must not re-create the collection."""
    from archon_search.server.app import _sync_watched_collection

    syncs: list[str] = []

    class _GatedSync:
        """``sync_collection`` that pauses mid-write and then writes the table, as the real one does."""

        async def sync_collection(self, collection_name: str, _source_path: Path) -> None:
            syncs.append(collection_name)
            world.first_write_started.set()
            await world.release_writer.wait()
            await world.store.ensure_collection(collection_name, _EMBEDDING_DIM)

    world.app.state.collection_sync = _GatedSync()
    world.app.state.sync_result = None
    source = str(world.corpus.resolve())
    sync = asyncio.create_task(_sync_watched_collection(world.app, world.name, source))
    try:
        await _within_timeout(world.first_write_started.wait())
        async with world.client() as client:
            refused = await _within_timeout(client.delete(f"/collections/{world.name}"))

            assert refused.status_code == 503, f"DELETE ran mid-sync: {refused.status_code} {refused.text}"
            assert refused.json() == {"detail": COLLECTION_WRITER_IN_PROGRESS_DETAIL, "code": STORE_BUSY_CODE}
            assert await world.exists()

            world.release_writer.set()
            await _within_timeout(sync)
            deleted = await _within_timeout(client.delete(f"/collections/{world.name}"))
            assert deleted.status_code == 200, deleted.text
            assert not await world.exists()

        await _within_timeout(_sync_watched_collection(world.app, world.name, source))

        assert syncs == [world.name], "the watcher synced a collection that was deleted from the config"
        assert not await world.exists(), "a watcher change after DELETE re-created the collection"
    finally:
        world.release_writer.set()
        sync.cancel()
        await asyncio.gather(sync, return_exceptions=True)

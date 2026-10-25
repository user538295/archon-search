"""``[ingest].max_concurrent_tasks`` caps the ingest jobs running at once across collections (SecReview #7e).

Driven through the real ASGI app: a blocking pipeline hook keeps jobs running, a further request is refused with
503 ``ingest_busy`` + ``Retry-After`` and leaves no job behind, and a finished job frees its slot.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app

_AUTH = {"Authorization": f"Bearer {'0' * 64}"}
_CAP = 2
_BUSY = 503
_ACCEPTED = 202


def _app(tmp_path: Path, cap: int = _CAP):
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.ingest.max_concurrent_tasks = cap
    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))
    app.state.search_store = _LockStore()
    return app


def _inline(collection: str) -> dict:
    return {"collection": collection, "documents": [{"text": "hello", "source_path": f"/docs/{collection}.md"}]}


def _blocking_pipeline(release: asyncio.Event):
    async def run(job_id, store, body, **kwargs):
        await release.wait()

    return run


async def _drain(app) -> None:
    while app.state._background_tasks:
        await asyncio.gather(*list(app.state._background_tasks))


@pytest.mark.asyncio
async def test_request_over_the_cap_is_refused_ingest_busy_and_leaves_no_job(tmp_path: Path) -> None:
    app = _app(tmp_path)
    release = asyncio.Event()
    app.state.ingest_pipeline = _blocking_pipeline(release)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        first = await client.post("/ingest", json=_inline("a"), headers=_AUTH)
        second = await client.post("/ingest", json=_inline("b"), headers=_AUTH)
        jobs_before = len(app.state.job_store.list())
        third = await client.post("/ingest", json=_inline("c"), headers=_AUTH)
        release.set()
        await _drain(app)

    assert (first.status_code, second.status_code) == (_ACCEPTED, _ACCEPTED)
    assert third.status_code == _BUSY
    assert third.json()["code"] == "ingest_busy"
    assert int(third.headers["Retry-After"]) > 0
    assert len(app.state.job_store.list()) == jobs_before


@pytest.mark.asyncio
async def test_finished_job_frees_its_slot(tmp_path: Path) -> None:
    app = _app(tmp_path, cap=1)
    release = asyncio.Event()
    app.state.ingest_pipeline = _blocking_pipeline(release)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.post("/ingest", json=_inline("a"), headers=_AUTH)).status_code == _ACCEPTED
        assert (await client.post("/ingest", json=_inline("b"), headers=_AUTH)).status_code == _BUSY
        release.set()
        await _drain(app)
        again = await client.post("/ingest", json=_inline("b"), headers=_AUTH)
        await _drain(app)

    assert again.status_code == _ACCEPTED


@pytest.mark.asyncio
async def test_refused_by_the_collection_lock_frees_the_slot(tmp_path: Path) -> None:
    """A request that dies before its task starts (lock 503) must not leak a slot."""
    app = _app(tmp_path, cap=1)
    busy_store = _LockStore()
    await busy_store.hold("a")
    app.state.search_store = busy_store
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        first = await client.post("/ingest", json=_inline("a"), headers=_AUTH)
        second = await client.post("/ingest", json=_inline("a"), headers=_AUTH)

    assert first.json()["code"] == second.json()["code"] == "store_busy"


class _LockStore:
    """The slice of a search store the ingest route touches: per-collection locks, and no foreign owner."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    async def hold(self, name: str) -> None:
        """Hold *name*'s lock forever, so ``acquire_collection_lock_or_503`` on it times out."""
        await self.lock_for(name).acquire()

    def lock_for(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    async def assert_collection_access(self, *_args: object) -> None:
        return None

    async def get_collection_meta(self, *_a, **_k):
        return None


@pytest.fixture(autouse=True)
def _short_lock_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("archon_search.constants.INGEST_LOCK_TIMEOUT_S", 0.05)


@pytest.mark.asyncio
async def test_add_collection_over_the_cap_is_refused(tmp_path: Path) -> None:
    app = _app(tmp_path, cap=1)
    release = asyncio.Event()
    app.state.ingest_pipeline = _blocking_pipeline(release)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("hello")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        held = await client.post("/ingest", json=_inline("a"), headers=_AUTH)
        refused = await client.post("/collections/", json={"path": str(docs)}, headers=_AUTH)
        release.set()
        await _drain(app)

    assert held.status_code == _ACCEPTED
    assert refused.status_code == _BUSY
    assert refused.json()["code"] == "ingest_busy"
    assert app.state.config.collections == []

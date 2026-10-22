"""A concurrent ``DELETE /collections/{name}`` must not be undone by a meta read-modify-write (debt CON-9).

``PATCH /collections/{name}``, the MCP ``update_collection`` tool and the in-place ``POST /collections/{name}/migrate``
read the collection's meta row, change it and write it back. ``SearchStore.update_collection_meta`` upserts, so a write
made from a row read before a DELETE dropped the collection re-inserted the row of a collection with no table. Each
caller now reads the row again under ``lock_for(name)`` and writes it with ``_locked_by_caller=True``; without the row
it answers 404 (MCP ``not_found``). A lock timeout answers 503 ``store_busy`` (MCP ``store_busy``).

The race tests drive the real routes against a real ``SearchStore`` (LanceDB) and interleave deterministically: the
caller is paused after its first, unlocked read; a real DELETE then takes the lock and parks inside its table drop;
the caller resumes and parks on the lock; only then does the DELETE finish.
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server._ingest_lock import STORE_BUSY_CODE, STORE_BUSY_DETAIL
from archon_search.server.app import create_app
from archon_search.store import STORE_SCHEMA_VERSION, SearchStore, StoreBusyError
from archon_search.sync import path_to_collection_name
from tests._fastmcp_stub import stubbed_fastmcp
from tests._parked_lock import parked_waiters, reaping

_DIM = 4
_OLD_MODEL = "BAAI/bge-small-en-v1.5"
_NEW_MODEL = "BAAI/bge-base-en-v1.5"
_TTL_S = 3_600
#: Bound on each awaited step: a hang fails the test instead of the run.
_STEP_TIMEOUT_S = 5.0
#: Seconds between two checks of the lock's waiter queue (real storage I/O needs real time, not loop iterations).
_POLL_INTERVAL_S = 0.01
_LOCK_TIMEOUT_S = 0.05
#: The migration of a collection stored at schema version 0 (``migrate_default_ttl_seconds`` is introduced at 1).
_PENDING_SCHEMA_VERSION = 0
_GATED_MIGRATION = "migrate_default_ttl_seconds"


class _Env(NamedTuple):
    app: Any
    store: SearchStore
    name: str


class _Gate:
    """A rendezvous: the paused caller sets ``reached`` and waits for ``release``."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def pause(self) -> None:
        self.reached.set()
        await asyncio.wait_for(self.release.wait(), timeout=_STEP_TIMEOUT_S)


async def _make_env(tmp_path: Path, *, schema_version: int = STORE_SCHEMA_VERSION) -> _Env:
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    name = path_to_collection_name(str(src))
    app = create_app(cfg, JobStore(path=tmp_path / "jobs.json"))
    app.state.config_path = str(tmp_path / "archon-search.toml")
    store = SearchStore(tmp_path / "lance")
    await store.connect()
    await store.ensure_collection(name, _DIM)
    await store.update_collection_meta(
        CollectionMeta(name=name, namespace="default", active_embedding_model=_OLD_MODEL, schema_version=schema_version)
    )
    app.state.search_store = store
    return _Env(app, store, name)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[_Env]:
    made = await _make_env(tmp_path)
    yield made
    await made.store.disconnect()


@pytest.fixture
async def stale_env(tmp_path: Path) -> AsyncIterator[_Env]:
    """The collection's row is at schema version 0, so the in-place migrations are pending."""
    made = await _make_env(tmp_path, schema_version=_PENDING_SCHEMA_VERSION)
    yield made
    await made.store.disconnect()


def _client(app: Any) -> httpx.AsyncClient:
    import os

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {key}"}
    )


def _pause_after_first_meta_read(monkeypatch: pytest.MonkeyPatch, store: SearchStore, gate: _Gate) -> None:
    """The first meta read (the caller's own, before any lock) returns the row, then waits for the gate."""
    real_read = store.get_collection_meta
    paused = False

    async def get_collection_meta(*args: Any, **kwargs: Any) -> CollectionMeta | None:
        nonlocal paused
        meta = await real_read(*args, **kwargs)
        if not paused:
            paused = True
            await gate.pause()
        return meta

    monkeypatch.setattr(store, "get_collection_meta", get_collection_meta)


def _pause_in_migration(monkeypatch: pytest.MonkeyPatch, store: SearchStore, gate: _Gate) -> None:
    """The in-place migration waits for the gate before it runs (the route holds no lock while it does)."""
    real_migration = getattr(store, _GATED_MIGRATION)

    async def migration() -> None:
        await gate.pause()
        await real_migration()

    monkeypatch.setattr(store, _GATED_MIGRATION, migration)


async def _until_parked_on(lock: asyncio.Lock) -> None:
    for _ in range(math.ceil(_STEP_TIMEOUT_S / _POLL_INTERVAL_S)):
        if parked_waiters(lock) >= 1:
            return
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail("the caller never waited on the collection lock DELETE held")


async def _race_delete[T](
    monkeypatch: pytest.MonkeyPatch,
    env: _Env,
    gate: _Gate,
    caller: Callable[[httpx.AsyncClient], Awaitable[T]],
) -> tuple[httpx.Response, T]:
    """Run *caller*, paused by *gate* after its unlocked read, against a real DELETE of the collection.

    DELETE takes the collection lock and parks in its table drop; the caller is then released and (having read the
    still-present row) waits for that lock; DELETE finishes only after that. Returns (DELETE's response, the caller's).
    """
    lock = env.store.lock_for(env.name)
    dropping, finish_drop = asyncio.Event(), asyncio.Event()
    real_drop = env.store.drop_collection

    async def drop_collection(*args: Any, **kwargs: Any) -> None:
        dropping.set()
        await asyncio.wait_for(finish_drop.wait(), timeout=_STEP_TIMEOUT_S)
        await real_drop(*args, **kwargs)

    monkeypatch.setattr(env.store, "drop_collection", drop_collection)
    async with _client(env.app) as client:
        caller_task = asyncio.create_task(caller(client))
        async with reaping({caller_task}):
            await asyncio.wait_for(gate.reached.wait(), timeout=_STEP_TIMEOUT_S)
            delete_task = asyncio.create_task(client.delete(f"/collections/{env.name}"))
            async with reaping({delete_task}):
                await asyncio.wait_for(dropping.wait(), timeout=_STEP_TIMEOUT_S)
                gate.release.set()
                await _until_parked_on(lock)
                assert not caller_task.done(), "the caller answered while DELETE held the collection lock"
                finish_drop.set()
                delete_response = await asyncio.wait_for(delete_task, timeout=_STEP_TIMEOUT_S)
                result = await asyncio.wait_for(caller_task, timeout=_STEP_TIMEOUT_S)
    return delete_response, result


async def _meta_rows(store: SearchStore, name: str) -> list[CollectionMeta]:
    return [m for m in await store.get_all_collections_meta() if m.name == name]


def _assert_collection_stayed_dropped(env: _Env, delete_response: httpx.Response, rows: list[CollectionMeta]) -> None:
    assert delete_response.status_code == 200, delete_response.text
    assert rows == [], "the dropped collection's meta row was re-inserted"
    lock = env.store.lock_for(env.name)
    assert not lock.locked(), "the caller kept the collection lock"


# ---------------------------------------------------------------------------
# PATCH /collections/{name}
# ---------------------------------------------------------------------------

_PATCH_BODIES = [
    pytest.param({"default_ttl_seconds": _TTL_S}, id="default-ttl"),
    pytest.param({"embedding_model": _NEW_MODEL}, id="embedding-model"),
]


@pytest.mark.parametrize("body", _PATCH_BODIES)
async def test_patch_racing_delete_answers_404_and_does_not_reinsert_the_meta_row(
    monkeypatch: pytest.MonkeyPatch, env: _Env, body: dict[str, Any]
) -> None:
    gate = _Gate()
    _pause_after_first_meta_read(monkeypatch, env.store, gate)

    with patch("archon_search.server.routes_collections.validate_embedding_model", AsyncMock(return_value=_DIM)):
        delete_response, response = await _race_delete(
            monkeypatch, env, gate, lambda client: client.patch(f"/collections/{env.name}", json=body)
        )

    _assert_collection_stayed_dropped(env, delete_response, await _meta_rows(env.store, env.name))
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": f"Collection {env.name!r} not found"}


@pytest.mark.parametrize("body", _PATCH_BODIES)
async def test_patch_writes_the_row_it_read_under_the_lock_not_the_stale_one(
    monkeypatch: pytest.MonkeyPatch, env: _Env, body: dict[str, Any]
) -> None:
    """A change another write made between the PATCH's unlocked read and its write is kept, not overwritten."""
    gate = _Gate()
    _pause_after_first_meta_read(monkeypatch, env.store, gate)
    lock = env.store.lock_for(env.name)
    described = "written while the PATCH waited"

    with patch("archon_search.server.routes_collections.validate_embedding_model", AsyncMock(return_value=_DIM)):
        async with _client(env.app) as client:
            request = asyncio.create_task(client.patch(f"/collections/{env.name}", json=body))
            async with reaping({request}):
                await asyncio.wait_for(gate.reached.wait(), timeout=_STEP_TIMEOUT_S)
                await lock.acquire()  # another write (e.g. an ingest's meta refresh) holds the lock
                try:
                    gate.release.set()
                    await _until_parked_on(lock)
                    stale = await env.store.get_collection_meta(env.name, "default")
                    assert stale is not None
                    stale.description = described
                    await env.store.update_collection_meta(stale, _locked_by_caller=True)
                finally:
                    lock.release()
                response = await asyncio.wait_for(request, timeout=_STEP_TIMEOUT_S)

    assert response.status_code == 200, response.text
    assert response.json()["description"] == described
    stored = await env.store.get_collection_meta(env.name, "default")
    assert stored is not None and stored.description == described


@pytest.mark.parametrize("body", _PATCH_BODIES)
async def test_patch_answers_503_store_busy_when_the_lock_is_not_acquired_in_time(
    monkeypatch: pytest.MonkeyPatch, env: _Env, body: dict[str, Any]
) -> None:
    monkeypatch.setattr("archon_search.constants.INGEST_LOCK_TIMEOUT_S", _LOCK_TIMEOUT_S)
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _LOCK_TIMEOUT_S)
    lock = env.store.lock_for(env.name)

    await lock.acquire()
    try:
        with patch("archon_search.server.routes_collections.validate_embedding_model", AsyncMock(return_value=_DIM)):
            async with _client(env.app) as client:
                response = await asyncio.wait_for(
                    client.patch(f"/collections/{env.name}", json=body), timeout=_STEP_TIMEOUT_S
                )
    finally:
        lock.release()

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": STORE_BUSY_DETAIL, "code": STORE_BUSY_CODE}
    assert response.headers.get("Retry-After") == str(math.ceil(_LOCK_TIMEOUT_S))
    stored = await env.store.get_collection_meta(env.name, "default")
    assert stored is not None and stored.default_ttl_seconds is None and stored.active_embedding_model == _OLD_MODEL


# ---------------------------------------------------------------------------
# POST /collections/{name}/migrate (in-place migrations)
# ---------------------------------------------------------------------------


async def test_in_place_migrate_racing_delete_answers_404_and_does_not_reinsert_the_meta_row(
    monkeypatch: pytest.MonkeyPatch, stale_env: _Env
) -> None:
    gate = _Gate()
    _pause_in_migration(monkeypatch, stale_env.store, gate)

    delete_response, response = await _race_delete(
        monkeypatch,
        stale_env,
        gate,
        lambda client: client.post(f"/collections/{stale_env.name}/migrate", json={}),
    )

    _assert_collection_stayed_dropped(stale_env, delete_response, await _meta_rows(stale_env.store, stale_env.name))
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": f"Collection {stale_env.name!r} not found"}


async def test_in_place_migrate_answers_503_store_busy_when_the_lock_is_not_acquired_in_time(
    monkeypatch: pytest.MonkeyPatch, stale_env: _Env
) -> None:
    monkeypatch.setattr("archon_search.constants.INGEST_LOCK_TIMEOUT_S", _LOCK_TIMEOUT_S)
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _LOCK_TIMEOUT_S)
    lock = stale_env.store.lock_for(stale_env.name)

    await lock.acquire()
    try:
        async with _client(stale_env.app) as client:
            response = await asyncio.wait_for(
                client.post(f"/collections/{stale_env.name}/migrate", json={}), timeout=_STEP_TIMEOUT_S
            )
    finally:
        lock.release()

    assert response.status_code == 503, response.text
    assert response.json() == {"detail": STORE_BUSY_DETAIL, "code": STORE_BUSY_CODE}
    assert response.headers.get("Retry-After") == str(math.ceil(_LOCK_TIMEOUT_S))
    stored = await stale_env.store.get_collection_meta(stale_env.name, "default")
    assert stored is not None and stored.schema_version == _PENDING_SCHEMA_VERSION


async def test_in_place_migrate_records_the_schema_version(stale_env: _Env) -> None:
    async with _client(stale_env.app) as client:
        response = await asyncio.wait_for(
            client.post(f"/collections/{stale_env.name}/migrate", json={}), timeout=_STEP_TIMEOUT_S
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"migrations_applied": ["migrate_expires_at_and_scopes", _GATED_MIGRATION]}
    stored = await stale_env.store.get_collection_meta(stale_env.name, "default")
    assert stored is not None and stored.schema_version == STORE_SCHEMA_VERSION
    assert not stale_env.store.lock_for(stale_env.name).locked()


# ---------------------------------------------------------------------------
# MCP update_collection
# ---------------------------------------------------------------------------


def _mcp_update_collection(env: _Env) -> Callable[..., Awaitable[dict[str, Any]]]:
    with stubbed_fastmcp():
        from archon_search.server import mcp as mcp_module

        pipeline = MagicMock()
        pipeline.store = env.store
        app = mcp_module.create_app(pipeline, "default", config=env.app.state.config, job_store=env.app.state.job_store)
    tool = app.tools["update_collection"]

    async def call() -> dict[str, Any]:
        return await tool(collection_name=env.name, embedding_model=_NEW_MODEL, ctx=MagicMock())

    return call


async def test_mcp_update_collection_racing_delete_answers_not_found_and_does_not_reinsert_the_meta_row(
    monkeypatch: pytest.MonkeyPatch, env: _Env
) -> None:
    gate = _Gate()
    _pause_after_first_meta_read(monkeypatch, env.store, gate)
    update_collection = _mcp_update_collection(env)

    async def call(_client: httpx.AsyncClient) -> dict[str, Any]:
        return await update_collection()

    with patch("archon_search.server.mcp.validate_embedding_model", AsyncMock(return_value=_DIM)):
        delete_response, result = await _race_delete(monkeypatch, env, gate, call)

    _assert_collection_stayed_dropped(env, delete_response, await _meta_rows(env.store, env.name))
    assert result == {"error": f"Collection {env.name!r} not found", "code": "not_found"}


async def test_mcp_update_collection_answers_store_busy_when_the_lock_is_not_acquired_in_time(
    monkeypatch: pytest.MonkeyPatch, env: _Env
) -> None:
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _LOCK_TIMEOUT_S)
    update_collection = _mcp_update_collection(env)
    lock = env.store.lock_for(env.name)

    await lock.acquire()
    try:
        with patch("archon_search.server.mcp.validate_embedding_model", AsyncMock(return_value=_DIM)):
            result = await asyncio.wait_for(update_collection(), timeout=_STEP_TIMEOUT_S)
    finally:
        lock.release()

    assert result["code"] == "store_busy", result
    stored = await env.store.get_collection_meta(env.name, "default")
    assert stored is not None and stored.active_embedding_model == _OLD_MODEL


# ---------------------------------------------------------------------------
# SearchStore.locked_collection_meta
# ---------------------------------------------------------------------------


async def test_locked_collection_meta_yields_the_current_row_under_the_lock(env: _Env) -> None:
    lock = env.store.lock_for(env.name)

    async with env.store.locked_collection_meta(env.name, "default") as meta:
        assert lock.locked()
        assert meta is not None and meta.name == env.name and meta.active_embedding_model == _OLD_MODEL

    assert not lock.locked()
    assert lock is env.store.lock_for(env.name), "the lock entry of a collection that still exists was discarded"


async def test_locked_collection_meta_yields_none_for_a_missing_row_and_discards_its_lock_entry(env: _Env) -> None:
    await env.store.delete_collection_meta(env.name, "default")
    lock = env.store.lock_for(env.name)

    async with env.store.locked_collection_meta(env.name, "default") as meta:
        assert meta is None
        assert lock.locked()

    assert not lock.locked()
    assert env.store.lock_for(env.name) is not lock, "the lock entry of a collection without a row leaked"


async def test_locked_collection_meta_does_not_yield_another_namespaces_row(env: _Env) -> None:
    async with env.store.locked_collection_meta(env.name, "tenant-x") as meta:
        assert meta is None


async def test_locked_collection_meta_releases_the_lock_when_its_body_raises(env: _Env) -> None:
    lock = env.store.lock_for(env.name)

    with pytest.raises(RuntimeError, match="boom"):
        async with env.store.locked_collection_meta(env.name, "default"):
            raise RuntimeError("boom")

    assert not lock.locked()


async def test_locked_collection_meta_raises_store_busy_when_the_lock_is_not_acquired_in_time(
    monkeypatch: pytest.MonkeyPatch, env: _Env
) -> None:
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _LOCK_TIMEOUT_S)
    lock = env.store.lock_for(env.name)

    await lock.acquire()
    try:
        with pytest.raises(StoreBusyError):
            async with env.store.locked_collection_meta(env.name, "default"):
                pytest.fail("the body ran without the lock")
    finally:
        lock.release()

    assert not lock.locked()


async def test_apply_in_place_migrations_reports_a_collection_whose_row_is_gone(stale_env: _Env) -> None:
    pending = await stale_env.store.pending_migrations(stale_env.name, "default")
    await stale_env.store.delete_collection_meta(stale_env.name, "default")

    applied = await stale_env.store.apply_in_place_migrations(stale_env.name, "default", pending)

    assert applied is False
    assert await _meta_rows(stale_env.store, stale_env.name) == []

"""BE-23 (C5, S80): the lifecycle's slot against a real LanceDB store, with real collection-lock contention.

The slot covers the sample read and the model call only. A generation whose guarded write waits on a held collection
lock has already released it, so another collection's generation is not delayed. ``tests/_parked_lock.py`` parks the
write deterministically.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.description_generator import DescriptionGenerated, DescriptionOutcome
from archon_search.description_lifecycle import (
    DescriptionLifecycle,
    GenerationKind,
    GenerationResult,
    RequestOutcome,
)
from archon_search.description_writer import DescriptionEmbedder, DescriptionWriter
from archon_search.store import SearchStore
from tests._parked_lock import until_parked

pytestmark = pytest.mark.integration

_NAMESPACE = "default"
_STUCK = "stuck"
_FREE = "free"
_MODEL = "model-a"
_VECTOR = [0.25, 0.5]
_SETTLE_TIMEOUT_S = 10.0


class _Generator:
    """Stands in for the model call: a description named after the collection, no store read."""

    def __init__(self) -> None:
        self.started: list[str] = []

    async def generate(self, collection: str, namespace: str) -> DescriptionOutcome:
        self.started.append(collection)
        return DescriptionGenerated(text=f"A description of {collection}.")


class _Embedder:
    def __init__(self, embedded: asyncio.Event) -> None:
        self._embedded = embedded

    async def embed_one(self, text: str) -> list[float]:
        self._embedded.set()
        return list(_VECTOR)


class _Cache:
    def __init__(self, embedded: asyncio.Event) -> None:
        self._embedded = embedded

    async def get_or_load(self, model_name: str) -> DescriptionEmbedder:
        return _Embedder(self._embedded)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SearchStore]:
    search_store = SearchStore(tmp_path / "db", config=SearchConfig())
    await search_store.connect()
    for name in (_STUCK, _FREE):
        await search_store.update_collection_meta(
            CollectionMeta(name=name, doc_count=3, chunk_count=3, active_embedding_model=_MODEL)
        )
    yield search_store
    await search_store.disconnect()


async def _description(store: SearchStore, name: str) -> str | None:
    meta = await store.get_collection_meta(name, _NAMESPACE)
    assert meta is not None
    return meta.description


async def test_slot_released_before_embedding_so_slow_lock_does_not_stall_others(store: SearchStore) -> None:
    """A collection whose guarded write waits on a held lock does not delay another collection's whole generation."""
    generator = _Generator()
    stuck_embedded = asyncio.Event()
    lifecycle = DescriptionLifecycle(
        store, generator, DescriptionWriter(store, _Cache(stuck_embedded), default_embedding_model=_MODEL)
    )
    lock = store.lock_for(_STUCK)
    await lock.acquire()  # an operator-side writer holds the collection while the generation tries to write
    try:
        stuck = await lifecycle.request_generation(_STUCK, _NAMESPACE, GenerationKind.INGEST)
        assert stuck.outcome is RequestOutcome.ACCEPTED
        await asyncio.wait_for(stuck_embedded.wait(), timeout=_SETTLE_TIMEOUT_S)
        await until_parked(lock, 1)  # the stuck collection has its text and its vector and waits for the lock to write

        free = await lifecycle.request_generation(_FREE, _NAMESPACE, GenerationKind.INGEST)
        settlement = await asyncio.wait_for(free.settled, timeout=_SETTLE_TIMEOUT_S)

        assert settlement.result is GenerationResult.GENERATED
        assert await _description(store, _FREE) == f"A description of {_FREE}."
        assert not stuck.settled.done()
        assert lifecycle.is_pending(_STUCK)
        assert not lifecycle.is_pending(_FREE)
        assert generator.started == [_STUCK, _FREE]

        lock.release()
        assert (await asyncio.wait_for(stuck.settled, timeout=_SETTLE_TIMEOUT_S)).result is GenerationResult.GENERATED
        assert await _description(store, _STUCK) == f"A description of {_STUCK}."
    finally:
        if lock.locked():
            lock.release()
        await lifecycle.drain()

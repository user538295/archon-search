"""BE-19 (C3, S22, S74): the guarded description write against a real LanceDB store, with real lock contention.

``tests/_parked_lock.py`` parks writers on the held collection lock, so each interleave below is deterministic:
the lock is a FIFO, and the order the writers parked in is the order they run in.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.description_write import (
    UNCONDITIONAL,
    DescriptionFields,
    DescriptionWriteOutcome,
    ExpectedState,
    FieldChange,
    WriteGuard,
)
from archon_search.store import SearchStore
from tests._parked_lock import reaping, until_parked

pytestmark = pytest.mark.integration

_NAME = "docs"
_NAMESPACE = "default"
_OLD_TEXT = "generated before the slow step"
_GENERATED_TEXT = "generated after the slow step"
_MANUAL_TEXT = "operator wrote this meanwhile"
_STAMP = datetime(2026, 1, 3, 4, 5, 6, tzinfo=UTC)
_GENERATED_STAMP = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
_MANUAL_STAMP = datetime(2026, 2, 4, 4, 5, 6, tzinfo=UTC)
_SHORT_LOCK_TIMEOUT_S = 0.05
_PARKED_WRITERS = 2
_SEEDED_BASELINE = 2
_GENERATED_BASELINE = 3
_GENERATED_VECTOR = [0.1, 0.2]
_MANUAL_VECTOR = [0.3, 0.4]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SearchStore]:
    search_store = SearchStore(tmp_path / "db", config=SearchConfig())
    await search_store.connect()
    await search_store.update_collection_meta(
        CollectionMeta(
            name=_NAME,
            description=_OLD_TEXT,
            doc_count=2,
            active_embedding_model="model-active",
            last_described=_STAMP,
            described_at_doc_count=_SEEDED_BASELINE,
            description_source="generated",
            description_status="ok",
        )
    )
    yield search_store
    await search_store.disconnect()


def _generated_fields() -> DescriptionFields:
    return DescriptionFields(
        description=FieldChange.set_to(_GENERATED_TEXT),
        source=FieldChange.set_to("generated"),
        status=FieldChange.set_to("ok"),
        last_described=FieldChange.set_to(_GENERATED_STAMP),
        described_at_doc_count=FieldChange.set_to(_GENERATED_BASELINE),
        description_embedding=FieldChange.set_to(list(_GENERATED_VECTOR)),
    )


def _manual_fields() -> DescriptionFields:
    return DescriptionFields(
        description=FieldChange.set_to(_MANUAL_TEXT),
        source=FieldChange.set_to("manual"),
        status=FieldChange.set_to("ok"),
        last_described=FieldChange.set_to(_MANUAL_STAMP),
        described_at_doc_count=FieldChange.keep(),
        description_embedding=FieldChange.set_to(list(_MANUAL_VECTOR)),
    )


async def _stored(store: SearchStore) -> CollectionMeta:
    meta = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert meta is not None
    return meta


@pytest.mark.parametrize("manual_set_first", [True, False], ids=["manual_set_lands_first", "generation_lands_first"])
async def test_manual_set_during_slow_step_survives_interleave(store: SearchStore, manual_set_first: bool) -> None:
    """A generation read its row and is in its slow step; the operator sets a manual description meanwhile.

    Both writers then wait on the collection lock. Whichever order they run in, the manual text survives and the
    row ends as one serial outcome: either the generation lands first (its baseline stays, because a set leaves the
    baseline alone) or it is refused whole (the baseline stays at the seeded value).
    """
    expected = ExpectedState(description=_OLD_TEXT, source="generated", last_described=_STAMP)
    lock = store.lock_for(_NAME)
    await lock.acquire()  # a third writer holds the collection while both queue behind it
    tasks: set[asyncio.Task[DescriptionWriteOutcome]] = set()

    def start(write_fields: DescriptionFields, guard: WriteGuard) -> asyncio.Task[DescriptionWriteOutcome]:
        task = asyncio.create_task(store.write_description(_NAME, _NAMESPACE, write_fields, guard))
        tasks.add(task)
        return task

    async with reaping(tasks):
        if manual_set_first:
            manual_task = start(_manual_fields(), UNCONDITIONAL)
            await until_parked(lock, 1)
            generated_task = start(_generated_fields(), expected)
        else:
            generated_task = start(_generated_fields(), expected)
            await until_parked(lock, 1)
            manual_task = start(_manual_fields(), UNCONDITIONAL)
        await until_parked(lock, _PARKED_WRITERS)
        lock.release()
        manual_outcome, generated_outcome = await asyncio.gather(manual_task, generated_task)

    assert manual_outcome is DescriptionWriteOutcome.WRITTEN
    expected_generated = (
        DescriptionWriteOutcome.REFUSED_MANUAL if manual_set_first else DescriptionWriteOutcome.WRITTEN
    )
    assert generated_outcome is expected_generated
    final = await _stored(store)
    assert final.description == _MANUAL_TEXT
    assert final.description_source == "manual"
    assert final.description_status == "ok"
    assert final.last_described == _MANUAL_STAMP
    assert final.description_embedding == _MANUAL_VECTOR
    assert final.described_at_doc_count == (_SEEDED_BASELINE if manual_set_first else _GENERATED_BASELINE)
    assert not lock.locked()


async def test_a_second_generation_that_finishes_first_makes_the_slower_one_stale(store: SearchStore) -> None:
    expected = ExpectedState(description=_OLD_TEXT, source="generated", last_described=_STAMP)
    lock = store.lock_for(_NAME)
    await lock.acquire()
    tasks = [
        asyncio.create_task(store.write_description(_NAME, _NAMESPACE, _generated_fields(), expected))
        for _ in range(_PARKED_WRITERS)
    ]
    async with reaping(set(tasks)):
        await until_parked(lock, _PARKED_WRITERS)
        lock.release()
        outcomes = await asyncio.gather(*tasks)

    assert outcomes == [DescriptionWriteOutcome.WRITTEN, DescriptionWriteOutcome.REFUSED_STALE]
    assert (await _stored(store)).description == _GENERATED_TEXT


async def test_lock_timeout_is_reported_not_swallowed(store: SearchStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """The collection stays busy past the lock wait: the outcome is ``lockTimeout`` (S22), which the caller records
    as a failed description; nothing is written and nothing is raised."""
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _SHORT_LOCK_TIMEOUT_S)
    before = await _stored(store)
    lock = store.lock_for(_NAME)
    await lock.acquire()
    try:
        outcome = await store.write_description(_NAME, _NAMESPACE, _generated_fields(), UNCONDITIONAL)
    finally:
        lock.release()

    assert outcome is DescriptionWriteOutcome.LOCK_TIMEOUT
    assert await _stored(store) == before

    retry = await store.write_description(_NAME, _NAMESPACE, _generated_fields(), UNCONDITIONAL)
    assert retry is DescriptionWriteOutcome.WRITTEN, "the timed-out write must not leave the lock or the row broken"

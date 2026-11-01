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
    expected = ExpectedState(description=_OLD_TEXT, source="generated", last_described=_STAMP, status="ok")
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
    expected = ExpectedState(description=_OLD_TEXT, source="generated", last_described=_STAMP, status="ok")
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


_EMBEDDING_DIM = 4
_OTHER_NAME = "other"
_FOREIGN_NAMESPACE = "team-foreign"


async def test_the_description_version_moves_only_when_a_description_write_lands(
    store: SearchStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader that remembered a version learns that the description state changed: a landed set (and so a clear, an
    import, a generated text or a recorded failure) moves it; a lock timeout, a refusal, a missing collection and another
    collection's write do not."""
    assert store.description_version(_NAME) == 0
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _SHORT_LOCK_TIMEOUT_S)
    lock = store.lock_for(_NAME)
    await lock.acquire()
    try:
        assert await store.write_description(_NAME, _NAMESPACE, _manual_fields(), UNCONDITIONAL) is (
            DescriptionWriteOutcome.LOCK_TIMEOUT
        )
    finally:
        lock.release()
    assert store.description_version(_NAME) == 0

    assert await store.write_description(_NAME, _NAMESPACE, _manual_fields(), UNCONDITIONAL) is (
        DescriptionWriteOutcome.WRITTEN
    )
    assert store.description_version(_NAME) == 1

    stale = ExpectedState(description=_OLD_TEXT, source="generated", last_described=_STAMP, status="ok")
    assert await store.write_description(_NAME, _NAMESPACE, _generated_fields(), stale) is (
        DescriptionWriteOutcome.REFUSED_MANUAL
    )
    assert await store.write_description(_OTHER_NAME, _NAMESPACE, _generated_fields(), UNCONDITIONAL) is (
        DescriptionWriteOutcome.COLLECTION_MISSING
    )
    assert (store.description_version(_NAME), store.description_version(_OTHER_NAME)) == (1, 0)

    cleared = DescriptionFields(
        description=FieldChange.clear(),
        source=FieldChange.clear(),
        status=FieldChange.clear(),
        last_described=FieldChange.clear(),
        described_at_doc_count=FieldChange.clear(),
        description_embedding=FieldChange.clear(),
    )
    assert await store.write_description(_NAME, _NAMESPACE, cleared, UNCONDITIONAL) is DescriptionWriteOutcome.WRITTEN
    assert store.description_version(_NAME) == 2


async def test_only_deleting_a_meta_row_moves_the_description_version_not_a_table_drop(store: SearchStore) -> None:
    """A collection whose meta row is deleted and created again under the same name is a new collection: nothing
    remembered about the old one may apply to it. A table drop alone leaves the meta row (and its description state)
    in place, and a delete that removes no row (absent row, another namespace) changes nothing."""
    await store.ensure_collection(_NAME, _EMBEDDING_DIM)

    await store.drop_collection(_NAME, namespace=_NAMESPACE)
    assert store.description_version(_NAME) == 0

    await store.delete_collection_meta(_OTHER_NAME, _NAMESPACE)  # no such row
    await store.delete_collection_meta(_NAME, _FOREIGN_NAMESPACE)  # another namespace's name
    assert (store.description_version(_NAME), store.description_version(_OTHER_NAME)) == (0, 0)

    await store.delete_collection_meta(_NAME, _NAMESPACE)
    assert store.description_version(_NAME) == 1
    await store.delete_collection_meta(_NAME, _NAMESPACE)  # already gone
    assert store.description_version(_NAME) == 1


async def test_a_write_whose_cleanup_raised_after_the_merge_is_still_counted(
    store: SearchStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version moves when the merge landed, not when the whole write returned: a reader must not keep a memory of
    the row as it was when the new text is already stored."""

    async def failing_cleanup(table: object, name: str) -> None:
        raise OSError("legacy row cleanup failed")

    monkeypatch.setattr("archon_search.store._delete_legacy_null_namespace_meta", failing_cleanup)

    with pytest.raises(OSError, match="cleanup failed"):
        await store.write_description(_NAME, _NAMESPACE, _manual_fields(), UNCONDITIONAL)

    assert (await _stored(store)).description == _MANUAL_TEXT
    assert store.description_version(_NAME) == 1

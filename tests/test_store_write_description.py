"""BE-19 (C3, S22, S74): ``SearchStore.write_description``, the guarded description write.

The write takes the collection lock itself, re-reads the row inside it, applies the guard, changes only the
description fields (each stated as keep, set or clear) and writes the row once.
"""
from __future__ import annotations

import dataclasses
import logging
import math
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from lancedb.table import AsyncTable

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.description_write import (
    _META_FIELD_OF,
    UNCONDITIONAL,
    DescriptionFields,
    DescriptionWriteOutcome,
    ExpectedState,
    FieldChange,
    FieldOp,
    WriteGuard,
)
from archon_search.store import SearchStore

_NAME = "docs"
_NAMESPACE = "ns-a"
_OLD_TEXT = "old generated text"
_NEW_TEXT = "new generated text"
_MANUAL_TEXT = "operator wrote this"
_OLD_VECTOR = [0.1, 0.2]
_NEW_VECTOR = [0.3, 0.4]
_OLD_STAMP = datetime(2026, 1, 3, 4, 5, 6, 789012, tzinfo=UTC)
_NEW_STAMP = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
_OLD_BASELINE = 5
_NEW_BASELINE = 9
_EXPECTED_GENERATED = ExpectedState(
    description=_OLD_TEXT, source="generated", last_described=_OLD_STAMP, status="ok"
)
_SHORT_LOCK_TIMEOUT_S = 0.05

#: Every ``CollectionMeta`` field a description write owns.
_DESCRIPTION_FIELDS = frozenset(
    {
        "description",
        "description_source",
        "description_status",
        "last_described",
        "described_at_doc_count",
        "description_embedding",
    }
)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SearchStore]:
    search_store = SearchStore(tmp_path / "db", config=SearchConfig())
    await search_store.connect()
    yield search_store
    await search_store.disconnect()


def _generated_row(**overrides: Any) -> CollectionMeta:
    """A collection that holds a generated description, with every unrelated field non-default."""
    meta = CollectionMeta(
        name=_NAME,
        description=_OLD_TEXT,
        centroid=[0.5, 1.0],
        centroid_sum=[1.0, 2.0],
        mutations_since_recompute=3,
        needs_recompute=True,
        doc_count=2,
        chunk_count=4,
        active_embedding_model="model-active",
        pending_embedding_model="model-pending",
        needs_reindex=True,
        reindex_job_id="reindex-job",
        community_rebuild_job_id="community-job",
        metadata_reindex_job_id="metadata-job",
        last_indexed=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        last_described=_OLD_STAMP,
        described_at_doc_count=_OLD_BASELINE,
        namespace=_NAMESPACE,
        description_embedding=list(_OLD_VECTOR),
        schema_version=7,
        default_ttl_seconds=3600,
        description_source="generated",
        description_status="ok",
    )
    return dataclasses.replace(meta, **overrides)


def _fields(**changes: FieldChange[Any]) -> DescriptionFields:
    """Every description field kept, except the ones in *changes*."""
    keep: FieldChange[Any] = FieldChange.keep()
    base: dict[str, FieldChange[Any]] = {
        "description": keep,
        "source": keep,
        "status": keep,
        "last_described": keep,
        "described_at_doc_count": keep,
        "description_embedding": keep,
    }
    return DescriptionFields(**{**base, **changes})


def _generated_fields() -> DescriptionFields:
    return _fields(
        description=FieldChange.set_to(_NEW_TEXT),
        source=FieldChange.set_to("generated"),
        status=FieldChange.set_to("ok"),
        last_described=FieldChange.set_to(_NEW_STAMP),
        described_at_doc_count=FieldChange.set_to(_NEW_BASELINE),
        description_embedding=FieldChange.set_to(list(_NEW_VECTOR)),
    )


def _manual_fields() -> DescriptionFields:
    return _fields(
        description=FieldChange.set_to(_MANUAL_TEXT),
        source=FieldChange.set_to("manual"),
        status=FieldChange.set_to("ok"),
        last_described=FieldChange.set_to(_NEW_STAMP),
        description_embedding=FieldChange.set_to(list(_NEW_VECTOR)),
    )


def _clear_fields() -> DescriptionFields:
    return DescriptionFields(
        description=FieldChange.clear(),
        source=FieldChange.clear(),
        status=FieldChange.clear(),
        last_described=FieldChange.clear(),
        described_at_doc_count=FieldChange.clear(),
        description_embedding=FieldChange.clear(),
    )


def _failure_fields() -> DescriptionFields:
    return _fields(status=FieldChange.set_to("failed"))


def _expected_of(meta: CollectionMeta) -> ExpectedState:
    return ExpectedState(
        description=meta.description,
        source=meta.description_source,
        last_described=meta.last_described,
        status=meta.description_status,
    )


async def _stored(store: SearchStore) -> CollectionMeta:
    meta = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert meta is not None
    return meta


def _assert_only_description_fields_changed(before: CollectionMeta, after: CollectionMeta) -> None:
    for field in dataclasses.fields(CollectionMeta):
        if field.name in _DESCRIPTION_FIELDS:
            continue
        assert getattr(after, field.name) == getattr(before, field.name), f"the write changed {field.name}"


async def _write(
    store: SearchStore, fields: DescriptionFields, guard: WriteGuard = UNCONDITIONAL
) -> DescriptionWriteOutcome:
    return await store.write_description(_NAME, _NAMESPACE, fields, guard)


def _count_merge_inserts(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Start counting LanceDB ``merge_insert`` calls (the row commit); one entry per call."""
    calls: list[Any] = []
    original = AsyncTable.merge_insert

    def counting(self: AsyncTable, on: Any) -> Any:
        calls.append(on)
        return original(self, on)

    monkeypatch.setattr(AsyncTable, "merge_insert", counting)
    return calls


# ---------------------------------------------------------------------------
# The two tests the plan names
# ---------------------------------------------------------------------------


async def test_generated_write_refused_over_manual_row(store: SearchStore) -> None:
    """A manual set that lands during a slow step is never overwritten (S74)."""
    seeded = _generated_row()
    await store.update_collection_meta(seeded)
    expected = _expected_of(seeded)  # the state the generation started from, before its slow step

    assert await _write(store, _manual_fields()) is DescriptionWriteOutcome.WRITTEN
    manual_row = await _stored(store)

    outcome = await _write(store, _generated_fields(), expected)

    assert outcome is DescriptionWriteOutcome.REFUSED_MANUAL
    assert await _stored(store) == manual_row, "a refused write must leave the row exactly as it was"
    assert manual_row.description == _MANUAL_TEXT
    assert manual_row.description_source == "manual"


async def test_stale_write_and_recorded_failure_refused_after_clear(store: SearchStore) -> None:
    """A clear is not undone by a stale write, and no ``failed`` lands on a cleared row (S74)."""
    seeded = _generated_row()
    await store.update_collection_meta(seeded)
    expected = _expected_of(seeded)

    assert await _write(store, _clear_fields()) is DescriptionWriteOutcome.WRITTEN
    cleared = await _stored(store)
    assert cleared.description is None
    assert cleared.description_status is None

    assert await _write(store, _generated_fields(), expected) is DescriptionWriteOutcome.REFUSED_STALE
    assert await _write(store, _failure_fields(), expected) is DescriptionWriteOutcome.REFUSED_STALE

    assert await _stored(store) == cleared, "neither the stale text nor a recorded failure may land on a cleared row"


# ---------------------------------------------------------------------------
# keep / set / clear per field, and nothing else changes
# ---------------------------------------------------------------------------


async def test_write_applies_keep_set_and_clear_per_field(store: SearchStore) -> None:
    seeded = _generated_row()
    await store.update_collection_meta(seeded)

    outcome = await _write(
        store,
        _fields(
            description=FieldChange.clear(),
            source=FieldChange.clear(),
            status=FieldChange.clear(),
            last_described=FieldChange.set_to(_NEW_STAMP),
            description_embedding=FieldChange.clear(),
        ),
    )

    assert outcome is DescriptionWriteOutcome.WRITTEN
    stored = await _stored(store)
    assert stored.description is None  # clear
    assert stored.description_source is None  # clear
    assert stored.description_status is None  # clear
    assert stored.last_described == _NEW_STAMP  # set
    assert stored.described_at_doc_count == _OLD_BASELINE  # keep
    assert stored.description_embedding is None  # clear
    _assert_only_description_fields_changed(seeded, stored)


async def test_write_sets_the_routing_vector_with_the_text_in_one_step(store: SearchStore) -> None:
    seeded = _generated_row(description_embedding=None)
    await store.update_collection_meta(seeded)

    assert await _write(store, _generated_fields(), _expected_of(seeded)) is DescriptionWriteOutcome.WRITTEN

    stored = await _stored(store)
    assert stored.description == _NEW_TEXT
    assert stored.description_embedding == _NEW_VECTOR
    assert stored.described_at_doc_count == _NEW_BASELINE
    _assert_only_description_fields_changed(seeded, stored)


async def test_write_keeps_a_vector_the_caller_leaves_alone(store: SearchStore) -> None:
    seeded = _generated_row()
    await store.update_collection_meta(seeded)

    outcome = await _write(store, _failure_fields(), _expected_of(seeded))

    assert outcome is DescriptionWriteOutcome.WRITTEN
    stored = await _stored(store)
    assert stored.description_status == "failed"
    assert stored.description_embedding == _OLD_VECTOR


async def test_clear_resets_every_description_field_and_nothing_else(store: SearchStore) -> None:
    seeded = _generated_row()
    await store.update_collection_meta(seeded)

    assert await _write(store, _clear_fields()) is DescriptionWriteOutcome.WRITTEN

    stored = await _stored(store)
    for name in _DESCRIPTION_FIELDS:
        assert getattr(stored, name) is None, f"{name} survived a clear"
    _assert_only_description_fields_changed(seeded, stored)


async def test_set_leaves_the_baseline_alone(store: SearchStore) -> None:
    await store.update_collection_meta(_generated_row())

    await _write(store, _manual_fields())

    stored = await _stored(store)
    assert stored.description_source == "manual"
    assert stored.description_status == "ok"
    assert stored.described_at_doc_count == _OLD_BASELINE


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


async def test_failure_is_recorded_on_an_unchanged_row_and_keeps_the_description(store: SearchStore) -> None:
    seeded = _generated_row()
    await store.update_collection_meta(seeded)

    assert await _write(store, _failure_fields(), _expected_of(seeded)) is DescriptionWriteOutcome.WRITTEN

    stored = await _stored(store)
    assert stored.description_status == "failed"
    assert stored.description == _OLD_TEXT
    assert stored.description_source == "generated"
    _assert_only_description_fields_changed(seeded, stored)


async def test_failure_on_a_collection_without_a_description_stores_failed_and_no_source(
    store: SearchStore,
) -> None:
    seeded = _generated_row(
        description=None,
        description_source=None,
        description_status=None,
        last_described=None,
        described_at_doc_count=None,
        description_embedding=None,
    )
    await store.update_collection_meta(seeded)

    outcome = await _write(store, _failure_fields(), _expected_of(seeded))

    assert outcome is DescriptionWriteOutcome.WRITTEN
    stored = await _stored(store)
    assert stored.description_status == "failed"
    assert stored.description_source is None
    assert stored.description is None


async def test_failure_is_refused_over_a_manual_row(store: SearchStore) -> None:
    """A manual ``ok`` never becomes ``failed``."""
    seeded = _generated_row()
    await store.update_collection_meta(seeded)
    expected = _expected_of(seeded)
    await _write(store, _manual_fields())

    assert await _write(store, _failure_fields(), expected) is DescriptionWriteOutcome.REFUSED_MANUAL

    assert (await _stored(store)).description_status == "ok"


async def test_manual_source_wins_even_when_the_expected_state_describes_it(store: SearchStore) -> None:
    manual = _generated_row(description=_MANUAL_TEXT, description_source="manual")
    await store.update_collection_meta(manual)

    outcome = await _write(store, _generated_fields(), _expected_of(manual))

    assert outcome is DescriptionWriteOutcome.REFUSED_MANUAL
    assert (await _stored(store)).description == _MANUAL_TEXT


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param({"description": "a second generation finished"}, id="description"),
        pytest.param({"last_described": datetime(2026, 5, 5, tzinfo=UTC)}, id="last_described"),
        pytest.param({"last_described": None}, id="last_described_cleared"),
        pytest.param({"description_status": "failed"}, id="status_failed_over_text"),
    ],
)
async def test_generated_write_is_refused_when_the_row_changed_since_the_generation_began(
    store: SearchStore, changed: dict[str, Any]
) -> None:
    seeded = _generated_row()
    await store.update_collection_meta(seeded)
    expected = _expected_of(seeded)
    drifted = dataclasses.replace(seeded, **changed)
    await store.update_collection_meta(drifted)

    assert await _write(store, _generated_fields(), expected) is DescriptionWriteOutcome.REFUSED_STALE
    assert await _stored(store) == drifted


async def test_generated_write_is_refused_when_a_description_appeared_since_the_generation_began(
    store: SearchStore,
) -> None:
    """The expected state was read from a row with no description; a description has since appeared."""
    empty = _generated_row(
        description=None, description_source=None, description_status=None, last_described=None
    )
    await store.update_collection_meta(empty)
    expected = _expected_of(empty)
    await store.update_collection_meta(_generated_row())

    assert await _write(store, _generated_fields(), expected) is DescriptionWriteOutcome.REFUSED_STALE


async def test_generated_write_is_refused_when_only_the_source_differs(store: SearchStore) -> None:
    """Description and last-described still match: the source alone makes the row a different one."""
    empty = _generated_row(
        description=None, description_source=None, description_status=None, last_described=None
    )
    await store.update_collection_meta(empty)
    expected = _expected_of(empty)
    sourced = dataclasses.replace(empty, description_source="generated")
    await store.update_collection_meta(sourced)
    assert (await _stored(store)).description_source == "generated"

    assert await _write(store, _generated_fields(), expected) is DescriptionWriteOutcome.REFUSED_STALE


async def test_a_write_is_refused_when_only_the_stored_status_changed_by_a_clear(store: SearchStore) -> None:
    """S74: an empty row holding ``failed`` is snapshotted; the clear resets the status only (text, source and stamp
    stay none), so a late recorded failure or generated write over it is stale and the cleared row stays clean."""
    empty_failed = _generated_row(
        description=None, description_source=None, description_status="failed", last_described=None
    )
    await store.update_collection_meta(empty_failed)
    expected = _expected_of(empty_failed)
    assert expected.status == "failed"
    assert await _write(store, _clear_fields()) is DescriptionWriteOutcome.WRITTEN
    assert (await _stored(store)).description_status is None

    assert await _write(store, _failure_fields(), expected) is DescriptionWriteOutcome.REFUSED_STALE
    assert await _write(store, _generated_fields(), expected) is DescriptionWriteOutcome.REFUSED_STALE

    stored = await _stored(store)
    assert (stored.description, stored.description_source, stored.description_status) == (None, None, None)


async def test_a_failure_is_recorded_when_the_stored_status_is_unchanged(store: SearchStore) -> None:
    empty_failed = _generated_row(
        description=None, description_source=None, description_status="failed", last_described=None
    )
    await store.update_collection_meta(empty_failed)

    assert await _write(store, _failure_fields(), _expected_of(empty_failed)) is DescriptionWriteOutcome.WRITTEN
    assert (await _stored(store)).description_status == "failed"


async def test_an_empty_expected_description_matches_an_unset_one(store: SearchStore) -> None:
    """The store cannot tell an empty description from none, so a caller's ``""`` equals the stored ``None``."""
    empty = _generated_row(
        description=None, description_source=None, description_status=None, last_described=None
    )
    await store.update_collection_meta(empty)
    expected = ExpectedState(description="", source=None, last_described=None, status=None)

    assert await _write(store, _generated_fields(), expected) is DescriptionWriteOutcome.WRITTEN


async def test_unconditional_write_overrides_a_manual_description(store: SearchStore) -> None:
    """Set, clear and import are authorities: they replace whatever is stored."""
    await store.update_collection_meta(_generated_row(description=_MANUAL_TEXT, description_source="manual"))

    assert await _write(store, _generated_fields(), UNCONDITIONAL) is DescriptionWriteOutcome.WRITTEN

    assert (await _stored(store)).description == _NEW_TEXT


async def test_refused_write_commits_no_row(store: SearchStore, monkeypatch: pytest.MonkeyPatch) -> None:
    seeded = _generated_row(description_source="manual")
    await store.update_collection_meta(seeded)
    merge_inserts = _count_merge_inserts(monkeypatch)

    assert await _write(store, _generated_fields(), _expected_of(seeded)) is DescriptionWriteOutcome.REFUSED_MANUAL
    assert merge_inserts == []


async def test_written_write_is_one_merge_insert(store: SearchStore, monkeypatch: pytest.MonkeyPatch) -> None:
    await store.update_collection_meta(_generated_row())
    merge_inserts = _count_merge_inserts(monkeypatch)

    assert await _write(store, _generated_fields()) is DescriptionWriteOutcome.WRITTEN
    assert merge_inserts == [["name", "namespace"]]


# ---------------------------------------------------------------------------
# Missing collection, lock handling, argument errors
# ---------------------------------------------------------------------------


async def test_missing_collection_is_reported_and_nothing_is_created(store: SearchStore) -> None:
    outcome = await _write(store, _generated_fields())

    assert outcome is DescriptionWriteOutcome.COLLECTION_MISSING
    assert await store.get_collection_meta(_NAME, _NAMESPACE) is None
    assert _NAME not in store._collection_locks, "a write to a missing collection must not grow the lock map"


async def test_a_row_of_another_namespace_counts_as_missing_and_is_untouched(store: SearchStore) -> None:
    other = _generated_row(namespace="ns-other")
    await store.update_collection_meta(other)

    outcome = await _write(store, _generated_fields())

    assert outcome is DescriptionWriteOutcome.COLLECTION_MISSING
    assert await store.get_collection_meta(_NAME, "ns-other") == other


@pytest.mark.parametrize(
    ("seed", "guard"),
    [
        pytest.param(_generated_row(), UNCONDITIONAL, id="written"),
        pytest.param(_generated_row(description_source="manual"), _EXPECTED_GENERATED, id="refused"),
        pytest.param(None, UNCONDITIONAL, id="missing"),
    ],
)
async def test_the_lock_is_released_whatever_the_outcome(
    store: SearchStore, seed: CollectionMeta | None, guard: WriteGuard
) -> None:
    if seed is not None:
        await store.update_collection_meta(seed)

    await _write(store, _generated_fields(), guard)

    assert not store.lock_for(_NAME).locked()


@pytest.mark.parametrize("failing_step", ["_do_read_meta_unlocked", "_do_write_meta_unlocked"])
async def test_the_lock_is_released_when_the_store_raises(store: SearchStore, failing_step: str) -> None:
    await store.update_collection_meta(_generated_row())

    async def failing(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk full")

    setattr(store, failing_step, failing)

    with pytest.raises(RuntimeError, match="disk full"):
        await _write(store, _generated_fields())

    assert not store.lock_for(_NAME).locked()


async def test_lock_timeout_logs_the_collection_and_never_the_text(
    store: SearchStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await store.update_collection_meta(_generated_row())
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _SHORT_LOCK_TIMEOUT_S)
    lock = store.lock_for(_NAME)
    await lock.acquire()
    try:
        with caplog.at_level(logging.DEBUG, logger="archon_search.store"):
            outcome = await _write(store, _generated_fields())
    finally:
        lock.release()

    assert outcome is DescriptionWriteOutcome.LOCK_TIMEOUT
    timeout_records = [r.getMessage() for r in caplog.records if "write_description" in r.getMessage()]
    assert len(timeout_records) == 1
    assert _NAME in timeout_records[0]
    assert "lock timeout" in timeout_records[0]
    assert _NEW_TEXT not in caplog.text


@pytest.mark.parametrize("invalid_name", ["", "../escape", "has space", "x" * 65])
async def test_invalid_collection_name_raises_without_creating_a_lock_entry(
    store: SearchStore, invalid_name: str
) -> None:
    with pytest.raises(ValueError, match="Invalid collection name"):
        await store.write_description(invalid_name, _NAMESPACE, _generated_fields(), UNCONDITIONAL)

    assert invalid_name not in store._collection_locks


@pytest.mark.parametrize("invalid_namespace", ["", "../escape", "has space", "deny-all"])
async def test_invalid_namespace_raises_before_any_lock_is_taken(
    store: SearchStore, invalid_namespace: str
) -> None:
    with pytest.raises(ValueError, match="(?i)namespace"):
        await store.write_description(_NAME, invalid_namespace, _generated_fields(), UNCONDITIONAL)

    assert _NAME not in store._collection_locks


async def test_a_disconnected_store_raises(tmp_path: Path) -> None:
    never_connected = SearchStore(tmp_path / "db", config=SearchConfig())

    with pytest.raises(RuntimeError, match="not connected"):
        await never_connected.write_description(_NAME, _NAMESPACE, _generated_fields(), UNCONDITIONAL)


@pytest.mark.parametrize("bad_guard", [None, "Unconditional", "refuseIfChanged", {"description": _OLD_TEXT}])
async def test_a_guard_that_is_not_the_contract_never_fails_open(store: SearchStore, bad_guard: Any) -> None:
    """A typo in the guard must not turn a generated write into an unconditional one that overwrites a manual row."""
    await store.update_collection_meta(_generated_row(description=_MANUAL_TEXT, description_source="manual"))

    with pytest.raises(TypeError, match="guard"):
        await _write(store, _generated_fields(), bad_guard)

    assert (await _stored(store)).description == _MANUAL_TEXT
    assert not store.lock_for(_NAME).locked()


async def test_a_caller_that_holds_the_collection_lock_gets_lock_timeout_not_a_deadlock(
    store: SearchStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is not reentrant: PATCH and DELETE hold it, so the set and clear routes must call without it."""
    await store.update_collection_meta(_generated_row())
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", _SHORT_LOCK_TIMEOUT_S)
    async with store.locked_collection_meta(_NAME, _NAMESPACE):
        outcome = await _write(store, _generated_fields())

    assert outcome is DescriptionWriteOutcome.LOCK_TIMEOUT


# ---------------------------------------------------------------------------
# The request types refuse what the store would refuse to write
# ---------------------------------------------------------------------------


def test_field_change_states_a_value_exactly_when_it_sets() -> None:
    assert FieldChange.keep().action is FieldOp.KEEP
    assert FieldChange.clear().action is FieldOp.CLEAR
    assert FieldChange.set_to("x") == FieldChange(FieldOp.SET, "x")
    with pytest.raises(ValueError, match="set"):
        FieldChange(FieldOp.SET)
    with pytest.raises(ValueError, match="keep"):
        FieldChange(FieldOp.KEEP, "x")
    with pytest.raises(ValueError, match="clear"):
        FieldChange(FieldOp.CLEAR, "x")


@pytest.mark.parametrize("field", ["source", "status"])
def test_a_source_or_status_outside_the_stored_contract_is_refused_on_construction(field: str) -> None:
    """``pending`` is memory only; a typo is no better. Refused before any lock is taken."""
    with pytest.raises(ValueError, match=field):
        _fields(**{field: FieldChange.set_to("pending")})


def test_an_expected_state_holding_the_pending_overlay_is_refused_on_construction() -> None:
    """The guard compares the STORED status; ``pending`` is never stored, so a snapshot of it is a caller bug."""
    with pytest.raises(ValueError, match="description_status"):
        ExpectedState(description=None, source=None, last_described=None, status="pending")  # type: ignore[arg-type]


@pytest.mark.parametrize("text_change", [FieldChange.set_to(_NEW_TEXT), FieldChange.clear()], ids=["set", "clear"])
@pytest.mark.parametrize("kept", ["source", "status", "description_embedding"])
def test_a_changed_text_cannot_keep_its_source_status_or_vector(text_change: FieldChange[Any], kept: str) -> None:
    """C3, C11, S43, S69: the source, the status and the routing vector follow every description change."""
    follow_up = FieldChange.clear() if text_change.action is FieldOp.CLEAR else None
    stated: dict[str, FieldChange[Any]] = {
        "source": follow_up or FieldChange.set_to("generated"),
        "status": follow_up or FieldChange.set_to("ok"),
        "description_embedding": follow_up or FieldChange.set_to(list(_NEW_VECTOR)),
    }
    del stated[kept]

    with pytest.raises(ValueError, match=kept):
        _fields(description=text_change, **stated)


def test_a_vector_may_change_alone_and_a_failed_embedding_clears_it() -> None:
    _fields(description_embedding=FieldChange.set_to(list(_NEW_VECTOR)))
    _fields(
        description=FieldChange.set_to(_NEW_TEXT),
        source=FieldChange.set_to("generated"),
        status=FieldChange.set_to("ok"),
        description_embedding=FieldChange.clear(),
    )


@pytest.mark.parametrize(
    ("field", "change", "match"),
    [
        pytest.param("described_at_doc_count", FieldChange.set_to(-1), "described_at_doc_count", id="negative_baseline"),
        pytest.param("description", FieldChange.set_to("  \n"), "blank", id="blank_text"),
        pytest.param("description_embedding", FieldChange.set_to([]), "description_embedding", id="empty_vector"),
        pytest.param(
            "description_embedding", FieldChange.set_to([0.1, math.nan]), "description_embedding", id="nan_vector"
        ),
        pytest.param(
            "description_embedding", FieldChange.set_to([math.inf]), "description_embedding", id="infinite_vector"
        ),
        pytest.param(
            "description_embedding", FieldChange.set_to([True, False]), "description_embedding", id="boolean_vector"
        ),
        pytest.param("description_embedding", FieldChange.set_to(["a"]), "description_embedding", id="text_vector"),
    ],
)
def test_a_value_the_row_cannot_hold_back_is_refused(field: str, change: FieldChange[Any], match: str) -> None:
    """A negative baseline reads back as unset, and a non-finite vector is dropped on read."""
    with pytest.raises(ValueError, match=match):
        _fields(**{field: change})


def test_every_description_field_maps_to_a_collection_meta_field() -> None:
    """A field added to ``DescriptionFields`` without a mapping entry would silently never be applied."""
    meta_fields = {field.name for field in dataclasses.fields(CollectionMeta)}

    assert set(_META_FIELD_OF) == {field.name for field in dataclasses.fields(DescriptionFields)}
    assert set(_META_FIELD_OF.values()) == _DESCRIPTION_FIELDS
    assert _DESCRIPTION_FIELDS <= meta_fields


@pytest.mark.parametrize("missing", [field.name for field in dataclasses.fields(DescriptionFields)])
def test_every_field_must_be_stated(missing: str) -> None:
    stated = {
        field.name: FieldChange.keep() for field in dataclasses.fields(DescriptionFields) if field.name != missing
    }

    with pytest.raises(TypeError, match=missing):
        DescriptionFields(**stated)


def test_outcome_values_are_the_contract_names() -> None:
    assert {outcome.value for outcome in DescriptionWriteOutcome} == {
        "written",
        "lockTimeout",
        "collectionMissing",
        "refusedManual",
        "refusedStale",
    }

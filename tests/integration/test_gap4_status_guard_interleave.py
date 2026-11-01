"""S74 (gap 4): a write that was in flight when only the STORED status changed is refused as stale.

A collection is empty and holds status ``failed``. A generation snapshots that row and parks in its slow step; the
operator clears the collection (status ``failed`` -> none, text/source/stamp unchanged); the generation then ends. Its
result, a ``failed`` record or a generated text, must not land: the cleared row keeps no ``failed`` (backfill would
skip it for good) and no stale text. A manual set during the slow step still wins. Real LanceDB, real collection
lock, ``tests/_parked_lock.py`` parks the write.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from archon_search.collection_meta import CollectionMeta, visible_description_status
from archon_search.config import SearchConfig
from archon_search.description_generator import (
    DescriptionFailed,
    DescriptionFailureReason,
    DescriptionGenerated,
    DescriptionOutcome,
)
from archon_search.description_lifecycle import (
    DescriptionLifecycle,
    GenerationKind,
    GenerationResult,
    RequestOutcome,
)
from archon_search.description_write import DescriptionWriteOutcome, ExpectedState
from archon_search.description_writer import (
    ClearChange,
    DescriptionEmbedder,
    DescriptionWriter,
    ManualSetChange,
)
from archon_search.store import SearchStore
from tests._parked_lock import until_parked

pytestmark = pytest.mark.integration

_NAME = "docs"
_NAMESPACE = "default"
_MODEL = "model-a"
_VECTOR = [0.25, 0.5]
_MANUAL_TEXT = "Written by the operator."
_SETTLE_TIMEOUT_S = 10.0


class _GatedGenerator:
    """The slow step: parks until released, then answers *outcome*."""

    def __init__(self, outcome: DescriptionOutcome) -> None:
        self.outcome = outcome
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, collection: str, namespace: str) -> DescriptionOutcome:
        self.started.set()
        await self.release.wait()
        return self.outcome


class _Embedder:
    async def embed_one(self, text: str) -> list[float]:
        return list(_VECTOR)


class _Cache:
    async def get_or_load(self, model_name: str) -> DescriptionEmbedder:
        return _Embedder()


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SearchStore]:
    search_store = SearchStore(tmp_path / "db", config=SearchConfig())
    await search_store.connect()
    # Empty description, stored status ``failed``: what a failed first generation leaves behind.
    await search_store.update_collection_meta(
        CollectionMeta(
            name=_NAME, doc_count=3, chunk_count=3, active_embedding_model=_MODEL, description_status="failed"
        )
    )
    yield search_store
    await search_store.disconnect()


def _writer(store: SearchStore) -> DescriptionWriter:
    return DescriptionWriter(store, _Cache(), default_embedding_model=_MODEL)


async def _stored(store: SearchStore) -> CollectionMeta:
    meta = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert meta is not None
    return meta


async def _park_a_describe_then(
    store: SearchStore, outcome: DescriptionOutcome, *operator_changes: ClearChange | ManualSetChange
) -> tuple[DescriptionLifecycle, GenerationResult]:
    """Start a describe, let it snapshot the row, apply *operator_changes* in its slow step, let it end."""
    generator = _GatedGenerator(outcome)
    lifecycle = DescriptionLifecycle(store, generator, _writer(store))
    try:
        ticket = await lifecycle.request_generation(_NAME, _NAMESPACE, GenerationKind.DESCRIBE)
        assert ticket.outcome is RequestOutcome.ACCEPTED
        await asyncio.wait_for(generator.started.wait(), timeout=_SETTLE_TIMEOUT_S)
        for change in operator_changes:
            await _writer(store).apply(_NAME, _NAMESPACE, change)
        generator.release.set()
        settlement = await asyncio.wait_for(ticket.settled, timeout=_SETTLE_TIMEOUT_S)
    except BaseException:
        generator.release.set()
        await lifecycle.drain()
        raise
    return lifecycle, settlement.result


async def test_backfill_skips_the_untouched_empty_failed_row(store: SearchStore) -> None:
    """Control for the next test: a failed empty row is NOT a backfill candidate, so being selected means the status
    really was reset."""
    lifecycle = DescriptionLifecycle(store, _GatedGenerator(DescriptionGenerated(text="x")), _writer(store))
    try:
        ticket = await lifecycle.request_generation(_NAME, _NAMESPACE, GenerationKind.BACKFILL)
        assert ticket.outcome is RequestOutcome.NOT_REQUIRED
    finally:
        await lifecycle.drain()


async def test_a_failure_after_a_clear_of_an_empty_failed_row_is_refused_and_backfill_still_selects_it(
    store: SearchStore,
) -> None:
    failed = DescriptionFailed(DescriptionFailureReason.CALL_FAILED, "provider down")

    lifecycle, result = await _park_a_describe_then(store, failed, ClearChange())
    try:
        assert result is GenerationResult.SUPERSEDED
        row = await _stored(store)
        assert row.description_status is None and not row.description
        assert visible_description_status(row, lifecycle) is None
        backfill = await lifecycle.request_generation(_NAME, _NAMESPACE, GenerationKind.BACKFILL)
        assert backfill.outcome is RequestOutcome.ACCEPTED, "a cleared row must stay a backfill candidate"
    finally:
        await lifecycle.drain()


async def test_a_generated_text_after_the_same_clear_is_refused_stale(store: SearchStore) -> None:
    generated = DescriptionGenerated(text="A text the operator no longer wants.")

    lifecycle, result = await _park_a_describe_then(store, generated, ClearChange())
    try:
        assert result is GenerationResult.SUPERSEDED
        row = await _stored(store)
        assert (row.description, row.description_source, row.description_status) == (None, None, None)
        assert row.description_embedding is None
    finally:
        await lifecycle.drain()


@pytest.mark.parametrize(
    "outcome",
    [
        DescriptionFailed(DescriptionFailureReason.CALL_FAILED, "provider down"),
        DescriptionGenerated(text="Generated while the operator typed."),
    ],
    ids=["failure", "generated"],
)
class TestOperatorChangesDuringTheSlowStep:
    async def test_a_manual_set_still_wins(self, store: SearchStore, outcome: DescriptionOutcome) -> None:
        lifecycle, result = await _park_a_describe_then(store, outcome, ManualSetChange(text=_MANUAL_TEXT))
        try:
            assert result is GenerationResult.REFUSED_MANUAL
            row = await _stored(store)
            assert (row.description, row.description_source, row.description_status) == (_MANUAL_TEXT, "manual", "ok")
        finally:
            await lifecycle.drain()

    async def test_a_manual_set_then_a_clear_leaves_the_cleared_row_alone(
        self, store: SearchStore, outcome: DescriptionOutcome
    ) -> None:
        """The set and the clear both land in the slow step: the row ends cleared (status none, no longer ``failed``),
        which differs from the snapshot in its stored status, so the late result is stale."""
        lifecycle, result = await _park_a_describe_then(
            store, outcome, ManualSetChange(text=_MANUAL_TEXT), ClearChange()
        )
        try:
            assert result is GenerationResult.SUPERSEDED
            row = await _stored(store)
            assert (row.description, row.description_source, row.description_status) == (None, None, None)
        finally:
            await lifecycle.drain()


@pytest.mark.parametrize("clear_first", [True, False], ids=["clear_lands_first", "failure_lands_first"])
async def test_the_failure_and_the_clear_queued_on_the_lock_end_as_a_serial_outcome(
    store: SearchStore, clear_first: bool
) -> None:
    """Both writes queue on the held collection lock; the lock is FIFO, so the creation order is the landing order.
    Clear first: the failure is stale. Failure first: it lands and the clear then resets it. Either way the final row
    carries no ``failed``."""
    writer = _writer(store)
    row = await _stored(store)
    expected = ExpectedState(row.description, row.description_source, row.last_described, row.description_status)
    lock = store.lock_for(_NAME)
    await lock.acquire()
    clear = asyncio.create_task(asyncio.sleep(0))  # placeholders replaced below, keeps the names bound for `finally`
    failure = clear
    try:
        if clear_first:
            clear = asyncio.create_task(writer.apply(_NAME, _NAMESPACE, ClearChange()))
            await until_parked(lock, 1)
            failure = asyncio.create_task(writer.record_failure(_NAME, _NAMESPACE, expected))
        else:
            failure = asyncio.create_task(writer.record_failure(_NAME, _NAMESPACE, expected))
            await until_parked(lock, 1)
            clear = asyncio.create_task(writer.apply(_NAME, _NAMESPACE, ClearChange()))
        await until_parked(lock, 2)
        lock.release()
        clear_result, failure_result = await asyncio.wait_for(asyncio.gather(clear, failure), timeout=_SETTLE_TIMEOUT_S)
    finally:
        if lock.locked():
            lock.release()
        await asyncio.gather(clear, failure, return_exceptions=True)
    assert clear_result.outcome is DescriptionWriteOutcome.WRITTEN
    assert failure_result is (DescriptionWriteOutcome.REFUSED_STALE if clear_first else DescriptionWriteOutcome.WRITTEN)
    assert (await _stored(store)).description_status is None

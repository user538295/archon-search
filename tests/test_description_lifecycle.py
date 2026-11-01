"""BE-23 (C5, S23, S24, S80): the description lifecycle service, against a fake store, generator and writer.

The lifecycle owns every generation task: the per-collection single-flight, the process-wide slot (one generation at a
time, held for the sample read and the model call only), the bounded and prioritised queue, the in-memory ``pending``
overlay and last attempt, and the shutdown order (refuse, then cancel and await). Nothing here sleeps: every test parks
the fakes on events and drives the event loop with ``until``.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import weakref
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.constants import DEFAULT_NAMESPACE, MAX_QUEUED_GENERATIONS
from archon_search.description_generator import (
    DescriptionFailed,
    DescriptionFailureReason,
    DescriptionGenerated,
    DescriptionOutcome,
)
from archon_search.description_lifecycle import (
    GENERATION_TASK_NAME_PREFIX,
    DescriptionLifecycle,
    GenerationKind,
    GenerationResult,
    GenerationTicket,
    LastAttempt,
    RequestOutcome,
)
from archon_search.description_write import DescriptionWriteOutcome, ExpectedState
from archon_search.description_writer import (
    DescriptionChange,
    GeneratedChange,
    VectorOutcome,
    WriteResult,
)

_NAMESPACE = DEFAULT_NAMESPACE
_COLLECTION = "docs"
_TEXT = "Notes about the gardening club and its seed library."
_STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
_NOW = datetime(2026, 5, 6, 7, 8, 9, tzinfo=UTC)
_DOC_COUNT = 10
_CHUNK_COUNT = 100
_SPIN_ITERATIONS = 500
_PARK_ITERATIONS = 20
_SETTLE_TIMEOUT_S = 5.0
_LIFECYCLE_LOGGER = "archon_search.description_lifecycle"


def _meta(
    name: str = _COLLECTION,
    *,
    description: str | None = "old text",
    source: str | None = "generated",
    doc_count: int = _DOC_COUNT,
    chunk_count: int = _CHUNK_COUNT,
    baseline: int | None = None,
) -> CollectionMeta:
    return CollectionMeta(
        name=name,
        description=description,
        description_source=source,  # type: ignore[arg-type]
        description_status="ok" if description else None,
        doc_count=doc_count,
        chunk_count=chunk_count,
        described_at_doc_count=baseline,
        last_described=_STAMP if description else None,
    )


async def until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until *condition* holds; fail the test if it never does."""
    for _ in range(_SPIN_ITERATIONS):
        if condition():
            return
        await asyncio.sleep(0)
    pytest.fail("the condition never held")


async def park() -> None:
    """Let every task that is ready run until it waits on something (a gate, the slot, a lock)."""
    for _ in range(_PARK_ITERATIONS):
        await asyncio.sleep(0)


def generation_tasks() -> list[asyncio.Task[object]]:
    """The lifecycle's tasks that are still alive, found by their name."""
    return [task for task in asyncio.all_tasks() if task.get_name().startswith(GENERATION_TASK_NAME_PREFIX)]


def generation_task(collection: str) -> asyncio.Task[object]:
    [task] = [task for task in generation_tasks() if task.get_name() == f"{GENERATION_TASK_NAME_PREFIX}{collection}"]
    return task


class FakeStore:
    """Serves rows by name (any other name reads as a collection that has never been described)."""

    def __init__(self) -> None:
        self.rows: dict[str, CollectionMeta | None] = {}
        self.reads: list[tuple[str, str]] = []
        self.error: Exception | None = None
        self.read_gate: asyncio.Event | None = None

    async def get_collection_meta(self, name: str, namespace: str = DEFAULT_NAMESPACE) -> CollectionMeta | None:
        self.reads.append((name, namespace))
        if self.read_gate is not None:
            await self.read_gate.wait()
        if self.error is not None:
            raise self.error
        return self.rows[name] if name in self.rows else _meta(name, description=None, source=None)


class FakeGenerator:
    """Answers a generated text unless told otherwise; ``gate`` parks one collection's call until opened."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.running = 0
        self.max_running = 0
        self.outcomes: dict[str, DescriptionOutcome] = {}
        self._gates: dict[str, asyncio.Event] = {}

    def gate(self, collection: str) -> asyncio.Event:
        return self._gates.setdefault(collection, asyncio.Event())

    async def generate(self, collection: str, namespace: str) -> DescriptionOutcome:
        self.started.append(collection)
        self.calls.append((collection, namespace))
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if collection in self._gates:
                await self._gates[collection].wait()
            await asyncio.sleep(0)
            return self.outcomes.get(collection, DescriptionGenerated(text=_TEXT))
        finally:
            self.running -= 1


class FakeWriter:
    """Records the guarded write and the recorded failure; ``gate`` parks one collection's write."""

    def __init__(self) -> None:
        self.applied: list[tuple[str, str, DescriptionChange]] = []
        self.failures: list[tuple[str, str, ExpectedState]] = []
        self.write_outcome = DescriptionWriteOutcome.WRITTEN
        self.failure_outcome = DescriptionWriteOutcome.WRITTEN
        self.apply_error: Exception | None = None
        self.failure_error: Exception | None = None
        self._gates: dict[str, asyncio.Event] = {}

    def gate(self, collection: str) -> asyncio.Event:
        return self._gates.setdefault(collection, asyncio.Event())

    async def apply(self, collection: str, namespace: str, change: DescriptionChange) -> WriteResult:
        self.applied.append((collection, namespace, change))
        if collection in self._gates:
            await self._gates[collection].wait()
        if self.apply_error is not None:
            raise self.apply_error
        return WriteResult(self.write_outcome, VectorOutcome.REFRESHED)

    async def record_failure(
        self, collection: str, namespace: str, expected: ExpectedState
    ) -> DescriptionWriteOutcome:
        self.failures.append((collection, namespace, expected))
        if self.failure_error is not None:
            raise self.failure_error
        return self.failure_outcome


class Rig:
    def __init__(self, max_queued: int = MAX_QUEUED_GENERATIONS) -> None:
        self.store = FakeStore()
        self.generator = FakeGenerator()
        self.writer = FakeWriter()
        self.lifecycle = DescriptionLifecycle(
            self.store, self.generator, self.writer, max_queued=max_queued, clock=lambda: _NOW
        )

    async def request(self, collection: str = _COLLECTION, kind: GenerationKind = GenerationKind.INGEST) -> GenerationTicket:
        return await self.lifecycle.request_generation(collection, _NAMESPACE, kind)

    async def hold_the_slot(self, collection: str = "holder") -> GenerationTicket:
        """Start a generation that parks in the generator and so keeps the slot until its gate opens."""
        self.generator.gate(collection)
        ticket = await self.request(collection)
        assert ticket.outcome is RequestOutcome.ACCEPTED
        await until(lambda: collection in self.generator.started)
        return ticket


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    created = Rig()
    yield created
    await created.lifecycle.drain()


async def test_second_request_for_one_collection_settles_already_running(rig: Rig) -> None:
    """S24, S80: while a generation of a collection is in flight a second request settles at once, never queues."""
    first = await rig.hold_the_slot(_COLLECTION)

    second = await rig.request(_COLLECTION, GenerationKind.DESCRIBE)
    third = await rig.request(_COLLECTION, GenerationKind.INGEST)

    assert (second.outcome, third.outcome) == (RequestOutcome.ALREADY_RUNNING, RequestOutcome.ALREADY_RUNNING)
    assert (await second.settled).result is GenerationResult.ALREADY_RUNNING
    assert (await third.settled).result is GenerationResult.ALREADY_RUNNING
    rig.generator.gate(_COLLECTION).set()
    assert (await first.settled).result is GenerationResult.GENERATED
    assert rig.generator.calls == [(_COLLECTION, _NAMESPACE)]
    again = await rig.request(_COLLECTION)  # single-flight ends with the run: the next request is accepted
    assert again.outcome is RequestOutcome.ACCEPTED


async def test_two_requests_racing_while_the_row_is_read_start_one_generation(rig: Rig) -> None:
    """S24: two ingests that finish together both ask while the row is read; one description results, the other
    request is refused ``alreadyRunning`` by the check that follows the read."""
    rig.store.read_gate = asyncio.Event()
    first = asyncio.create_task(rig.request())
    second = asyncio.create_task(rig.request())
    await until(lambda: len(rig.store.reads) == 2)

    rig.store.read_gate.set()
    tickets = [await first, await second]

    assert sorted(ticket.outcome for ticket in tickets) == [RequestOutcome.ACCEPTED, RequestOutcome.ALREADY_RUNNING]
    results = [(await ticket.settled).result for ticket in tickets]
    assert sorted(results) == sorted([GenerationResult.GENERATED, GenerationResult.ALREADY_RUNNING])
    assert [call[0] for call in rig.generator.calls] == [_COLLECTION]
    assert len(rig.writer.applied) == 1


async def test_only_one_generation_runs_in_the_process_at_a_time(rig: Rig) -> None:
    """S80: the slot serialises across collections, and across the three kinds."""
    names = ["a", "b", "c", "d"]
    kinds = [GenerationKind.INGEST, GenerationKind.DESCRIBE, GenerationKind.BACKFILL, GenerationKind.INGEST]

    tickets = [await rig.request(name, kind) for name, kind in zip(names, kinds, strict=True)]

    assert [(await ticket.settled).result for ticket in tickets] == [GenerationResult.GENERATED] * len(names)
    assert rig.generator.max_running == 1
    assert sorted(rig.generator.started) == names


async def test_the_slot_is_released_before_the_write_so_a_slow_write_does_not_stall_another_collection(rig: Rig) -> None:
    """S80: the slot covers the sample read and the model call; the embedding and the guarded write come after."""
    rig.writer.gate("slow")
    slow = await rig.request("slow")
    await until(lambda: [name for name, _, _ in rig.writer.applied] == ["slow"])

    quick = await rig.request("quick")

    assert (await asyncio.wait_for(quick.settled, timeout=_SETTLE_TIMEOUT_S)).result is GenerationResult.GENERATED
    assert not slow.settled.done()
    rig.writer.gate("slow").set()
    assert (await slow.settled).result is GenerationResult.GENERATED


async def test_waiting_requests_served_describe_then_ingest_then_backfill(rig: Rig) -> None:
    """S80: a waiting describe goes first, then the ingest rule, then backfill; arrival order inside a class."""
    holder = await rig.hold_the_slot()
    arrivals = [
        ("b1", GenerationKind.BACKFILL),
        ("i1", GenerationKind.INGEST),
        ("d1", GenerationKind.DESCRIBE),
        ("i2", GenerationKind.INGEST),
        ("d2", GenerationKind.DESCRIBE),
        ("b2", GenerationKind.BACKFILL),
    ]
    tickets = [await rig.request(name, kind) for name, kind in arrivals]
    assert [ticket.outcome for ticket in tickets] == [RequestOutcome.ACCEPTED] * len(arrivals)
    assert rig.generator.started == ["holder"]

    rig.generator.gate("holder").set()
    await holder.settled
    for ticket in tickets:
        await ticket.settled

    assert rig.generator.started == ["holder", "d1", "d2", "i1", "i2", "b1", "b2"]


async def test_queue_over_bound_drops_ingest_and_fails_describe(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    """S80: the queue holds MAX_QUEUED_GENERATIONS waiting requests; over it ingest and backfill are dropped (one WARNING
    per process, nothing pending) and a describe settles failed saying the queue is full."""
    assert MAX_QUEUED_GENERATIONS == 64
    holder = await rig.hold_the_slot()
    waiting = [await rig.request(f"w{i}") for i in range(MAX_QUEUED_GENERATIONS)]
    assert [ticket.outcome for ticket in waiting] == [RequestOutcome.ACCEPTED] * MAX_QUEUED_GENERATIONS

    with caplog.at_level(logging.WARNING, logger=_LIFECYCLE_LOGGER):
        dropped_ingest = await rig.request("late-ingest", GenerationKind.INGEST)
        dropped_backfill = await rig.request("late-backfill", GenerationKind.BACKFILL)
        failed_describe = await rig.request("late-describe", GenerationKind.DESCRIBE)

    assert dropped_ingest.outcome is RequestOutcome.QUEUE_FULL
    assert dropped_backfill.outcome is RequestOutcome.QUEUE_FULL
    assert failed_describe.outcome is RequestOutcome.QUEUE_FULL
    assert (await dropped_ingest.settled).result is GenerationResult.DROPPED
    assert (await dropped_backfill.settled).result is GenerationResult.DROPPED
    describe_settlement = await failed_describe.settled
    assert describe_settlement.result is GenerationResult.FAILED
    assert describe_settlement.detail is not None
    assert "queue is full" in describe_settlement.detail
    assert not any(rig.lifecycle.is_pending(name) for name in ("late-ingest", "late-backfill", "late-describe"))
    assert len([r for r in caplog.records if r.name == _LIFECYCLE_LOGGER and r.levelno == logging.WARNING]) == 1
    rig.generator.gate("holder").set()
    await holder.settled
    for ticket in waiting:
        assert (await ticket.settled).result is GenerationResult.GENERATED
    assert "late-ingest" not in rig.generator.started
    assert len(rig.generator.started) == MAX_QUEUED_GENERATIONS + 1


async def test_a_request_is_not_queued_while_the_slot_is_free() -> None:
    """The bound counts waiting requests: the one holding the slot is not one of them. With room for nobody to wait, the
    first request still runs and the second is over the bound."""
    rig = Rig(max_queued=0)
    try:
        rig.generator.gate("first")
        first = await rig.request("first")
        second = await rig.request("second")

        assert (first.outcome, second.outcome) == (RequestOutcome.ACCEPTED, RequestOutcome.QUEUE_FULL)
        rig.generator.gate("first").set()
        assert (await first.settled).result is GenerationResult.GENERATED
        third = await rig.request("third")
        assert third.outcome is RequestOutcome.ACCEPTED
    finally:
        await rig.lifecycle.drain()


async def test_a_waiting_request_that_is_cancelled_frees_its_place_and_passes_the_slot_on(rig: Rig) -> None:
    """Shutdown cancels waiters: the places they held are free again, and the slot goes on to the next one."""
    holder = await rig.hold_the_slot()
    first = await rig.request("first")
    second = await rig.request("second")
    await park()
    first_task = generation_task("first")

    first_task.cancel()
    await until(lambda: first.settled.done())
    rig.generator.gate("holder").set()
    await holder.settled

    assert (await first.settled).result is GenerationResult.SHUTTING_DOWN
    assert (await second.settled).result is GenerationResult.GENERATED
    assert rig.generator.started == ["holder", "second"]


async def test_a_cancelled_waiter_frees_its_place_in_the_bound() -> None:
    """With room for one waiter, cancelling it makes room for the next request."""
    rig = Rig(max_queued=1)
    try:
        rig.generator.gate("holder")
        holder = await rig.request("holder")
        waiter = await rig.request("waiter")
        refused = await rig.request("over-the-bound")
        await park()
        assert (waiter.outcome, refused.outcome) == (RequestOutcome.ACCEPTED, RequestOutcome.QUEUE_FULL)

        generation_task("waiter").cancel()
        await until(lambda: waiter.settled.done())
        replacement = await rig.request("replacement")

        assert replacement.outcome is RequestOutcome.ACCEPTED
        rig.generator.gate("holder").set()
        await holder.settled
        assert (await replacement.settled).result is GenerationResult.GENERATED
        assert rig.generator.started == ["holder", "replacement"]
    finally:
        await rig.lifecycle.drain()


@pytest.mark.parametrize("cancelled", ["backfill", "ingest-1", "ingest-2", "ingest-3"])
async def test_cancelling_any_waiter_keeps_the_order_of_the_others(rig: Rig, cancelled: str) -> None:
    """Removing a place from the queue must not disturb the class-then-arrival order of those that stay."""
    holder = await rig.hold_the_slot()
    arrivals = [
        ("backfill", GenerationKind.BACKFILL),
        ("ingest-1", GenerationKind.INGEST),
        ("ingest-2", GenerationKind.INGEST),
        ("ingest-3", GenerationKind.INGEST),
    ]
    tickets = [await rig.request(name, kind) for name, kind in arrivals]
    await park()

    generation_task(cancelled).cancel()
    rig.generator.gate("holder").set()
    await holder.settled
    for ticket in tickets:
        await ticket.settled

    served = ["ingest-1", "ingest-2", "ingest-3", "backfill"]
    assert rig.generator.started == ["holder", *[name for name in served if name != cancelled]]


async def test_a_holder_and_a_waiter_cancelled_together_hand_the_slot_to_the_next_request(rig: Rig) -> None:
    """A granted slot whose task is cancelled in the same loop step, then a waiter cancelled, must not leak the slot."""
    holder = await rig.hold_the_slot()
    doomed = await rig.request("doomed")
    survivor = await rig.request("survivor")
    await park()

    generation_task("holder").cancel()
    generation_task("doomed").cancel()

    assert (await holder.settled).result is GenerationResult.SHUTTING_DOWN
    assert (await doomed.settled).result is GenerationResult.SHUTTING_DOWN
    assert (await asyncio.wait_for(survivor.settled, timeout=_SETTLE_TIMEOUT_S)).result is GenerationResult.GENERATED
    assert rig.generator.started == ["holder", "survivor"]


async def test_a_finished_task_is_not_kept_by_the_lifecycle(rig: Rig) -> None:
    """The service tracks live tasks only: a finished one is collectable, so a long-running server does not grow."""
    ticket = await rig.request()
    finished = weakref.ref(generation_task(_COLLECTION))

    await ticket.settled
    await park()
    gc.collect()

    assert finished() is None


async def test_a_restart_forgets_pending_and_the_next_qualifying_ingest_asks_again(rig: Rig) -> None:
    """S23: ``pending`` lives in memory only; a lifecycle built on the same store starts with nothing pending."""
    holder = await rig.hold_the_slot(_COLLECTION)
    assert rig.lifecycle.is_pending(_COLLECTION)

    restarted = DescriptionLifecycle(rig.store, rig.generator, rig.writer, clock=lambda: _NOW)
    try:
        assert not restarted.is_pending(_COLLECTION)
        assert restarted.last_attempt is None
        again = await restarted.request_generation(_COLLECTION, _NAMESPACE, GenerationKind.INGEST)
        assert again.outcome is RequestOutcome.ACCEPTED
        rig.generator.gate(_COLLECTION).set()
        assert (await again.settled).result is GenerationResult.GENERATED
    finally:
        await restarted.drain()
    await holder.settled


async def test_manual_description_is_never_generated_nor_marked_pending(rig: Rig) -> None:
    """The manual short-circuit reads through the store and runs before anything is queued."""
    rig.store.rows["docs"] = _meta(source="manual", baseline=None)

    tickets = [await rig.request("docs", kind) for kind in GenerationKind]

    assert [ticket.outcome for ticket in tickets] == [RequestOutcome.REFUSED_MANUAL] * len(GenerationKind)
    assert [(await ticket.settled).result for ticket in tickets] == [GenerationResult.REFUSED_MANUAL] * len(GenerationKind)
    assert not rig.lifecycle.is_pending("docs")
    assert rig.generator.calls == []
    assert rig.writer.applied == []
    assert rig.lifecycle.last_attempt is None


async def test_a_manual_set_that_lands_while_the_request_waits_stops_it_before_the_provider(rig: Rig) -> None:
    """The row is read again once the request has the slot: a manual description that arrived meanwhile wins at no cost."""
    holder = await rig.hold_the_slot()
    waiting = await rig.request("docs")
    rig.store.rows["docs"] = _meta(source="manual")

    rig.generator.gate("holder").set()
    await holder.settled
    settlement = await waiting.settled

    assert settlement.result is GenerationResult.REFUSED_MANUAL
    assert "docs" not in rig.generator.started
    assert not rig.lifecycle.is_pending("docs")


@pytest.mark.parametrize(
    ("doc_count", "chunk_count", "baseline", "fires"),
    [
        (13, 100, 10, True),
        (7, 100, 10, True),
        (12, 100, 10, True),
        (8, 100, 10, True),
        (11, 100, 10, False),
        (9, 100, 10, False),
        (10, 100, None, True),
        (10, 100, 0, True),
        (10, 0, None, False),
        (10, 0, 10, False),
    ],
    ids=["grew-30%", "shrank-30%", "grew-20%-boundary", "shrank-20%-boundary", "grew-10%", "shrank-10%", "no-baseline",
         "zero-baseline", "no-chunks-no-baseline", "no-chunks"],
)
async def test_ingest_request_applies_the_regeneration_rule_to_the_current_counts(
    rig: Rig, doc_count: int, chunk_count: int, baseline: int | None, fires: bool
) -> None:
    """S18, S24: the ingest rule is the unchanged 20% rule over the counts stored now against the saved baseline."""
    rig.store.rows["docs"] = _meta(doc_count=doc_count, chunk_count=chunk_count, baseline=baseline)

    ticket = await rig.request("docs", GenerationKind.INGEST)

    if fires:
        assert ticket.outcome is RequestOutcome.ACCEPTED
        assert (await ticket.settled).result is GenerationResult.GENERATED
    else:
        assert ticket.outcome is RequestOutcome.NOT_REQUIRED
        assert (await ticket.settled).result is GenerationResult.NOT_REQUIRED
        assert not rig.lifecycle.is_pending("docs")
        assert rig.generator.calls == []


async def test_describe_and_backfill_do_not_apply_the_ingest_rule(rig: Rig) -> None:
    """The describe job forces; backfill is selected by its own loop, so neither asks the 20% rule."""
    rig.store.rows["docs"] = _meta(doc_count=10, baseline=10)
    rig.store.rows["other"] = _meta("other", description=None, source=None, doc_count=10, baseline=10)

    describe = await rig.request("docs", GenerationKind.DESCRIBE)
    backfill = await rig.request("other", GenerationKind.BACKFILL)

    assert (describe.outcome, backfill.outcome) == (RequestOutcome.ACCEPTED, RequestOutcome.ACCEPTED)
    assert (await describe.settled).result is GenerationResult.GENERATED
    assert (await backfill.settled).result is GenerationResult.GENERATED


async def test_backfill_asks_only_for_a_collection_without_a_description(rig: Rig) -> None:
    """The backfill loop picks empty collections; the service refuses a described one, at the request and at the slot."""
    rig.store.rows["described"] = _meta("described", description="already here")
    rig.store.rows["empty"] = _meta("empty", description=None, source=None)
    rig.store.rows["filled-meanwhile"] = _meta("filled-meanwhile", description=None, source=None)
    holder = await rig.hold_the_slot()

    described = await rig.request("described", GenerationKind.BACKFILL)
    waiting = await rig.request("filled-meanwhile", GenerationKind.BACKFILL)
    rig.store.rows["filled-meanwhile"] = _meta("filled-meanwhile", description="an import landed")
    rig.generator.gate("holder").set()
    await holder.settled

    assert described.outcome is RequestOutcome.NOT_REQUIRED
    assert (await waiting.settled).result is GenerationResult.NOT_REQUIRED
    assert (await (await rig.request("empty", GenerationKind.BACKFILL)).settled).result is GenerationResult.GENERATED
    assert sorted(rig.generator.started) == ["empty", "holder"]



async def test_the_ingest_rule_is_asked_again_when_the_request_gets_the_slot(rig: Rig) -> None:
    """A describe that landed while the ingest request waited made the description fresh: no second paid call."""
    rig.store.rows["docs"] = _meta(baseline=None)
    holder = await rig.hold_the_slot()
    waiting = await rig.request("docs", GenerationKind.INGEST)
    rig.store.rows["docs"] = _meta(doc_count=10, baseline=10)

    rig.generator.gate("holder").set()
    await holder.settled

    assert (await waiting.settled).result is GenerationResult.NOT_REQUIRED
    assert "docs" not in rig.generator.started
    assert not rig.lifecycle.is_pending("docs")


async def test_a_collection_that_does_not_exist_needs_no_description(rig: Rig) -> None:
    rig.store.rows["gone"] = None

    ticket = await rig.request("gone", GenerationKind.DESCRIBE)

    assert ticket.outcome is RequestOutcome.NOT_REQUIRED
    assert rig.generator.calls == []


async def test_a_collection_deleted_while_the_request_waits_settles_superseded(rig: Rig) -> None:
    holder = await rig.hold_the_slot()
    waiting = await rig.request("docs")
    rig.store.rows["docs"] = None

    rig.generator.gate("holder").set()
    await holder.settled

    assert (await waiting.settled).result is GenerationResult.SUPERSEDED
    assert "docs" not in rig.generator.started


async def test_unreadable_store_at_request_time_starts_nothing_and_raises_nothing(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.store.error = OSError("disk gone: secret chunk text")

    with caplog.at_level(logging.WARNING, logger=_LIFECYCLE_LOGGER):
        ticket = await rig.request()

    assert ticket.outcome is RequestOutcome.UNREADABLE
    settlement = await ticket.settled
    assert settlement.result is GenerationResult.FAILED
    assert not rig.lifecycle.is_pending(_COLLECTION)
    assert rig.generator.calls == []
    assert "secret chunk text" not in caplog.text
    assert "OSError" in caplog.text


async def test_pending_overlays_the_stored_status_only_while_a_run_is_in_flight(rig: Rig) -> None:
    """S70: ``pending`` is memory only, laid over the stored status while a generation of that collection is in flight."""
    assert not rig.lifecycle.is_pending(_COLLECTION)
    assert rig.lifecycle.overlay_status(_COLLECTION, "failed") == "failed"
    assert rig.lifecycle.overlay_status(_COLLECTION, None) is None
    holder = await rig.hold_the_slot(_COLLECTION)
    waiting = await rig.request("queued")

    assert rig.lifecycle.is_pending(_COLLECTION)
    assert rig.lifecycle.is_pending("queued")  # pending from the moment the request is accepted
    assert rig.lifecycle.overlay_status(_COLLECTION, "ok") == "pending"
    assert rig.lifecycle.overlay_status(_COLLECTION, None) == "pending"
    assert rig.lifecycle.overlay_status("untouched", "ok") == "ok"
    rig.generator.gate(_COLLECTION).set()
    await holder.settled
    await waiting.settled

    assert not rig.lifecycle.is_pending(_COLLECTION)
    assert not rig.lifecycle.is_pending("queued")
    assert rig.lifecycle.overlay_status(_COLLECTION, "ok") == "ok"


async def test_pending_is_cleared_when_the_run_fails(rig: Rig) -> None:
    rig.generator.outcomes[_COLLECTION] = DescriptionFailed(DescriptionFailureReason.CALL_FAILED, "the provider is down")

    ticket = await rig.request()
    await ticket.settled

    assert not rig.lifecycle.is_pending(_COLLECTION)


async def test_a_generated_description_is_written_from_the_state_read_after_the_slot(rig: Rig) -> None:
    """C5/C11: the row read once the slot is held supplies the guard baseline and the document count at generation."""
    rig.store.rows["docs"] = _meta(description=None, source=None, doc_count=7, baseline=None)

    ticket = await rig.request("docs")
    settlement = await ticket.settled

    assert settlement.result is GenerationResult.GENERATED
    [(collection, namespace, change)] = rig.writer.applied
    assert (collection, namespace) == ("docs", _NAMESPACE)
    assert change == GeneratedChange(
        text=_TEXT, doc_count_at_generation=7, expected=ExpectedState(description=None, source=None, last_described=None)
    )
    assert rig.writer.failures == []


async def test_the_expected_state_carries_the_old_description_source_and_stamp(rig: Rig) -> None:
    rig.store.rows["docs"] = _meta(description="older", source="generated", baseline=None)

    await (await rig.request("docs")).settled

    [(_, _, change)] = rig.writer.applied
    assert isinstance(change, GeneratedChange)
    assert change.expected == ExpectedState(description="older", source="generated", last_described=_STAMP)


async def test_a_failed_generation_records_failure_under_the_expected_state_and_never_writes_text(rig: Rig) -> None:
    rig.store.rows["docs"] = _meta(description="older", source="generated", baseline=None)
    rig.generator.outcomes["docs"] = DescriptionFailed(
        DescriptionFailureReason.NO_UNRESTRICTED_CHUNKS, "no unrestricted chunks to describe"
    )

    ticket = await rig.request("docs")
    settlement = await ticket.settled

    assert settlement.result is GenerationResult.FAILED
    assert settlement.reason is DescriptionFailureReason.NO_UNRESTRICTED_CHUNKS
    assert settlement.detail == "no unrestricted chunks to describe"
    assert rig.writer.failures == [
        ("docs", _NAMESPACE, ExpectedState(description="older", source="generated", last_described=_STAMP))
    ]
    assert rig.writer.applied == []


@pytest.mark.parametrize(
    ("write_outcome", "expected"),
    [
        (DescriptionWriteOutcome.REFUSED_MANUAL, GenerationResult.REFUSED_MANUAL),
        (DescriptionWriteOutcome.REFUSED_STALE, GenerationResult.SUPERSEDED),
        (DescriptionWriteOutcome.COLLECTION_MISSING, GenerationResult.SUPERSEDED),
    ],
)
async def test_a_refused_write_discards_the_generation_and_records_nothing(
    rig: Rig, write_outcome: DescriptionWriteOutcome, expected: GenerationResult
) -> None:
    rig.writer.write_outcome = write_outcome

    settlement = await (await rig.request()).settled

    assert settlement.result is expected
    assert settlement.detail is not None
    assert rig.writer.failures == []
    assert rig.lifecycle.last_attempt is None


async def test_a_lock_timeout_on_the_write_records_the_failure_as_save_failed(rig: Rig) -> None:
    rig.writer.write_outcome = DescriptionWriteOutcome.LOCK_TIMEOUT

    settlement = await (await rig.request()).settled

    assert settlement.result is GenerationResult.FAILED
    assert settlement.reason is DescriptionFailureReason.SAVE_FAILED
    assert [name for name, _, _ in rig.writer.failures] == [_COLLECTION]
    assert rig.lifecycle.last_attempt == LastAttempt(ok=False, at=_NOW)


async def test_a_store_error_on_the_write_is_a_failure_not_an_exception(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.writer.apply_error = OSError("lance exploded with secret description text")

    with caplog.at_level(logging.WARNING, logger=_LIFECYCLE_LOGGER):
        settlement = await (await rig.request()).settled

    assert settlement.result is GenerationResult.FAILED
    assert settlement.reason is DescriptionFailureReason.SAVE_FAILED
    assert [name for name, _, _ in rig.writer.failures] == [_COLLECTION]
    assert "secret description text" not in caplog.text
    assert "OSError" in caplog.text
    assert not rig.lifecycle.is_pending(_COLLECTION)


async def test_a_store_error_on_recording_the_failure_is_swallowed_and_logged(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.generator.outcomes[_COLLECTION] = DescriptionFailed(DescriptionFailureReason.CALL_FAILED, "down")
    rig.writer.failure_error = OSError("cannot write")

    with caplog.at_level(logging.WARNING, logger=_LIFECYCLE_LOGGER):
        settlement = await (await rig.request()).settled

    assert settlement.result is GenerationResult.FAILED
    assert (settlement.reason, settlement.detail) == (DescriptionFailureReason.CALL_FAILED, "down")
    assert not rig.lifecycle.is_pending(_COLLECTION)
    assert "OSError" in caplog.text


@pytest.mark.parametrize(
    ("failure_outcome", "expected"),
    [
        (DescriptionWriteOutcome.WRITTEN, GenerationResult.FAILED),
        (DescriptionWriteOutcome.LOCK_TIMEOUT, GenerationResult.FAILED),
        (DescriptionWriteOutcome.REFUSED_MANUAL, GenerationResult.REFUSED_MANUAL),
        (DescriptionWriteOutcome.REFUSED_STALE, GenerationResult.SUPERSEDED),
    ],
)
async def test_a_failure_that_lands_after_a_manual_set_or_a_clear_is_discarded(
    rig: Rig, failure_outcome: DescriptionWriteOutcome, expected: GenerationResult
) -> None:
    rig.generator.outcomes[_COLLECTION] = DescriptionFailed(DescriptionFailureReason.CALL_FAILED, "down")
    rig.writer.failure_outcome = failure_outcome

    settlement = await (await rig.request()).settled

    assert settlement.result is expected


async def test_last_attempt_records_time_and_outcome_of_the_latest_generation(rig: Rig) -> None:
    assert rig.lifecycle.last_attempt is None
    rig.generator.outcomes["bad"] = DescriptionFailed(DescriptionFailureReason.CALL_FAILED, "down")

    await (await rig.request("good")).settled
    assert rig.lifecycle.last_attempt == LastAttempt(ok=True, at=_NOW)
    await (await rig.request("bad")).settled
    assert rig.lifecycle.last_attempt == LastAttempt(ok=False, at=_NOW)
    await (await rig.request("good")).settled
    assert rig.lifecycle.last_attempt == LastAttempt(ok=True, at=_NOW)


async def test_requests_after_close_are_refused_without_touching_the_store(rig: Rig) -> None:
    """S85: once shutdown begins nothing starts and nothing is read from a store that is about to disconnect."""
    rig.lifecycle.close()

    ticket = await rig.request()

    assert ticket.outcome is RequestOutcome.CLOSED
    assert (await ticket.settled).result is GenerationResult.SHUTTING_DOWN
    assert rig.store.reads == []
    assert not rig.lifecycle.is_pending(_COLLECTION)


async def test_close_during_the_row_read_refuses_the_request(rig: Rig) -> None:
    rig.store.read_gate = asyncio.Event()
    request = asyncio.create_task(rig.request())
    await until(lambda: rig.store.reads != [])

    rig.lifecycle.close()
    rig.store.read_gate.set()
    ticket = await request

    assert ticket.outcome is RequestOutcome.CLOSED
    assert rig.generator.calls == []
    assert not rig.lifecycle.is_pending(_COLLECTION)


async def test_close_alone_does_not_cancel_a_running_generation(rig: Rig) -> None:
    holder = await rig.hold_the_slot()

    rig.lifecycle.close()
    rig.generator.gate("holder").set()

    assert (await holder.settled).result is GenerationResult.GENERATED


async def test_drain_cancels_running_and_waiting_generations_and_clears_pending(rig: Rig) -> None:
    """S35, S85: shutdown cancels and awaits every task the service owns before the stores disconnect."""
    holder = await rig.hold_the_slot()
    waiting = await rig.request("waiting")

    await rig.lifecycle.drain()

    assert (await holder.settled).result is GenerationResult.SHUTTING_DOWN
    assert (await waiting.settled).result is GenerationResult.SHUTTING_DOWN
    assert not rig.lifecycle.is_pending("holder")
    assert not rig.lifecycle.is_pending("waiting")
    assert generation_tasks() == []
    assert rig.generator.running == 0
    assert rig.writer.applied == []
    late = await rig.request("late")
    assert late.outcome is RequestOutcome.CLOSED


async def test_drain_cancels_a_generation_parked_in_the_write(rig: Rig) -> None:
    rig.writer.gate("slow")
    slow = await rig.request("slow")
    await until(lambda: rig.writer.applied != [])

    await rig.lifecycle.drain()

    assert (await slow.settled).result is GenerationResult.SHUTTING_DOWN
    assert not rig.lifecycle.is_pending("slow")


async def test_drain_is_idempotent_and_returns_at_once_with_nothing_running(rig: Rig) -> None:
    await rig.lifecycle.drain()
    await rig.lifecycle.drain()

    assert generation_tasks() == []


async def test_the_slot_passes_on_when_the_running_generation_is_cancelled(rig: Rig) -> None:
    """A cancelled holder frees the slot instead of leaking it (checked through a lifecycle that keeps running)."""
    holder = await rig.hold_the_slot()
    waiting = await rig.request("next")
    holder_task = generation_task("holder")

    holder_task.cancel()

    assert (await holder.settled).result is GenerationResult.SHUTTING_DOWN
    assert (await waiting.settled).result is GenerationResult.GENERATED


async def test_a_generator_that_raises_anyway_is_a_recorded_failed_call_and_frees_the_slot(
    rig: Rig, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``DescriptionGenerator.generate`` never raises; if a stand-in or a future bug does, the slot must not leak and
    the failure is recorded like any failed call."""

    async def explode(collection: str, namespace: str) -> DescriptionOutcome:
        raise RuntimeError("boom with secret excerpt text")

    with monkeypatch.context() as patch, caplog.at_level(logging.WARNING, logger=_LIFECYCLE_LOGGER):
        patch.setattr(rig.generator, "generate", explode)
        failed = await (await rig.request("first")).settled
    ok = await (await rig.request("second")).settled

    assert failed.result is GenerationResult.FAILED
    assert failed.reason is DescriptionFailureReason.CALL_FAILED
    assert [name for name, _, _ in rig.writer.failures] == ["first"]
    assert ok.result is GenerationResult.GENERATED
    assert "secret excerpt text" not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_a_row_that_cannot_be_read_once_the_slot_is_held_settles_failed_and_frees_the_slot(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """No state to record against: the request settles failed, nothing is stored, and the next one runs."""
    holder = await rig.hold_the_slot()
    waiting = await rig.request("waiting")
    follower = await rig.request("follower")
    rig.store.error = OSError("disk gone: secret chunk text")

    with caplog.at_level(logging.WARNING, logger=_LIFECYCLE_LOGGER):
        rig.generator.gate("holder").set()
        await holder.settled
        failed = await waiting.settled
        rig.store.error = None
        followed = await follower.settled

    assert failed.result is GenerationResult.FAILED
    assert rig.writer.failures == []
    assert followed.result is GenerationResult.GENERATED
    assert "secret chunk text" not in caplog.text
    assert "OSError" in caplog.text


@pytest.mark.parametrize("cancel_while", ["running", "waiting"])
async def test_a_caller_that_cancels_the_ticket_future_does_not_leak_the_collection(rig: Rig, cancel_while: str) -> None:
    """A ``wait_for`` timeout or a cancelled describe job cancels the future it awaits: the run's cleanup still happens,
    so the collection is neither ``pending`` nor ``alreadyRunning`` for ever."""
    holder = await rig.hold_the_slot()
    ticket = holder if cancel_while == "running" else await rig.request("waiting")
    await park()

    ticket.settled.cancel()
    rig.generator.gate("holder").set()
    await until(lambda: not rig.lifecycle.is_pending("holder") and not rig.lifecycle.is_pending("waiting"))

    again = await rig.request("holder")
    assert again.outcome is RequestOutcome.ACCEPTED
    assert (await again.settled).result is GenerationResult.GENERATED
    assert generation_tasks() == []


async def test_a_cancelled_ticket_future_of_a_never_started_task_is_cleaned_up_by_drain(rig: Rig) -> None:
    """The task was cancelled before its first step, so only the done-callback can free its place."""
    holder = await rig.hold_the_slot()
    waiting = await rig.request("waiting")
    waiting.settled.cancel()

    await rig.lifecycle.drain()

    assert not rig.lifecycle.is_pending("waiting")
    assert (await holder.settled).result is GenerationResult.SHUTTING_DOWN


# ---------------------------------------------------------------------------
# BE-34: the forced entry ``generate_now`` (the describe job; S29, S58, S59, S80, S85)
# ---------------------------------------------------------------------------


def _row(*, description: str | None, source: str | None, status: str | None, baseline: int | None) -> CollectionMeta:
    return CollectionMeta(
        name=_COLLECTION,
        description=description,
        description_source=source,  # type: ignore[arg-type]
        description_status=status,  # type: ignore[arg-type]
        doc_count=_DOC_COUNT,
        chunk_count=_CHUNK_COUNT,
        described_at_doc_count=baseline,
        last_described=_STAMP if description else None,
    )


_FORCEABLE_ROWS = {
    "generated_and_unchanged": _row(description="old", source="generated", status="ok", baseline=_DOC_COUNT),
    "failed_over_old_text": _row(description="old", source="generated", status="failed", baseline=_DOC_COUNT),
    "failed_with_no_text": _row(description=None, source=None, status="failed", baseline=None),
    "empty": _row(description=None, source=None, status=None, baseline=None),
}


@pytest.mark.parametrize("row", _FORCEABLE_ROWS.values(), ids=_FORCEABLE_ROWS.keys())
async def test_generate_now_forces_over_existing_generated_description(rig: Rig, row: CollectionMeta) -> None:
    """S29: a generated, failed or empty description is regenerated whatever the 20% rule says; the ingest request
    for the very same unchanged row is not required, so the force is the entry's own."""
    rig.store.rows[_COLLECTION] = row
    if row.described_at_doc_count is not None:
        assert (await rig.request(_COLLECTION, GenerationKind.INGEST)).outcome is RequestOutcome.NOT_REQUIRED

    settlement = await rig.lifecycle.generate_now(_COLLECTION, _NAMESPACE)

    assert settlement.result is GenerationResult.GENERATED
    assert rig.generator.calls == [(_COLLECTION, _NAMESPACE)]
    assert [(name, ns) for name, ns, _change in rig.writer.applied] == [(_COLLECTION, _NAMESPACE)]


async def test_generate_now_over_manual_is_refused(rig: Rig) -> None:
    """S59: a manual description is never generated; nothing is asked of the model or the writer."""
    rig.store.rows[_COLLECTION] = _row(description="mine", source="manual", status="ok", baseline=None)

    settlement = await rig.lifecycle.generate_now(_COLLECTION, _NAMESPACE)

    assert settlement.result is GenerationResult.REFUSED_MANUAL
    assert rig.generator.calls == []
    assert rig.writer.applied == []
    assert not rig.lifecycle.is_pending(_COLLECTION)


async def test_generate_now_reports_the_status_stored_when_it_settled(rig: Rig) -> None:
    """The settlement carries the collection's status as read after the run (the job result's ``description_status``):
    the writer stores ``failed`` and that, not the row from before the run, is what is reported."""
    rig.store.rows[_COLLECTION] = _FORCEABLE_ROWS["empty"]
    rig.generator.outcomes[_COLLECTION] = DescriptionFailed(DescriptionFailureReason.CALL_FAILED, "provider down")
    record_failure = rig.writer.record_failure

    async def recording_the_stored_status(
        collection: str, namespace: str, expected: ExpectedState
    ) -> DescriptionWriteOutcome:
        rig.store.rows[collection] = _row(description=None, source=None, status="failed", baseline=None)
        return await record_failure(collection, namespace, expected)

    rig.writer.record_failure = recording_the_stored_status  # type: ignore[method-assign]

    settlement = await rig.lifecycle.generate_now(_COLLECTION, _NAMESPACE)

    assert (settlement.result, settlement.description_status) == (GenerationResult.FAILED, "failed")


async def test_generate_now_settles_failed_when_the_row_cannot_be_read(rig: Rig) -> None:
    """A store that cannot be read is a failed describe (never an exception) with no status to report."""
    rig.store.error = OSError("disk")

    settlement = await rig.lifecycle.generate_now(_COLLECTION, _NAMESPACE)

    assert (settlement.result, settlement.description_status) == (GenerationResult.FAILED, None)
    assert rig.generator.calls == []


async def test_generate_now_over_a_full_queue_settles_failed() -> None:
    """S80: the forced entry is a describe: over the queue bound it settles ``failed``, and nothing is left pending."""
    rig = Rig(max_queued=0)
    try:
        holder = await rig.hold_the_slot()

        settlement = await rig.lifecycle.generate_now(_COLLECTION, _NAMESPACE)

        assert settlement.result is GenerationResult.FAILED
        assert not rig.lifecycle.is_pending(_COLLECTION)
        rig.generator.gate("holder").set()
        await holder.settled
    finally:
        await rig.lifecycle.drain()


async def test_a_cancelled_generate_now_caller_leaves_nothing_pending_and_frees_the_slot(rig: Rig) -> None:
    """The caller (the describe job's task) may be cancelled while it waits: the generation's own cleanup still runs, so
    the collection is not pending afterwards and the next request is served."""
    rig.generator.gate(_COLLECTION)
    caller = asyncio.create_task(rig.lifecycle.generate_now(_COLLECTION, _NAMESPACE))
    await until(lambda: _COLLECTION in rig.generator.started)
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    rig.generator.gate(_COLLECTION).set()
    await until(lambda: not rig.lifecycle.is_pending(_COLLECTION))
    again = await rig.lifecycle.generate_now(_COLLECTION, _NAMESPACE)
    assert again.result is GenerationResult.GENERATED


async def test_generate_now_while_a_generation_runs_settles_already_running_and_reads_pending(rig: Rig) -> None:
    """S80: single-flight holds for the forced entry; the status read says ``pending`` while the other run is in flight."""
    holder = await rig.hold_the_slot(_COLLECTION)

    settlement = await rig.lifecycle.generate_now(_COLLECTION, _NAMESPACE)

    assert (settlement.result, settlement.description_status) == (GenerationResult.ALREADY_RUNNING, "pending")
    rig.generator.gate(_COLLECTION).set()
    await holder.settled


async def test_generate_now_takes_the_describe_slot_class_ahead_of_ingest_and_backfill(rig: Rig) -> None:
    """S80: the forced entry waits for the slot like a describe and is served before waiting ingest and backfill."""
    holder = await rig.hold_the_slot()
    waiting = [await rig.request("i1", GenerationKind.INGEST), await rig.request("b1", GenerationKind.BACKFILL)]
    forced = asyncio.create_task(rig.lifecycle.generate_now("d1", _NAMESPACE))
    await until(lambda: ("d1", _NAMESPACE) in rig.store.reads)

    rig.generator.gate("holder").set()
    await holder.settled
    assert (await forced).result is GenerationResult.GENERATED
    for ticket in waiting:
        await ticket.settled

    assert rig.generator.started == ["holder", "d1", "i1", "b1"]


async def test_generate_now_after_close_is_refused_as_shutting_down_and_reads_nothing(rig: Rig) -> None:
    """S85: once shutdown began nothing starts and no row is read."""
    rig.lifecycle.close()

    settlement = await rig.lifecycle.generate_now(_COLLECTION, _NAMESPACE)

    assert settlement.result is GenerationResult.SHUTTING_DOWN
    assert rig.store.reads == []
    assert rig.generator.calls == []


async def test_generate_now_of_a_missing_collection_settles_superseded_and_writes_nothing(rig: Rig) -> None:
    """S87: the collection was dropped: nothing generated, nothing written, nothing recreated."""
    rig.store.rows[_COLLECTION] = None

    settlement = await rig.lifecycle.generate_now(_COLLECTION, _NAMESPACE)

    assert settlement.result is GenerationResult.SUPERSEDED
    assert (rig.generator.calls, rig.writer.applied, rig.writer.failures) == ([], [], [])
    assert settlement.description_status is None


async def test_generate_now_cancelled_by_drain_settles_shutting_down(rig: Rig) -> None:
    """S85: a describe waiting on its generation when the server drains is told the server is shutting down."""
    rig.generator.gate(_COLLECTION)
    forced = asyncio.create_task(rig.lifecycle.generate_now(_COLLECTION, _NAMESPACE))
    await until(lambda: _COLLECTION in rig.generator.started)

    await rig.lifecycle.drain()

    assert (await forced).result is GenerationResult.SHUTTING_DOWN

"""C5: the description lifecycle service (Use Cases), which owns every generation task.

Three triggers ask for a generation: the ingest rule, the describe job and the startup backfill. All of them come
through :meth:`DescriptionLifecycle.request_generation`, which answers at once with a typed ticket; the work runs in a
background task that the service tracks, so shutdown can refuse new requests and then cancel and await every task
before the stores disconnect (:meth:`close`, :meth:`drain`).

* **Single-flight.** One generation per collection at a time (collection names are globally unique, so the name is the
  key). A second request settles ``alreadyRunning`` at once and never queues, whatever its kind: a describe that meets
  a queued backfill of its collection is refused rather than overtaking it.
* **The slot.** One generation at a time in the whole process, across the three kinds. It covers exactly the read of the
  row, the sample read and the model call (``DescriptionGenerator.generate``) and is released BEFORE the embedding and
  the guarded write (C11), so a collection stuck on its lock or on a slow embedding never stalls the others. The row is
  read after the slot is held: a request that waited neither describes a stale sample nor writes over a description that
  was set or generated meanwhile.
* **The queue.** Waiting requests are served describe first, then the ingest rule, then backfill, in arrival order
  inside a class. It holds ``max_queued`` requests (``MAX_QUEUED_GENERATIONS``); over it a describe settles ``failed``
  and an ingest or backfill request is dropped with one WARNING per process (its baseline stays unset, so the next
  qualifying ingest asks again).
* **State.** ``pending`` (a request that is waiting or running), the last attempt and the failures the store could not
  record live in memory only and are lost on restart (S23). The stored status is never ``pending``: readers lay the
  overlay over the stored status with :meth:`overlay_status`.
* **Manual wins.** A collection whose description source is ``manual`` is never generated, checked when the request
  arrives and again when it gets the slot; the guarded write checks it a third time inside the collection lock.

A failed generation is recorded on the collection through ``DescriptionWriter.record_failure`` under the state the
generation started from. Recording needs the collection lock; when the lock stays busy (or the store errors), and when a
run breaks before it can record anything (a row that cannot be read once the slot is held), the failure is not stored and
the service remembers it instead, with the store's ``description_version`` read before the row: the collection reads
``failed`` through :meth:`overlay_status` for as long as that version is current. Every change of the collection's
description state (a manual set, a clear, an import, any later guarded write, a drop or a meta-row delete) bumps the
version in the store and ends the memory; so does a newer unstored failure of the collection, which replaces it. The
state a reader sees is therefore never the stored ``ok`` of a run that failed, and never a ``failed`` about a row that
was changed or recreated since. The store itself is not touched: a restart reads the old row, and the next ingest,
describe or backfill asks again. Nothing in this module logs an excerpt, a prompt, a description or an exception
message; a failure is logged by collection name and the exception's type.

Shutdown is two calls: :meth:`close` first, so a request that races it is refused, then :meth:`drain`, which cancels and
awaits the tasks and has no timeout of its own (the caller that hosts the service bounds it).

The describe job forces a generation and waits for its settlement through :meth:`DescriptionLifecycle.generate_now`
(``GenerationKind.DESCRIBE`` and the ticket's ``settled`` future); the rule that only the ingest kind asks is
:func:`_should_regenerate`.
"""
from __future__ import annotations

import asyncio
import dataclasses
import heapq
import itertools
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol

from archon_search.collection_meta import (
    DESCRIPTION_SOURCE_MANUAL,
    DESCRIPTION_STATUS_FAILED,
    DESCRIPTION_STATUS_PENDING,
    CollectionMeta,
    DescriptionStatus,
    DescriptionStoredStatus,
)
from archon_search.constants import DEFAULT_NAMESPACE, MAX_QUEUED_GENERATIONS
from archon_search.description_generator import (
    DescriptionFailed,
    DescriptionFailureReason,
    DescriptionOutcome,
    _should_regenerate,
)
from archon_search.description_write import DescriptionWriteOutcome, ExpectedState
from archon_search.description_writer import (
    DescriptionChange,
    GeneratedChange,
    WriteResult,
)
from archon_search.llm_client.once_per_process import warn_once

logger = logging.getLogger(__name__)

#: ``warn_once`` key: one WARNING per process for every dropped ingest or backfill request.
_QUEUE_FULL_WARN_KEY: Final = "description_generation_queue_full"
GENERATION_TASK_NAME_PREFIX: Final = "description-generation:"


class GenerationKind(StrEnum):
    """Who asks, which decides the slot class and whether the ingest rule applies."""

    #: The describe job (``collection describe``): forces, and an operator is waiting, so it is served first.
    DESCRIBE = "describe"
    #: The end of an ingest: generates only when the 20% rule (or a first ingest) says so.
    INGEST = "ingest"
    #: The startup backfill: the loop that calls it picks empty, unfailed, non-manual collections.
    BACKFILL = "backfill"


#: Waiting requests are served in the order the kinds are declared above (describe, ingest, backfill); a smaller rank
#: goes first.
_SLOT_RANK: Final = {kind: rank for rank, kind in enumerate(GenerationKind)}


class RequestOutcome(StrEnum):
    """What ``request_generation`` answers at once."""

    ACCEPTED = "accepted"
    #: A generation of this collection is already waiting or running.
    ALREADY_RUNNING = "alreadyRunning"
    #: The collection's description is manual: never generated.
    REFUSED_MANUAL = "refusedManual"
    #: The ingest rule did not fire, or the collection does not exist: nothing to generate.
    NOT_REQUIRED = "notRequired"
    #: Over ``max_queued`` waiting requests: dropped (ingest, backfill) or settled ``failed`` (describe).
    QUEUE_FULL = "queueFull"
    #: Shutdown began: refused, nothing read and nothing started.
    CLOSED = "closed"
    #: The collection's row could not be read, so nothing was started (logged by collection name and error type).
    UNREADABLE = "unreadable"


class GenerationResult(StrEnum):
    """How a request settled."""

    GENERATED = "generated"
    #: The generation or its save failed, or a describe met a full queue; ``detail`` says why.
    FAILED = "failed"
    ALREADY_RUNNING = "alreadyRunning"
    REFUSED_MANUAL = "refusedManual"
    #: A set, clear or other change landed during the run, or the collection was deleted: the result was discarded.
    SUPERSEDED = "superseded"
    #: The service is shutting down: nothing ran, or the run was cancelled.
    SHUTTING_DOWN = "shuttingDown"
    NOT_REQUIRED = "notRequired"
    #: An ingest or backfill request over the queue bound.
    DROPPED = "dropped"


@dataclasses.dataclass(frozen=True, slots=True)
class GenerationSettlement:
    result: GenerationResult
    #: Operator-readable, free of document text; set for every generation that ran and failed (also when the store
    #: then discarded the recorded failure) and for a describe that met a full queue.
    detail: str | None = None
    #: The failure reason of the generator (C2) or ``saveFailed``; set where ``detail`` comes from a generation.
    reason: DescriptionFailureReason | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class GenerationTicket:
    """The answer to a request: its immediate outcome and a future that settles when the run is over.

    ``settled`` is already done for every outcome but ``accepted``. Ingest and backfill callers ignore it.
    """

    outcome: RequestOutcome
    settled: asyncio.Future[GenerationSettlement]


@dataclasses.dataclass(frozen=True, slots=True)
class DescribeSettlement:
    """How a forced generation (the describe job) settled, with the status a reader sees at that moment."""

    result: GenerationResult
    #: The collection's ``description_status`` as read once the run was over: ``pending`` when another generation of
    #: it is in flight (``alreadyRunning``), ``None`` when nothing is stored or the row cannot be read.
    description_status: DescriptionStatus | None


@dataclasses.dataclass(frozen=True, slots=True)
class LastAttempt:
    """In-memory result of the latest generation that ran to a verdict (generated or failed); shown by ``/status``."""

    ok: bool
    at: datetime


class LifecycleStore(Protocol):
    """What the lifecycle reads of the store: the row (its source, counts and baseline) and the version of its description
    state (``SearchStore.description_version``, which changes whenever that state does)."""

    async def get_collection_meta(self, name: str, namespace: str = DEFAULT_NAMESPACE) -> CollectionMeta | None: ...

    def description_version(self, collection: str) -> int: ...


class DescriptionGeneration(Protocol):
    """``DescriptionGenerator.generate``: samples, asks the model (one retry at most), never raises."""

    async def generate(self, collection: str, namespace: str) -> DescriptionOutcome: ...


class DescriptionWrites(Protocol):
    """``DescriptionWriter``: the guarded write of a generated description and of a recorded failure (C11)."""

    async def apply(self, collection: str, namespace: str, change: DescriptionChange) -> WriteResult: ...

    async def record_failure(
        self, collection: str, namespace: str, expected: ExpectedState
    ) -> DescriptionWriteOutcome: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _GenerationSlot:
    """The process-wide slot of one generation, with a bounded queue served by class and then by arrival.

    A request reserves its place synchronously (so the bound is exact at request time) and awaits the returned future,
    which completes when the slot is its. ``release`` is idempotent and also removes a place that is still waiting.
    """

    def __init__(self, max_queued: int) -> None:
        self._max_queued = max_queued
        self._holder: asyncio.Future[None] | None = None
        self._waiting: list[tuple[int, int, asyncio.Future[None]]] = []
        self._arrival = itertools.count()

    @property
    def max_queued(self) -> int:
        return self._max_queued

    def is_full(self) -> bool:
        """Whether one more waiting request would exceed the bound; a free slot never queues anyone."""
        return self._holder is not None and len(self._waiting) >= self._max_queued

    def reserve(self, rank: int) -> asyncio.Future[None]:
        granted: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if self._holder is None:
            self._holder = granted
            granted.set_result(None)
        else:
            heapq.heappush(self._waiting, (rank, next(self._arrival), granted))
        return granted

    def release(self, granted: asyncio.Future[None]) -> None:
        if self._holder is granted:
            self._pass_on()
            return
        remaining = [entry for entry in self._waiting if entry[2] is not granted]
        if len(remaining) != len(self._waiting):
            heapq.heapify(remaining)
            self._waiting = remaining
            granted.cancel()

    def _pass_on(self) -> None:
        self._holder = None
        while self._waiting:
            _, _, next_granted = heapq.heappop(self._waiting)
            if not next_granted.done():
                self._holder = next_granted
                next_granted.set_result(None)
                return


@dataclasses.dataclass(slots=True)
class _Job:
    collection: str
    namespace: str
    kind: GenerationKind
    granted: asyncio.Future[None]
    settled: asyncio.Future[GenerationSettlement]
    #: Set by ``_finish``: its cleanup runs once, whatever happens to ``settled`` (a caller may cancel that future).
    finished: bool = False
    #: The store's ``description_version`` of the collection, read once the slot is held and before the row is read
    #: (``None`` until then).
    version: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class _Draft:
    """What the slot produced: the outcome of the generator and the state it started from."""

    outcome: DescriptionOutcome
    expected: ExpectedState
    doc_count: int


_SHUTDOWN: Final = GenerationSettlement(GenerationResult.SHUTTING_DOWN)
#: A refusal at request time and at slot time share one rule; the request answers with the matching outcome.
_OUTCOME_OF_REFUSAL: Final = {
    GenerationResult.REFUSED_MANUAL: RequestOutcome.REFUSED_MANUAL,
    GenerationResult.NOT_REQUIRED: RequestOutcome.NOT_REQUIRED,
}
#: What a failure that the store refused (a manual set or a change landed meanwhile) settles as.
_RESULT_OF_REFUSED_FAILURE: Final = {
    DescriptionWriteOutcome.REFUSED_MANUAL: GenerationResult.REFUSED_MANUAL,
    DescriptionWriteOutcome.REFUSED_STALE: GenerationResult.SUPERSEDED,
    DescriptionWriteOutcome.COLLECTION_MISSING: GenerationResult.SUPERSEDED,
}
_RESULT_OF_WRITE: Final = {
    DescriptionWriteOutcome.WRITTEN: GenerationResult.GENERATED,
    **_RESULT_OF_REFUSED_FAILURE,
}
#: What the operator reads when the guarded write discarded a generated description.
_DETAIL_OF_DISCARDED_WRITE: Final = {
    DescriptionWriteOutcome.REFUSED_MANUAL: "a manual description was set meanwhile; the generated text was discarded",
    DescriptionWriteOutcome.REFUSED_STALE: "the description changed meanwhile; the generated text was discarded",
    DescriptionWriteOutcome.COLLECTION_MISSING: "the collection no longer exists",
}
_ATTEMPT_RESULTS: Final = frozenset({GenerationResult.GENERATED, GenerationResult.FAILED})
#: Why a write was not made when the collection lock stayed taken past ``INGEST_LOCK_TIMEOUT_S``.
_COLLECTION_BUSY: Final = "the collection was busy"


def _refusal(kind: GenerationKind, meta: CollectionMeta) -> GenerationResult | None:
    """Why a generation of *meta*'s collection must not happen, or ``None``: manual wins; the ingest kind also asks the
    unchanged 20% rule over the counts stored now against the baseline of the last description; backfill fills only a
    collection that has no description and whose stored status is not ``failed`` (the row may have changed since the
    backfill listed it)."""
    if meta.description_source == DESCRIPTION_SOURCE_MANUAL:
        return GenerationResult.REFUSED_MANUAL
    if kind is GenerationKind.INGEST and not _should_regenerate(
        meta.doc_count, meta.chunk_count, meta.described_at_doc_count
    ):
        return GenerationResult.NOT_REQUIRED
    if kind is GenerationKind.BACKFILL and (meta.description or meta.description_status == DESCRIPTION_STATUS_FAILED):
        return GenerationResult.NOT_REQUIRED
    return None


def _resolved(outcome: RequestOutcome, settlement: GenerationSettlement) -> GenerationTicket:
    settled: asyncio.Future[GenerationSettlement] = asyncio.get_running_loop().create_future()
    settled.set_result(settlement)
    return GenerationTicket(outcome, settled)


class DescriptionLifecycle:
    """Owns every description generation task of the process (C5)."""

    def __init__(
        self,
        store: LifecycleStore,
        generator: DescriptionGeneration,
        writer: DescriptionWrites,
        *,
        max_queued: int = MAX_QUEUED_GENERATIONS,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._generator = generator
        self._writer = writer
        self._clock = clock
        self._slot = _GenerationSlot(max_queued)
        #: Collections with a request waiting or running: the single-flight map and the ``pending`` overlay.
        self._in_flight: set[str] = set()
        #: Collection -> the ``description_version`` a failed generation started from, for a failure the store could not
        #: record (see the module docstring); only entries whose version is still current are kept.
        self._unstored_failures: dict[str, int] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._last_attempt: LastAttempt | None = None

    # ------------------------------------------------------------------ requests

    async def request_generation(self, collection: str, namespace: str, kind: GenerationKind) -> GenerationTicket:
        """Ask for a generation; the work runs in the background and the ticket says what became of the request.

        Reads the row once (manual short-circuit, and the ingest rule for the ingest kind), so the ticket is answered
        after that one read and not before it; never raises for a store failure. A request that is accepted is
        ``pending`` from that moment until it settles.
        """
        if (refused := self._refused_without_reading(collection)) is not None:
            return refused
        try:
            meta = await self._store.get_collection_meta(collection, namespace)
        except Exception as failure:  # noqa: BLE001 — the ingest path must not fail because the row could not be read
            logger.warning(
                "Description request for collection %r not started: its row could not be read (%s)",
                collection, type(failure).__name__,
            )
            detail = f"the collection could not be read ({type(failure).__name__})"
            return _resolved(RequestOutcome.UNREADABLE, GenerationSettlement(GenerationResult.FAILED, detail=detail))
        # The read was awaited: shutdown may have begun and a twin request may have been accepted meanwhile.
        if (refused := self._refused_without_reading(collection)) is not None:
            return refused
        if meta is None:
            return _resolved(RequestOutcome.NOT_REQUIRED, GenerationSettlement(GenerationResult.NOT_REQUIRED))
        if (refusal := _refusal(kind, meta)) is not None:
            return _resolved(_OUTCOME_OF_REFUSAL[refusal], GenerationSettlement(refusal))
        return self._admit(collection, namespace, kind)

    async def generate_now(self, collection: str, namespace: str) -> DescribeSettlement:
        """The forced entry of the describe job: generate *collection*'s description whatever the 20% rule or the
        stored status say, wait for the run and answer how it settled (C5 ``generateNow``).

        It is ``request_generation(DESCRIBE)`` awaited: the describe kind skips the ingest rule and the
        empty-description rule of backfill, and keeps the manual short-circuit (``refusedManual``), single-flight
        (``alreadyRunning``), the process slot and the queue (served first, a full queue settles ``failed``). After
        :meth:`close` nothing starts and nothing is read: ``shuttingDown``. A collection that is gone settles
        ``superseded``; a row that cannot be read, ``failed``. Never raises for a store failure.
        """
        ticket = await self.request_generation(collection, namespace, GenerationKind.DESCRIBE)
        if ticket.outcome is RequestOutcome.UNREADABLE:  # the row cannot be read: no second read for a status
            return DescribeSettlement((await ticket.settled).result, None)
        result = (await ticket.settled).result
        if result is GenerationResult.NOT_REQUIRED:  # no row: the collection no longer exists
            result = GenerationResult.SUPERSEDED
        if result is GenerationResult.SHUTTING_DOWN:
            return DescribeSettlement(result, None)
        return DescribeSettlement(result, await self._visible_status(collection, namespace))

    async def _visible_status(self, collection: str, namespace: str) -> DescriptionStatus | None:
        try:
            meta = await self._store.get_collection_meta(collection, namespace)
        except Exception as failure:  # noqa: BLE001 — the status is informational; the settlement stands without it
            logger.warning(
                "Reading the description status of collection %r failed (%s)", collection, type(failure).__name__
            )
            return None
        return self.overlay_status(collection, meta.description_status if meta is not None else None)

    def _refused_without_reading(self, collection: str) -> GenerationTicket | None:
        if self._closed:
            return _resolved(RequestOutcome.CLOSED, _SHUTDOWN)
        if collection in self._in_flight:
            return _resolved(RequestOutcome.ALREADY_RUNNING, GenerationSettlement(GenerationResult.ALREADY_RUNNING))
        return None

    def _admit(self, collection: str, namespace: str, kind: GenerationKind) -> GenerationTicket:
        if self._slot.is_full():
            return self._queue_full(collection, kind)
        loop = asyncio.get_running_loop()
        job = _Job(collection, namespace, kind, self._slot.reserve(_SLOT_RANK[kind]), loop.create_future())
        self._in_flight.add(collection)
        task = loop.create_task(self._run(job), name=f"{GENERATION_TASK_NAME_PREFIX}{collection}")
        self._tasks.add(task)
        task.add_done_callback(lambda finished: self._on_task_done(job, finished))
        return GenerationTicket(RequestOutcome.ACCEPTED, job.settled)

    def _queue_full(self, collection: str, kind: GenerationKind) -> GenerationTicket:
        if kind is GenerationKind.DESCRIBE:
            detail = f"the generation queue is full ({self._slot.max_queued} waiting); try again later"
            return _resolved(RequestOutcome.QUEUE_FULL, GenerationSettlement(GenerationResult.FAILED, detail=detail))
        warn_once(
            logger,
            _QUEUE_FULL_WARN_KEY,
            "The description generation queue is full (%d waiting): ingest and backfill requests are dropped "
            "until it drains, and the next qualifying ingest asks again",
            self._slot.max_queued,
        )
        return _resolved(RequestOutcome.QUEUE_FULL, GenerationSettlement(GenerationResult.DROPPED))

    # ------------------------------------------------------------------ state for readers

    def is_pending(self, collection: str) -> bool:
        """Whether a generation of *collection* is waiting or running."""
        return collection in self._in_flight

    def overlay_status(self, collection: str, stored: DescriptionStoredStatus | None) -> DescriptionStatus | None:
        """The status a reader sees: *stored* (no overlay, from the store) with ``pending`` laid over it while a
        generation of *collection* is in flight, and ``failed`` while its last generation failed without the store
        being able to record it and nothing has changed the collection's description state since (the store's
        ``description_version`` is still the one the failed run started from). Collection names are globally unique, so
        unlike the contract's ``overlayStatus`` this takes no namespace. Reads only."""
        if self.is_pending(collection):
            return DESCRIPTION_STATUS_PENDING
        remembered = self._unstored_failures.get(collection)
        if remembered is not None and remembered == self._store.description_version(collection):
            return DESCRIPTION_STATUS_FAILED
        return stored

    @property
    def last_attempt(self) -> LastAttempt | None:
        """The latest generation that ran to a verdict (generated or failed), or ``None`` since the process started."""
        return self._last_attempt

    # ------------------------------------------------------------------ shutdown

    def close(self) -> None:
        """Refuse every new request from now on. Running and waiting generations carry on until :meth:`drain`."""
        self._closed = True

    async def drain(self) -> None:
        """Close, then cancel and await every task the service owns, so no description is half written and no provider
        call outlives the stores. Idempotent."""
        self.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ the task

    def _on_task_done(self, job: _Job, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        # A task cancelled before its first step never ran its ``finally``; this settles it. A no-op after a normal run.
        self._finish(job, _SHUTDOWN)

    def _finish(self, job: _Job, settlement: GenerationSettlement) -> None:
        """Clear ``pending``, free the slot or the place in the queue, settle and record the attempt. Runs once."""
        if job.finished:
            return
        job.finished = True
        self._in_flight.discard(job.collection)
        self._slot.release(job.granted)
        if not job.settled.done():  # a caller may have cancelled the ticket's future; the cleanup above still ran
            job.settled.set_result(settlement)
        if settlement.result in _ATTEMPT_RESULTS:
            self._last_attempt = LastAttempt(ok=settlement.result is GenerationResult.GENERATED, at=self._clock())

    async def _run(self, job: _Job) -> None:
        settlement = _SHUTDOWN  # what a cancellation settles as
        try:
            drafted = await self._draft(job)
            settlement = drafted if isinstance(drafted, GenerationSettlement) else await self._land(job, drafted)
        except Exception as failure:  # noqa: BLE001 — a task must settle its ticket whatever raised
            logger.warning(
                "Description generation of collection %r stopped unexpectedly (%s)", job.collection, type(failure).__name__
            )
            settlement = GenerationSettlement(GenerationResult.FAILED, detail=f"unexpected {type(failure).__name__}")
            self._remember_unstored_failure(job)
        finally:
            self._finish(job, settlement)

    async def _draft(self, job: _Job) -> _Draft | GenerationSettlement:
        """Hold the slot for the row read, the sample read and the model call, and nothing else."""
        try:
            await job.granted
            job.version = self._store.description_version(job.collection)  # before the row read: a change during it counts
            meta = await self._store.get_collection_meta(job.collection, job.namespace)
            if meta is None:
                return GenerationSettlement(GenerationResult.SUPERSEDED, detail="the collection no longer exists")
            if (refusal := _refusal(job.kind, meta)) is not None:
                return GenerationSettlement(refusal)
            expected = ExpectedState(
                meta.description, meta.description_source, meta.last_described, meta.description_status
            )
            return _Draft(await self._generate(job), expected, meta.doc_count)
        finally:
            self._slot.release(job.granted)

    async def _generate(self, job: _Job) -> DescriptionOutcome:
        """The generator never raises; a stand-in or a bug that does is a failed call, so the slot and the status hold."""
        try:
            return await self._generator.generate(job.collection, job.namespace)
        except Exception as failure:  # noqa: BLE001 — see above
            logger.warning(
                "Description generation of collection %r raised unexpectedly (%s)", job.collection, type(failure).__name__
            )
            detail = f"unexpected {type(failure).__name__}"
            return DescriptionFailed(DescriptionFailureReason.CALL_FAILED, detail)

    async def _land(self, job: _Job, draft: _Draft) -> GenerationSettlement:
        """Embed and write the generated text, or record the failure, under the state the generation started from."""
        outcome = draft.outcome
        if isinstance(outcome, DescriptionFailed):
            return await self._record_failure(job, draft.expected, outcome.reason, outcome.detail)
        change = GeneratedChange(text=outcome.text, doc_count_at_generation=draft.doc_count, expected=draft.expected)
        try:
            written = await self._writer.apply(job.collection, job.namespace, change)
        except Exception as failure:  # noqa: BLE001 — a store error while saving is a failed generation, not a crash
            logger.warning(
                "Saving the description of collection %r failed (%s)", job.collection, type(failure).__name__
            )
            detail = f"the description could not be saved ({type(failure).__name__})"
            return await self._record_failure(job, draft.expected, DescriptionFailureReason.SAVE_FAILED, detail)
        verdict = written.outcome
        if verdict is DescriptionWriteOutcome.LOCK_TIMEOUT:
            detail = f"{_COLLECTION_BUSY}: the description could not be saved"
            return await self._record_failure(job, draft.expected, DescriptionFailureReason.SAVE_FAILED, detail)
        return GenerationSettlement(_RESULT_OF_WRITE[verdict], detail=_DETAIL_OF_DISCARDED_WRITE.get(verdict))

    async def _record_failure(
        self, job: _Job, expected: ExpectedState, reason: DescriptionFailureReason, detail: str
    ) -> GenerationSettlement:
        """Store status ``failed`` (the old text is kept). A failure the store refuses (a manual set, a clear or a
        change landed meanwhile) is discarded and settles as that refusal; one it cannot take (the collection stayed
        busy, or the store errored) is remembered, so readers still see ``failed`` (:meth:`overlay_status`)."""
        verdict = GenerationResult.FAILED
        not_stored: str | None = None
        try:
            recorded = await self._writer.record_failure(job.collection, job.namespace, expected)
        except Exception as failure:  # noqa: BLE001 — the failure is reported to the caller even when it cannot be stored
            not_stored = type(failure).__name__
        else:
            verdict = _RESULT_OF_REFUSED_FAILURE.get(recorded, GenerationResult.FAILED)
            if recorded is DescriptionWriteOutcome.LOCK_TIMEOUT:
                not_stored = _COLLECTION_BUSY
        if not_stored is not None:
            self._remember_unstored_failure(job)
            logger.warning(
                "The failed description of collection %r could not be stored (%s); it reads as failed until the next attempt",
                job.collection, not_stored,
            )
        return GenerationSettlement(verdict, detail=detail, reason=reason)

    def _remember_unstored_failure(self, job: _Job) -> None:
        """Remember that *job*'s generation failed and the store did not take it (:meth:`overlay_status` reads it back).
        Entries a later change of their collection made stale are forgotten here, so the map holds only what readers
        still see."""
        current = {
            name: version
            for name, version in self._unstored_failures.items()
            if version == self._store.description_version(name)
        }
        if job.version is not None:
            current[job.collection] = job.version
        self._unstored_failures = current

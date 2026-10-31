"""Stand-ins for the description lifecycle that pipeline tests inject (``SearchPipeline(description_lifecycle=...)``).

Two kinds. ``fake_lifecycle`` answers every request with a fixed ticket, for a test that only cares what the ingest asks.
``recorded_lifecycle`` is the real ``DescriptionLifecycle`` over the given store with a scripted generator and the real
guarded ``DescriptionWriter``, whose tickets a ``TicketLog`` keeps so a test can wait for the background run it asked for.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from archon_search.description_generator import (
    DescriptionFailed,
    DescriptionFailureReason,
    DescriptionGenerated,
    DescriptionOutcome,
)
from archon_search.description_lifecycle import (
    DescriptionGeneration,
    DescriptionLifecycle,
    GenerationKind,
    GenerationResult,
    GenerationSettlement,
    GenerationTicket,
    RequestOutcome,
)
from archon_search.description_writer import (
    DescriptionEmbedder,
    DescriptionWriter,
    DescriptionWriteStore,
)

#: The model the test pipelines embed with (``MockEmbedderBackend.model_name``); the writer's default for a collection
#: that pins none.
PIPELINE_EMBEDDING_MODEL = "mock-embedder"
SETTLE_TIMEOUT_S = 30.0
_STUB_VECTOR = [0.25, 0.5, 0.75, 1.0]


class ScriptedGenerator:
    """A ``DescriptionGenerator`` whose ``generate`` answers *text* as a generated description (a failed call for
    ``None``). ``gate`` parks every call until it is set, like a provider that has not answered yet."""

    def __init__(self, text: str | None, *, gate: asyncio.Event | None = None) -> None:
        self._outcome: DescriptionOutcome = (
            DescriptionFailed(DescriptionFailureReason.CALL_FAILED, "the provider is down")
            if text is None
            else DescriptionGenerated(text=text)
        )
        self._gate = gate
        self.started = asyncio.Event()
        self.calls: list[tuple[str, str]] = []

    async def generate(self, collection: str, namespace: str) -> DescriptionOutcome:
        self.calls.append((collection, namespace))
        self.started.set()
        if self._gate is not None:
            await self._gate.wait()
        return self._outcome


class _VectorEmbedder:
    async def embed_one(self, text: str) -> list[float]:
        return list(_STUB_VECTOR)


class _VectorCache:
    async def get_or_load(self, model_name: str) -> DescriptionEmbedder:
        return _VectorEmbedder()


class TicketLog:
    """Keeps the ticket of every request the pipeline made to a lifecycle."""

    def __init__(self, lifecycle: DescriptionLifecycle) -> None:
        self.tickets: list[GenerationTicket] = []
        self._request = lifecycle.request_generation
        lifecycle.request_generation = self._spy  # type: ignore[method-assign]

    async def _spy(self, collection: str, namespace: str, kind: GenerationKind) -> GenerationTicket:
        ticket = await self._request(collection, namespace, kind)
        self.tickets.append(ticket)
        return ticket

    @property
    def outcomes(self) -> list[RequestOutcome]:
        return [ticket.outcome for ticket in self.tickets]

    async def settle(self) -> list[GenerationSettlement]:
        """Wait for every accepted background run (a run that never settles fails the test instead of hanging it); the
        settlement of each request, in request order."""
        return [await asyncio.wait_for(ticket.settled, timeout=SETTLE_TIMEOUT_S) for ticket in self.tickets]

    async def generation_results(self) -> list[GenerationResult]:
        return [settlement.result for settlement in await self.settle()]


def recorded_lifecycle(store: DescriptionWriteStore, generator: DescriptionGeneration) -> tuple[DescriptionLifecycle, TicketLog]:
    """The real lifecycle over *store*, its tickets logged."""
    writer = DescriptionWriter(store, _VectorCache(), default_embedding_model=PIPELINE_EMBEDDING_MODEL)
    lifecycle = DescriptionLifecycle(store, generator, writer)  # type: ignore[arg-type]  # the store serves both protocols
    return lifecycle, TicketLog(lifecycle)


def fake_lifecycle() -> MagicMock:
    """A ``DescriptionLifecycle`` whose ``request_generation`` answers an accepted ticket and runs nothing."""
    ticket = GenerationTicket(RequestOutcome.ACCEPTED, None)  # type: ignore[arg-type]  # nothing awaits a fake's ticket
    return MagicMock(spec=DescriptionLifecycle, request_generation=AsyncMock(return_value=ticket))

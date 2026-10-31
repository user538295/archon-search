"""C11: the shared write-description operation (generate, manual set, clear and import).

One operation owns every change of a collection's description, so whoever changes the text also refreshes its routing
vector in the same step (S43). The text's vector is computed with the COLLECTION's own embedder (the model pinned in
its row, taken from the embedder cache), not the pipeline's global one, because collections carry per-collection
models and the router accepts a description vector only for a matching model. The embedding is slow, so it runs
OUTSIDE the collection lock; the store's guarded write (C3, ``SearchStore.write_description``) then takes the lock
itself, re-reads the row, checks the guard and writes text and vector once. The caller must not hold the lock.

An embedding that fails or outruns ``embed_wait_seconds`` is not a failed write: the description is kept without a
vector (the old text's vector is removed with the old text) and one WARNING says so (S69). The wait wraps the whole
step, the embedder load and the first embedding, because the cache's own bound covers only a caller that joins another
caller's load, never the caller that is itself the loader.

This module is the only writer of the description vector for a CHANGED description: the metadata recompute carries the
stored vector and only fills in a legacy description that never had one (S88). Known limit (C11 guards the
description, source and last-described, not the model): a reindex that promotes the collection to another model
between the read of the model here and the guarded write leaves a vector of the old model until the text next changes.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from archon_search.collection_meta import (
    DESCRIPTION_SOURCE_GENERATED,
    DESCRIPTION_SOURCE_MANUAL,
    DESCRIPTION_STATUS_FAILED,
    DESCRIPTION_STATUS_OK,
    CollectionMeta,
    DescriptionSource,
)
from archon_search.constants import DEFAULT_NAMESPACE, EMBED_WAIT_SECONDS
from archon_search.description_write import (
    UNCONDITIONAL,
    DescriptionFields,
    DescriptionWriteOutcome,
    ExpectedState,
    FieldChange,
    WriteGuard,
    check_description_text,
    is_storable_vector,
)

logger = logging.getLogger(__name__)


class DescriptionWriteStore(Protocol):
    """What the operation needs of the store: the row (for its embedding model) and the guarded write (C3)."""

    async def get_collection_meta(self, name: str, namespace: str = DEFAULT_NAMESPACE) -> CollectionMeta | None: ...

    async def write_description(
        self, collection: str, namespace: str, fields: DescriptionFields, guard: WriteGuard
    ) -> DescriptionWriteOutcome: ...


class DescriptionEmbedder(Protocol):
    async def embed_one(self, text: str) -> list[float]: ...


class CollectionEmbedders(Protocol):
    """Hands out the embedder of a model; ``EmbedderCache`` is the production implementation."""

    async def get_or_load(self, model_name: str) -> DescriptionEmbedder: ...


@dataclasses.dataclass(frozen=True)
class GeneratedChange:
    """A generated description. ``expected`` is the state the generation started from, read after it got its slot (C5):
    the write is refused when the row differs inside the lock."""

    text: str
    doc_count_at_generation: int
    expected: ExpectedState


@dataclasses.dataclass(frozen=True)
class ManualSetChange:
    """A description the operator wrote: source ``manual``, last-described now, baseline untouched."""

    text: str


@dataclasses.dataclass(frozen=True)
class ClearChange:
    """Reset the description, its source, status, last-described, baseline and vector. Nothing is generated."""


@dataclasses.dataclass(frozen=True)
class ImportedChange:
    """A description restored from an archive. ``text`` is already normalised by the import rules (C9)."""

    text: str
    source: DescriptionSource


type DescriptionChange = GeneratedChange | ManualSetChange | ClearChange | ImportedChange


class VectorOutcome(StrEnum):
    REFRESHED = "refreshed"
    REMOVED = "removed"
    #: Embedding failed or timed out: the description is kept without a vector and a WARNING was logged.
    KEPT_WITHOUT_VECTOR = "keptWithoutVector"
    #: Nothing was written, so the vector is unchanged.
    UNTOUCHED = "untouched"


@dataclasses.dataclass(frozen=True)
class WriteResult:
    outcome: DescriptionWriteOutcome
    vector: VectorOutcome


class DescriptionWriter:
    """Writes a collection's description and its routing vector in one guarded step (C11)."""

    def __init__(
        self,
        store: DescriptionWriteStore,
        embedders: CollectionEmbedders,
        *,
        default_embedding_model: str,
        embed_wait_seconds: float = EMBED_WAIT_SECONDS,
    ) -> None:
        self._store = store
        self._embedders = embedders
        self._default_embedding_model = default_embedding_model
        self._embed_wait_seconds = embed_wait_seconds

    async def apply(self, collection: str, namespace: str, change: DescriptionChange) -> WriteResult:
        """Apply *change*: embed (outside the lock) and write text and vector once, guarded.

        Returns the store's outcome with what happened to the vector. A lock timeout, a refusal and a missing
        collection are returned, never raised, so the caller can record them; a store error propagates.

        Raises:
            ValueError: if the text of *change* is blank (clear it instead); nothing is embedded or written.
        """
        if isinstance(change, ClearChange):
            outcome = await self._store.write_description(collection, namespace, _clear_fields(), UNCONDITIONAL)
            return WriteResult(outcome, _vector_outcome(outcome, VectorOutcome.REMOVED))

        check_description_text(change.text)
        row = await self._store.get_collection_meta(collection, namespace)
        if row is None:
            return WriteResult(DescriptionWriteOutcome.COLLECTION_MISSING, VectorOutcome.UNTOUCHED)
        model = row.active_embedding_model or self._default_embedding_model
        vector = await self._embed(collection, model, change.text)
        vector_change = FieldChange.clear() if vector is None else FieldChange.set_to(vector)
        guard = change.expected if isinstance(change, GeneratedChange) else UNCONDITIONAL
        outcome = await self._store.write_description(
            collection, namespace, _text_fields(change, vector_change), guard
        )
        landed = VectorOutcome.KEPT_WITHOUT_VECTOR if vector is None else VectorOutcome.REFRESHED
        return WriteResult(outcome, _vector_outcome(outcome, landed))

    async def record_failure(
        self, collection: str, namespace: str, expected: ExpectedState
    ) -> DescriptionWriteOutcome:
        """Record a failed generation: status ``failed``, the earlier description kept.

        Guarded like a generated write by the state the generation started from, so a failure that lands after a
        manual set (``refusedManual``) or a clear (``refusedStale``) is refused and discarded.
        """
        fields = DescriptionFields(
            description=FieldChange.keep(),
            source=FieldChange.keep(),
            status=FieldChange.set_to(DESCRIPTION_STATUS_FAILED),
            last_described=FieldChange.keep(),
            described_at_doc_count=FieldChange.keep(),
            description_embedding=FieldChange.keep(),
        )
        return await self._store.write_description(collection, namespace, fields, expected)

    async def _embed(self, collection: str, model: str, text: str) -> list[float] | None:
        """The routing vector of *text* from *model*'s embedder, or ``None`` after one WARNING when it cannot be had."""
        try:
            async with asyncio.timeout(self._embed_wait_seconds):
                embedder = await self._embedders.get_or_load(model)
                vector = await embedder.embed_one(text)
        except Exception as exc:  # noqa: BLE001 — any failure to embed keeps the description without a vector (S69)
            self._warn_kept_without_vector(collection, model, type(exc).__name__)
            return None
        if not is_storable_vector(vector):
            self._warn_kept_without_vector(collection, model, "unusable vector")
            return None
        return vector

    @staticmethod
    def _warn_kept_without_vector(collection: str, model: str, reason: str) -> None:
        # Names the collection, the model and the reason's class only: never the description text, and no traceback.
        logger.warning(
            "Description of collection %r kept without a routing vector: embedding with model %r failed (%s)",
            collection, model, reason,
        )


def _vector_outcome(outcome: DescriptionWriteOutcome, written: VectorOutcome) -> VectorOutcome:
    return written if outcome is DescriptionWriteOutcome.WRITTEN else VectorOutcome.UNTOUCHED


def _clear_fields() -> DescriptionFields:
    return DescriptionFields(
        description=FieldChange.clear(),
        source=FieldChange.clear(),
        status=FieldChange.clear(),
        last_described=FieldChange.clear(),
        described_at_doc_count=FieldChange.clear(),
        description_embedding=FieldChange.clear(),
    )


def _text_fields(
    change: GeneratedChange | ManualSetChange | ImportedChange, vector: FieldChange[list[float]]
) -> DescriptionFields:
    """The fields of a write that sets text: status ``ok`` and last-described now, with the source and baseline each
    kind of change owns (C3: generated saves the baseline, set leaves it, import leaves it unset)."""
    match change:
        case GeneratedChange():
            source, baseline = DESCRIPTION_SOURCE_GENERATED, FieldChange.set_to(change.doc_count_at_generation)
        case ManualSetChange():
            source, baseline = DESCRIPTION_SOURCE_MANUAL, FieldChange.keep()
        case ImportedChange():
            source, baseline = change.source, FieldChange.clear()
    return DescriptionFields(
        description=FieldChange.set_to(change.text),
        source=FieldChange.set_to(source),
        status=FieldChange.set_to(DESCRIPTION_STATUS_OK),
        last_described=FieldChange.set_to(datetime.now(UTC)),
        described_at_doc_count=baseline,
        description_embedding=vector,
    )

"""BE-20 (C11, S43, S69): the shared write-description operation, against a recording store and embedder cache.

The operation embeds with the collection's own embedder, under ``embedWaitSeconds``, outside the collection lock, and
hands the text and its routing vector to the store's guarded write in one step. An embedding failure keeps the text.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.constants import DEFAULT_NAMESPACE, EMBED_WAIT_SECONDS
from archon_search.description_write import (
    UNCONDITIONAL,
    DescriptionFields,
    DescriptionWriteOutcome,
    ExpectedState,
    FieldOp,
    WriteGuard,
)
from archon_search.description_writer import (
    ClearChange,
    CollectionEmbedders,
    DescriptionWriter,
    GeneratedChange,
    ImportedChange,
    ManualSetChange,
    VectorOutcome,
    WriteResult,
)
from archon_search.embedder_cache import EmbedderNotReadyError

_COLLECTION = "docs"
_COLLECTION_MODEL = "model-collection"
_DEFAULT_MODEL = "model-default"
_TEXT = "Notes about the gardening club and its seed library."
_EMBEDDING = [0.25, 0.5, 0.75]
_DOC_COUNT = 42
_EXPECTED = ExpectedState(description="old text", source="generated", last_described=datetime(2026, 1, 1, tzinfo=UTC))
_SHORT_WAIT_S = 0.01
_WRITER_LOGGER = "archon_search.description_writer"


class _RecordingStore:
    """A store port that serves one row and records every guarded write."""

    def __init__(self, row: CollectionMeta | None, outcome: DescriptionWriteOutcome = DescriptionWriteOutcome.WRITTEN):
        self.row = row
        self.outcome = outcome
        self.writes: list[tuple[str, str, DescriptionFields, WriteGuard]] = []
        self.events: list[str] = []

    async def get_collection_meta(self, name: str, namespace: str = DEFAULT_NAMESPACE) -> CollectionMeta | None:
        self.events.append("read")
        return self.row

    async def write_description(
        self, collection: str, namespace: str, fields: DescriptionFields, guard: WriteGuard
    ) -> DescriptionWriteOutcome:
        self.events.append("write")
        self.writes.append((collection, namespace, fields, guard))
        return self.outcome


class _Embedder:
    def __init__(self, vector: list[float] | None = None, error: Exception | None = None, hang: bool = False):
        self._vector = list(_EMBEDDING) if vector is None else vector
        self._error = error
        self._hang = hang
        self.texts: list[str] = []

    async def embed_one(self, text: str) -> list[float]:
        self.texts.append(text)
        if self._hang:
            await asyncio.sleep(3600)
        if self._error is not None:
            raise self._error
        return list(self._vector)


class _Cache:
    """An embedder cache that records which model each caller asked for."""

    def __init__(self, embedder: _Embedder, load_error: Exception | None = None):
        self.embedder = embedder
        self.load_error = load_error
        self.requested: list[str] = []

    async def get_or_load(self, model_name: str) -> _Embedder:
        self.requested.append(model_name)
        if self.load_error is not None:
            raise self.load_error
        return self.embedder


def _row(model: str = _COLLECTION_MODEL) -> CollectionMeta:
    return CollectionMeta(name=_COLLECTION, description="old text", active_embedding_model=model)


def _writer(
    store: _RecordingStore, cache: CollectionEmbedders, embed_wait_seconds: float = EMBED_WAIT_SECONDS
) -> DescriptionWriter:
    return DescriptionWriter(
        store, cache, default_embedding_model=_DEFAULT_MODEL, embed_wait_seconds=embed_wait_seconds
    )


def _only_write(store: _RecordingStore) -> tuple[DescriptionFields, WriteGuard]:
    assert len(store.writes) == 1
    collection, namespace, fields, guard = store.writes[0]
    assert (collection, namespace) == (_COLLECTION, DEFAULT_NAMESPACE)
    return fields, guard


async def test_generated_write_sets_every_field_with_the_collections_vector_under_the_expected_guard() -> None:
    store, embedder = _RecordingStore(_row()), _Embedder()
    cache = _Cache(embedder)
    before = datetime.now(UTC)

    result = await _writer(store, cache).apply(
        _COLLECTION,
        DEFAULT_NAMESPACE,
        GeneratedChange(text=_TEXT, doc_count_at_generation=_DOC_COUNT, expected=_EXPECTED),
    )

    assert result == WriteResult(DescriptionWriteOutcome.WRITTEN, VectorOutcome.REFRESHED)
    assert cache.requested == [_COLLECTION_MODEL], "the vector comes from the collection's own embedder"
    assert embedder.texts == [_TEXT]
    fields, guard = _only_write(store)
    assert guard == _EXPECTED
    assert (fields.description.action, fields.description.value) == (FieldOp.SET, _TEXT)
    assert fields.source.value == "generated"
    assert fields.status.value == "ok"
    assert fields.described_at_doc_count.value == _DOC_COUNT
    assert fields.description_embedding.value == _EMBEDDING
    stamp = fields.last_described.value
    assert stamp is not None and before <= stamp <= datetime.now(UTC)


async def test_manual_set_is_unconditional_leaves_the_baseline_and_sets_manual_ok() -> None:
    store, cache = _RecordingStore(_row()), _Cache(_Embedder())

    result = await _writer(store, cache).apply(_COLLECTION, DEFAULT_NAMESPACE, ManualSetChange(text=_TEXT))

    assert result == WriteResult(DescriptionWriteOutcome.WRITTEN, VectorOutcome.REFRESHED)
    fields, guard = _only_write(store)
    assert guard == UNCONDITIONAL
    assert fields.description.value == _TEXT
    assert fields.source.value == "manual"
    assert fields.status.value == "ok"
    assert fields.described_at_doc_count.action is FieldOp.KEEP
    assert fields.last_described.value is not None
    assert fields.description_embedding.value == _EMBEDDING


@pytest.mark.parametrize("source", ["generated", "manual"])
async def test_import_writes_the_given_source_status_ok_and_an_unset_baseline(source: str) -> None:
    store, cache = _RecordingStore(_row()), _Cache(_Embedder())

    result = await _writer(store, cache).apply(
        _COLLECTION, DEFAULT_NAMESPACE, ImportedChange(text=_TEXT, source=source)  # type: ignore[arg-type]
    )

    assert result == WriteResult(DescriptionWriteOutcome.WRITTEN, VectorOutcome.REFRESHED)
    fields, guard = _only_write(store)
    assert guard == UNCONDITIONAL
    assert fields.description.value == _TEXT
    assert fields.source.value == source
    assert fields.status.value == "ok"
    assert fields.described_at_doc_count.action is FieldOp.CLEAR
    assert fields.description_embedding.value == _EMBEDDING


async def test_clear_resets_every_field_and_removes_the_vector_without_embedding() -> None:
    embedder = _Embedder()
    store, cache = _RecordingStore(_row()), _Cache(embedder)

    result = await _writer(store, cache).apply(_COLLECTION, DEFAULT_NAMESPACE, ClearChange())

    assert result == WriteResult(DescriptionWriteOutcome.WRITTEN, VectorOutcome.REMOVED)
    fields, guard = _only_write(store)
    assert guard == UNCONDITIONAL
    assert {change.action for change in (
        fields.description, fields.source, fields.status, fields.last_described,
        fields.described_at_doc_count, fields.description_embedding,
    )} == {FieldOp.CLEAR}
    assert cache.requested == [] and embedder.texts == [], "a clear never touches the embedder"


async def test_a_collection_without_a_pinned_model_embeds_with_the_default_model() -> None:
    store, cache = _RecordingStore(_row(model="")), _Cache(_Embedder())

    await _writer(store, cache).apply(_COLLECTION, DEFAULT_NAMESPACE, ManualSetChange(text=_TEXT))

    assert cache.requested == [_DEFAULT_MODEL]


class _HangingCache:
    """A cache whose load never finishes, as a loader stuck on a slow disk would be."""

    async def get_or_load(self, model_name: str) -> _Embedder:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


_FAILING_CACHES: dict[str, Callable[[], tuple[CollectionEmbedders, float]]] = {
    "embed_raises": lambda: (_Cache(_Embedder(error=RuntimeError(f"cannot embed {_TEXT}"))), EMBED_WAIT_SECONDS),
    "load_raises": lambda: (_Cache(_Embedder(), load_error=RuntimeError("weights missing")), EMBED_WAIT_SECONDS),
    "load_not_ready": lambda: (
        _Cache(_Embedder(), load_error=EmbedderNotReadyError("still loading")),
        EMBED_WAIT_SECONDS,
    ),
    "embed_hangs": lambda: (_Cache(_Embedder(hang=True)), _SHORT_WAIT_S),
    "load_hangs": lambda: (_HangingCache(), _SHORT_WAIT_S),
    "bad_vector": lambda: (_Cache(_Embedder(vector=[math.nan, 1.0])), EMBED_WAIT_SECONDS),
}

_FAILURE_REASONS = {
    "embed_raises": "RuntimeError",
    "load_raises": "RuntimeError",
    "load_not_ready": "EmbedderNotReadyError",
    "embed_hangs": "TimeoutError",
    "load_hangs": "TimeoutError",
    "bad_vector": "unusable vector",
}


@pytest.mark.parametrize("failure", list(_FAILING_CACHES))
async def test_embedding_failure_or_timeout_keeps_description_without_vector(
    failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    """S69: the text is stored, no vector is written, and exactly one WARNING (without the text) says so. The wait
    wraps the embedder load as well as the first embedding: the cache bounds only a caller that joins another load,
    never the loader itself."""
    cache, wait = _FAILING_CACHES[failure]()
    store = _RecordingStore(_row())

    with caplog.at_level(logging.DEBUG, logger=_WRITER_LOGGER):
        result = await _writer(store, cache, wait).apply(
            _COLLECTION,
            DEFAULT_NAMESPACE,
            GeneratedChange(text=_TEXT, doc_count_at_generation=_DOC_COUNT, expected=_EXPECTED),
        )

    assert result == WriteResult(DescriptionWriteOutcome.WRITTEN, VectorOutcome.KEPT_WITHOUT_VECTOR)
    fields, _guard = _only_write(store)
    assert (fields.description.action, fields.description.value) == (FieldOp.SET, _TEXT)
    assert fields.source.value == "generated" and fields.status.value == "ok"
    assert fields.description_embedding.action is FieldOp.CLEAR, "the old text's vector must not outlive the old text"
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1, [record.getMessage() for record in caplog.records]
    assert _COLLECTION in warnings[0].getMessage()
    assert _FAILURE_REASONS[failure] in warnings[0].getMessage()
    assert all(_TEXT not in record.getMessage() for record in caplog.records)
    assert warnings[0].exc_info is None, "a traceback could echo the text"


async def test_cancellation_during_the_embedding_propagates_and_writes_nothing() -> None:
    """A cancelled caller (shutdown) is not an embedding failure: nothing is swallowed, nothing is written."""
    store = _RecordingStore(_row())
    task = asyncio.create_task(_writer(store, _Cache(_Embedder(hang=True))).apply(
        _COLLECTION, DEFAULT_NAMESPACE, ManualSetChange(text=_TEXT)
    ))
    await asyncio.sleep(_SHORT_WAIT_S)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.writes == []


async def test_a_store_error_from_the_guarded_write_propagates() -> None:
    """A LanceDB or I/O error is the caller's to record as a failed description; it is never turned into an outcome."""

    class _FailingStore(_RecordingStore):
        async def write_description(
            self, collection: str, namespace: str, fields: DescriptionFields, guard: WriteGuard
        ) -> DescriptionWriteOutcome:
            raise OSError("table unreadable")

    with pytest.raises(OSError, match="table unreadable"):
        await _writer(_FailingStore(_row()), _Cache(_Embedder())).apply(
            _COLLECTION, DEFAULT_NAMESPACE, ManualSetChange(text=_TEXT)
        )


@pytest.mark.parametrize(
    "outcome",
    [
        DescriptionWriteOutcome.LOCK_TIMEOUT,
        DescriptionWriteOutcome.COLLECTION_MISSING,
        DescriptionWriteOutcome.REFUSED_MANUAL,
        DescriptionWriteOutcome.REFUSED_STALE,
    ],
)
async def test_a_refused_or_failed_write_reports_the_outcome_and_an_untouched_vector(
    outcome: DescriptionWriteOutcome,
) -> None:
    store, cache = _RecordingStore(_row(), outcome), _Cache(_Embedder())

    result = await _writer(store, cache).apply(
        _COLLECTION,
        DEFAULT_NAMESPACE,
        GeneratedChange(text=_TEXT, doc_count_at_generation=_DOC_COUNT, expected=_EXPECTED),
    )

    assert result == WriteResult(outcome, VectorOutcome.UNTOUCHED)


async def test_a_missing_collection_is_reported_without_embedding_or_writing() -> None:
    embedder = _Embedder()
    store, cache = _RecordingStore(None), _Cache(embedder)

    result = await _writer(store, cache).apply(_COLLECTION, DEFAULT_NAMESPACE, ManualSetChange(text=_TEXT))

    assert result == WriteResult(DescriptionWriteOutcome.COLLECTION_MISSING, VectorOutcome.UNTOUCHED)
    assert embedder.texts == [] and store.writes == []


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
async def test_blank_text_is_refused_before_anything_is_embedded_or_written(text: str) -> None:
    embedder = _Embedder()
    store, cache = _RecordingStore(_row()), _Cache(embedder)

    with pytest.raises(ValueError, match="blank"):
        await _writer(store, cache).apply(_COLLECTION, DEFAULT_NAMESPACE, ManualSetChange(text=text))

    assert embedder.texts == [] and store.writes == [] and store.events == []


async def test_the_embedding_is_computed_before_the_store_write() -> None:
    store = _RecordingStore(_row())
    order: list[str] = []

    class _OrderedEmbedder(_Embedder):
        async def embed_one(self, text: str) -> list[float]:
            order.append(f"embed after {store.events[-1] if store.events else 'nothing'}")
            return await super().embed_one(text)

    await _writer(store, _Cache(_OrderedEmbedder())).apply(_COLLECTION, DEFAULT_NAMESPACE, ManualSetChange(text=_TEXT))

    assert order == ["embed after read"]
    assert store.events == ["read", "write"]


async def test_record_failure_sets_status_failed_under_the_expected_guard_and_keeps_everything_else() -> None:
    store, cache = _RecordingStore(_row(), DescriptionWriteOutcome.REFUSED_STALE), _Cache(_Embedder())

    outcome = await _writer(store, cache).record_failure(_COLLECTION, DEFAULT_NAMESPACE, _EXPECTED)

    assert outcome is DescriptionWriteOutcome.REFUSED_STALE
    fields, guard = _only_write(store)
    assert guard == _EXPECTED
    assert (fields.status.action, fields.status.value) == (FieldOp.SET, "failed")
    assert {change.action for change in (
        fields.description, fields.source, fields.last_described,
        fields.described_at_doc_count, fields.description_embedding,
    )} == {FieldOp.KEEP}
    assert cache.requested == []

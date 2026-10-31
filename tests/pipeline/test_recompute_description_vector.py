"""BE-20 (C11, S88): the metadata recompute carries the description state and the routing vector read under the lock.

Only the shared write operation writes the description vector. The recompute computes one itself only for a legacy
description that has none, and only when the embedder it was passed has the collection's model.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.pipeline import SearchPipeline
from tests._collection_lock_stub import with_locked_collection_meta

from .conftest import MockEmbedderBackend, make_pipeline

_COLLECTION = "docs"
_GLOBAL_MODEL = MockEmbedderBackend.model_name
_OTHER_MODEL = "model-of-the-collection"
_TEXT = "A description"
_STORED_VECTOR = [9.0, 9.0, 9.0, 9.0]
_COMPUTED_VECTOR = [0.7, 0.7, 0.7, 0.7]
_VECTORS = [[0.1, 0.2, 0.3, 0.4], [0.3, 0.4, 0.5, 0.6]]
_STAMP = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


def _row(model: str = _GLOBAL_MODEL, **changes: Any) -> CollectionMeta:
    fields: dict[str, Any] = {"needs_recompute": True, **changes}
    return CollectionMeta(name=_COLLECTION, active_embedding_model=model, **fields)


def _setup(
    *rows: CollectionMeta | None, vectors: list[list[float]] | None = None
) -> tuple[MagicMock, SearchPipeline]:
    """A pipeline over a mock store whose successive ``get_collection_meta`` reads serve *rows*: the first is the
    read before the scan, the second the one under the collection lock."""
    store = MagicMock()
    store.get_collection_meta = AsyncMock(side_effect=list(rows))
    store.get_all_vectors = AsyncMock(return_value=_VECTORS if vectors is None else vectors)
    store.count_documents = AsyncMock(return_value=2)
    store.update_collection_meta = AsyncMock()
    with_locked_collection_meta(store)
    return store, make_pipeline(store)


async def _recompute(pipeline: SearchPipeline, embedder_model: str = _GLOBAL_MODEL, **kwargs: object) -> AsyncMock:
    """Run the recompute with a stand-in embedder named *embedder_model*; returns its ``embed_one`` mock."""
    embedder = MagicMock()
    embedder.model_name = embedder_model
    embedder.embed_one = AsyncMock(return_value=list(_COMPUTED_VECTOR))
    await pipeline.recompute_collection_meta(_COLLECTION, embedder, **kwargs)  # type: ignore[arg-type]
    return embedder.embed_one


def _saved(store: MagicMock) -> CollectionMeta:
    store.update_collection_meta.assert_awaited_once()
    saved: CollectionMeta = store.update_collection_meta.call_args[0][0]
    return saved


@pytest.mark.parametrize(
    "embedder_model",
    [_OTHER_MODEL, _GLOBAL_MODEL],
    ids=["embedder_has_the_collections_model", "embedder_has_another_model"],
)
async def test_recompute_carries_stored_vector_for_other_model(embedder_model: str) -> None:
    """A vector the shared write computed with the collection's model is carried unchanged: the recompute never
    replaces an existing vector, least of all with one from a different model (S88)."""
    row = _row(_OTHER_MODEL, description=_TEXT, description_embedding=list(_STORED_VECTOR))
    store, pipeline = _setup(row, row)

    embed_one = await _recompute(pipeline, embedder_model=embedder_model)

    embed_one.assert_not_awaited()
    assert _saved(store).description_embedding == _STORED_VECTOR


@pytest.mark.parametrize(
    ("row_model", "embedder_model", "computed"),
    [
        pytest.param(_OTHER_MODEL, _OTHER_MODEL, True, id="matching_model"),
        pytest.param(_OTHER_MODEL, _GLOBAL_MODEL, False, id="differing_model"),
        pytest.param("", _GLOBAL_MODEL, True, id="unpinned_collection_uses_the_default_model"),
        pytest.param("", _OTHER_MODEL, False, id="unpinned_collection_differs_from_the_default_model"),
    ],
)
async def test_recompute_computes_vector_only_for_matching_model(
    row_model: str, embedder_model: str, computed: bool
) -> None:
    """A legacy description (no vector yet) gets one only when the passed embedder has the collection's model."""
    row = _row(row_model, description=_TEXT)
    store, pipeline = _setup(row, row)

    embed_one = await _recompute(pipeline, embedder_model=embedder_model)

    saved = _saved(store)
    if computed:
        embed_one.assert_awaited_once_with(_TEXT)
        assert saved.description_embedding == _COMPUTED_VECTOR
    else:
        embed_one.assert_not_awaited()
        assert saved.description_embedding is None


async def test_recompute_computes_no_vector_for_a_collection_without_a_description() -> None:
    row = _row()
    store, pipeline = _setup(row, row)

    embed_one = await _recompute(pipeline)

    embed_one.assert_not_awaited()
    assert _saved(store).description_embedding is None


async def test_recompute_carries_the_description_state_read_inside_the_lock() -> None:
    """A manual set landed during the scan: the row under the lock, not the one read before the scan, is written
    back, so the description, its source, status, last-described and vector all survive (C3)."""
    before_scan = _row(description="generated before", description_source="generated", description_status="ok")
    under_lock = _row(
        description="set by the operator meanwhile",
        description_source="manual",
        description_status="ok",
        last_described=_STAMP,
        described_at_doc_count=7,
        description_embedding=list(_STORED_VECTOR),
    )
    store, pipeline = _setup(before_scan, under_lock)

    await _recompute(pipeline)

    saved = _saved(store)
    assert saved.description == "set by the operator meanwhile"
    assert saved.description_source == "manual"
    assert saved.description_status == "ok"
    assert saved.last_described == _STAMP
    assert saved.described_at_doc_count == 7
    assert saved.description_embedding == _STORED_VECTOR
    assert saved.chunk_count == len(_VECTORS)
    assert saved.needs_recompute is False


async def test_recompute_drops_a_vector_computed_for_text_that_changed_meanwhile() -> None:
    """The legacy vector was computed from the text read before the scan. The text under the lock differs, so the
    vector would not belong to it: it is dropped and left to the shared write (C3)."""
    store, pipeline = _setup(_row(description="old text"), _row(description="new text"))

    embed_one = await _recompute(pipeline)

    embed_one.assert_awaited_once_with("old text")
    saved = _saved(store)
    assert saved.description == "new text"
    assert saved.description_embedding is None


async def test_recompute_of_an_empty_collection_carries_the_stored_vector() -> None:
    row = _row(description=_TEXT, description_embedding=list(_STORED_VECTOR))
    store, pipeline = _setup(row, row, vectors=[])

    embed_one = await _recompute(pipeline, force=True)

    embed_one.assert_not_awaited()
    saved = _saved(store)
    assert saved.centroid is None
    assert saved.description_embedding == _STORED_VECTOR


async def test_recompute_does_not_resurrect_a_collection_dropped_during_the_scan() -> None:
    """The row existed before the scan and is gone under the lock: a write would re-insert the dropped collection."""
    store, pipeline = _setup(_row(description=_TEXT), None)

    await _recompute(pipeline)

    store.update_collection_meta.assert_not_awaited()


async def test_recompute_writes_inside_the_collection_lock() -> None:
    store, pipeline = _setup(_row(), _row())
    lock = store.lock_for.return_value
    held: list[bool] = []

    async def _record(_meta: CollectionMeta, **_kwargs: object) -> None:
        held.append(lock.locked())

    store.update_collection_meta = AsyncMock(side_effect=_record)

    await _recompute(pipeline)

    assert held == [True]
    assert store.update_collection_meta.call_args.kwargs == {"_locked_by_caller": True}


async def test_recompute_of_an_empty_collection_still_repairs_a_legacy_description_of_the_matching_model() -> None:
    row = _row(description=_TEXT)
    store, pipeline = _setup(row, row, vectors=[])

    embed_one = await _recompute(pipeline, force=True)

    embed_one.assert_awaited_once_with(_TEXT)
    assert _saved(store).description_embedding == _COMPUTED_VECTOR


async def test_recompute_drops_a_legacy_vector_when_the_model_changed_during_the_scan() -> None:
    """A reindex promoted the collection to another model while the scan ran: the vector was computed with the
    old one and would route with the wrong model."""
    store, pipeline = _setup(_row(description=_TEXT), _row(_OTHER_MODEL, description=_TEXT))

    embed_one = await _recompute(pipeline)

    embed_one.assert_awaited_once_with(_TEXT)
    assert _saved(store).description_embedding is None


@pytest.mark.parametrize(
    ("embed_outcome", "reason"),
    [
        pytest.param({"side_effect": RuntimeError("model unavailable")}, "RuntimeError", id="embedding_raises"),
        pytest.param({"side_effect": TimeoutError()}, "TimeoutError", id="embedding_times_out"),
        pytest.param({"return_value": [float("nan")]}, "unusable vector", id="unusable_vector"),
    ],
)
async def test_a_failed_legacy_embedding_leaves_the_description_without_a_vector_and_the_centroid_written(
    embed_outcome: dict[str, Any], reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The repair is optional: it never blocks the centroid, and its warning names no description text."""
    row = _row(description=_TEXT)
    store, pipeline = _setup(row, row)
    embedder = MagicMock()
    embedder.model_name = _GLOBAL_MODEL
    embedder.embed_one = AsyncMock(**embed_outcome)

    with caplog.at_level("WARNING", logger="archon_search.pipeline"):
        await pipeline.recompute_collection_meta(_COLLECTION, embedder)

    saved = _saved(store)
    assert saved.description_embedding is None
    assert saved.chunk_count == len(_VECTORS) and saved.needs_recompute is False
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1 and reason in messages[0] and _COLLECTION in messages[0]
    assert _TEXT not in messages[0]


@pytest.mark.parametrize(
    "under_lock",
    [
        pytest.param({"mutations_since_recompute": 4}, id="counter_moved"),
        pytest.param({"mutations_since_recompute": 3, "needs_recompute": True}, id="flag_raised"),
    ],
)
async def test_a_batch_that_landed_during_the_scan_keeps_the_rows_own_totals(under_lock: dict[str, Any]) -> None:
    """The scanned totals may predate the batch: the row keeps the totals it maintained incrementally, its counter
    and its flag, so the next trigger recomputes again, and nothing is written when there is no repair to apply."""
    before_scan = _row(mutations_since_recompute=3, needs_recompute=False)
    store, pipeline = _setup(before_scan, _row(chunk_count=9, **under_lock))

    await _recompute(pipeline, force=True)

    store.update_collection_meta.assert_not_awaited()


async def test_a_batch_that_landed_during_the_scan_still_gets_the_legacy_vector_repair() -> None:
    before_scan = _row(description=_TEXT, mutations_since_recompute=3)
    under_lock = _row(description=_TEXT, mutations_since_recompute=4, chunk_count=9)
    store, pipeline = _setup(before_scan, under_lock)

    await _recompute(pipeline)

    saved = _saved(store)
    assert saved.description_embedding == _COMPUTED_VECTOR
    assert (saved.mutations_since_recompute, saved.chunk_count) == (4, 9)


async def test_a_scan_that_missed_nothing_marks_the_row_fresh() -> None:
    row = _row(mutations_since_recompute=3)
    store, pipeline = _setup(row, row)

    await _recompute(pipeline)

    saved = _saved(store)
    assert saved.needs_recompute is False
    assert saved.mutations_since_recompute == 0

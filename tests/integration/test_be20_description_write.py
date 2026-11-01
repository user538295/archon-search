"""BE-20 (C11, S43, S69, S88): the shared write-description operation and the metadata recompute against a real
LanceDB store.

The operation embeds with the collection's own embedder (from the cache, outside the collection lock) and the store
writes the text and its routing vector in one guarded step; the recompute then carries that vector.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.description_write import (
    UNCONDITIONAL,
    DescriptionFields,
    ExpectedState,
    FieldChange,
)
from archon_search.description_writer import (
    ClearChange,
    DescriptionWriter,
    GeneratedChange,
    ImportedChange,
    ManualSetChange,
    VectorOutcome,
)
from archon_search.store import SearchStore
from tests.integration.conftest import make_real_pipeline

pytestmark = pytest.mark.integration

_NAME = "docs"
_NAMESPACE = "default"
_COLLECTION_MODEL = "model-a"
_DEFAULT_MODEL = "model-default"
_GLOBAL_MODEL = "mock-embedder"  # the model of ``make_real_pipeline``'s embedder
_GLOBAL_VECTOR = [0.1, 0.2, 0.3, 0.4]  # what its backend returns for every text


def _vector_of(text: str, model: str) -> list[float]:
    """A vector that differs by text and by model, so a replaced or foreign vector is visible."""
    return [float(len(text)), float(len(model)), 1.0]


class _ModelEmbedder:
    def __init__(self, model: str, store: SearchStore | None = None) -> None:
        self.model = model
        self.lock_held_while_embedding: list[bool] = []
        self._store = store

    async def embed_one(self, text: str) -> list[float]:
        if self._store is not None:
            self.lock_held_while_embedding.append(self._store.lock_for(_NAME).locked())
        return _vector_of(text, self.model)


class _Cache:
    def __init__(self, store: SearchStore | None = None) -> None:
        self.requested: list[str] = []
        self.embedders: dict[str, _ModelEmbedder] = {}
        self._store = store

    async def get_or_load(self, model_name: str) -> _ModelEmbedder:
        self.requested.append(model_name)
        return self.embedders.setdefault(model_name, _ModelEmbedder(model_name, self._store))


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SearchStore]:
    search_store = SearchStore(tmp_path / "db")
    await search_store.connect()
    await search_store.update_collection_meta(
        CollectionMeta(name=_NAME, doc_count=2, active_embedding_model=_COLLECTION_MODEL)
    )
    yield search_store
    await search_store.disconnect()


async def _stored(store: SearchStore) -> CollectionMeta:
    meta = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert meta is not None
    return meta


async def test_vector_follows_generate_set_and_clear(store: SearchStore) -> None:
    """S43/S69: the routing vector is written with a generated text, replaced by a manual set and an import, and
    removed by a clear; every vector comes from the collection's own model, never the default one."""
    cache = _Cache()
    writer = DescriptionWriter(store, cache, default_embedding_model=_DEFAULT_MODEL)

    generated = await writer.apply(
        _NAME,
        _NAMESPACE,
        GeneratedChange(text="generated text", doc_count_at_generation=2, expected=_expected(await _stored(store))),
    )
    row = await _stored(store)
    assert generated.vector is VectorOutcome.REFRESHED
    assert (row.description, row.description_source, row.description_status) == ("generated text", "generated", "ok")
    assert row.description_embedding == _vector_of("generated text", _COLLECTION_MODEL)

    manual = await writer.apply(_NAME, _NAMESPACE, ManualSetChange(text="the operator's own words"))
    row = await _stored(store)
    assert manual.vector is VectorOutcome.REFRESHED
    assert (row.description, row.description_source) == ("the operator's own words", "manual")
    assert row.description_embedding == _vector_of("the operator's own words", _COLLECTION_MODEL)

    imported = await writer.apply(_NAME, _NAMESPACE, ImportedChange(text="restored from a backup", source="generated"))
    row = await _stored(store)
    assert imported.vector is VectorOutcome.REFRESHED
    assert (row.description, row.description_source) == ("restored from a backup", "generated")
    assert row.description_embedding == _vector_of("restored from a backup", _COLLECTION_MODEL)
    assert row.described_at_doc_count is None

    cleared = await writer.apply(_NAME, _NAMESPACE, ClearChange())
    row = await _stored(store)
    assert cleared.vector is VectorOutcome.REMOVED
    assert row.description is None and row.description_embedding is None
    assert row.description_source is None and row.description_status is None

    assert set(cache.requested) == {_COLLECTION_MODEL}, "the collection's own embedder, never the default model's"


async def test_embedding_runs_outside_the_collection_lock(store: SearchStore) -> None:
    cache = _Cache(store)
    writer = DescriptionWriter(store, cache, default_embedding_model=_DEFAULT_MODEL)

    await writer.apply(_NAME, _NAMESPACE, ManualSetChange(text="slow to embed"))

    assert cache.embedders[_COLLECTION_MODEL].lock_held_while_embedding == [False]
    assert not store.lock_for(_NAME).locked()
    assert (await _stored(store)).description == "slow to embed"


async def test_a_text_without_a_vector_keeps_no_stale_vector(store: SearchStore) -> None:
    """S69: when the embedding fails the new text is stored and the previous text's vector is removed with it."""

    class _Failing:
        async def get_or_load(self, model_name: str) -> _ModelEmbedder:
            raise RuntimeError("model unavailable")

    await DescriptionWriter(store, _Cache(), default_embedding_model=_DEFAULT_MODEL).apply(
        _NAME, _NAMESPACE, ManualSetChange(text="first text")
    )
    assert (await _stored(store)).description_embedding is not None

    result = await DescriptionWriter(store, _Failing(), default_embedding_model=_DEFAULT_MODEL).apply(
        _NAME, _NAMESPACE, ManualSetChange(text="second text")
    )

    row = await _stored(store)
    assert result.vector is VectorOutcome.KEPT_WITHOUT_VECTOR
    assert row.description == "second text" and row.description_embedding is None


async def test_recompute_keeps_the_written_vector_whatever_model_the_global_embedder_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S88: a collection whose model differs from the pipeline's global one keeps the vector the shared write
    computed; the recompute (ingest, sync, import) never replaces it with the global embedder's."""
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    try:
        doc = tmp_path / "a.txt"
        doc.write_text("some document text to give the collection a vector", encoding="utf-8")
        assert (await pipeline.ingest_file(doc, _NAME, embedder=pipeline._global_embedder)).status == "ok"
        row = await _stored(store)
        await store.update_collection_meta(replace(row, active_embedding_model=_COLLECTION_MODEL))
        await DescriptionWriter(store, _Cache(), default_embedding_model=_GLOBAL_MODEL).apply(
            _NAME, _NAMESPACE, ManualSetChange(text="written by the shared write")
        )

        await pipeline.recompute_collection_meta(_NAME, pipeline._global_embedder, force=True)

        after = await _stored(store)
        assert after.description == "written by the shared write"
        assert after.description_embedding == _vector_of("written by the shared write", _COLLECTION_MODEL)
        assert after.description_embedding != _GLOBAL_VECTOR
        assert after.centroid is not None and after.chunk_count > 0
    finally:
        await store.disconnect()


@pytest.mark.parametrize(
    ("row_model", "gets_vector"), [(_GLOBAL_MODEL, True), (_COLLECTION_MODEL, False)], ids=["matching", "differing"]
)
async def test_recompute_gives_a_legacy_description_a_vector_only_for_the_matching_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row_model: str, gets_vector: bool
) -> None:
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    try:
        doc = tmp_path / "a.txt"
        doc.write_text("some document text to give the collection a vector", encoding="utf-8")
        assert (await pipeline.ingest_file(doc, _NAME, embedder=pipeline._global_embedder)).status == "ok"
        row = await _stored(store)
        # A legacy row: a description (as an older server wrote it) and no vector.
        await store.update_collection_meta(
            replace(row, active_embedding_model=row_model, description="legacy text", description_embedding=None)
        )

        await pipeline.recompute_collection_meta(_NAME, pipeline._global_embedder, force=True)

        after = await _stored(store)
        assert after.description == "legacy text"
        assert after.description_embedding == (_GLOBAL_VECTOR if gets_vector else None)
    finally:
        await store.disconnect()


async def test_recompute_does_not_clobber_a_description_written_during_its_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S74/C3: a manual set lands while the recompute scans the vectors. The recompute writes the row it reads under
    the lock, so the manual text, source, status and vector survive."""
    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    try:
        doc = tmp_path / "a.txt"
        doc.write_text("some document text to give the collection a vector", encoding="utf-8")
        assert (await pipeline.ingest_file(doc, _NAME, embedder=pipeline._global_embedder)).status == "ok"
        await store.update_collection_meta(replace(await _stored(store), description="before the scan"))
        manual = DescriptionFields(
            description=FieldChange.set_to("set during the scan"),
            source=FieldChange.set_to("manual"),
            status=FieldChange.set_to("ok"),
            last_described=FieldChange.keep(),
            described_at_doc_count=FieldChange.keep(),
            description_embedding=FieldChange.set_to([5.0, 5.0, 5.0, 5.0]),
        )
        real_scan = store.get_all_vectors

        async def scan_then_manual_set(collection: str) -> list[list[float]]:
            vectors = await real_scan(collection)
            await store.write_description(_NAME, _NAMESPACE, manual, UNCONDITIONAL)
            return vectors

        monkeypatch.setattr(store, "get_all_vectors", scan_then_manual_set)

        await pipeline.recompute_collection_meta(_NAME, pipeline._global_embedder, force=True)

        after = await _stored(store)
        assert after.description == "set during the scan"
        assert (after.description_source, after.description_status) == ("manual", "ok")
        assert after.description_embedding == [5.0, 5.0, 5.0, 5.0]
        assert after.centroid is not None
    finally:
        await store.disconnect()


def _expected(row: CollectionMeta) -> ExpectedState:
    return ExpectedState(
        description=row.description,
        source=row.description_source,
        last_described=row.last_described,
        status=row.description_status,
    )


async def test_touch_last_indexed_keeps_the_text_its_vector_and_the_recompute_flag(store: SearchStore) -> None:
    """The ingest's own row write stamps ``last_indexed`` and nothing else: the text's vector stays on its text, and
    no recompute is flagged because the description did not change."""
    await DescriptionWriter(store, _Cache(), default_embedding_model=_DEFAULT_MODEL).apply(
        _NAME, _NAMESPACE, ManualSetChange(text="same text")
    )
    before = await _stored(store)
    assert before.description_embedding is not None
    stamp = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)

    await store.touch_last_indexed(_NAME, stamp, namespace=_NAMESPACE)

    row = await _stored(store)
    assert row.last_indexed == stamp
    assert row.description == "same text" and row.description_embedding == before.description_embedding
    assert (row.description_source, row.description_status, row.needs_recompute) == (
        before.description_source,
        before.description_status,
        before.needs_recompute,
    )

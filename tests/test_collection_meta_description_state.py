"""BE-18: ``description_source`` / ``description_status`` on ``CollectionMeta`` and the metadata table (C3).

A writer that rebuilds the metadata row field by field and forgets a field silently resets it, so the first test
builds a row with EVERY field non-default and sends it through every writer path.
"""
from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.lance_session import list_all_table_names
from archon_search.pipeline import SearchPipeline
from archon_search.store import STORE_SCHEMA_VERSION, SearchStore

_NAME = "every-field"
_NAMESPACE = "ns-every-field"
_MODEL = "model-active"
_META_TABLE = "_archon_collection_meta"
_VECTOR_DIM = 2
RECOMPUTED_DOC_COUNT = 9
RECOMPUTED_EMBEDDING = [0.5, 0.5]


def _every_field_non_default() -> CollectionMeta:
    """A row whose every field differs from its dataclass default."""
    meta = CollectionMeta(
        name=_NAME,
        description="what the collection holds",
        centroid=[0.5, 1.0],
        centroid_sum=[1.0, 2.0],
        mutations_since_recompute=3,
        needs_recompute=True,
        doc_count=2,
        chunk_count=4,
        active_embedding_model=_MODEL,
        pending_embedding_model="model-pending",
        needs_reindex=True,
        reindex_job_id="reindex-job",
        community_rebuild_job_id="community-job",
        metadata_reindex_job_id="metadata-job",
        last_indexed=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        last_described=datetime(2026, 1, 3, 4, 5, 6, tzinfo=UTC),
        described_at_doc_count=2,
        namespace=_NAMESPACE,
        description_embedding=[0.25, 0.75],
        schema_version=7,
        default_ttl_seconds=3600,
        description_source="manual",
        description_status="failed",
    )
    for field in dataclasses.fields(CollectionMeta):
        if field.default is not dataclasses.MISSING:
            assert getattr(meta, field.name) != field.default, (
                f"{field.name} is still at its default: extend _every_field_non_default() for the new field"
            )
    return meta


def _assert_only_changed(before: CollectionMeta, after: CollectionMeta, changed: set[str]) -> None:
    """Every field outside *changed* must survive the writer."""
    for field in dataclasses.fields(CollectionMeta):
        if field.name in changed:
            continue
        assert getattr(after, field.name) == getattr(before, field.name), f"writer dropped {field.name}"


@pytest.fixture
async def store(tmp_path: Path):
    from archon_search.config import SearchConfig

    search_store = SearchStore(tmp_path / "db", config=SearchConfig())
    await search_store.connect()
    yield search_store
    await search_store.disconnect()


async def _seed(store: SearchStore, **overrides: Any) -> CollectionMeta:
    meta = dataclasses.replace(_every_field_non_default(), **overrides)
    await store.update_collection_meta(meta)
    read = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert read is not None
    _assert_only_changed(meta, read, set())
    return meta


# ---------------------------------------------------------------------------
# test_row_with_every_field_non_default_survives_every_writer
# ---------------------------------------------------------------------------


async def test_row_with_every_field_non_default_survives_every_writer_update_collection_meta(store) -> None:
    await _seed(store)


async def test_row_with_every_field_non_default_survives_every_writer_do_write_meta_unlocked(store) -> None:
    meta = _every_field_non_default()
    async with store.lock_for(meta.name):
        await store._do_write_meta_unlocked(store._require_connected(), meta.name, meta)
    read = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert read is not None
    _assert_only_changed(meta, read, set())


async def test_row_with_every_field_non_default_survives_every_writer_touch_last_indexed(store) -> None:
    meta = await _seed(store)
    stamp = datetime(2026, 6, 7, 8, 9, 10, tzinfo=UTC)
    await store.touch_last_indexed(_NAME, stamp, namespace=_NAMESPACE)
    read = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert read is not None
    assert read.last_indexed == stamp
    _assert_only_changed(meta, read, {"last_indexed"})


async def _update_meta_on_add(store: SearchStore, embedding_model: str, vectors: list[list[float]]) -> None:
    async with store.lock_for(_NAME):
        await store._do_update_meta_on_add(
            store._require_connected(), _NAME, vectors, 1, embedding_model, _VECTOR_DIM, namespace=_NAMESPACE
        )


async def test_row_with_every_field_non_default_survives_every_writer_meta_on_add(store) -> None:
    meta = await _seed(store)
    await _update_meta_on_add(store, _MODEL, [[1.0, 1.0]])
    read = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert read is not None
    _assert_only_changed(
        meta, read, {"centroid", "centroid_sum", "doc_count", "chunk_count", "mutations_since_recompute"}
    )


async def test_row_with_every_field_non_default_survives_every_writer_meta_on_add_stale_centroid(store) -> None:
    meta = await _seed(store, needs_recompute=False)
    await _update_meta_on_add(store, "another-model", [[1.0, 1.0]])
    read = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert read is not None
    assert read.needs_recompute is True
    _assert_only_changed(meta, read, {"centroid", "centroid_sum", "needs_recompute"})


async def test_row_with_every_field_non_default_survives_every_writer_meta_on_add_invalid_batch(store) -> None:
    meta = await _seed(store, needs_recompute=False)
    await _update_meta_on_add(store, _MODEL, [[float("nan"), 1.0]])
    read = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert read is not None
    assert read.needs_recompute is True
    _assert_only_changed(meta, read, {"needs_recompute"})


async def test_row_with_every_field_non_default_survives_every_writer_meta_on_delete(store) -> None:
    meta = await _seed(store)
    async with store.lock_for(_NAME):
        await store._do_subtract_meta_on_delete(
            store._require_connected(), _NAME, [[0.5, 0.5]], namespace=_NAMESPACE
        )
    read = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert read is not None
    _assert_only_changed(
        meta,
        read,
        {"centroid", "centroid_sum", "doc_count", "chunk_count", "mutations_since_recompute", "last_indexed"},
    )


async def test_row_with_every_field_non_default_survives_every_writer_meta_on_delete_stale_centroid(store) -> None:
    meta = await _seed(store, centroid_sum=None, needs_recompute=False)
    async with store.lock_for(_NAME):
        await store._do_subtract_meta_on_delete(
            store._require_connected(), _NAME, [[0.5, 0.5]], namespace=_NAMESPACE
        )
    read = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert read is not None
    assert read.needs_recompute is True
    _assert_only_changed(
        meta, read, {"centroid", "centroid_sum", "mutations_since_recompute", "last_indexed", "needs_recompute"}
    )


class _RecordingStore:
    """The store calls ``recompute_collection_meta`` makes, over one in-memory row (or none)."""

    def __init__(self, existing: CollectionMeta | None, vectors: list[list[float]]) -> None:
        self.existing = existing
        self.vectors = vectors
        self.written: CollectionMeta | None = None

    async def get_collection_meta(self, name: str, namespace: str = DEFAULT_NAMESPACE) -> CollectionMeta | None:
        return self.existing

    @contextlib.asynccontextmanager
    async def locked_collection_meta(
        self, name: str, namespace: str = DEFAULT_NAMESPACE
    ) -> AsyncIterator[CollectionMeta | None]:
        yield self.existing

    async def get_all_vectors(self, collection: str) -> list[list[float]]:
        return self.vectors

    async def count_documents(self, collection: str) -> int:
        return RECOMPUTED_DOC_COUNT

    async def update_collection_meta(self, meta: CollectionMeta, **_: Any) -> None:
        self.written = meta


class _StubEmbedder:
    model_name = _MODEL

    async def embed_one(self, text: str) -> list[float]:
        return RECOMPUTED_EMBEDDING


@pytest.mark.parametrize("vectors", [[], [[1.0, 2.0], [3.0, 4.0]]], ids=["no-vectors", "with-vectors"])
async def test_row_with_every_field_non_default_survives_every_writer_pipeline_recompute(vectors) -> None:
    meta = _every_field_non_default()
    fake = _RecordingStore(meta, vectors)
    pipeline = SearchPipeline.__new__(SearchPipeline)
    pipeline.store = fake  # type: ignore[assignment]

    await pipeline.recompute_collection_meta(_NAME, _StubEmbedder(), namespace=_NAMESPACE, force=True)  # type: ignore[arg-type]

    assert fake.written is not None
    assert fake.written.doc_count == RECOMPUTED_DOC_COUNT
    assert fake.written.needs_recompute is False
    assert fake.written.description_embedding == meta.description_embedding, "the stored vector is carried"
    _assert_only_changed(
        meta,
        fake.written,
        {
            "centroid",
            "centroid_sum",
            "doc_count",
            "chunk_count",
            "last_indexed",
            "mutations_since_recompute",
            "needs_recompute",
        },
    )


async def test_recompute_of_a_collection_without_a_row_starts_from_the_defaults() -> None:
    fake = _RecordingStore(None, [[1.0, 2.0]])
    pipeline = SearchPipeline.__new__(SearchPipeline)
    pipeline.store = fake  # type: ignore[assignment]

    await pipeline.recompute_collection_meta(_NAME, _StubEmbedder(), namespace=_NAMESPACE, force=True)  # type: ignore[arg-type]

    written = fake.written
    assert written is not None
    assert (written.name, written.namespace, written.active_embedding_model) == (_NAME, _NAMESPACE, _MODEL)
    assert written.schema_version == STORE_SCHEMA_VERSION
    assert (written.description, written.description_source, written.description_status) == (None, None, None)
    assert written.default_ttl_seconds is None


# ---------------------------------------------------------------------------
# test_legacy_row_reads_generated_ok_and_empty_reads_none
# ---------------------------------------------------------------------------


def _legacy_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": "legacy",
        "description": "",
        "centroid_json": "",
        "description_embedding_json": "",
        "doc_count": 0,
        "chunk_count": 0,
        "active_embedding_model": "m",
        "last_indexed": "",
        "last_described": "",
        "described_at_doc_count": -1,
        "namespace": DEFAULT_NAMESPACE,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("extra", [{}, {"description_source": None, "description_status": None},
                                   {"description_source": "", "description_status": ""}],
                         ids=["columns-absent", "columns-null", "columns-empty"])
def test_legacy_row_reads_generated_ok_and_empty_reads_none(extra: dict[str, Any]) -> None:
    with_description = SearchStore._row_to_meta(_legacy_row(description="docs about cats", **extra))
    assert (with_description.description_source, with_description.description_status) == ("generated", "ok")

    empty = SearchStore._row_to_meta(_legacy_row(**extra))
    assert (empty.description, empty.description_source, empty.description_status) == (None, None, None)


def test_stored_source_and_status_are_read_as_stored() -> None:
    manual = SearchStore._row_to_meta(
        _legacy_row(description="by hand", description_source="manual", description_status="ok")
    )
    assert (manual.description_source, manual.description_status) == ("manual", "ok")

    failed_after_generated = SearchStore._row_to_meta(
        _legacy_row(description="old text", description_source="generated", description_status="failed")
    )
    assert (failed_after_generated.description_source, failed_after_generated.description_status) == (
        "generated",
        "failed",
    )


def test_failed_attempt_on_a_collection_without_description_reads_failed_and_no_source() -> None:
    meta = SearchStore._row_to_meta(_legacy_row(description_status="failed"))
    assert (meta.description, meta.description_source, meta.description_status) == (None, None, "failed")


@pytest.mark.parametrize("field", ["description_status", "description_source"])
async def test_values_outside_the_contract_are_refused_by_every_writer(store, field: str) -> None:
    """``pending`` is memory only (C3): the store refuses to write it, or any other unknown value."""
    bad = dataclasses.replace(_every_field_non_default(), **{field: "pending"})
    with pytest.raises(ValueError, match=field):
        await store.update_collection_meta(bad)
    with pytest.raises(ValueError, match=field):
        async with store.lock_for(bad.name):
            await store._do_write_meta_unlocked(store._require_connected(), bad.name, bad)


# ---------------------------------------------------------------------------
# test_older_writer_dict_without_new_keys_leaves_stored_values
# ---------------------------------------------------------------------------


async def test_older_writer_dict_without_new_keys_leaves_stored_values(store) -> None:
    """An older server's merge carries no ``description_source``/``description_status`` keys (rollback, S78)."""
    meta = await _seed(store)
    table = await store._require_connected().open_table(_META_TABLE)
    row = (await table.query().to_list())[0]
    older_dict = {k: v for k, v in row.items() if k not in ("description_source", "description_status")}
    older_dict["doc_count"] = 99

    await table.merge_insert(["name", "namespace"]).when_matched_update_all().when_not_matched_insert_all().execute(
        [older_dict]
    )

    read = await store.get_collection_meta(_NAME, _NAMESPACE)
    assert read is not None
    assert read.doc_count == 99
    assert (read.description_source, read.description_status) == (meta.description_source, meta.description_status)


# ---------------------------------------------------------------------------
# _migrate_description_columns
# ---------------------------------------------------------------------------


async def test_migrate_description_columns_adds_both_nullable_columns_idempotently(tmp_path: Path) -> None:
    import pyarrow as pa

    search_store = SearchStore(tmp_path / "db")
    await search_store.connect()
    try:
        db = search_store._require_connected()
        old_schema = pa.schema(
            [f for f in SearchStore._meta_schema() if f.name not in ("description_source", "description_status")]
        )
        await db.create_table(_META_TABLE, schema=old_schema)

        await search_store._migrate_description_columns()
        await search_store._migrate_description_columns()

        schema = await (await db.open_table(_META_TABLE)).schema()
        for column in ("description_source", "description_status"):
            assert schema.field(column).type == pa.utf8()
            assert schema.field(column).nullable
    finally:
        await search_store.disconnect()


async def test_migrate_description_columns_without_meta_table_is_a_no_op(tmp_path: Path) -> None:
    search_store = SearchStore(tmp_path / "db")
    await search_store.connect()
    try:
        await search_store._migrate_description_columns()
        assert _META_TABLE not in await list_all_table_names(search_store._require_connected())
    finally:
        await search_store.disconnect()


async def test_migrate_description_columns_adds_only_the_missing_column(tmp_path: Path) -> None:
    import pyarrow as pa

    search_store = SearchStore(tmp_path / "db")
    await search_store.connect()
    try:
        db = search_store._require_connected()
        old_schema = pa.schema([f for f in SearchStore._meta_schema() if f.name != "description_status"])
        table = await db.create_table(_META_TABLE, schema=old_schema)
        assert "description_source" in (await table.schema()).names

        await search_store._migrate_description_columns()

        names = (await (await db.open_table(_META_TABLE)).schema()).names
        assert names.count("description_source") == 1
        assert "description_status" in names
    finally:
        await search_store.disconnect()


async def test_migrate_description_columns_tolerates_a_concurrent_add(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, patch

    import lancedb.table
    import pyarrow as pa

    search_store = SearchStore(tmp_path / "db")
    await search_store.connect()
    try:
        db = search_store._require_connected()
        old_schema = pa.schema(
            [f for f in SearchStore._meta_schema() if f.name not in ("description_source", "description_status")]
        )
        await db.create_table(_META_TABLE, schema=old_schema)
        racing_add = AsyncMock(side_effect=RuntimeError("Column description_source already exists in the dataset"))
        with patch.object(lancedb.table.AsyncTable, "add_columns", new=racing_add):
            await search_store._migrate_description_columns()
        racing_add.assert_awaited_once()
        names = (await (await db.open_table(_META_TABLE)).schema()).names
        assert "description_source" not in names  # the stub swallowed the add: the lost race neither raises nor adds
    finally:
        await search_store.disconnect()


async def test_migrate_description_columns_propagates_an_unexpected_error(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, patch

    import lancedb.table
    import pyarrow as pa

    search_store = SearchStore(tmp_path / "db")
    await search_store.connect()
    try:
        db = search_store._require_connected()
        old_schema = pa.schema(
            [f for f in SearchStore._meta_schema() if f.name not in ("description_source", "description_status")]
        )
        await db.create_table(_META_TABLE, schema=old_schema)
        failing_add = AsyncMock(side_effect=RuntimeError("disk full"))
        with patch.object(lancedb.table.AsyncTable, "add_columns", new=failing_add), pytest.raises(
            RuntimeError, match="disk full"
        ):
            await search_store._migrate_description_columns()
    finally:
        await search_store.disconnect()


@pytest.mark.parametrize("column", ["description_source", "description_status"])
def test_stored_value_outside_the_contract_reads_as_unset_so_writers_never_meet_it(column: str) -> None:
    """A newer build's value (rollback) must not make every whole-row writer of the collection fail."""
    row = _legacy_row(description="docs", **{column: "somethingnew"})
    meta = SearchStore._row_to_meta(row)
    assert "somethingnew" not in repr(meta)
    assert (meta.description_source, meta.description_status) == ("generated", "ok")


def test_stored_value_outside_the_contract_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="archon_search.store"):
        SearchStore._row_to_meta(_legacy_row(description="docs", description_status="pending"))
    assert any("description_status" in r.message and "pending" in r.message for r in caplog.records)

"""M8 / M9: ownership is recorded before a collection's rows land, orphans are reported, the scan is projected.

M8: the meta row is the only record of a collection's owner. It used to be written after the rows (and not at all when
the caller passed no ``embedding_model``), so a crash or a failed meta write left a table with rows and no owner, which
every non-default namespace — the creator included — was then refused. The claim is now written first.

M9: ``_owner_namespaces`` runs on every ownership check (about three per ingested file); it must read the ``name`` and
``namespace`` columns only, not the centroid and description-embedding JSON of every collection.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from archon_search._types import ChunkRecord
from archon_search.collection_meta import CollectionMeta
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.store import _META_TABLE, CollectionOwnershipError, SearchStore

_DIM = 4
_MODEL = "test-model"
_OWNER = "team-a"
_OTHER = "team-b"
_COLLECTION = "claimed"


def _doc_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _chunk(label: str) -> ChunkRecord:
    doc_id = _doc_id(label)
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text=label,
        vector=[1.0, 0.0, 0.0, 0.0],
        source_path=f"/data/{label}.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )


@pytest.fixture
async def store(tmp_path: Path):
    search_store = SearchStore(tmp_path / "db")
    await search_store.connect()
    yield search_store
    await search_store.disconnect()


# ---------------------------------------------------------------------------
# M8: claim first
# ---------------------------------------------------------------------------


async def test_the_owner_is_recorded_before_the_rows_land(store: SearchStore, monkeypatch: pytest.MonkeyPatch) -> None:
    real_do_ingest = store._do_ingest
    seen: dict[str, Any] = {}

    async def spying_do_ingest(db: Any, collection: str, chunks: list[ChunkRecord]) -> int:
        seen["meta"] = await store._do_read_meta_unlocked(db, collection, namespace=_OWNER)
        return await real_do_ingest(db, collection, chunks)

    monkeypatch.setattr(store, "_do_ingest", spying_do_ingest)
    await store.ensure_collection(_COLLECTION, _DIM, namespace=_OWNER)

    await store.ingest_chunks(_COLLECTION, [_chunk("one")], embedding_model=_MODEL, namespace=_OWNER)

    assert seen["meta"] is not None and seen["meta"].namespace == _OWNER, "no owner on record when the rows landed"


async def test_a_write_that_dies_after_the_claim_leaves_the_owner_able_to_continue(
    store: SearchStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure that used to orphan the table: the rows are the part that fails, the owner stays recorded."""
    await store.ensure_collection(_COLLECTION, _DIM, namespace=_OWNER)
    real_do_ingest = store._do_ingest
    calls = {"n": 0}

    async def failing_once(db: Any, collection: str, chunks: list[ChunkRecord]) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return await real_do_ingest(db, collection, chunks)

    monkeypatch.setattr(store, "_do_ingest", failing_once)

    with pytest.raises(OSError, match="disk full"):
        await store.ingest_chunks(_COLLECTION, [_chunk("one")], embedding_model=_MODEL, namespace=_OWNER)
    assert await store.get_collection_meta(_COLLECTION, _OWNER) is None, "a claim with no rows is rolled back"

    await store.ingest_chunks(_COLLECTION, [_chunk("one")], embedding_model=_MODEL, namespace=_OWNER)

    meta = await store.get_collection_meta(_COLLECTION, _OWNER)
    assert meta is not None and meta.chunk_count == 1
    with pytest.raises(CollectionOwnershipError):
        await store.assert_collection_access(_COLLECTION, _OTHER)


async def test_a_failed_first_write_leaves_the_collection_claimable_by_anyone(
    store: SearchStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.ensure_collection(_COLLECTION, _DIM, namespace=_OWNER)

    async def failing(*_args: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_do_ingest", failing)
    with pytest.raises(OSError):
        await store.ingest_chunks(_COLLECTION, [_chunk("one")], embedding_model=_MODEL, namespace=_OWNER)
    monkeypatch.undo()

    await store.ingest_chunks(_COLLECTION, [_chunk("two")], embedding_model=_MODEL, namespace=_OTHER)

    meta = await store.get_collection_meta(_COLLECTION, _OTHER)
    assert meta is not None and meta.namespace == _OTHER


async def test_a_caller_that_names_no_embedding_model_writes_no_claim_row(store: SearchStore) -> None:
    """Known gap, kept on purpose: such a caller (none in production) leaves the old meta-less behaviour."""
    await store.ensure_collection(_COLLECTION, _DIM, namespace=_OWNER)

    await store.ingest_chunks(_COLLECTION, [_chunk("one")], namespace=_OWNER)

    assert await store.get_collection_meta(_COLLECTION, _OWNER) is None


async def test_the_first_batch_still_records_its_centroid_and_counts(store: SearchStore) -> None:
    await store.ensure_collection(_COLLECTION, _DIM, namespace=_OWNER)

    await store.ingest_chunks(_COLLECTION, [_chunk("one"), _chunk("two")], embedding_model=_MODEL, namespace=_OWNER)

    meta = await store.get_collection_meta(_COLLECTION, _OWNER)
    assert meta is not None
    assert (meta.chunk_count, meta.doc_count) == (2, 2)
    assert meta.centroid == [1.0, 0.0, 0.0, 0.0]
    assert meta.active_embedding_model == _MODEL
    assert meta.needs_recompute is False


async def test_a_second_batch_of_the_same_document_is_not_counted_twice(store: SearchStore) -> None:
    await store.ensure_collection(_COLLECTION, _DIM, namespace=_OWNER)
    await store.ingest_chunks(_COLLECTION, [_chunk("one")], embedding_model=_MODEL, namespace=_OWNER)

    await store.ingest_chunks(
        _COLLECTION, [_chunk("one")], embedding_model=_MODEL, namespace=_OWNER, _is_continuation=True
    )

    meta = await store.get_collection_meta(_COLLECTION, _OWNER)
    assert meta is not None and (meta.chunk_count, meta.doc_count) == (2, 1)


# ---------------------------------------------------------------------------
# M8: tables that have no owner on record are reported at startup
# ---------------------------------------------------------------------------


async def _orphan(store: SearchStore, name: str) -> None:
    await store.ensure_collection(name, _DIM)
    await store.ingest_chunks(name, [_chunk(name)], embedding_model=_MODEL)
    await store.delete_collection_meta(name, DEFAULT_NAMESPACE)


async def test_startup_names_the_collection_tables_that_have_no_meta_row(
    store: SearchStore, caplog: pytest.LogCaptureFixture
) -> None:
    await store.ensure_collection("healthy", _DIM, namespace=_OWNER)
    await store.ingest_chunks("healthy", [_chunk("healthy")], embedding_model=_MODEL, namespace=_OWNER)
    await _orphan(store, "lost-owner")
    await store.ensure_collection("empty-table", _DIM)  # no rows, no meta row: claimable, so not reported

    with caplog.at_level(logging.WARNING, logger="archon_search.store"):
        await store._run_startup_migrations()

    warnings = [r.getMessage() for r in caplog.records if "no meta row" in r.getMessage()]
    assert len(warnings) == 1
    assert "lost-owner" in warnings[0] and "healthy" not in warnings[0]
    assert DEFAULT_NAMESPACE in warnings[0]
    assert "empty-table" not in warnings[0]


async def test_startup_logs_nothing_when_every_table_has_a_meta_row(
    store: SearchStore, caplog: pytest.LogCaptureFixture
) -> None:
    await store.ensure_collection("healthy", _DIM, namespace=_OWNER)
    await store.ingest_chunks("healthy", [_chunk("healthy")], embedding_model=_MODEL, namespace=_OWNER)

    with caplog.at_level(logging.WARNING, logger="archon_search.store"):
        await store._run_startup_migrations()

    assert not [r for r in caplog.records if "no meta row" in r.getMessage()]


async def test_startup_on_a_fresh_store_logs_nothing(store: SearchStore, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="archon_search.store"):
        await store._run_startup_migrations()

    assert not [r for r in caplog.records if "no meta row" in r.getMessage()]


# ---------------------------------------------------------------------------
# M8: collections a build from before ownership checks wrote
# ---------------------------------------------------------------------------


async def _write_pre_ownership_meta(store: SearchStore, rows: list[dict[str, Any]]) -> None:
    """The meta table as a pre-namespace-migration build left it: no schema_version, job ids, TTL or centroid sums."""
    schema = pa.schema(
        [
            pa.field("name", pa.utf8()),
            pa.field("description", pa.utf8()),
            pa.field("centroid_json", pa.utf8()),
            pa.field("doc_count", pa.int64()),
            pa.field("chunk_count", pa.int64()),
            pa.field("embedding_model", pa.utf8()),
            pa.field("last_indexed", pa.utf8()),
            pa.field("last_described", pa.utf8()),
            pa.field("described_at_doc_count", pa.int64()),
            pa.field("namespace", pa.utf8(), nullable=True),
        ]
    )
    table = pa.Table.from_pylist(
        [
            {
                "description": "",
                "centroid_json": "[]",
                "doc_count": 1,
                "chunk_count": 1,
                "embedding_model": _MODEL,
                "last_indexed": "",
                "last_described": "",
                "described_at_doc_count": 0,
                **row,
            }
            for row in rows
        ],
        schema=schema,
    )
    await store._require_connected().create_table(_META_TABLE, table, mode="overwrite")


async def test_a_collection_a_pre_ownership_build_created_keeps_its_namespace_owner(store: SearchStore) -> None:
    await store.ensure_collection("legacy-ns", _DIM, namespace=_OWNER)
    await store.ingest_chunks("legacy-ns", [_chunk("old")], namespace=_OWNER)
    await store.ensure_collection("legacy-null", _DIM)
    await store.ingest_chunks("legacy-null", [_chunk("older")])
    await _write_pre_ownership_meta(
        store, [{"name": "legacy-ns", "namespace": _OWNER}, {"name": "legacy-null", "namespace": None}]
    )

    await store._run_startup_migrations()
    await store.migrate_default_ttl_seconds()  # applied by the in-place migration flow, not at startup

    await store.assert_collection_access("legacy-ns", _OWNER)
    await store.assert_collection_access("legacy-null", DEFAULT_NAMESPACE)
    with pytest.raises(CollectionOwnershipError):
        await store.assert_collection_access("legacy-ns", _OTHER)
    with pytest.raises(CollectionOwnershipError):
        await store.assert_collection_access("legacy-null", _OWNER)
    await store.ingest_chunks("legacy-ns", [_chunk("new")], embedding_model=_MODEL, namespace=_OWNER)
    assert await store.count_chunks("legacy-ns", _OWNER) == 2


# ---------------------------------------------------------------------------
# M9: the ownership scan reads two columns
# ---------------------------------------------------------------------------


class _RecordingQuery:
    def __init__(self, inner: Any, record: dict[str, Any]) -> None:
        self._inner = inner
        self._record = record

    def select(self, columns: Any) -> _RecordingQuery:
        return _RecordingQuery(self._inner.select(columns), self._record)

    def where(self, predicate: str, *args: Any, **kwargs: Any) -> _RecordingQuery:
        return _RecordingQuery(self._inner.where(predicate, *args, **kwargs), self._record)

    async def to_list(self) -> list[dict[str, Any]]:
        rows = await self._inner.to_list()
        self._record["columns"] = {column for row in rows for column in row}
        return rows


class _RecordingTable:
    def __init__(self, inner: Any, record: dict[str, Any]) -> None:
        self._inner = inner
        self._record = record

    def query(self) -> _RecordingQuery:
        return _RecordingQuery(self._inner.query(), self._record)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _RecordingDb:
    def __init__(self, inner: Any, record: dict[str, Any]) -> None:
        self._inner = inner
        self._record = record

    async def open_table(self, name: str, *args: Any, **kwargs: Any) -> Any:
        table = await self._inner.open_table(name, *args, **kwargs)
        return _RecordingTable(table, self._record) if name == _META_TABLE else table

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_the_ownership_scan_reads_only_the_name_and_namespace_columns(store: SearchStore) -> None:
    big_vector = [0.5] * 1024
    for index in range(5):
        await store.update_collection_meta(
            CollectionMeta(
                name=f"col-{index}", namespace=_OWNER if index % 2 else DEFAULT_NAMESPACE,
                centroid=big_vector, centroid_sum=big_vector, description_embedding=big_vector,
                active_embedding_model=_MODEL,
            )
        )
    record: dict[str, Any] = {}

    owners = await store._owner_namespaces(_RecordingDb(store._require_connected(), record), "col-1")

    assert owners == {_OWNER}
    assert record["columns"] <= {"name", "namespace"}


async def test_the_ownership_scan_of_a_legacy_null_namespace_row_is_the_default_namespace(store: SearchStore) -> None:
    await store.ensure_collection("legacy-null", _DIM)
    await _write_pre_ownership_meta(store, [{"name": "legacy-null", "namespace": None}])

    assert await store._owner_namespaces(store._require_connected(), "legacy-null") == {DEFAULT_NAMESPACE}
    assert await store._owner_namespaces(store._require_connected(), "someone-else") == set()


async def test_the_ownership_scan_of_a_meta_table_without_a_namespace_column_is_default_owned(
    store: SearchStore,
) -> None:
    """A table from before the namespace column existed (the startup migration adds it): every row is ``default``."""
    schema = pa.schema([pa.field("name", pa.utf8()), pa.field("description", pa.utf8())])
    table = pa.Table.from_pylist([{"name": "ancient", "description": ""}], schema=schema)
    await store._require_connected().create_table(_META_TABLE, table, mode="overwrite")

    assert await store._owner_namespaces(store._require_connected(), "ancient") == {DEFAULT_NAMESPACE}

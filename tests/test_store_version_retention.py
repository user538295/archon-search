"""Bounded LanceDB version retention: ``SearchStore.prune_versions`` and ``GraphStore.prune_versions``.

LanceDB keeps every table version (manifest, transaction and data files) until something cleans
them up. The collection-meta table is rewritten by every ingest batch and nothing ever optimized
it; chunk tables were only pruned on a 7-day default and only when an FTS optimize happened to run.
Measured before this policy: 40 ingest+delete cycles left 9679 files under the database directory.
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from archon_search._types import ChunkRecord
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_VERSION_RETENTION_HOURS
from archon_search.graph_store import GraphStore
from archon_search.store import _META_TABLE, SearchStore

# These tests prune with zero retention on purpose, to observe every superseded version going away.
# LanceDB (>= 0.41) warns about exactly that call; the warning is the library's caution for
# concurrent readers, which these tests control. Production passes DEFAULT_VERSION_RETENTION_HOURS.
pytestmark = pytest.mark.filterwarnings(
    r"ignore:optimize\(cleanup_older_than=0\) removes every version:UserWarning"
)

_DIM = 4
_COLLECTION = "retention_col"
_DOCS = 6
_NO_RETENTION = timedelta(0)
_CUSTOM_RETENTION_HOURS = 3


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _chunk(doc_id: str) -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text="retention probe text",
        vector=[1.0] * _DIM,
        source_path=f"/tmp/{doc_id[:8]}.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )


async def _versions(store: SearchStore, table_name: str) -> int:
    table = await store._require_connected().open_table(table_name)
    return len(await table.list_versions())


async def _ingest_documents(store: SearchStore, collection: str = _COLLECTION, documents: int = _DOCS) -> None:
    await store.ensure_collection(collection, _DIM)
    for _ in range(documents):
        await store.ingest_chunks(collection, [_chunk(_doc_id())], embedding_model="m")


@pytest.fixture
async def store(tmp_path: Path):
    search_store = SearchStore(tmp_path / "db", config=SearchConfig())
    await search_store.connect()
    try:
        yield search_store
    finally:
        await search_store.disconnect()


def test_default_retention_is_a_conservative_day() -> None:
    assert DEFAULT_VERSION_RETENTION_HOURS == 24
    assert SearchConfig().maintenance.version_retention_hours == DEFAULT_VERSION_RETENTION_HOURS


async def test_prune_versions_drops_superseded_versions_and_keeps_the_data(store: SearchStore) -> None:
    await _ingest_documents(store)
    assert await _versions(store, _COLLECTION) > 1
    assert await _versions(store, _META_TABLE) > 1

    await store.prune_versions(_NO_RETENTION)

    assert await _versions(store, _COLLECTION) == 1
    assert await _versions(store, _META_TABLE) == 1
    assert await store.count_chunks(_COLLECTION, "default") == _DOCS
    meta = await store.get_collection_meta(_COLLECTION, "default")
    assert meta is not None
    assert meta.chunk_count == _DOCS


async def test_prune_versions_never_removes_versions_inside_the_retention_window(store: SearchStore) -> None:
    await _ingest_documents(store)
    chunk_versions = await _versions(store, _COLLECTION)
    meta_versions = await _versions(store, _META_TABLE)

    await store.prune_versions(timedelta(hours=DEFAULT_VERSION_RETENTION_HOURS))

    assert await _versions(store, _COLLECTION) >= chunk_versions  # compaction may add one; none is removed
    assert await _versions(store, _META_TABLE) >= meta_versions


async def test_prune_versions_does_not_disturb_concurrent_readers(store: SearchStore) -> None:
    """A reader on the latest version keeps reading while cleanup runs; cleanup keeps that version."""
    await _ingest_documents(store)
    reader = await store._require_connected().open_table(_COLLECTION)

    async def read_repeatedly() -> list[int]:
        return [await reader.count_rows() for _ in range(20)]

    counts, _ = await asyncio.gather(read_repeatedly(), store.prune_versions(_NO_RETENTION))

    assert set(counts) == {_DOCS}


async def test_prune_versions_on_an_empty_database_is_a_no_op(store: SearchStore) -> None:
    await store.prune_versions(_NO_RETENTION)


async def test_prune_versions_requires_a_connection(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not connected"):
        await SearchStore(tmp_path / "db").prune_versions(_NO_RETENTION)


async def test_a_table_that_fails_to_prune_does_not_stop_the_others(
    store: SearchStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    await _ingest_documents(store)
    await _ingest_documents(store, "other_col", 2)
    original = store._prune_table

    async def prune_table(name: str, retention: timedelta) -> None:
        if name == _COLLECTION:
            raise OSError("disk went away")
        await original(name, retention)

    monkeypatch.setattr(store, "_prune_table", prune_table)

    await store.prune_versions(_NO_RETENTION)

    assert await _versions(store, "other_col") == 1
    assert await _versions(store, _META_TABLE) == 1
    assert any(_COLLECTION in record.getMessage() for record in caplog.records)


async def test_a_busy_collection_is_skipped_not_waited_for(
    store: SearchStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _ingest_documents(store)
    before = await _versions(store, _COLLECTION)
    monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", 0.05)

    async with store.lock_for(_COLLECTION):
        await store.prune_versions(_NO_RETENTION)

    assert await _versions(store, _COLLECTION) == before


async def test_optimize_fts_prunes_with_the_configured_retention(tmp_path: Path) -> None:
    """The ingest-path optimize used lancedb's 7-day default; it follows the knob now."""
    config = SearchConfig()
    config.maintenance.version_retention_hours = _CUSTOM_RETENTION_HOURS
    store = SearchStore(tmp_path / "db", config=config)
    await store.connect()
    try:
        await _ingest_documents(store)
        await store.rebuild_fts_index(_COLLECTION)
        table_type = type(await store._require_connected().open_table(_COLLECTION))
        seen: list[timedelta | None] = []
        original = table_type.optimize

        async def spy(self, *, cleanup_older_than=None, **kwargs):
            seen.append(cleanup_older_than)
            return await original(self, cleanup_older_than=cleanup_older_than, **kwargs)

        table_type.optimize = spy
        try:
            await store.optimize_fts(_COLLECTION)
        finally:
            table_type.optimize = original
    finally:
        await store.disconnect()

    assert seen == [timedelta(hours=_CUSTOM_RETENTION_HOURS)]


def _placeholder_row(schema: pa.Schema) -> dict[str, object]:
    """One row of type defaults for *schema* — just enough to commit a table version."""
    defaults: dict[str, object] = {}
    for field in schema:
        if pa.types.is_string(field.type) or pa.types.is_large_string(field.type):
            defaults[field.name] = ""
        elif pa.types.is_integer(field.type):
            defaults[field.name] = 0
        elif pa.types.is_floating(field.type):
            defaults[field.name] = 0.0
        elif pa.types.is_boolean(field.type):
            defaults[field.name] = False
        else:
            defaults[field.name] = None
    return defaults


async def test_graph_store_prune_versions_covers_its_tables(tmp_path: Path) -> None:
    graph = GraphStore(tmp_path / "db")
    await graph.connect()
    try:
        await graph.ensure_graph_tables(_COLLECTION, "default")
        db = graph._require_db()
        graph_tables = [name for name in (await db.list_tables()).tables if name.startswith("_archon_graph_")]
        assert graph_tables
        for name in graph_tables:
            table = await db.open_table(name)
            for _ in range(2):
                await table.add([_placeholder_row(await table.schema())])
            assert len(await table.list_versions()) > 1

        await graph.prune_versions(_NO_RETENTION)

        for name in graph_tables:
            assert len(await (await db.open_table(name)).list_versions()) == 1
    finally:
        await graph.disconnect()

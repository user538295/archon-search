"""Tests for ``SearchStore.reindex_metadata``.

Implements Task 6.2 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from archon_search._types import ChunkRecord
from archon_search.store import ReindexResult, SearchStore, StoreBusyError

_DIM = 4


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _chunk(source_path: str, **overrides) -> ChunkRecord:
    did = overrides.pop("doc_id", _doc_id())
    return ChunkRecord(
        doc_id=did,
        chunk_id=overrides.pop("chunk_id", f"{did}-000000"),
        text=overrides.pop("text", "hello world for reindex"),
        vector=overrides.pop("vector", [0.0] * _DIM),
        source_path=source_path,
        indexed_at=overrides.pop("indexed_at", datetime.now(timezone.utc).isoformat()),
        ingested_by=overrides.pop("ingested_by", "cli"),
        **overrides,
    )


async def _force_legacy_row(store: SearchStore, col: str, chunk_id: str) -> None:
    """Mutate the underlying row to inject pre-A1 state."""
    db = store._require_connected()
    table = await db.open_table(col)
    await table.update(
        where=f"chunk_id = '{chunk_id}'",
        updates={
            "ingested_by": "archon-search-cli",
            "file_type": "",
            "updated_at": "",
        },
    )


async def _read_raw(store: SearchStore, col: str, chunk_id: str) -> dict:
    db = store._require_connected()
    table = await db.open_table(col)
    rows = await table.query().where(f"chunk_id = '{chunk_id}'").to_list()
    assert len(rows) == 1
    return rows[0]


def test_reindex_result_dataclass_shape() -> None:
    r = ReindexResult(processed=10, updated=3, skipped=7, warnings=["x"])
    assert r.processed == 10 and r.updated == 3 and r.skipped == 7
    assert r.warnings == ["x"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_empty_collection_is_noop(connected_store: SearchStore) -> None:
    col = f"test-{uuid.uuid4().hex[:8]}"
    await connected_store.ensure_collection(col, _DIM)
    result = await connected_store.reindex_metadata(col)
    assert result.processed == 0 and result.updated == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_populates_file_type_and_ingested_by(
    connected_store: SearchStore,
) -> None:
    col = f"test-{uuid.uuid4().hex[:8]}"
    await connected_store.ensure_collection(col, _DIM)
    tmp = Path("/tmp/reindex_target.md")
    tmp.write_text("seed file for reindex test")
    chunk = _chunk(source_path=str(tmp), file_type="md", ingested_by="cli")
    await connected_store.ingest_chunks(col, [chunk])
    await _force_legacy_row(connected_store, col, chunk.chunk_id)

    result = await connected_store.reindex_metadata(col)
    assert result.processed == 1
    assert result.updated == 1

    row = await _read_raw(connected_store, col, chunk.chunk_id)
    assert row["file_type"] == "md"
    assert row["ingested_by"] == "reindex"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_preserves_non_legacy_ingested_by(
    connected_store: SearchStore,
) -> None:
    col = f"test-{uuid.uuid4().hex[:8]}"
    await connected_store.ensure_collection(col, _DIM)
    tmp = Path("/tmp/reindex_keep.md")
    tmp.write_text("file")
    chunk = _chunk(source_path=str(tmp), file_type="md", ingested_by="http")
    await connected_store.ingest_chunks(col, [chunk])

    await connected_store.reindex_metadata(col)
    row = await _read_raw(connected_store, col, chunk.chunk_id)
    # "http" must not be retroactively rewritten to "reindex".
    assert row["ingested_by"] == "http"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_dry_run_writes_nothing(connected_store: SearchStore) -> None:
    col = f"test-{uuid.uuid4().hex[:8]}"
    await connected_store.ensure_collection(col, _DIM)
    tmp = Path("/tmp/reindex_dry.md")
    tmp.write_text("file")
    chunk = _chunk(source_path=str(tmp), ingested_by="cli")
    await connected_store.ingest_chunks(col, [chunk])
    await _force_legacy_row(connected_store, col, chunk.chunk_id)

    result = await connected_store.reindex_metadata(col, dry_run=True)
    assert result.processed == 1
    assert result.updated == 0  # dry_run reports counts without writing

    row = await _read_raw(connected_store, col, chunk.chunk_id)
    assert row["ingested_by"] == "archon-search-cli"  # unchanged


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_progress_cb_invoked(connected_store: SearchStore) -> None:
    col = f"test-{uuid.uuid4().hex[:8]}"
    await connected_store.ensure_collection(col, _DIM)
    tmp = Path("/tmp/reindex_pcb.md")
    tmp.write_text("file")
    chunks = []
    for i in range(3):
        did = _doc_id()
        chunks.append(_chunk(
            doc_id=did, chunk_id=f"{did}-000000",
            source_path=str(tmp), ingested_by="cli",
        ))
    await connected_store.ingest_chunks(col, chunks)

    calls: list[tuple[int, int]] = []
    await connected_store.reindex_metadata(col, progress_cb=lambda p, t: calls.append((p, t)))
    assert calls, "progress_cb must be invoked at least once"
    assert calls[-1] == (3, 3)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_missing_source_file_warning(connected_store: SearchStore) -> None:
    col = f"test-{uuid.uuid4().hex[:8]}"
    await connected_store.ensure_collection(col, _DIM)
    bogus = "/tmp/does-not-exist-XYZ.md"
    chunk = _chunk(source_path=bogus, ingested_by="cli")
    await connected_store.ingest_chunks(col, [chunk])
    await _force_legacy_row(connected_store, col, chunk.chunk_id)

    result = await connected_store.reindex_metadata(col)
    assert any(bogus in w for w in result.warnings)
    # chunk is preserved (not deleted) and ingested_by becomes "reindex"
    row = await _read_raw(connected_store, col, chunk.chunk_id)
    assert row["ingested_by"] == "reindex"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_blocks_concurrent_ingest_same_collection(
    connected_store: SearchStore, monkeypatch,
) -> None:
    col = f"test-{uuid.uuid4().hex[:8]}"
    await connected_store.ensure_collection(col, _DIM)
    tmp = Path("/tmp/reindex_lock.md")
    tmp.write_text("x")
    seed = _chunk(source_path=str(tmp), ingested_by="cli")
    await connected_store.ingest_chunks(col, [seed])

    # Hold the lock manually to simulate an in-flight reindex.
    lock = connected_store.lock_for(col)
    await lock.acquire()
    try:
        monkeypatch.setattr("archon_search.store.INGEST_LOCK_TIMEOUT_S", 0.1)
        with pytest.raises(StoreBusyError):
            await connected_store.ingest_chunks(col, [_chunk(source_path=str(tmp))])
    finally:
        lock.release()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_idempotent_logical_equality(
    connected_store: SearchStore,
) -> None:
    col = f"test-{uuid.uuid4().hex[:8]}"
    await connected_store.ensure_collection(col, _DIM)
    tmp = Path("/tmp/reindex_idem.md")
    tmp.write_text("x")
    chunk = _chunk(source_path=str(tmp), ingested_by="cli")
    await connected_store.ingest_chunks(col, [chunk])
    await _force_legacy_row(connected_store, col, chunk.chunk_id)

    await connected_store.reindex_metadata(col)
    row1 = await _read_raw(connected_store, col, chunk.chunk_id)
    await connected_store.reindex_metadata(col)
    row2 = await _read_raw(connected_store, col, chunk.chunk_id)
    for key in ("file_type", "ingested_by", "updated_at"):
        assert row1[key] == row2[key]

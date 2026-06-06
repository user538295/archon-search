"""Tests pinning that ``SearchStore.ingest_chunks`` writes metadata fields
as-is, without silent rewriting to the legacy ``"archon-search-cli"``.

Implements Task 3.4 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

import dataclasses
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from archon_search._types import ChunkRecord
from archon_search.store import SearchStore

_DIM = 4


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _chunk(**overrides) -> ChunkRecord:
    doc_id = overrides.pop("doc_id", _doc_id())
    chunk_id = overrides.pop("chunk_id", f"{doc_id}-000000")
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text=overrides.pop("text", "hello world"),
        vector=overrides.pop("vector", [0.0] * _DIM),
        source_path=overrides.pop("source_path", "/tmp/x.md"),
        indexed_at=overrides.pop("indexed_at", datetime.now(timezone.utc).isoformat()),
        **overrides,
    )


async def _read_row(store: SearchStore, col: str, chunk_id: str) -> dict:
    db = store._require_connected()
    table = await db.open_table(col)
    rows = await table.query().where(f"chunk_id = '{chunk_id}'").to_list()
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    return rows[0]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_writes_file_type_and_ingested_by(
    connected_store: SearchStore, col_name: str
) -> None:
    chunk = _chunk(
        file_type="md",
        ingested_by="cli",
        updated_at="2026-05-21T10:00:00+00:00",
    )
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [chunk])

    row = await _read_row(connected_store, col_name, chunk.chunk_id)
    assert row["file_type"] == "md"
    assert row["ingested_by"] == "cli"
    assert row["updated_at"] == "2026-05-21T10:00:00+00:00"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_updated_at_fallback_to_indexed_at(
    connected_store: SearchStore, col_name: str
) -> None:
    indexed = "2026-05-21T05:00:00+00:00"
    chunk = _chunk(updated_at="", indexed_at=indexed, ingested_by="cli")
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [chunk])

    row = await _read_row(connected_store, col_name, chunk.chunk_id)
    assert row["updated_at"] == indexed


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ingest_does_not_silently_rewrite_to_legacy(
    connected_store: SearchStore, col_name: str
) -> None:
    """Force ``ingested_by=""`` and confirm it is NOT silently rewritten to legacy."""
    chunk = _chunk(ingested_by="cli")  # valid value at construction
    chunk = dataclasses.replace(chunk)  # ensure mutable copy
    # bypass the type system to force empty string
    object.__setattr__(chunk, "ingested_by", "")

    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [chunk])

    row = await _read_row(connected_store, col_name, chunk.chunk_id)
    # Pins removal of the ``or "archon-search-cli"`` write coercion.
    assert row["ingested_by"] == ""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_updated_at_is_utc_iso8601_with_offset(
    connected_store: SearchStore, col_name: str
) -> None:
    ts = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    assert ts.endswith("+00:00")
    chunk = _chunk(updated_at=ts, ingested_by="cli")
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [chunk])

    row = await _read_row(connected_store, col_name, chunk.chunk_id)
    assert row["updated_at"].endswith("+00:00")
    datetime.fromisoformat(row["updated_at"])  # parses cleanly


@pytest.mark.integration
@pytest.mark.asyncio
async def test_chunk_offsets_absent_from_lancedb_row(
    connected_store: SearchStore, col_name: str
) -> None:
    """Transient start_offset/end_offset on ChunkRecord must NOT appear in LanceDB rows."""
    chunk = _chunk(start_offset=5, end_offset=10)
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [chunk])

    row = await _read_row(connected_store, col_name, chunk.chunk_id)
    assert "start_offset" not in row, "start_offset must not be persisted to LanceDB"
    assert "end_offset" not in row, "end_offset must not be persisted to LanceDB"

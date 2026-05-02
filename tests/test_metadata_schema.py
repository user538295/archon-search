"""Tests for extended chunk metadata schema (FEAT-038 Task 6.1)."""
from __future__ import annotations

import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from archon_search._types import ChunkRecord
from archon_search.store import SearchStore, parse_metadata, validate_metadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(
    doc_id: str = "a" * 64,
    chunk_id: str = "",
    text: str = "hello",
    vector: list[float] | None = None,
    source_path: str = "/tmp/test.txt",
    indexed_at: str = "",
    file_type: str = "",
    language: str | None = None,
    metadata: dict[str, str] | None = None,
    custom_score: float | None = None,
    ingested_by: str = "archon-search-cli",
    updated_at: str = "",
) -> ChunkRecord:
    now = datetime.now(timezone.utc).isoformat()
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=chunk_id or f"{doc_id}-000000",
        text=text,
        vector=vector or [0.1, 0.2, 0.3],
        source_path=source_path,
        indexed_at=indexed_at or now,
        file_type=file_type,
        language=language,
        metadata=metadata or {},
        custom_score=custom_score,
        ingested_by=ingested_by,
        updated_at=updated_at or now,
    )


async def _store_with_chunk(chunk: ChunkRecord, tmp: str) -> tuple[SearchStore, str]:
    """Create a store at tmp, ingest one chunk, return (store, collection)."""
    store = SearchStore(tmp)
    await store.connect()
    collection = "test_col"
    await store.ensure_collection(collection, len(chunk.vector))
    await store.ingest_chunks(collection, [chunk])
    return store, collection


async def _read_first_row(store: SearchStore, collection: str) -> dict[str, Any]:
    table = await store._db.open_table(collection)  # type: ignore[attr-defined]
    rows = await table.query().to_list()
    assert rows, "No rows found"
    return rows[0]


# ---------------------------------------------------------------------------
# doc_id / chunk_id format
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingested_chunk_has_doc_id(tmp_path):
    """doc_id must be a 64-hex-char SHA-256 digest."""
    doc_id = "b" * 64
    chunk = _make_chunk(doc_id=doc_id)
    store, col = await _store_with_chunk(chunk, str(tmp_path))
    try:
        row = await _read_first_row(store, col)
        assert row["doc_id"] == doc_id
        assert re.match(r"^[a-f0-9]{64}$", row["doc_id"])
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_ingested_chunk_has_chunk_id(tmp_path):
    """chunk_id must follow the "{doc_id}-{idx:06d}" format."""
    doc_id = "c" * 64
    chunk_id = f"{doc_id}-000042"
    chunk = _make_chunk(doc_id=doc_id, chunk_id=chunk_id)
    store, col = await _store_with_chunk(chunk, str(tmp_path))
    try:
        row = await _read_first_row(store, col)
        assert row["chunk_id"] == chunk_id
        assert re.match(r"^[a-f0-9]{64}-\d{6}$", row["chunk_id"])
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# indexed_at
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingested_chunk_has_indexed_at(tmp_path):
    """indexed_at must be an ISO-8601 UTC timestamp."""
    chunk = _make_chunk()
    store, col = await _store_with_chunk(chunk, str(tmp_path))
    try:
        row = await _read_first_row(store, col)
        ts = row["indexed_at"]
        assert ts, "indexed_at should not be empty"
        # Must parse as ISO-8601
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None or ts.endswith("Z") or "+" in ts or ts.endswith("+00:00")
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# metadata dict round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metadata_dict_stored_and_retrieved(tmp_path):
    """metadata dict {"key": "val"} must round-trip correctly and raw JSON preserved in LanceDB."""
    meta = {"author": "alice", "version": "1.0"}
    chunk = _make_chunk(metadata=meta)
    store, col = await _store_with_chunk(chunk, str(tmp_path))
    try:
        row = await _read_first_row(store, col)
        # Verify LanceDB preserved the raw JSON string
        assert row["metadata"] == '{"author": "alice", "version": "1.0"}'
        # Verify parse round-trip
        retrieved = parse_metadata(row.get("metadata", "{}"))
        assert retrieved == meta
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# metadata validation
# ---------------------------------------------------------------------------

def test_metadata_max_fields_validation():
    """51 fields must raise ValueError."""
    meta = {f"key{i}": "value" for i in range(51)}
    with pytest.raises(ValueError, match="max 50"):
        validate_metadata(meta)


def test_metadata_exactly_50_fields_accepted():
    """Exactly 50 fields must NOT raise."""
    meta = {f"key{i}": "value" for i in range(50)}
    validate_metadata(meta)  # must not raise


def test_metadata_key_too_long_validation():
    """A key longer than 256 chars must raise ValueError."""
    meta = {"k" * 257: "value"}
    with pytest.raises(ValueError, match="key"):
        validate_metadata(meta)


def test_metadata_value_too_long_validation():
    """A value longer than 4096 chars must raise ValueError."""
    meta = {"key": "v" * 4097}
    with pytest.raises(ValueError, match="value"):
        validate_metadata(meta)


def test_metadata_non_string_key_raises_value_error():
    """Non-string key must raise ValueError (not TypeError)."""
    with pytest.raises(ValueError):
        validate_metadata({1: "value"})  # type: ignore[dict-item]


def test_metadata_non_string_value_raises_value_error():
    """Non-string value must raise ValueError (not TypeError)."""
    with pytest.raises(ValueError):
        validate_metadata({"key": 42})  # type: ignore[dict-item]


# ---------------------------------------------------------------------------
# custom_score
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_custom_score_stored(tmp_path):
    """custom_score=0.9 must round-trip correctly."""
    chunk = _make_chunk(custom_score=0.9)
    store, col = await _store_with_chunk(chunk, str(tmp_path))
    try:
        row = await _read_first_row(store, col)
        assert row["custom_score"] is not None
        assert abs(row["custom_score"] - 0.9) < 1e-6
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_custom_score_none_round_trip(tmp_path):
    """custom_score=None (default) must round-trip as None."""
    chunk = _make_chunk(custom_score=None)
    store, col = await _store_with_chunk(chunk, str(tmp_path))
    try:
        row = await _read_first_row(store, col)
        assert row.get("custom_score") is None
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# New field round-trip tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_type_stored(tmp_path):
    """file_type='python' must round-trip correctly."""
    chunk = _make_chunk(file_type="python")
    store, col = await _store_with_chunk(chunk, str(tmp_path))
    try:
        row = await _read_first_row(store, col)
        assert row["file_type"] == "python"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_ingested_by_stored(tmp_path):
    """ingested_by='custom-tool' must round-trip correctly via fetch_adjacent_chunks."""
    doc_id = "e" * 64
    chunk = _make_chunk(doc_id=doc_id, chunk_id=f"{doc_id}-000000", ingested_by="custom-tool")
    store, col = await _store_with_chunk(chunk, str(tmp_path))
    try:
        # Use the production read path: fetch adjacent chunks around index 1
        # so chunk at index 0 is returned (center=1, window=1 fetches idx 0 and 2)
        results = await store.fetch_adjacent_chunks(col, doc_id, center_idx=1, window=1)
        assert len(results) == 1, f"Expected 1 chunk, got {len(results)}"
        assert results[0].ingested_by == "custom-tool"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_language_en_stored(tmp_path):
    """language='en' must round-trip as 'en', not None."""
    chunk = _make_chunk(language="en")
    store, col = await _store_with_chunk(chunk, str(tmp_path))
    try:
        row = await _read_first_row(store, col)
        # language="en" stored as "en", fetch_adjacent_chunks maps "" → None but "en" stays "en"
        assert row["language"] == "en"
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_updated_at_stored(tmp_path):
    """Explicit updated_at must round-trip correctly."""
    explicit_ts = "2025-01-15T10:00:00+00:00"
    chunk = _make_chunk(updated_at=explicit_ts)
    store, col = await _store_with_chunk(chunk, str(tmp_path))
    try:
        row = await _read_first_row(store, col)
        assert row["updated_at"] == explicit_ts
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# Schema evolution: old-schema chunk read → new field defaults to None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_existing_chunk_missing_new_field_defaults_null(tmp_path):
    """Old-schema chunk (no 'language' field) read via fetch_adjacent_chunks must have language=None."""
    import pyarrow as pa

    store = SearchStore(str(tmp_path))
    await store.connect()
    collection = "old_schema_col"

    # Create table with OLD schema (no language field)
    old_schema = pa.schema([
        pa.field("doc_id", pa.utf8()),
        pa.field("chunk_id", pa.utf8()),
        pa.field("text", pa.utf8()),
        pa.field("vector", pa.list_(pa.float32(), 3)),
        pa.field("source_path", pa.utf8()),
        pa.field("indexed_at", pa.utf8()),
    ])
    db = store._db  # type: ignore[attr-defined]
    table = await db.create_table(collection, schema=old_schema, exist_ok=True)

    doc_id = "d" * 64
    now = datetime.now(timezone.utc).isoformat()
    await table.add([{
        "doc_id": doc_id,
        "chunk_id": f"{doc_id}-000000",
        "text": "old chunk",
        "vector": [0.1, 0.2, 0.3],
        "source_path": "/old/path.txt",
        "indexed_at": now,
    }])

    try:
        # Use the production read path: fetch_adjacent_chunks with center_idx=1, window=1
        # This fetches chunk indices 0 and 2 (excluding center=1). Chunk 0 exists.
        results = await store.fetch_adjacent_chunks(collection, doc_id, center_idx=1, window=1)
        assert len(results) == 1, f"Expected 1 chunk, got {len(results)}"
        assert results[0].language is None
    finally:
        await store.disconnect()

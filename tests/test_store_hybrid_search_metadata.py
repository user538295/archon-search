"""Tests pinning that ``SearchStore.hybrid_search`` populates metadata
fields on every returned ``SearchResult``, including legacy normalization
for ``ingested_by``.

Implements Task 4.3 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import pytest

from archon_search._types import ChunkRecord, SearchResult
from archon_search.store import SearchStore

_DIM = 4


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _chunk(**overrides) -> ChunkRecord:
    did = overrides.pop("doc_id", _doc_id())
    return ChunkRecord(
        doc_id=did,
        chunk_id=overrides.pop("chunk_id", f"{did}-000000"),
        text=overrides.pop("text", "searchable test content"),
        vector=overrides.pop("vector", [0.0] * _DIM),
        source_path=overrides.pop("source_path", "/tmp/x.py"),
        indexed_at=overrides.pop("indexed_at", datetime.now(timezone.utc).isoformat()),
        **overrides,
    )


async def _seed_and_search(store, col, chunk):
    await store.ensure_collection(col, _DIM)
    await store.ingest_chunks(col, [chunk])
    await store.rebuild_fts_index(col)
    return await store.hybrid_search(col, [0.0] * _DIM, "searchable", top_k=5)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_returns_file_type(connected_store, col_name):
    chunk = _chunk(file_type="py", ingested_by="cli")
    results = await _seed_and_search(connected_store, col_name, chunk)
    assert results
    assert results[0].file_type == "py"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_metadata_is_dict_not_string(connected_store, col_name):
    chunk = _chunk(metadata={"k": "v"}, ingested_by="cli")
    results = await _seed_and_search(connected_store, col_name, chunk)
    assert results
    assert isinstance(results[0].metadata, dict)
    assert results[0].metadata == {"k": "v"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_updated_at_falls_back_to_indexed_at(
    connected_store, col_name
):
    indexed = "2026-05-21T05:00:00+00:00"
    chunk = _chunk(updated_at="", indexed_at=indexed, ingested_by="cli")
    results = await _seed_and_search(connected_store, col_name, chunk)
    assert results
    assert results[0].updated_at == results[0].indexed_at
    assert results[0].updated_at == indexed


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_normalizes_legacy_ingested_by_to_cli(
    connected_store, col_name
):
    """Write a row with the legacy value, then assert the search result
    normalizes it to the canonical ``"cli"`` at the read boundary."""
    chunk = _chunk(ingested_by="cli")
    # Write via the store, then mutate the underlying row to inject the legacy
    # string as if it were pre-A1 data.
    await connected_store.ensure_collection(col_name, _DIM)
    await connected_store.ingest_chunks(col_name, [chunk])

    db = connected_store._require_connected()
    table = await db.open_table(col_name)
    # LanceDB Python API supports SQL-style update for in-place writes.
    await table.update(
        where=f"chunk_id = '{chunk.chunk_id}'",
        updates={"ingested_by": "'archon-search-cli'"},
    )
    await connected_store.rebuild_fts_index(col_name)

    results = await connected_store.hybrid_search(
        col_name, [0.0] * _DIM, "searchable", top_k=5
    )
    assert results
    assert results[0].ingested_by == "cli"

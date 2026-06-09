"""Tests for Task 4.1 of C6 — ``reindex_metadata`` must NOT call any FTS method.

Covers:
- Unit: ``rebuild_fts_index`` and ``optimize_fts`` are never called by ``reindex_metadata``.
- Integration: FTS search still works correctly after a metadata-only reindex.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from archon_search._types import ChunkRecord
from archon_search.store import SearchStore


_DIM = 4


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _chunk(source_path: str, text: str = "hello world for fts test") -> ChunkRecord:
    did = _doc_id()
    return ChunkRecord(
        doc_id=did,
        chunk_id=f"{did}-000000",
        text=text,
        vector=[0.1] * _DIM,
        source_path=source_path,
        indexed_at=datetime.now(timezone.utc).isoformat(),
        ingested_by="cli",
    )


# ---------------------------------------------------------------------------
# Unit tests — verify no FTS methods called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reindex_metadata_does_not_call_rebuild_fts(tmp_path: Path) -> None:
    """reindex_metadata must not call rebuild_fts_index or optimize_fts."""
    store = SearchStore(tmp_path / "db")
    await store.connect()
    await store.ensure_collection("col", _DIM)

    chunk = _chunk("/data/a.md")
    await store.ingest_chunks("col", [chunk])
    await store.rebuild_fts_index("col")

    with (
        patch.object(store, "rebuild_fts_index", new_callable=AsyncMock) as mock_rebuild,
        patch.object(store, "optimize_fts", new_callable=AsyncMock) as mock_optimize,
    ):
        await store.reindex_metadata("col")

    mock_rebuild.assert_not_called()
    mock_optimize.assert_not_called()


@pytest.mark.asyncio
async def test_reindex_metadata_does_not_call_rebuild_fts_when_updates_present(
    tmp_path: Path,
) -> None:
    """Even when rows are actually updated, reindex_metadata must not call FTS methods."""
    store = SearchStore(tmp_path / "db")
    await store.connect()
    await store.ensure_collection("col", _DIM)

    # Insert a legacy row that reindex_metadata will pick up as needing update
    chunk = _chunk("/data/b.md")
    await store.ingest_chunks("col", [chunk])

    # Force a legacy state so updates list is non-empty
    db = store._require_connected()
    table = await db.open_table("col")
    await table.update(
        where=f"chunk_id = '{chunk.chunk_id}'",
        updates={"ingested_by": "archon-search-cli", "file_type": ""},
    )

    with (
        patch.object(store, "rebuild_fts_index", new_callable=AsyncMock) as mock_rebuild,
        patch.object(store, "optimize_fts", new_callable=AsyncMock) as mock_optimize,
    ):
        result = await store.reindex_metadata("col")

    assert result.updated >= 1, "Expected at least one row to be updated"
    mock_rebuild.assert_not_called()
    mock_optimize.assert_not_called()


# ---------------------------------------------------------------------------
# Integration test — FTS still works after metadata reindex
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reindex_metadata_fts_still_valid_after_metadata_update(
    tmp_path: Path,
) -> None:
    """After reindex_metadata with actual row mutations, FTS must still work correctly.

    Forces a legacy ``ingested_by`` value so that ``reindex_metadata`` produces a
    non-empty ``updates`` list and performs real ``table.update()`` calls — the path
    that previously triggered the (now-removed) ``rebuild_fts_index`` call.
    """
    store = SearchStore(tmp_path / "db")
    await store.connect()
    await store.ensure_collection("col", _DIM)

    unique_text = "incremental_fts_maintenance_unique_token_xyzzy"
    chunk = _chunk("/data/c.md", text=unique_text)
    await store.ingest_chunks("col", [chunk])
    await store.rebuild_fts_index("col")

    # Force a legacy ingested_by value so reindex_metadata will actually update the row
    db = store._require_connected()
    table = await db.open_table("col")
    await table.update(
        where=f"chunk_id = '{chunk.chunk_id}'",
        updates={"ingested_by": "archon-search-cli", "file_type": ""},
    )

    # Run metadata reindex — must NOT rebuild or optimize FTS,
    # but must still update the row (ingested_by -> "reindex", file_type -> "md")
    result = await store.reindex_metadata("col")
    assert result.updated >= 1, "Expected at least one row to be updated by reindex_metadata"

    # FTS must still find the chunk by its unique text after real row mutations
    results = await store.hybrid_search(
        "col",
        query_vector=[0.1] * _DIM,
        query_text=unique_text,
        top_k=5,
    )
    doc_ids = [r.doc_id for r in results]
    assert chunk.doc_id in doc_ids, (
        f"Expected chunk {chunk.doc_id!r} in FTS results after reindex_metadata with updates, "
        f"got: {doc_ids}"
    )

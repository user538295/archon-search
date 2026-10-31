"""BE-21 (S82) — a failing store read raises; it never reads as "this collection has no chunks".

A real LanceDB table whose data files are gone fails on read. The port calls must raise ``StoreReadError`` instead of
returning ``[]``, because ``[]`` from ``list_chunk_ids`` means the read worked and found no open chunk (the
description generator settles that as ``noUnrestrictedChunks``, a different failure from ``sampleReadFailed``).
"""
from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archon_search._types import ChunkRecord
from archon_search.description_sample import StoreReadError
from archon_search.store import SearchStore

pytestmark = pytest.mark.integration

_DIM = 4
_COLLECTION = "be21-readerror"
_DOC_ID = "d" * 64


def _chunk(idx: int) -> ChunkRecord:
    return ChunkRecord(
        doc_id=_DOC_ID,
        chunk_id=f"{_DOC_ID}-{idx:06d}",
        text=f"excerpt {idx}",
        vector=[0.1] * _DIM,
        source_path="/tmp/d.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )


@pytest.mark.asyncio
async def test_store_read_error_raises_not_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "db"
    store = SearchStore(str(db_path))
    await store.connect()
    try:
        chunks = [_chunk(0), _chunk(1)]
        await store.ensure_collection(_COLLECTION, _DIM)
        await store.ingest_chunks(_COLLECTION, chunks)
        ids = [c.chunk_id for c in chunks]
        assert await store.list_chunk_ids(_COLLECTION, "default") == ids
        assert await store.read_excerpts(_COLLECTION, "default", ids) == [c.text for c in chunks]

        data_dir = db_path / f"{_COLLECTION}.lance" / "data"
        assert any(data_dir.iterdir()), "the table must have data files to remove"
        shutil.rmtree(data_dir)

        with pytest.raises(StoreReadError):
            await store.list_chunk_ids(_COLLECTION, "default")
        with pytest.raises(StoreReadError):
            await store.read_excerpts(_COLLECTION, "default", ids)
    finally:
        await store.disconnect()

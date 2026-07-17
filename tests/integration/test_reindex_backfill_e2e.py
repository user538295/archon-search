"""End-to-end pin for ``SearchStore.reindex_metadata`` against a
Python-constructed pre-A1 collection.

Implements Task 6.4 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.

These tests are intentionally sync (no ``@pytest.mark.asyncio``): they drive
a fresh ``SearchStore`` synchronously via ``asyncio.run()`` and assert via
raw-row reads. The CLI was a proxy (CSP120) so tests now call the store
method directly.
"""
from __future__ import annotations

import asyncio
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


def _legacy_chunk(source_path: str) -> ChunkRecord:
    did = _doc_id()
    return ChunkRecord(
        doc_id=did,
        chunk_id=f"{did}-000000",
        text="pre-A1 chunk content searching",
        vector=[0.0] * _DIM,
        source_path=source_path,
        indexed_at=datetime.now(timezone.utc).isoformat(),
        ingested_by="cli",  # overwritten via table.update below
    )


async def _force_pre_a1(store: SearchStore, col: str, chunk_id: str) -> None:
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


async def _seed(store: SearchStore, col: str, source_path: str) -> str:
    await store.ensure_collection(col, _DIM)
    chunk = _legacy_chunk(source_path)
    await store.ingest_chunks(col, [chunk])
    await _force_pre_a1(store, col, chunk.chunk_id)
    return chunk.chunk_id


def _run_reindex(store: SearchStore, name: str, *, dry_run: bool = False):
    """Call store.reindex_metadata() directly (CSP120: CLI is now a proxy)."""
    return asyncio.run(store.reindex_metadata(name, dry_run=dry_run))


@pytest.fixture
def store_e2e(tmp_path: Path):
    s = SearchStore(tmp_path / "store")
    asyncio.run(s.connect())
    yield s
    asyncio.run(s.disconnect())


@pytest.mark.integration
def test_pre_a1_collection_after_reindex(
    store_e2e: SearchStore, tmp_path: Path
) -> None:
    col = f"test-{uuid.uuid4().hex[:8]}"
    src = tmp_path / "real.md"
    src.write_text("seed content")
    chunk_id = asyncio.run(_seed(store_e2e, col, str(src)))

    _run_reindex(store_e2e, col)

    row = asyncio.run(_read_raw(store_e2e, col, chunk_id))
    assert row["file_type"] == "md"
    assert row["ingested_by"] == "reindex"
    assert row["updated_at"] != ""


@pytest.mark.integration
def test_pre_a1_collection_dry_run_changes_nothing(
    store_e2e: SearchStore, tmp_path: Path
) -> None:
    col = f"test-{uuid.uuid4().hex[:8]}"
    src = tmp_path / "x.md"
    src.write_text("seed")
    chunk_id = asyncio.run(_seed(store_e2e, col, str(src)))

    _run_reindex(store_e2e, col, dry_run=True)

    row = asyncio.run(_read_raw(store_e2e, col, chunk_id))
    assert row["ingested_by"] == "archon-search-cli"
    assert row["file_type"] == ""
    assert row["updated_at"] == ""

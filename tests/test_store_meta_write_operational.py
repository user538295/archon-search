"""Operational behaviour of the collection-meta writes: log noise and table-version growth."""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archon_search._types import ChunkRecord
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.store import _META_TABLE, SearchStore

_DIM = 4
_STALE_WARNING = "centroid stale"


def _doc_id() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _chunk(doc_id: str, idx: int) -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text="hello",
        vector=[float(idx + 1)] * _DIM,
        source_path=f"/tmp/{doc_id[:8]}.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )


def _stale_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if _STALE_WARNING in r.getMessage()]


@pytest.fixture
async def store(tmp_path: Path):
    search_store = SearchStore(tmp_path / "db", config=SearchConfig())
    await search_store.connect()
    try:
        yield search_store
    finally:
        await search_store.disconnect()


async def test_first_ingest_into_a_stub_collection_is_not_a_stale_centroid_warning(
    store: SearchStore, caplog: pytest.LogCaptureFixture
) -> None:
    """POST /collections writes an empty stub meta row (no centroid yet); every ingest batch into it
    used to log "centroid stale" at WARNING — one line per document of the first ingest."""
    collection = "stub_col"
    await store.ensure_collection(collection, _DIM)
    await store.update_collection_meta(CollectionMeta(name=collection, active_embedding_model="m"))

    with caplog.at_level(logging.DEBUG, logger="archon_search.store"):
        for _ in range(3):
            result = await store.ingest_chunks(collection, [_chunk(_doc_id(), 0)], embedding_model="m")

    assert result.needs_recompute is True  # the centroid is still built by the end-of-ingest recompute
    assert not [r for r in _stale_warnings(caplog) if r.levelno >= logging.WARNING]


async def test_populated_collection_that_lost_its_centroid_still_warns(
    store: SearchStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning stays for the genuine case: chunks are indexed but the centroid sum is gone."""
    collection = "lost_col"
    await store.ensure_collection(collection, _DIM)
    await store.update_collection_meta(
        CollectionMeta(name=collection, active_embedding_model="m", chunk_count=5, doc_count=2)
    )

    with caplog.at_level(logging.WARNING, logger="archon_search.store"):
        await store.ingest_chunks(collection, [_chunk(_doc_id(), 0)], embedding_model="m")

    assert len(_stale_warnings(caplog)) == 1


async def test_meta_write_commits_one_table_version(store: SearchStore) -> None:
    """Rewriting a meta row must commit exactly one Lance version of the meta table.

    The write used to follow its merge with a legacy-orphan ``DELETE`` that matched no row and still
    committed a version: two versions (and manifest + transaction files) per write, on a table
    every ingest batch writes to and nothing compacts.
    """
    collection = "versions_col"
    await store.update_collection_meta(CollectionMeta(name=collection, active_embedding_model="m"))
    table = await store._require_connected().open_table(_META_TABLE)
    before = len(await table.list_versions())

    await store.update_collection_meta(CollectionMeta(name=collection, active_embedding_model="m", chunk_count=1))

    assert len(await table.list_versions()) - before == 1

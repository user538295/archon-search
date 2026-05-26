"""Integration pin: watcher re-ingest on file change deletes old chunks and
emits new chunks with refreshed metadata and ``ingested_by="watcher"``.

Implements Task 7.1 of Documentation/Backlog/A1-metadata-schema-v1-plan.md.

The plan asks for invoking the watcher's event-handler seam directly rather
than driving watchdog (CI timing flakiness). The "equivalent public seam"
is ``pipeline.ingest_file(..., ingested_by="watcher")``, which is exactly
what sync.py calls when watcher debounce fires. This test exercises that
seam end-to-end against a real LanceDB store.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from pathlib import Path

import pytest

from archon_search._types import ChunkRecord
from archon_search.embedder import Embedder
from archon_search.reranker import Reranker
from archon_search.store import SearchStore


class _MockEmbedderBackend:
    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts):
        return [[0.1] * 4 for _ in texts]


class _MockRerankerBackend:
    def predict(self, pairs):
        return [0.5] * len(pairs)


def _make_pipeline(store):
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    return SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )


async def _rows_for_doc(store, col, doc_id):
    db = store._require_connected()
    table = await db.open_table(col)
    return await table.query().where(f"doc_id = '{doc_id}'").to_list()


@pytest.fixture
def store_for_watcher(tmp_path: Path):
    s = SearchStore(tmp_path / "store")
    asyncio.run(s.connect())
    yield s
    asyncio.run(s.disconnect())


@pytest.mark.integration
def test_watcher_replace_no_stale_duplicates(
    store_for_watcher: SearchStore, tmp_path: Path
) -> None:
    col = f"test-{uuid.uuid4().hex[:8]}"
    md_file = tmp_path / "doc.md"
    md_file.write_text("# Original\n\nFirst content for the watcher test.\n" * 3)
    pipeline = _make_pipeline(store_for_watcher)

    async def _full() -> tuple[str, str, str]:
        # 1) Initial ingest as watcher
        first = await pipeline.ingest_file(md_file, col, ingested_by="watcher")
        rows_initial = await _rows_for_doc(store_for_watcher, col, first.doc_id)
        assert rows_initial, "initial ingest must produce rows"
        initial_updated_at = rows_initial[0]["updated_at"]
        initial_count = len(rows_initial)

        # 2) Mutate the file (content + mtime)
        await asyncio.sleep(0.05)
        md_file.write_text("# Replaced\n\nCompletely new content body.\n" * 5)
        future_mtime = time.time() + 1
        os.utime(md_file, (future_mtime, future_mtime))

        # 3) Re-ingest as watcher (same source_path -> same doc_id).
        second = await pipeline.ingest_file(md_file, col, ingested_by="watcher")
        assert second.doc_id == first.doc_id
        rows_after = await _rows_for_doc(store_for_watcher, col, second.doc_id)
        return initial_updated_at, rows_after, initial_count

    initial_updated_at, rows_after, initial_count = asyncio.run(_full())

    assert rows_after, "post-replace ingest must leave rows"
    # No stale duplicates: pipeline deletes the old doc before re-adding,
    # so the row count reflects only the new ingest.
    new_count = len(rows_after)
    # Different content + chunk_size -> likely different count; even if equal,
    # the rows are fresh (updated_at advanced and ingested_by=="watcher").
    assert all(r["ingested_by"] == "watcher" for r in rows_after)
    assert all(r["updated_at"] != initial_updated_at for r in rows_after), (
        f"updated_at must advance after file mutation; "
        f"initial={initial_updated_at!r}, after={[r['updated_at'] for r in rows_after]!r}"
    )
    assert all(r["updated_at"] > initial_updated_at for r in rows_after)
    assert all(r["file_type"] == "md" for r in rows_after)

"""Permanent regression tests for S276: watcher writes must carry ingested_by='watcher'.

Two guarantees are locked down here, both reachable from `[collections] watch = true`:

1. The server lifespan creates and starts a `WatcherManager` (`server/app.py`), so
   `GET /status` reports `readiness.watcher.running = True`.
2. A file the watcher picks up is tagged `ingested_by='watcher'` — including during the
   boot window, when the collection has a sync-state entry but no `indexed_chunk_size`
   yet. `_check_collection_changes` raises `force_full_reindex` in that window, so
   `sync_collection()` must NOT let that alone relabel the chunks as `"reindex"`.

See `Documentation/UserManual/50_ingestion_and_collections.md` ("Watcher behavior") and
`Documentation/Architecture/130_data_architecture_and_persistence.md` ("Reindex semantics").
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


class _MockEmbedder:
    """Deterministic 4-dim embedder backend — every text maps to the same vector."""

    model_name = "mock"
    is_warm = False
    embedding_dim = 4

    def encode(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _MockReranker:
    """Constant-score reranker backend — preserves retrieval order."""

    is_warm = False

    def predict(self, pairs):
        return [0.5] * len(pairs)


def test_watcher_manager_is_started_when_watch_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When [collections] watch = true and at least one collection is configured,
    the server must create and start a WatcherManager (app.state.watcher_manager
    must not be None and GET /status must report watcher.running=True).

    Guards the lifespan wiring in `server/app.py`: `create_app()` initialises
    `app.state.watcher_manager = None`, and the lifespan must overwrite it with a
    started `WatcherManager` — otherwise `readiness.watcher.running` reads False and
    filesystem changes are never picked up at all.
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "seed.md").write_text(
        "# Seed\n\nSeed content for the watcher test.\n" * 4
    )

    toml = (
        "[collections]\n"
        f'collections = ["{corpus}"]\n'
        "watch = true\n"
    )

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.get("/status", headers=headers)
        assert resp.status_code == 200, f"GET /status failed: {resp.text}"

        body = resp.json()
        readiness = body.get("readiness", {})
        watcher = readiness.get("watcher", {})

        assert watcher.get("running") is True, (
            "Expected readiness.watcher.running=True when [collections] watch=true "
            f"and a collection directory is configured; got watcher={watcher!r}. "
            "Root cause S276: WatcherManager is never created in the server lifespan."
        )


@pytest.mark.asyncio
async def test_watcher_ingested_new_file_is_tagged_watcher(tmp_path: Path) -> None:
    """A file the real watcher picks up must land with ingested_by='watcher'.

    Drives the production seam end to end: a real ``WatcherManager`` watching a
    real directory fires ``SearchCollectionSync.sync_collection()`` (exactly what
    the server lifespan's ``_watch_callback`` does), which ingests the new file
    through the real ``SearchPipeline``/``SearchStore``.

    Precondition — the collection has a state entry but no ``indexed_chunk_size``:
    that is what ``_ingest_collection`` writes while a collection is being
    indexed (sync.py, the IN_PROGRESS update), so it is the state a server is in
    whenever a file lands in a watched directory while the startup sync for that
    collection is still running. The lifespan starts the WatcherManager *before*
    it creates the startup-sync task (server/app.py), so this window is reachable
    on every boot. Per
    ``Documentation/UserManual/50_ingestion_and_collections.md`` ("Watcher
    behavior") the chunks the watcher writes carry ingested_by='watcher'.
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore
    from archon_search.sync import SearchCollectionSync
    from archon_search.watcher import WatcherManager

    chunk_size = 128
    db_path = tmp_path / "db"
    store = SearchStore(db_path)
    await store.connect()

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedder()),
        reranker=Reranker(_MockReranker()),
        chunker=DocumentChunker(chunk_size=chunk_size),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    state_store = IndexingStateStore(db_path)
    col_sync = SearchCollectionSync(
        pipeline=pipeline,
        state_store=state_store,
        chunk_size=chunk_size,
        # Mirrors the production default (config.auto_reindex_on_chunk_size_change).
        auto_reindex_on_chunk_size_change=True,
    )

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    seed = corpus / "seed.md"
    seed.write_text("# Seed\n\nSeed content for the watcher test.\n" * 4)

    col_name = "corpus"
    result = await pipeline.ingest_file(
        seed, col_name, embedder=pipeline._global_embedder, collection_root=corpus,
    )
    assert result.status == "ok", f"seed ingest failed: {result}"
    state_store.update_collection(
        col_name,
        CollectionProgress(status=IndexingStatus.IN_PROGRESS, total_files=0),
    )

    async def _on_change(name: str) -> None:
        await col_sync.sync_collection(name, corpus)

    wm = WatcherManager(
        on_change=_on_change,
        loop=asyncio.get_running_loop(),
        debounce_seconds=0.2,
    )
    wm.add(col_name, corpus)
    try:
        (corpus / "canary.md").write_text(
            "# Canary\n\nxyzzy_watcher_canary_1234 unique canary content.\n" * 4
        )

        match = None
        deadline = asyncio.get_running_loop().time() + 60.0
        while asyncio.get_running_loop().time() < deadline:
            results = await store.hybrid_search(
                col_name,
                query_vector=[0.1, 0.2, 0.3, 0.4],
                query_text="xyzzy_watcher_canary_1234",
                top_k=10,
            )
            match = next((r for r in results if "canary.md" in r.source_path), None)
            if match is not None:
                break
            await asyncio.sleep(0.2)

        assert match is not None, (
            f"watcher never made canary.md searchable in collection {col_name!r} within 60s"
        )
        assert match.ingested_by == "watcher", (
            "a file picked up by the watcher must carry ingested_by='watcher' "
            "(UserManual/50_ingestion_and_collections.md - Watcher behavior); "
            f"got {match.ingested_by!r}"
        )
    finally:
        await wm.stop_all()
        await store.disconnect()


@pytest.mark.asyncio
async def test_sync_collection_genuine_chunk_size_change_is_tagged_reindex(
    tmp_path: Path,
) -> None:
    """A genuine chunk-size change detected via sync_collection() must still be
    tagged ingested_by='reindex' — the S276 fix narrows the guard to the "prior
    size unknown" case only (a mid-flight/never-tracked collection); it must not
    also swallow real reindex triggers on an already-DONE collection.

    Precondition: the collection has a DONE state entry recording a prior
    `indexed_chunk_size` that differs from the configured chunk size, so
    `_check_collection_changes` sets `force_full_reindex=True` via the chunk-size
    guard (sync.py) with `indexed_chunk_size is not None` — the first arm of the
    `sync_collection()` guard, which must resolve to "reindex".
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore
    from archon_search.sync import SearchCollectionSync

    configured_chunk_size = 128
    db_path = tmp_path / "db"
    store = SearchStore(db_path)
    await store.connect()

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedder()),
        reranker=Reranker(_MockReranker()),
        chunker=DocumentChunker(chunk_size=configured_chunk_size),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    state_store = IndexingStateStore(db_path)
    col_sync = SearchCollectionSync(
        pipeline=pipeline,
        state_store=state_store,
        chunk_size=configured_chunk_size,
        auto_reindex_on_chunk_size_change=True,
    )

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    seed = corpus / "seed.md"
    seed.write_text(
        "# Seed document\n\nThis is the initial content for the watcher test.\n" * 4
    )

    col_name = "watcher_test_col"
    # Ingest seed directly through the pipeline (as if a prior sync already ran).
    result = await pipeline.ingest_file(
        seed, col_name, embedder=pipeline._global_embedder, ingested_by="watcher",
        collection_root=corpus,
    )
    assert result.status == "ok", f"seed ingest failed: {result}"

    # DONE state with a recorded indexed_chunk_size that differs from the configured
    # chunk size — this is the genuine-reindex trigger, not the "unknown prior size"
    # window S276 fixed.
    state_store.update_collection(
        col_name,
        CollectionProgress(
            status=IndexingStatus.DONE,
            total_files=1,
            processed_files=1,
            processed_paths=[str(seed.resolve())],
            file_mtimes={str(seed.resolve()): seed.stat().st_mtime},
            indexed_chunk_size=64,
        ),
    )

    # Write a new file so there is something for sync_collection to detect and apply.
    new_file = corpus / "new_xyzzy_canary_s276.md"
    new_file.write_text(
        "# New file\n\nxyzzy_watcher_canary_s276 unique content for S276.\n" * 4
    )

    await col_sync.sync_collection(col_name, corpus)

    results = await store.hybrid_search(
        col_name,
        query_vector=[0.1, 0.2, 0.3, 0.4],
        query_text="xyzzy_watcher_canary_s276",
        top_k=10,
    )
    canary_results = [r for r in results if "new_xyzzy_canary_s276" in r.source_path]
    assert canary_results, (
        "Expected search results for the new file after a chunk-size-triggered "
        "reindex; got no results."
    )
    for r in canary_results:
        assert r.ingested_by == "reindex", (
            f"Expected ingested_by='reindex' for a genuine chunk-size change routed "
            f"through sync_collection(); got ingested_by={r.ingested_by!r}. "
            "The S276 fix must not relabel real reindex triggers as 'watcher'."
        )

    await store.disconnect()

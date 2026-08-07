"""Integration test for S276: watcher-ingested files must carry ingested_by='watcher'.

Bug: When config.watch=True, the server never creates a WatcherManager in the
lifespan. app.state.watcher_manager is always None, so filesystem changes to
collection directories are never detected and new files are never indexed via
the watcher path.

Fix: The server lifespan must create a WatcherManager when config.watch=True,
wired to call collection_sync.sync_collection(col_name, source_path), which
already passes ingested_by='watcher' to the pipeline.

The failing assertion:
  readiness.watcher.running is True

Currently fails because watcher_manager=None → WatcherReport(running=False).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def test_watcher_manager_is_started_when_watch_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When [collections] watch = true and at least one collection is configured,
    the server must create and start a WatcherManager (app.state.watcher_manager
    must not be None and GET /status must report watcher.running=True).

    S276 root cause: app.state.watcher_manager = None is set once in create_app()
    and never overwritten — the WatcherManager is never instantiated.
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
async def test_sync_collection_uses_watcher_ingested_by(
    tmp_path: Path,
) -> None:
    """Verify that SearchCollectionSync.sync_collection() uses ingested_by='watcher'.

    This tests the correct seam: the WatcherManager callback should call
    sync_collection(), which already passes ingested_by='watcher' to the pipeline.
    This test proves the lower-level wiring is correct; the server-level test
    above proves the WatcherManager is created and connected.
    """
    import asyncio
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.progress import IndexingStateStore
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore
    from archon_search.sync import SearchCollectionSync

    class _MockEmbedder:
        model_name = "mock"
        is_warm = False
        embedding_dim = 4

        def encode(self, texts):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    class _MockReranker:
        is_warm = False

        def predict(self, pairs):
            return [0.5] * len(pairs)

    db_path = tmp_path / "db"
    store = SearchStore(db_path)
    await store.connect()

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedder()),
        reranker=Reranker(_MockReranker()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    state_store = IndexingStateStore(db_path)
    col_sync = SearchCollectionSync(
        pipeline=pipeline,
        state_store=state_store,
    )

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    seed = corpus / "seed.md"
    seed.write_text(
        "# Seed document\n\nThis is the initial content for the watcher test.\n" * 4
    )

    # Seed the collection by calling sync_collection (simulates watcher initial ingest)
    col_name = "watcher_test_col"
    from archon_search.progress import CollectionProgress, IndexingStatus
    state_store.update_collection(
        col_name,
        CollectionProgress(
            status=IndexingStatus.DONE,
            total_files=0,
            processed_files=0,
            processed_paths=[],
            file_mtimes={},
        ),
    )
    # Ingest seed file directly through the pipeline with ingested_by='watcher'
    # (this is what sync_collection/_apply_collection_changes does for new files)
    result = await pipeline.ingest_file(
        seed, col_name, embedder=pipeline._global_embedder, ingested_by="watcher",
        collection_root=corpus,
    )
    assert result.status == "ok", f"seed ingest failed: {result}"

    # Write the NEW file (simulating a user action on the watched directory)
    new_file = corpus / "new_xyzzy_canary_s276.md"
    new_file.write_text(
        "# New file\n\nxyzzy_watcher_canary_s276 unique content for S276.\n" * 4
    )

    # Update state so sync_collection will detect the new file
    from archon_search.progress import CollectionProgress, IndexingStatus
    state_store.update_collection(
        col_name,
        CollectionProgress(
            status=IndexingStatus.DONE,
            total_files=1,
            processed_files=1,
            processed_paths=[str(seed.resolve())],
            file_mtimes={str(seed.resolve()): seed.stat().st_mtime},
        ),
    )

    # Call sync_collection — this is what the WatcherManager callback should do
    await col_sync.sync_collection(col_name, corpus)

    # Search for the new file's unique content
    query_vector = [0.1, 0.2, 0.3, 0.4]
    results = await store.hybrid_search(
        col_name,
        query_vector=query_vector,
        query_text="xyzzy_watcher_canary_s276",
        top_k=10,
    )

    watcher_results = [r for r in results if "xyzzy_watcher_canary_s276" in r.source_path or "new_xyzzy_canary_s276" in r.source_path]
    assert watcher_results, (
        "Expected search results for the new file written after seed ingest; "
        "got no results. This verifies sync_collection detects and indexes the new file."
    )

    for r in watcher_results:
        assert r.ingested_by == "watcher", (
            f"Expected ingested_by='watcher' on watcher-ingested chunk; "
            f"got ingested_by={r.ingested_by!r} "
            f"(UserManual/50_ingestion_and_collections.md — Watcher behavior). "
            f"assert {r.ingested_by!r} == 'watcher'"
        )

    await store.disconnect()

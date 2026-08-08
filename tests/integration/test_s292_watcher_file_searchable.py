"""Integration test for S292: added file becomes searchable via watcher.

Bug: When config.watch=True, the server never creates a WatcherManager in the
lifespan (app.state.watcher_manager is always None). Filesystem changes are
never detected, so a new file written into a watched collection directory is
never ingested and never appears in search results.

Fix: Create WatcherManager in the server lifespan when config.watch=True,
wired to call collection_sync.sync_collection() for each configured collection.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app, ingest_file_via_path, search

pytestmark = pytest.mark.integration


def test_watcher_manager_created_when_watch_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When [collections] watch = true and at least one collection exists,
    app.state.watcher_manager must not be None.

    S292 root cause: app.state.watcher_manager = None is set in create_app()
    but never overwritten — the WatcherManager is never instantiated, so new
    files in watched directories are never detected or ingested.
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
        watcher = body.get("readiness", {}).get("watcher", {})

        assert watcher.get("running") is True, (
            "Expected readiness.watcher.running=True when [collections] watch=true "
            f"and a collection directory is configured; got watcher={watcher!r}. "
            "Root cause S292: WatcherManager is never created in the server lifespan."
        )


@pytest.mark.asyncio
async def test_new_file_becomes_searchable_via_sync_collection(
    tmp_path: Path,
) -> None:
    """After sync_collection() runs on a directory with a new file, that file
    must appear in search results with the expected fields.

    This tests the sync_collection → _check_collection_changes → _apply_collection_changes
    pipeline that the WatcherManager callback invokes.
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.progress import CollectionProgress, IndexingStatus, IndexingStateStore
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
        "# Seed document\n\nThis is the initial seed content for the watcher test.\n" * 4
    )

    col_name = "watch_e2e"

    # Ingest seed file
    result = await pipeline.ingest_file(
        seed, col_name, embedder=pipeline._global_embedder, ingested_by="watcher",
        collection_root=corpus,
    )
    assert result.status == "ok", f"seed ingest failed: {result}"

    # Record seed file in state as DONE
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

    # Write the NEW file (simulating a user adding a file to the watched directory)
    new_file = corpus / "s292_new.md"
    new_file.write_text(
        "# New document\n\nplugh_watcher_e2e_5678 unique content for S292.\n" * 4
    )

    # Call sync_collection — this is what the WatcherManager callback does
    await col_sync.sync_collection(col_name, corpus)

    # Search for the new file's unique content
    query_vector = [0.1, 0.2, 0.3, 0.4]
    results = await store.hybrid_search(
        col_name,
        query_vector=query_vector,
        query_text="plugh_watcher_e2e_5678",
        top_k=10,
    )

    # Find the result that references the new file
    match = None
    for r in results:
        if "s292_new" in r.source_path:
            match = r
            break

    assert match is not None, (
        "Watcher did not make the new file 's292_new.md' searchable; "
        f"no result with that source_path for query 'plugh_watcher_e2e_5678' "
        f"in collection 'watch_e2e'. Got {len(results)} results: "
        f"{[r.source_path for r in results]}"
    )

    # Verify documented result fields
    assert match.doc_id is not None, "doc_id must not be None"
    assert match.chunk_id is not None, "chunk_id must not be None"
    assert match.text is not None, "text must not be None"
    assert match.score is not None, "score must not be None"
    assert match.source_path is not None, "source_path must not be None"
    assert match.collection == col_name, (
        f"Expected collection={col_name!r}, got {match.collection!r}"
    )

    await store.disconnect()

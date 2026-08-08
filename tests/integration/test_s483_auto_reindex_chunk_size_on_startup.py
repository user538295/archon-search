"""S483 — auto_reindex_on_chunk_size_change must fire on server startup.

When ``auto_reindex_on_chunk_size_change = true`` and the configured
``chunk_size`` differs from the value used to index existing collections,
the server should detect the mismatch and reindex affected collections
**on the next server start**.

Root cause: ``lifespan()`` in ``app.py`` never calls
``collection_sync.sync()`` at startup.  The chunk_size change detection
logic lives in ``sync.py`` ``_diff_collection()`` (line ~408), which only
runs inside ``sync()``.  Since ``sync()`` is only triggered by
``POST /sync`` or the file watcher callback, the check never fires at
startup.
"""
from __future__ import annotations

import time

import pytest

from archon_search.progress import IndexingStateStore
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def test_auto_reindex_on_chunk_size_change_fires_on_startup(tmp_path, monkeypatch):
    """Chunk_size change between server restarts should trigger auto-reindex on startup.

    Phase 1: start server with chunk_size=128, sync one collection.
    Phase 2: restart server with chunk_size=256 + auto_reindex=true.
    Assert: after phase 2 startup, indexed_chunk_size is 256 (reindex fired).

    Regression guard: lifespan() must call sync() so the mismatch is detected.
    """
    # Set up a collection source directory with a real document
    col_dir = tmp_path / "testcol"
    col_dir.mkdir()
    doc = col_dir / "doc.txt"
    doc.write_text(
        "This is a test document with enough content for chunking. "
        "The chunk size change detection test needs real text to ingest."
    )

    col_path = str(col_dir.resolve())

    # --- Phase 1: ingest with chunk_size=128 ---
    toml_phase1 = (
        "[database]\n"
        "chunk_size = 128\n"
        "auto_reindex_on_chunk_size_change = true\n"
        "\n"
        "[collections]\n"
        f'collections = ["{col_path}"]\n'
    )

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_phase1) as (
        client,
        cfg,
        api_key,
    ):
        headers = {"Authorization": f"Bearer {api_key}"}

        # Trigger sync to ingest the collection with chunk_size=128
        resp = client.post("/sync", headers=headers)
        assert resp.status_code == 202, f"POST /sync failed: {resp.status_code} {resp.text}"
        job_id = resp.json()["job_id"]

        # Poll until the sync job completes
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            r = client.get(f"/jobs/{job_id}", headers=headers)
            assert r.status_code == 200
            status = r.json()["status"]
            if status in {"DONE", "FAILED"}:
                break
            time.sleep(0.1)
        assert r.json()["status"] == "DONE", f"Sync job failed: {r.json()}"

    # Verify state store recorded indexed_chunk_size=128
    db_dir = tmp_path / "db"
    state_store = IndexingStateStore(db_dir)
    state = state_store.read()
    assert state is not None, "State store should exist after phase 1 sync"

    col_name = "testcol"
    assert col_name in state.collections, (
        f"Collection '{col_name}' missing from state; got {list(state.collections)}"
    )
    assert state.collections[col_name].indexed_chunk_size == 128, (
        f"Phase 1: expected indexed_chunk_size=128, "
        f"got {state.collections[col_name].indexed_chunk_size}"
    )

    # --- Phase 2: restart with chunk_size=256 ---
    toml_phase2 = (
        "[database]\n"
        "chunk_size = 256\n"
        "auto_reindex_on_chunk_size_change = true\n"
        "\n"
        "[collections]\n"
        f'collections = ["{col_path}"]\n'
    )

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_phase2) as (
        _client2,
        _cfg2,
        _api_key2,
    ):
        state_after_restart = state_store.read()
        assert state_after_restart is not None, "State store should still exist after restart"
        assert col_name in state_after_restart.collections, (
            f"Collection '{col_name}' missing from state after restart"
        )

        assert state_after_restart.collections[col_name].indexed_chunk_size == 256, (
            f"S483: expected indexed_chunk_size=256 after restart with changed chunk_size, "
            f"got {state_after_restart.collections[col_name].indexed_chunk_size}"
        )

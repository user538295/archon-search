"""Suite 15 — Crash Recovery e2e tests (S15.7–S15.9).

Uses real SearchCollectionSync + real IndexingStateStore + real SearchPipeline
with the fastembed stubs from conftest.py.  No HTTP involved.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from archon_search.progress import (
    CollectionProgress,
    IndexingState,
    IndexingStateStore,
    IndexingStatus,
)
from archon_search.sync import SearchCollectionSync


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

class MockEmbedderBackend:
    model_name: str = "mock-embedder"

    def encode(self, texts: list[str]) -> list[list[float]]:
        import numpy as np
        # 384-dim zeros match the conftest _FakeTextEmbedding to avoid schema divergence
        # when sharing the module-scoped connected_store with other test functions.
        return [np.zeros(384, dtype=float).tolist() for _ in texts]


class MockRerankerBackend:
    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5] * len(pairs)


def make_pipeline(store):  # type: ignore[no-untyped-def]
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    return SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )


# ---------------------------------------------------------------------------
# S15.7 — IN_PROGRESS from prior crash is reset to PENDING before sync
# ---------------------------------------------------------------------------

class TestS15_7_CrashRecoveryResetInProgress:
    """S15.7: IndexingStateStore has IN_PROGRESS from prior crash → reset to PENDING."""

    @pytest.mark.asyncio
    async def test_sync_resets_stale_in_progress_and_completes(self, connected_store, tmp_path):
        """Integration test: sync() calls _reset_stale_in_progress() before indexing.

        Proof: seed IN_PROGRESS state with processed_files=3 for a fresh directory.
        If reset is skipped, sync() would treat the collection as resumable from 3 files
        (resume_offset=3 → exclude_set would be populated from processed_paths).
        With the reset, state becomes PENDING → clean ingest → DONE with correct counts.
        We assert DONE to prove the reset path was exercised end-to-end.
        """
        state_store = IndexingStateStore(tmp_path / "state")

        source_dir = tmp_path / "myproject"
        source_dir.mkdir()
        (source_dir / "doc.md").write_text("# Hello\n\nSome content here.\n" * 5)

        # Pre-seed a stale IN_PROGRESS entry (simulating a prior crash)
        stale = IndexingState(collections={
            "myproject": CollectionProgress(
                status=IndexingStatus.IN_PROGRESS,
                total_files=10,
                processed_files=3,
            )
        })
        state_store.write(stale)

        pipeline = make_pipeline(connected_store)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        # sync() calls _reset_stale_in_progress() before indexing begins (line 130 of sync.py)
        result = await syncer.sync([str(source_dir)])

        assert result.errors == [], f"Unexpected errors during sync: {result.errors}"

        # Collection must be DONE with correct file count (1, not the stale 3) —
        # proves the reset happened: if reset were skipped, processed_files would stay at 3
        # (the stale value) rather than reflecting the actual 1 file that was indexed.
        final_state = state_store.read()
        assert final_state is not None
        assert "myproject" in final_state.collections, (
            f"Collection 'myproject' missing from state after sync. "
            f"result.added={result.added}, result.errors={result.errors}"
        )
        cp = final_state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE, (
            f"Expected DONE after sync with stale IN_PROGRESS reset, got {cp.status}"
        )
        assert cp.processed_files == 1, (
            f"Expected processed_files=1 (actual file count) after reset, "
            f"got {cp.processed_files} — stale value 3 would indicate reset was skipped"
        )

    def test_reset_stale_in_progress_unit(self, connected_store, tmp_path):
        """Unit test for _reset_stale_in_progress(): IN_PROGRESS → PENDING, preserving fields."""
        state_store = IndexingStateStore(tmp_path / "state_unit")
        stale = IndexingState(collections={
            "col_a": CollectionProgress(
                status=IndexingStatus.IN_PROGRESS,
                total_files=5,
                processed_files=2,
                processed_paths=["/some/path/a.md", "/some/path/b.md"],
            )
        })
        state_store.write(stale)

        pipeline = make_pipeline(connected_store)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        syncer._reset_stale_in_progress()

        reset_state = state_store.read()
        assert reset_state is not None
        assert "col_a" in reset_state.collections
        cp = reset_state.collections["col_a"]
        assert cp.status == IndexingStatus.PENDING, (
            f"Expected PENDING after crash recovery reset, got {cp.status}"
        )
        # Preserved fields survive the reset
        assert cp.total_files == 5
        assert cp.processed_files == 2
        assert cp.processed_paths == ["/some/path/a.md", "/some/path/b.md"]

    @pytest.mark.asyncio
    async def test_done_status_not_reset_on_sync(self, connected_store, tmp_path):
        """S15.7 boundary: DONE collections are left untouched by _reset_stale_in_progress."""
        state_store = IndexingStateStore(tmp_path / "state")
        done_state = IndexingState(collections={
            "finished_col": CollectionProgress(
                status=IndexingStatus.DONE,
                total_files=3,
                processed_files=3,
            )
        })
        state_store.write(done_state)

        pipeline = make_pipeline(connected_store)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        syncer._reset_stale_in_progress()

        after = state_store.read()
        assert after is not None
        assert after.collections["finished_col"].status == IndexingStatus.DONE


# ---------------------------------------------------------------------------
# S15.8 — processed_paths causes already-indexed files to be skipped
# ---------------------------------------------------------------------------

class TestS15_8_ResumeSkipsProcessedPaths:
    """S15.8: processed_paths in state → sync resumes, skipping already-indexed files."""

    @pytest.mark.asyncio
    async def test_resume_skips_already_indexed_files(self, connected_store, tmp_path):
        source_dir = tmp_path / "docs"
        source_dir.mkdir()
        file_a = source_dir / "a.md"
        file_b = source_dir / "b.md"
        file_a.write_text("# A\n\nContent of A.\n" * 5)
        file_b.write_text("# B\n\nContent of B.\n" * 5)

        state_store = IndexingStateStore(tmp_path / "state")
        col_name = "docs"

        # Simulate that file_a was already processed in a prior (interrupted) run
        prior_state = IndexingState(collections={
            col_name: CollectionProgress(
                status=IndexingStatus.PENDING,  # reset from IN_PROGRESS already
                total_files=2,
                processed_files=1,
                processed_paths=[str(file_a.resolve())],
            )
        })
        state_store.write(prior_state)

        pipeline = make_pipeline(connected_store)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        # Patch ingest_directory to capture which paths are excluded
        excluded_in_call: list[frozenset] = []
        original_ingest = pipeline.ingest_directory

        async def capturing_ingest(path, col, **kwargs):
            excluded_in_call.append(kwargs.get("exclude_paths", frozenset()))
            return await original_ingest(path, col, **kwargs)

        pipeline.ingest_directory = capturing_ingest

        result = await syncer.sync([str(source_dir)])

        # Sync should complete without error
        assert result.errors == []

        # The already-processed file should have been in the exclude set
        assert len(excluded_in_call) >= 1, "ingest_directory was never called — cannot verify exclusion"
        assert str(file_a.resolve()) in excluded_in_call[0], (
            "file_a should have been excluded (already in processed_paths)"
        )

        # Final state should be DONE
        final_state = state_store.read()
        assert final_state is not None
        assert col_name in final_state.collections
        assert final_state.collections[col_name].status == IndexingStatus.DONE


# ---------------------------------------------------------------------------
# S15.9 — Registered path doesn't exist → warning logged, no crash
# ---------------------------------------------------------------------------

class TestS15_9_MissingPathNocrash:
    """S15.9: collection path doesn't exist on disk → sync logs warning, doesn't crash."""

    @pytest.mark.asyncio
    async def test_nonexistent_path_logged_not_crashed(self, connected_store, tmp_path):
        missing_dir = tmp_path / "ghost" / "nowhere"
        # Deliberately do NOT create this directory

        state_store = IndexingStateStore(tmp_path / "state")
        pipeline = make_pipeline(connected_store)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        result = await syncer.sync([str(missing_dir)])

        # Must not raise; path error captured in result.errors
        assert any("does not exist" in e for e in result.errors), (
            f"Expected 'does not exist' in errors, got: {result.errors}"
        )
        # Nothing should have been added
        assert result.added == []

        # State store must not contain a stale entry for the missing collection
        final_state = state_store.read()
        missing_col_name = "nowhere"  # path_to_collection_name("ghost/nowhere") → "nowhere"
        if final_state is not None:
            assert missing_col_name not in final_state.collections, (
                f"State store must not contain a stale entry for the missing collection "
                f"'{missing_col_name}'; got collections: {list(final_state.collections.keys())}"
            )

    @pytest.mark.asyncio
    async def test_nonexistent_path_with_valid_path_ok(self, connected_store, tmp_path):
        """S15.9 compound: one missing path + one valid path → valid one succeeds."""
        missing_dir = tmp_path / "ghost"
        valid_dir = tmp_path / "real"
        valid_dir.mkdir()
        (valid_dir / "note.md").write_text("# Note\n\nContent.\n" * 5)

        state_store = IndexingStateStore(tmp_path / "state2")
        pipeline = make_pipeline(connected_store)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        result = await syncer.sync([str(missing_dir), str(valid_dir)])

        # Missing path produces an error entry
        assert any("does not exist" in e for e in result.errors)
        # Valid path is added
        assert "real" in result.added


# ---------------------------------------------------------------------------
# S15.1 — New directory → sync starts ingest, state → IN_PROGRESS → DONE
# ---------------------------------------------------------------------------

class TestS15_1_NewDirectoryIngest:
    """S15.1: directory registered but not yet indexed → sync() ingests it, state → DONE."""

    @pytest.mark.asyncio
    async def test_sync_adds_new_collection(self, connected_store, tmp_path):
        """New directory not yet in LanceDB → ingest runs, result.added contains collection."""
        source_dir = tmp_path / "newproject"
        source_dir.mkdir()
        (source_dir / "readme.md").write_text("# New Project\n\nContent here.\n" * 5)

        state_store = IndexingStateStore(tmp_path / "state")
        pipeline = make_pipeline(connected_store)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        # Spy on all state_store.write() calls to verify IN_PROGRESS intermediate state
        writes = []
        _orig_write = state_store.write

        def _spy_write(state):
            import copy
            writes.append(copy.deepcopy(state))
            return _orig_write(state)

        state_store.write = _spy_write

        result = await syncer.sync([str(source_dir)])

        assert result.errors == [], f"Unexpected errors: {result.errors}"
        assert "newproject" in result.added, f"Expected 'newproject' in added, got: {result.added}"

        # Verify IN_PROGRESS was written at some point during ingest
        in_progress_writes = [
            w for w in writes
            if "newproject" in w.collections
            and w.collections["newproject"].status == IndexingStatus.IN_PROGRESS
        ]
        assert in_progress_writes, "Expected at least one write with IN_PROGRESS status during ingest"

        # State must be DONE after a successful ingest
        final_state = state_store.read()
        assert final_state is not None
        assert "newproject" in final_state.collections
        assert final_state.collections["newproject"].status == IndexingStatus.DONE


# ---------------------------------------------------------------------------
# S15.2 — DONE + no changes → sync skips (unchanged)
# ---------------------------------------------------------------------------

class TestS15_2_DoneNoChangesSkipped:
    """S15.2: collection state = DONE, no file changes → sync does not reindex."""

    @pytest.mark.asyncio
    async def test_sync_skips_already_done_collection(self, connected_store, tmp_path):
        """After first successful sync, second sync with no file changes → result.unchanged."""
        source_dir = tmp_path / "stable"
        source_dir.mkdir()
        doc = source_dir / "doc.md"
        doc.write_text("# Stable\n\nContent.\n" * 5)

        state_store = IndexingStateStore(tmp_path / "state")
        pipeline = make_pipeline(connected_store)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        # First sync — ingest the collection
        result1 = await syncer.sync([str(source_dir)])
        assert "stable" in result1.added, f"First sync should add 'stable'; got added={result1.added}"
        assert result1.errors == []

        # Second sync — nothing changed, should be unchanged
        result2 = await syncer.sync([str(source_dir)])
        assert result2.errors == [], f"Unexpected errors on second sync: {result2.errors}"
        assert "stable" in result2.unchanged, (
            f"Expected 'stable' in unchanged on second sync; got unchanged={result2.unchanged}, "
            f"updated={result2.updated}, added={result2.added}"
        )
        assert "stable" not in result2.updated, "No file changed — should not be in updated"


# ---------------------------------------------------------------------------
# S15.3 — File modified → incremental update
# ---------------------------------------------------------------------------

class TestS15_3_FileModifiedIncrementalUpdate:
    """S15.3: one file modified since last index → sync starts incremental update."""

    @pytest.mark.asyncio
    async def test_sync_reindexes_on_file_change(self, connected_store, tmp_path):
        """Modify a file's mtime after first sync → second sync detects change → result.updated."""
        import time

        source_dir = tmp_path / "evolving"
        source_dir.mkdir()
        doc = source_dir / "evolving.md"
        doc.write_text("# Evolving\n\nOriginal content.\n" * 5)

        state_store = IndexingStateStore(tmp_path / "state")
        pipeline = make_pipeline(connected_store)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        # First sync
        result1 = await syncer.sync([str(source_dir)])
        assert "evolving" in result1.added, f"First sync should add 'evolving'; got {result1.added}"
        assert result1.errors == []

        # Simulate file modification: update content + ensure new mtime
        time.sleep(0.01)
        doc.write_text("# Evolving\n\nUpdated content.\n" * 5)
        # Force a distinct mtime by updating it explicitly
        new_mtime = doc.stat().st_mtime + 1.0
        import os
        os.utime(str(doc), (new_mtime, new_mtime))

        # Second sync — should detect file change → incremental update
        result2 = await syncer.sync([str(source_dir)])
        assert result2.errors == [], f"Unexpected errors: {result2.errors}"
        assert "evolving" in result2.updated, (
            f"Expected 'evolving' in updated after file modification; "
            f"got updated={result2.updated}, unchanged={result2.unchanged}"
        )

        # Verify state is updated with new mtime
        state_after = state_store.read()
        assert state_after is not None
        assert state_after.collections["evolving"].status == IndexingStatus.DONE


# ---------------------------------------------------------------------------
# S15.4 — Embedding model changed → full reindex
# ---------------------------------------------------------------------------

class TestS15_4_EmbeddingModelChangedFullReindex:
    """S15.4: indexed_embedding_model in state differs from config → full reindex."""

    @pytest.mark.asyncio
    async def test_sync_reindexes_on_embedding_model_change(self, connected_store, tmp_path):
        """After indexing with model A, switch to model B → full reindex (result.updated)."""
        source_dir = tmp_path / "modelchange"
        source_dir.mkdir()
        (source_dir / "doc.md").write_text("# Doc\n\nContent.\n" * 5)

        state_store = IndexingStateStore(tmp_path / "state")

        # First sync with model "model-a"
        pipeline = make_pipeline(connected_store)
        syncer_a = SearchCollectionSync(
            pipeline,
            state_store=state_store,
            embedding_model="model-a",
        )
        result1 = await syncer_a.sync([str(source_dir)])
        assert "modelchange" in result1.added, f"First sync should add; got {result1.added}"
        assert result1.errors == []

        # Verify state records model-a
        state = state_store.read()
        assert state is not None
        assert state.collections["modelchange"].indexed_embedding_model == "model-a"

        # Second sync with model "model-b" → must force full reindex
        syncer_b = SearchCollectionSync(
            pipeline,
            state_store=state_store,
            embedding_model="model-b",
        )
        result2 = await syncer_b.sync([str(source_dir)])
        assert result2.errors == [], f"Unexpected errors: {result2.errors}"
        assert "modelchange" in result2.updated, (
            f"Expected full reindex when embedding model changes; "
            f"got updated={result2.updated}, unchanged={result2.unchanged}"
        )

        # State must now record the new model
        state2 = state_store.read()
        assert state2 is not None
        assert state2.collections["modelchange"].indexed_embedding_model == "model-b"


# ---------------------------------------------------------------------------
# S15.5 — chunk_size changed → full reindex
# ---------------------------------------------------------------------------

class TestS15_5_ChunkSizeChangedFullReindex:
    """S15.5: indexed_chunk_size differs from config → full reindex (with auto_reindex=True)."""

    @pytest.mark.asyncio
    async def test_sync_reindexes_on_chunk_size_change(self, connected_store, tmp_path):
        """After indexing with chunk_size=128, switch to chunk_size=256 → full reindex."""
        source_dir = tmp_path / "chunkchange"
        source_dir.mkdir()
        (source_dir / "doc.md").write_text("# Doc\n\nContent.\n" * 5)

        state_store = IndexingStateStore(tmp_path / "state")

        # First sync with chunk_size=128
        pipeline = make_pipeline(connected_store)
        syncer_128 = SearchCollectionSync(
            pipeline,
            state_store=state_store,
            chunk_size=128,
            auto_reindex_on_chunk_size_change=True,
        )
        result1 = await syncer_128.sync([str(source_dir)])
        assert "chunkchange" in result1.added, f"First sync should add; got {result1.added}"
        assert result1.errors == []

        state = state_store.read()
        assert state is not None
        assert state.collections["chunkchange"].indexed_chunk_size == 128

        # Second sync with chunk_size=256 + auto_reindex → must force full reindex
        syncer_256 = SearchCollectionSync(
            pipeline,
            state_store=state_store,
            chunk_size=256,
            auto_reindex_on_chunk_size_change=True,
        )
        result2 = await syncer_256.sync([str(source_dir)])
        assert result2.errors == [], f"Unexpected errors: {result2.errors}"
        assert "chunkchange" in result2.updated, (
            f"Expected full reindex when chunk_size changes with auto_reindex=True; "
            f"got updated={result2.updated}, unchanged={result2.unchanged}"
        )

        state2 = state_store.read()
        assert state2 is not None
        assert state2.collections["chunkchange"].indexed_chunk_size == 256


# ---------------------------------------------------------------------------
# S15.5b — chunk_size changed but auto_reindex=False → no reindex
# ---------------------------------------------------------------------------

class TestS15_5b_ChunkSizeNoAutoReindex:
    """S15.5b: chunk_size changed but auto_reindex_on_chunk_size_change=False → no reindex."""

    @pytest.mark.asyncio
    async def test_sync_skips_reindex_when_auto_reindex_disabled(self, connected_store, tmp_path):
        """chunk_size mismatch with auto_reindex=False → collection stays unchanged, not updated."""
        source_dir = tmp_path / "chunknoauto"
        source_dir.mkdir()
        (source_dir / "doc.md").write_text("# Doc\n\nContent.\n" * 5)

        state_store = IndexingStateStore(tmp_path / "state")
        pipeline = make_pipeline(connected_store)

        syncer_128 = SearchCollectionSync(
            pipeline,
            state_store=state_store,
            chunk_size=128,
            auto_reindex_on_chunk_size_change=False,
        )
        result1 = await syncer_128.sync([str(source_dir)])
        assert "chunknoauto" in result1.added
        assert result1.errors == []

        # Second sync with different chunk_size but auto_reindex=False → should NOT reindex
        syncer_256 = SearchCollectionSync(
            pipeline,
            state_store=state_store,
            chunk_size=256,
            auto_reindex_on_chunk_size_change=False,
        )
        result2 = await syncer_256.sync([str(source_dir)])
        assert result2.errors == [], f"Unexpected errors: {result2.errors}"
        assert "chunknoauto" not in result2.updated, (
            f"Expected no reindex when auto_reindex=False; got updated={result2.updated}"
        )
        assert "chunknoauto" in result2.unchanged, (
            f"Expected 'chunknoauto' in unchanged when auto_reindex=False; got unchanged={result2.unchanged}"
        )
        # Verify old chunk_size is preserved in state (no reindex occurred)
        state2 = state_store.read()
        assert state2 is not None
        assert state2.collections["chunknoauto"].indexed_chunk_size == 128


# ---------------------------------------------------------------------------
# S15.6 — Collection removed from config → drops LanceDB table, cleans state
# ---------------------------------------------------------------------------

class TestS15_6_CollectionRemovedFromConfig:
    """S15.6: collection removed from config → drops LanceDB table and cleans state."""

    @pytest.mark.asyncio
    async def test_sync_removes_deleted_collection(self, connected_store, tmp_path):
        """After indexing, remove from config → second sync drops LanceDB table + state."""
        source_dir = tmp_path / "tobedeleted"
        source_dir.mkdir()
        (source_dir / "doc.md").write_text("# To Be Deleted\n\nContent.\n" * 5)

        state_store = IndexingStateStore(tmp_path / "state")
        pipeline = make_pipeline(connected_store)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        # First sync — add the collection
        result1 = await syncer.sync([str(source_dir)])
        assert "tobedeleted" in result1.added, f"First sync should add; got {result1.added}"
        assert result1.errors == []

        # Verify collection exists in state
        state = state_store.read()
        assert state is not None
        assert "tobedeleted" in state.collections

        # Verify LanceDB table exists
        existing = await connected_store.list_collections()
        names = {c.name for c in existing}
        assert "tobedeleted" in names, f"LanceDB table should exist after ingest; got {names}"

        # Verify table actually has data (not a phantom empty table)
        table_info = next(c for c in existing if c.name == "tobedeleted")
        assert table_info.chunk_count > 0, (
            f"Expected ingested chunks in table before removal; got chunk_count={table_info.chunk_count}"
        )

        # Second sync with empty collection list — "tobedeleted" is removed from config
        result2 = await syncer.sync([])
        assert result2.errors == [], f"Unexpected errors: {result2.errors}"
        assert "tobedeleted" in result2.removed, (
            f"Expected 'tobedeleted' in removed; got removed={result2.removed}"
        )

        # LanceDB table must be gone
        existing2 = await connected_store.list_collections()
        names2 = {c.name for c in existing2}
        assert "tobedeleted" not in names2, (
            f"LanceDB table should be dropped after collection removed from config; got {names2}"
        )

        # State must be cleaned
        state2 = state_store.read()
        assert state2 is None or "tobedeleted" not in state2.collections, (
            f"State entry should be removed after collection dropped; "
            f"got collections={list(state2.collections.keys()) if state2 else 'None'}"
        )


# ---------------------------------------------------------------------------
# S15.10 — Legacy path ~/$archon/history → migrated to .../sessions table
# ---------------------------------------------------------------------------

class TestS15_10_LegacyHistoryPathMigration:
    """S15.10: LanceDB table 'archon-history' exists → _maybe_migrate renames to 'sessions'."""

    @pytest.mark.asyncio
    async def test_sync_migrates_archon_history_to_sessions_subpath(self, connected_store, tmp_path):
        """When archon-history table exists but sessions does not, sync migrates it."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from archon_search._types import CollectionInfo

        # Create a mock pipeline with archon-history in LanceDB but no sessions table
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "note.md").write_text("# Note\n\nSome history content.\n" * 5)

        mock_pipeline = MagicMock()
        mock_pipeline.store._db_path = tmp_path / "db"
        (tmp_path / "db").mkdir(parents=True, exist_ok=True)

        # Simulate: archon-history exists, sessions does not
        mock_pipeline.store.list_collections = AsyncMock(
            side_effect=[
                # First call (before migration)
                [CollectionInfo(name="archon-history", doc_count=5, chunk_count=50)],
                # Second call (after migration refresh)
                [CollectionInfo(name="sessions", doc_count=5, chunk_count=50)],
            ]
        )
        mock_pipeline.store.rename_collection = AsyncMock()
        mock_pipeline.store.drop_collection = AsyncMock()
        mock_pipeline.store.rebuild_fts_index = AsyncMock()
        mock_pipeline.store.delete_by_source_path = AsyncMock()
        mock_pipeline.ingest_directory = AsyncMock(return_value=[])
        mock_pipeline.ingest_file = AsyncMock(return_value=MagicMock(status="ok"))
        mock_pipeline.recompute_collection_meta = AsyncMock()

        state_store = IndexingStateStore(tmp_path / "state")
        syncer = SearchCollectionSync(mock_pipeline, state_store=state_store)

        result = await syncer.sync([str(sessions_dir)])

        # rename_collection must have been called with archon-history → sessions
        mock_pipeline.store.rename_collection.assert_called_once_with(
            "archon-history", "sessions"
        )
        mock_pipeline.store.drop_collection.assert_not_called()
        # No errors from the migration itself
        assert result.errors == [], f"Expected no errors; got: {result.errors}"


# ---------------------------------------------------------------------------
# S15.10b — Both archon-history and sessions exist → migration skipped
# ---------------------------------------------------------------------------

class TestS15_10b_BothTablesExistNoMigration:
    """S15.10b: both archon-history and sessions exist → migration is skipped (no rename)."""

    @pytest.mark.asyncio
    async def test_sync_skips_migration_when_sessions_already_exists(self, connected_store, tmp_path):
        """When both archon-history and sessions exist, _maybe_migrate must NOT rename."""
        from unittest.mock import AsyncMock, MagicMock

        from archon_search._types import CollectionInfo

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "note.md").write_text("# Note\n\nContent.\n" * 5)

        mock_pipeline = MagicMock()
        mock_pipeline.store._db_path = tmp_path / "db"
        (tmp_path / "db").mkdir(parents=True, exist_ok=True)

        # Both tables exist — migration should be skipped
        mock_pipeline.store.list_collections = AsyncMock(
            return_value=[
                CollectionInfo(name="archon-history", doc_count=5, chunk_count=50),
                CollectionInfo(name="sessions", doc_count=3, chunk_count=30),
            ]
        )
        mock_pipeline.store.rename_collection = AsyncMock()
        mock_pipeline.store.drop_collection = AsyncMock()
        mock_pipeline.store.rebuild_fts_index = AsyncMock()
        mock_pipeline.store.delete_by_source_path = AsyncMock()
        mock_pipeline.ingest_directory = AsyncMock(return_value=[])
        mock_pipeline.ingest_file = AsyncMock(return_value=MagicMock(status="ok"))
        mock_pipeline.recompute_collection_meta = AsyncMock()

        state_store = IndexingStateStore(tmp_path / "state")
        syncer = SearchCollectionSync(mock_pipeline, state_store=state_store)

        result = await syncer.sync([str(sessions_dir)])

        # rename_collection must NOT be called when both tables exist
        mock_pipeline.store.rename_collection.assert_not_called()
        assert result.errors == [], f"Expected no errors; got: {result.errors}"

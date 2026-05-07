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

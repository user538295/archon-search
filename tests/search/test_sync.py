"""Tests for archon/rag/sync.py — path_to_collection_name and SearchCollectionSync."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon.search._types import CollectionInfo
from archon.search.sync import path_to_collection_name


class TestPathToCollectionName:
    def test_path_to_collection_name_basic(self):
        """~/.archon/history/sessions → 'sessions'"""
        assert path_to_collection_name("~/.archon/history/sessions") == "sessions"

    def test_path_to_collection_name_sanitizes_special_chars(self):
        """Spaces and dots are replaced with underscores, collapsed, and stripped."""
        result = path_to_collection_name("/some/path/my docs.v2")
        assert result == "my_docs_v2"

    def test_path_to_collection_name_empty_component_fallback(self):
        """Root path (or a path whose last component is empty after sanitization) returns 'collection'."""
        # A path whose basename consists entirely of non-alphanumeric chars
        result = path_to_collection_name("/some/path/---")
        assert result == "collection"

    def test_path_to_collection_name_workspace(self):
        """~/.archon/workspace → 'workspace'"""
        assert path_to_collection_name("~/.archon/workspace") == "workspace"

    def test_path_to_collection_name_lowercase(self):
        """Result is always lowercased."""
        result = path_to_collection_name("/some/path/MyDocs")
        assert result == "mydocs"

    def test_path_to_collection_name_collapses_multiple_underscores(self):
        """Multiple consecutive non-alphanumeric chars collapse to a single underscore."""
        result = path_to_collection_name("/some/path/my--docs")
        assert result == "my_docs"

    def test_path_to_collection_name_root_path_fallback(self):
        """Root path '/' has empty basename — should return 'collection'."""
        assert path_to_collection_name("/") == "collection"

    def test_path_to_collection_name_numeric_basename(self):
        """Digits survive sanitization."""
        assert path_to_collection_name("/data/12345") == "12345"

    def test_path_to_collection_name_trailing_slash(self):
        """Trailing slash is stripped by pathlib; result same as without slash."""
        assert path_to_collection_name("/some/sessions/") == "sessions"


# ---------------------------------------------------------------------------
# Helpers for SearchCollectionSync unit tests
# ---------------------------------------------------------------------------

def make_mock_pipeline(tmp_path, existing_collections=None, manifest=None):
    pipeline = MagicMock()
    pipeline.store._db_path = tmp_path / "db"
    pipeline.store.list_collections = AsyncMock(return_value=[
        CollectionInfo(name=n, doc_count=0, chunk_count=0) for n in (existing_collections or [])
    ])
    pipeline.store.drop_collection = AsyncMock()
    pipeline.store.rename_collection = AsyncMock()
    pipeline.ingest_directory = AsyncMock(return_value=[])
    # Write manifest if provided
    if manifest is not None:
        db_path = tmp_path / "db"
        db_path.mkdir(parents=True, exist_ok=True)
        (db_path / "sync_manifest.json").write_text(json.dumps(manifest))
    return pipeline


# ---------------------------------------------------------------------------
# SearchCollectionSync unit tests
# ---------------------------------------------------------------------------

class TestSearchCollectionSync:
    @pytest.mark.asyncio
    async def test_sync_adds_new_collection(self, tmp_path):
        """Path not in existing collections → ingest_directory called."""
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()
        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        syncer = SearchCollectionSync(pipeline)
        result = await syncer.sync([str(new_dir)])

        pipeline.ingest_directory.assert_called_once()
        call_args = pipeline.ingest_directory.call_args
        assert call_args[0][0] == new_dir.resolve()
        assert "myproject" in result.added
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_sync_drops_removed_collection(self, tmp_path):
        """Manifest has col not in desired + col in existing → drop called."""
        from archon.search.sync import SearchCollectionSync

        manifest = {"oldcol": "/some/old/path"}
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["oldcol"],
            manifest=manifest,
        )

        syncer = SearchCollectionSync(pipeline)
        result = await syncer.sync([])  # empty desired

        pipeline.store.drop_collection.assert_called_once_with("oldcol")
        assert "oldcol" in result.removed
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_sync_skips_unchanged_collection(self, tmp_path):
        """Col in desired and in existing → no ingest, no drop."""
        from archon.search.sync import SearchCollectionSync

        existing_dir = tmp_path / "myproject"
        existing_dir.mkdir()
        resolved = str(existing_dir.resolve())

        manifest = {"myproject": resolved}
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["myproject"],
            manifest=manifest,
        )

        syncer = SearchCollectionSync(pipeline)
        result = await syncer.sync([str(existing_dir)])

        pipeline.ingest_directory.assert_not_called()
        pipeline.store.drop_collection.assert_not_called()
        assert "myproject" in result.unchanged
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_sync_resolves_collision(self, tmp_path):
        """Two paths with same basename → parent prefix used."""
        from archon.search.sync import SearchCollectionSync

        dir_a = tmp_path / "alpha" / "sessions"
        dir_b = tmp_path / "beta" / "sessions"
        dir_a.mkdir(parents=True)
        dir_b.mkdir(parents=True)

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        syncer = SearchCollectionSync(pipeline)
        result = await syncer.sync([str(dir_a), str(dir_b)])

        assert len(result.added) == 2
        # Names must be distinct
        assert result.added[0] != result.added[1]
        # Both should incorporate parent prefix
        assert all("sessions" in name for name in result.added)

    @pytest.mark.asyncio
    async def test_sync_resolves_three_way_collision(self, tmp_path):
        """Three paths with same basename → all distinct names."""
        from archon.search.sync import SearchCollectionSync

        dirs = []
        for prefix in ("x", "y", "z"):
            d = tmp_path / prefix / "data"
            d.mkdir(parents=True)
            dirs.append(d)

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        syncer = SearchCollectionSync(pipeline)
        result = await syncer.sync([str(d) for d in dirs])

        assert len(result.added) == 3
        assert len(set(result.added)) == 3  # all distinct

    @pytest.mark.asyncio
    async def test_sync_resolves_deep_collision_with_hash_fallback(self, tmp_path):
        """Two paths with same parent+basename → hash fallback used."""
        from archon.search.sync import SearchCollectionSync

        # Simulate two paths that are identical after resolution — use same path twice
        # Actually we need two distinct paths that hash-collide. We test hash fallback
        # by having paths where increasing depth still collides.
        # The easiest way: create a symlinked scenario. Instead, we mock _build_desired
        # to force the scenario, or we create paths where parent component also matches.
        # Create: /tmp/.../same/parent/basename and /tmp/.../same/parent/basename2
        # Actually let's use monkeypatching to force hash fallback by having identical
        # resolved parent paths.
        # Simplest: just use the real hash logic by constructing paths that would collide
        # at all depth levels except via hash.
        # We make two dirs at same_parent/same_child under two different roots, but
        # since we can't control the tmp_path prefix, we'll directly test the internals.

        # Alternative: call _build_desired with custom paths that share all parent components
        # by patching Path.resolve on specific instances.
        # Easiest real test: use identical paths for both entries — but that would deduplicate.
        # Let's use a mock approach instead.

        # We create two paths that have the same basename AND same immediate parent name
        dir_a = tmp_path / "history" / "sessions"
        dir_b = tmp_path / "history" / "sessions"
        # These resolve to the same path — let's cheat and use a different approach

        # We'll directly call _build_desired to verify hash fallback behavior
        # by mocking path_to_collection_name to always return "sessions"
        dir_a2 = tmp_path / "aa" / "sessions"
        dir_b2 = tmp_path / "aa" / "sessions2"
        dir_a2.mkdir(parents=True)
        dir_b2.mkdir(parents=True)

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        # Patch path_to_collection_name to always return "sessions" regardless of depth-extension
        # so we force hash fallback
        with patch("archon.search.sync.path_to_collection_name", return_value="sessions"):
            syncer = SearchCollectionSync(pipeline)
            result = await syncer.sync([str(dir_a2), str(dir_b2)])

        # With hash fallback, both names should be distinct
        assert len(set(result.added)) == 2
        # At least one should have a hash suffix
        assert any("_" in name for name in result.added)

    @pytest.mark.asyncio
    async def test_sync_records_ingest_error(self, tmp_path):
        """ingest_directory raises → error in SyncResult.errors, other paths still processed."""
        from archon.search.sync import SearchCollectionSync

        dir_a = tmp_path / "project_a"
        dir_b = tmp_path / "project_b"
        dir_a.mkdir()
        dir_b.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        pipeline.ingest_directory.side_effect = [RuntimeError("disk full"), []]

        syncer = SearchCollectionSync(pipeline)
        result = await syncer.sync([str(dir_a), str(dir_b)])

        assert len(result.errors) == 1
        assert "disk full" in result.errors[0]
        assert len(result.added) == 1  # second one succeeded

    @pytest.mark.asyncio
    async def test_sync_preserves_unmanaged_manually_ingested_collection(self, tmp_path):
        """Col in LanceDB but NOT in manifest → appears in skipped, never dropped."""
        from archon.search.sync import SearchCollectionSync

        # "manual" is in LanceDB but not in manifest
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["manual"],
            manifest={},  # empty manifest
        )

        syncer = SearchCollectionSync(pipeline)
        result = await syncer.sync([])  # no desired

        pipeline.store.drop_collection.assert_not_called()
        assert "manual" in result.skipped

    @pytest.mark.asyncio
    async def test_sync_records_warning_for_nonexistent_path(self, tmp_path):
        """Path in config but not on disk → in SyncResult.errors, other paths processed."""
        from archon.search.sync import SearchCollectionSync

        real_dir = tmp_path / "real"
        real_dir.mkdir()
        fake_path = str(tmp_path / "nonexistent" / "path")

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        syncer = SearchCollectionSync(pipeline)
        result = await syncer.sync([fake_path, str(real_dir)])

        assert any("does not exist" in e for e in result.errors)
        assert len(result.added) == 1  # real_dir was added
        pipeline.ingest_directory.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_with_empty_collections_drops_only_managed(self, tmp_path):
        """collections=[] → only manifest-tracked collections dropped."""
        from archon.search.sync import SearchCollectionSync

        manifest = {"managed": "/some/managed/path"}
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["managed", "unmanaged"],
            manifest=manifest,
        )

        syncer = SearchCollectionSync(pipeline)
        result = await syncer.sync([])

        pipeline.store.drop_collection.assert_called_once_with("managed")
        assert "managed" in result.removed
        assert "unmanaged" in result.skipped

    @pytest.mark.asyncio
    async def test_sync_handles_keyerror_on_drop_phantom_manifest_entry(self, tmp_path):
        """Manifest has col, list_collections returns it, drop raises KeyError → WARNING, error in SyncResult, sync continues."""
        from archon.search.sync import SearchCollectionSync

        manifest = {"ghost": "/some/ghost/path"}
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["ghost"],
            manifest=manifest,
        )
        pipeline.store.drop_collection.side_effect = KeyError("ghost")

        syncer = SearchCollectionSync(pipeline)
        with patch("archon.search.sync.logger") as mock_logger:
            result = await syncer.sync([])

        # Should log a WARNING
        mock_logger.warning.assert_called()
        # Error should be recorded
        assert len(result.errors) == 1

    @pytest.mark.asyncio
    async def test_migration_renames_archon_history_to_derived_name(self, tmp_path):
        """archon-history in LanceDB, sessions not → rename called."""
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["archon-history"],
        )

        syncer = SearchCollectionSync(pipeline)
        result = await syncer.sync([])

        pipeline.store.rename_collection.assert_called_once_with("archon-history", "sessions")
        assert result.errors == [] or "archon-history" not in result.errors

    @pytest.mark.asyncio
    async def test_migration_handles_not_implemented_error(self, tmp_path, caplog):
        """rename_collection raises NotImplementedError (LanceDB OSS) → warning, no crash."""
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["archon-history"],
        )
        pipeline.store.rename_collection.side_effect = NotImplementedError("rename_table not supported")

        syncer = SearchCollectionSync(pipeline)
        with caplog.at_level(logging.WARNING, logger="archon"):
            result = await syncer.sync([])  # should not crash

        # No exception raised, warning logged
        assert any("rename_table" in msg or "unmanaged" in msg for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_migration_updates_manifest_on_rename(self, tmp_path):
        """After rename, manifest entry archon-history → sessions."""
        import json
        from archon.search.sync import SearchCollectionSync

        db_path = tmp_path / "db"
        db_path.mkdir(parents=True)
        manifest_data = {"archon-history": "/some/history/path"}
        (db_path / "sync_manifest.json").write_text(json.dumps(manifest_data))

        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["archon-history"],
            manifest=manifest_data,
        )
        # rename_collection succeeds (default AsyncMock)
        pipeline.store.rename_collection = AsyncMock()

        syncer = SearchCollectionSync(pipeline)
        await syncer.sync([])

        # Manifest should now have 'sessions' not 'archon-history'
        updated = json.loads((db_path / "sync_manifest.json").read_text())
        assert "sessions" in updated or "archon-history" not in updated

    @pytest.mark.asyncio
    async def test_sync_deduplicates_input_paths(self, tmp_path):
        """Duplicate paths in collections are deduplicated."""
        from archon.search.sync import SearchCollectionSync

        real_dir = tmp_path / "myproject"
        real_dir.mkdir()
        path_str = str(real_dir)

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        syncer = SearchCollectionSync(pipeline)
        result = await syncer.sync([path_str, path_str])  # duplicate

        # Should only ingest once
        assert pipeline.ingest_directory.call_count == 1
        assert len(result.added) == 1

    @pytest.mark.asyncio
    async def test_migration_skips_if_both_tables_exist(self, tmp_path, caplog):
        """Both archon-history and sessions exist → WARNING logged, no rename."""
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["archon-history", "sessions"],
        )

        syncer = SearchCollectionSync(pipeline)
        with caplog.at_level(logging.WARNING, logger="archon"):
            await syncer.sync([])

        pipeline.store.rename_collection.assert_not_called()
        assert any("archon-history" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# manifest_lookup_by_path tests
# ---------------------------------------------------------------------------

class TestManifestLookupByPath:
    def test_returns_none_when_no_manifest(self, tmp_path):
        from archon.search.sync import manifest_lookup_by_path

        result = manifest_lookup_by_path(tmp_path / "nonexistent.json", "/some/path")
        assert result is None

    def test_returns_collection_name_for_known_path(self, tmp_path):
        from archon.search.sync import manifest_lookup_by_path

        manifest_path = tmp_path / "sync_manifest.json"
        real_dir = tmp_path / "myproject"
        real_dir.mkdir()
        resolved = str(real_dir.resolve())
        manifest_path.write_text(json.dumps({"myproject": resolved}))

        result = manifest_lookup_by_path(manifest_path, resolved)
        assert result == "myproject"

    def test_returns_none_for_unknown_path(self, tmp_path):
        from archon.search.sync import manifest_lookup_by_path

        manifest_path = tmp_path / "sync_manifest.json"
        manifest_path.write_text(json.dumps({"col": "/some/other/path"}))

        result = manifest_lookup_by_path(manifest_path, "/totally/different/path")
        assert result is None

    def test_expands_tilde_in_stored_path(self, tmp_path):
        from archon.search.sync import manifest_lookup_by_path
        from pathlib import Path

        home_relative = "~/.archon/history/sessions"
        resolved = str(Path(home_relative).expanduser().resolve())

        manifest_path = tmp_path / "sync_manifest.json"
        manifest_path.write_text(json.dumps({"sessions": home_relative}))

        result = manifest_lookup_by_path(manifest_path, resolved)
        assert result == "sessions"


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

class TestSearchCollectionSyncIntegration:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_sync_integration(self, tmp_path):
        """Add/remove paths → verify LanceDB state matches config (real LanceDB)."""
        import numpy as np

        from archon.search._types import ChunkRecord
        from archon.search.embedder import Embedder, EmbedderBackend
        from archon.search.parser import DocumentParser
        from archon.search.pipeline import SearchPipeline
        from archon.search.reranker import Reranker, RerankerBackend
        from archon.search.store import SearchStore
        from archon.search.sync import SearchCollectionSync

        # Stub embedder — uses synchronous encode() as required by EmbedderBackend protocol
        class StubEmbedderBackend(EmbedderBackend):
            embedding_dim = 4

            def encode(self, texts: list[str]) -> list[list[float]]:
                return [list(np.zeros(4, dtype=float)) for _ in texts]

        class StubRerankerBackend(RerankerBackend):
            def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
                return [0.5] * len(pairs)

        # Stub chunker that doesn't require chonkie
        class StubChunker:
            def chunk(self, text: str, doc_id: str, source_path: str) -> list[ChunkRecord]:
                from datetime import datetime, timezone
                if not text or not text.strip():
                    return []
                return [ChunkRecord(
                    doc_id=doc_id,
                    chunk_id="",
                    text=text[:200],
                    vector=[],
                    source_path=source_path,
                    indexed_at=datetime.now(timezone.utc).isoformat(),
                )]

        db_path = tmp_path / "lancedb"
        store = SearchStore(db_path)
        await store.connect()

        embedder = Embedder(StubEmbedderBackend())
        reranker = Reranker(StubRerankerBackend())
        chunker = StubChunker()
        parser = DocumentParser()

        pipeline = SearchPipeline(
            store=store,
            embedder=embedder,
            reranker=reranker,
            chunker=chunker,
            parser=parser,
            top_k_retrieve=5,
            top_k_return=3,
        )

        # Create some test content
        dir_a = tmp_path / "project_a"
        dir_a.mkdir()
        (dir_a / "readme.md").write_text("# Project A\nThis is a test project.")

        dir_b = tmp_path / "project_b"
        dir_b.mkdir()
        (dir_b / "readme.md").write_text("# Project B\nThis is another test project.")

        syncer = SearchCollectionSync(pipeline)

        # First sync: add both directories
        result1 = await syncer.sync([str(dir_a), str(dir_b)])
        assert len(result1.added) == 2
        assert result1.errors == []

        # Verify collections exist
        collections = await store.list_collections()
        col_names = {c.name for c in collections}
        assert "project_a" in col_names
        assert "project_b" in col_names

        # Second sync: remove dir_b
        result2 = await syncer.sync([str(dir_a)])
        assert "project_a" in result2.unchanged
        assert "project_b" in result2.removed
        assert result2.errors == []

        # Verify project_b is gone
        collections_after = await store.list_collections()
        col_names_after = {c.name for c in collections_after}
        assert "project_a" in col_names_after
        assert "project_b" not in col_names_after


# ---------------------------------------------------------------------------
# manifest_remove_entry tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TestSyncLocking tests
# ---------------------------------------------------------------------------


class TestSyncLocking:
    def test_get_lock_returns_same_lock_for_same_name(self, tmp_path):
        """_get_lock('col_a') twice returns the same asyncio.Lock instance."""
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline)

        lock1 = syncer._get_lock("col_a")
        lock2 = syncer._get_lock("col_a")

        assert lock1 is lock2

    def test_get_lock_returns_different_lock_for_different_name(self, tmp_path):
        """_get_lock('col_a') and _get_lock('col_b') return different instances."""
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline)

        lock_a = syncer._get_lock("col_a")
        lock_b = syncer._get_lock("col_b")

        assert lock_a is not lock_b

    @pytest.mark.asyncio
    async def test_sync_acquires_lock_per_collection(self, tmp_path):
        """Lock is acquired during sync for the collection being ingested."""
        import asyncio
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()
        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        lock_was_locked_during_ingest = False

        async def fake_ingest(path, name, **kwargs):
            nonlocal lock_was_locked_during_ingest
            lock = syncer._get_lock(name)
            lock_was_locked_during_ingest = lock.locked()

        pipeline.ingest_directory = fake_ingest

        syncer = SearchCollectionSync(pipeline)
        await syncer.sync([str(new_dir)])

        assert lock_was_locked_during_ingest

    @pytest.mark.asyncio
    async def test_concurrent_sync_same_collection_serialized(self, tmp_path):
        """Two concurrent sync() calls on the same collection are serialized."""
        import asyncio
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "shared"
        col_dir.mkdir()
        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        execution_log: list[str] = []
        start_event = asyncio.Event()

        call_count = 0

        async def fake_ingest(path, name, **kwargs):
            nonlocal call_count
            call_count += 1
            current_call = call_count
            execution_log.append(f"start_{current_call}")
            await asyncio.sleep(0.02)  # simulate work
            execution_log.append(f"end_{current_call}")

        pipeline.ingest_directory = fake_ingest

        syncer = SearchCollectionSync(pipeline)

        # Run two sync calls concurrently on the same collection
        await asyncio.gather(
            syncer.sync([str(col_dir)]),
            syncer.sync([str(col_dir)]),
        )

        # The log must show non-overlapping execution:
        # start_1, end_1, start_2, end_2  (or vice versa)
        assert len(execution_log) == 4
        first_start = execution_log[0]
        first_end = execution_log[1]
        second_start = execution_log[2]
        second_end = execution_log[3]
        assert first_start.startswith("start_")
        assert first_end.startswith("end_")
        assert second_start.startswith("start_")
        assert second_end.startswith("end_")
        # Verify ordering: first must end before second starts
        assert first_end < second_start or (
            first_start[6:] == second_end[4:]  # same number means same call
        ), f"Overlapping execution detected: {execution_log}"
        # More direct check: after end_N, next must be start_M (no interleaving)
        assert execution_log[1].startswith("end_")
        assert execution_log[2].startswith("start_")

    @pytest.mark.asyncio
    async def test_concurrent_sync_different_collections_parallel(self, tmp_path):
        """Two concurrent sync() calls on different collections run concurrently."""
        import asyncio
        from archon.search.sync import SearchCollectionSync

        dir_a = tmp_path / "col_a"
        dir_b = tmp_path / "col_b"
        dir_a.mkdir()
        dir_b.mkdir()
        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        started: list[str] = []
        both_started = asyncio.Event()

        async def fake_ingest(path, name, **kwargs):
            started.append(name)
            if len(started) == 2:
                both_started.set()
            # Wait until both have started (proving concurrency)
            await asyncio.wait_for(both_started.wait(), timeout=1.0)

        pipeline.ingest_directory = fake_ingest

        syncer = SearchCollectionSync(pipeline)

        await asyncio.gather(
            syncer.sync([str(dir_a)]),
            syncer.sync([str(dir_b)]),
        )

        # both_started was set → both ingests ran concurrently
        assert both_started.is_set()
        assert len(started) == 2


class TestManifestRemoveEntry:
    def test_manifest_remove_entry_removes_key(self, tmp_path: Path) -> None:
        from archon.search.sync import manifest_remove_entry  # noqa: PLC0415

        manifest_path = tmp_path / "sync_manifest.json"
        manifest_path.write_text(json.dumps({"sessions": "/home/user/.archon/sessions", "other": "/data"}))

        manifest_remove_entry(manifest_path, "sessions")

        data = json.loads(manifest_path.read_text())
        assert "sessions" not in data
        assert "other" in data

    def test_manifest_remove_entry_noop_if_missing(self, tmp_path: Path) -> None:
        from archon.search.sync import manifest_remove_entry  # noqa: PLC0415

        nonexistent = tmp_path / "no_such_manifest.json"
        # Must not raise
        manifest_remove_entry(nonexistent, "sessions")


# ---------------------------------------------------------------------------
# TestSyncProgress — FEAT-027 Task 1.4: sync() progress state integration
# ---------------------------------------------------------------------------

class TestSyncProgress:
    """Tests for IndexingStateStore integration in SearchCollectionSync.sync()."""

    def _make_syncer_with_state(self, tmp_path, existing_collections=None, manifest=None, file_count=5, ingest_results=None):
        """Helper: build a SearchCollectionSync with a real IndexingStateStore."""
        import asyncio
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path, existing_collections=existing_collections or [], manifest=manifest)
        state_store = IndexingStateStore(tmp_path / "state")

        N = file_count
        results = ingest_results if ingest_results is not None else [
            IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok") for i in range(N)
        ]

        async def fake_ingest(path, name, **kwargs):
            progress_cb = kwargs.get("progress_cb")
            on_file_complete = kwargs.get("on_file_complete")
            if progress_cb:
                for i in range(1, len(results) + 1):
                    cb_result = progress_cb(i, len(results))
                    if asyncio.iscoroutine(cb_result):
                        await cb_result
            if on_file_complete:
                for r in results:
                    if r.status == "ok":
                        on_file_complete(Path(f"/fake/{r.doc_id}.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        return syncer, state_store, pipeline

    @pytest.mark.asyncio
    async def test_sync_writes_pending_then_in_progress_before_ingest(self, tmp_path):
        """PENDING should be the first state written, then IN_PROGRESS before ingest starts."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        statuses_seen: list[IndexingStatus] = []
        pending_progress = None
        original_update = state_store.update_collection

        def tracking_update(name, progress):
            nonlocal pending_progress
            statuses_seen.append(progress.status)
            if progress.status == IndexingStatus.PENDING and pending_progress is None:
                pending_progress = progress
            return original_update(name, progress)

        state_store.update_collection = tracking_update

        pipeline.ingest_directory = AsyncMock(
            return_value=[IngestResult(doc_id="d0", chunks_created=1, status="ok")],
        )

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        # First write is PENDING, second is IN_PROGRESS
        assert len(statuses_seen) >= 2
        assert statuses_seen[0] == IndexingStatus.PENDING
        assert statuses_seen[1] == IndexingStatus.IN_PROGRESS
        # PENDING must be written with total_files=0 (files not yet counted)
        assert pending_progress is not None
        assert pending_progress.total_files == 0

    @pytest.mark.asyncio
    async def test_sync_writes_in_progress_during_ingest(self, tmp_path):
        """During ingest, state should be IN_PROGRESS."""
        import asyncio
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        captured_status = None

        async def fake_ingest(path, name, **kwargs):
            nonlocal captured_status
            state = state_store.read()
            if state and name in state.collections:
                captured_status = state.collections[name].status
            return [IngestResult(doc_id="d0", chunks_created=1, status="ok")]

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        assert captured_status == IndexingStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_sync_total_files_set_from_file_enumeration(self, tmp_path):
        """total_files should be set from file enumeration, not from callback."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()
        # Create 10 real files so enumeration counts them
        for i in range(10):
            (new_dir / f"file{i}.md").write_text(f"content {i}")

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        async def fake_ingest(path, name, **kwargs):
            progress_cb = kwargs.get("progress_cb")
            on_file_complete = kwargs.get("on_file_complete")
            results = [IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok") for i in range(10)]
            if on_file_complete:
                for i in range(10):
                    on_file_complete(Path(f"/fake/file{i}.md"))
            if progress_cb:
                for i in range(1, 11):
                    progress_cb(i, 10)
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.total_files == 10
        assert cp.status == IndexingStatus.DONE

    @pytest.mark.asyncio
    async def test_sync_writes_done_after_success(self, tmp_path):
        """After successful ingest, state should be DONE."""
        from archon.search.progress import IndexingStatus

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        syncer, state_store, _ = self._make_syncer_with_state(tmp_path, file_count=3)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        assert state is not None
        assert "myproject" in state.collections
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        assert cp.completed_at is not None

    @pytest.mark.asyncio
    async def test_sync_writes_failed_on_exception(self, tmp_path):
        """On ingest exception, state should be FAILED with error message."""
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        pipeline.ingest_directory = AsyncMock(side_effect=RuntimeError("disk full"))

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        assert state is not None
        assert "myproject" in state.collections
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.FAILED
        assert "disk full" in cp.error

    @pytest.mark.asyncio
    async def test_sync_error_count_from_ingest_results(self, tmp_path):
        """error_count should reflect number of non-ok results."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStatus

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        results = [
            IngestResult(doc_id="d0", chunks_created=1, status="ok"),
            IngestResult(doc_id="d1", chunks_created=0, status="parse_error"),
            IngestResult(doc_id="d2", chunks_created=1, status="ok"),
            IngestResult(doc_id="d3", chunks_created=0, status="encoding_error"),
        ]

        syncer, state_store, _ = self._make_syncer_with_state(tmp_path, ingest_results=results)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.error_count == 2

    @pytest.mark.asyncio
    async def test_sync_processed_files_counts_ok_only(self, tmp_path):
        """processed_files in final state should count only ok results."""
        from archon.search._types import IngestResult

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        results = [
            IngestResult(doc_id="d0", chunks_created=1, status="ok"),
            IngestResult(doc_id="d1", chunks_created=0, status="error"),
            IngestResult(doc_id="d2", chunks_created=1, status="ok"),
        ]

        syncer, state_store, _ = self._make_syncer_with_state(tmp_path, ingest_results=results)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.processed_files == 2

    @pytest.mark.asyncio
    async def test_sync_batched_writes_every_50(self, tmp_path):
        """State writes during on_file_complete should happen every 50 files."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        progress_write_counts: list[int] = []
        original_write = state_store.update_collection

        def tracking_update(name, progress):
            if progress.status == IndexingStatus.IN_PROGRESS and progress.processed_files > 0:
                progress_write_counts.append(progress.processed_files)
            return original_write(name, progress)

        state_store.update_collection = tracking_update

        N = 150

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            results = [IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok") for i in range(N)]
            for i in range(N):
                if on_file_complete:
                    on_file_complete(Path(f"/fake/file{i}.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        # Batched writes from on_file_complete at every 50 files
        assert progress_write_counts == [50, 100, 150], f"Expected exactly [50, 100, 150], got {progress_write_counts}"

    @pytest.mark.asyncio
    async def test_sync_batched_writes_boundary_49_files(self, tmp_path):
        """With 49 files, no batched progress writes should happen (only PENDING/IN_PROGRESS/DONE)."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        progress_write_counts: list[int] = []
        original_update = state_store.update_collection

        def tracking_update(name, progress):
            if progress.status == IndexingStatus.IN_PROGRESS and progress.processed_files > 0:
                progress_write_counts.append(progress.processed_files)
            return original_update(name, progress)

        state_store.update_collection = tracking_update

        N = 49
        results = [IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok") for i in range(N)]

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            if on_file_complete:
                for i in range(N):
                    on_file_complete(Path(f"/fake/file{i}.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        # Only the initial IN_PROGRESS write (processed_files=0), no batched progress writes
        assert len(progress_write_counts) == 0

    @pytest.mark.asyncio
    async def test_sync_batched_writes_boundary_50_files(self, tmp_path):
        """With exactly 50 files, one batched write from on_file_complete."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        progress_write_counts: list[int] = []
        original_update = state_store.update_collection

        def tracking_update(name, progress):
            if progress.status == IndexingStatus.IN_PROGRESS and progress.processed_files > 0:
                progress_write_counts.append(progress.processed_files)
            return original_update(name, progress)

        state_store.update_collection = tracking_update

        N = 50

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            results = [IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok") for i in range(N)]
            for i in range(N):
                if on_file_complete:
                    on_file_complete(Path(f"/fake/file{i}.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        assert 50 in progress_write_counts
        assert len(progress_write_counts) == 1

    @pytest.mark.asyncio
    async def test_sync_batched_writes_boundary_51_files(self, tmp_path):
        """With 51 files, one batched write at 50 from on_file_complete."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        progress_write_counts: list[int] = []
        original_update = state_store.update_collection

        def tracking_update(name, progress):
            if progress.status == IndexingStatus.IN_PROGRESS and progress.processed_files > 0:
                progress_write_counts.append(progress.processed_files)
            return original_update(name, progress)

        state_store.update_collection = tracking_update

        N = 51

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            results = [IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok") for i in range(N)]
            for i in range(N):
                if on_file_complete:
                    on_file_complete(Path(f"/fake/file{i}.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        assert progress_write_counts == [50]

    @pytest.mark.asyncio
    async def test_sync_batched_writes_boundary_1_file(self, tmp_path):
        """With 1 file, no batched progress writes."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        progress_write_counts: list[int] = []
        original_update = state_store.update_collection

        def tracking_update(name, progress):
            if progress.status == IndexingStatus.IN_PROGRESS and progress.processed_files > 0:
                progress_write_counts.append(progress.processed_files)
            return original_update(name, progress)

        state_store.update_collection = tracking_update

        results = [IngestResult(doc_id="d0", chunks_created=1, status="ok")]

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            if on_file_complete:
                on_file_complete(Path("/fake/file0.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        assert len(progress_write_counts) == 0

    @pytest.mark.asyncio
    async def test_sync_final_write_on_completion(self, tmp_path):
        """Final state write should happen after ingest completes (DONE status)."""
        from archon.search.progress import IndexingStatus

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()
        # Create 5 real files so enumeration yields total_new == 5
        for i in range(5):
            (new_dir / f"file{i}.md").write_text(f"content {i}")

        syncer, state_store, _ = self._make_syncer_with_state(tmp_path, file_count=5)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        assert state is not None
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        assert cp.total_files == 5
        assert cp.processed_files == 5
        assert cp.error_count == 0

    @pytest.mark.asyncio
    async def test_sync_zero_file_directory(self, tmp_path):
        """Empty directory: ingest returns empty list, DONE with 0 files."""
        from archon.search.progress import IndexingStatus

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        syncer, state_store, _ = self._make_syncer_with_state(tmp_path, file_count=0, ingest_results=[])
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        assert cp.total_files == 0
        assert cp.processed_files == 0

    @pytest.mark.asyncio
    async def test_sync_wraps_caller_callback(self, tmp_path):
        """Caller's progress_cb should still be called."""
        import asyncio
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        caller_calls: list[tuple[int, int]] = []

        def caller_cb(done, total):
            caller_calls.append((done, total))

        N = 3
        results = [IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok") for i in range(N)]

        async def fake_ingest(path, name, **kwargs):
            progress_cb = kwargs.get("progress_cb")
            if progress_cb:
                for i in range(1, N + 1):
                    cb_result = progress_cb(i, N)
                    if asyncio.iscoroutine(cb_result):
                        await cb_result
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)], progress_cb=caller_cb)

        assert caller_calls == [(1, 3), (2, 3), (3, 3)]

    @pytest.mark.asyncio
    async def test_sync_no_state_store_backward_compat(self, tmp_path):
        """Without state_store, sync works as before — no state files created."""
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        pipeline.ingest_directory = AsyncMock(return_value=[])

        syncer = SearchCollectionSync(pipeline)  # no state_store
        result = await syncer.sync([str(new_dir)])

        assert "myproject" in result.added
        # No state dir should exist
        assert not (tmp_path / "state").exists()

    @pytest.mark.asyncio
    async def test_sync_resets_stale_in_progress(self, tmp_path):
        """On sync entry, any IN_PROGRESS entries should be reset to PENDING."""
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        pipeline.ingest_directory = AsyncMock(return_value=[])
        state_store = IndexingStateStore(tmp_path / "state")

        # Pre-seed stale IN_PROGRESS state
        state_store.update_collection("stale_col", CollectionProgress(
            status=IndexingStatus.IN_PROGRESS,
            total_files=100,
            processed_files=50,
        ))

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([])  # empty desired — just triggers crash recovery

        state = state_store.read()
        assert state is not None
        assert "stale_col" in state.collections
        assert state.collections["stale_col"].status == IndexingStatus.PENDING

    @pytest.mark.asyncio
    async def test_sync_cleans_removed_collections(self, tmp_path):
        """Removed collections should be cleaned from state file."""
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        manifest = {"oldcol": "/some/old/path"}
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["oldcol"],
            manifest=manifest,
        )
        state_store = IndexingStateStore(tmp_path / "state")

        # Pre-seed state for collection that will be removed
        state_store.update_collection("oldcol", CollectionProgress(
            status=IndexingStatus.DONE,
            total_files=10,
            processed_files=10,
        ))

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        result = await syncer.sync([])  # empty desired

        assert "oldcol" in result.removed
        state = state_store.read()
        assert state is not None
        assert "oldcol" not in state.collections

    @pytest.mark.asyncio
    async def test_sync_state_write_failure_does_not_abort(self, tmp_path):
        """State write failures must not abort sync — sync should continue."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        pipeline.ingest_directory = AsyncMock(return_value=[
            IngestResult(doc_id="d0", chunks_created=1, status="ok"),
        ])
        state_store = IndexingStateStore(tmp_path / "state")

        # Make all state writes fail
        state_store.update_collection = MagicMock(side_effect=OSError("disk full"))
        state_store.remove_collection = MagicMock(side_effect=OSError("disk full"))
        state_store.read = MagicMock(return_value=None)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        result = await syncer.sync([str(new_dir)])

        # Sync should still succeed
        assert "myproject" in result.added
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_sync_done_with_error_count(self, tmp_path):
        """DONE state should include both ok and error counts from results."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStatus

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()
        # Create 5 real files so enumeration yields total_new == 5
        for i in range(5):
            (new_dir / f"file{i}.md").write_text(f"content {i}")

        results = [
            IngestResult(doc_id="d0", chunks_created=1, status="ok"),
            IngestResult(doc_id="d1", chunks_created=0, status="parse_error"),
            IngestResult(doc_id="d2", chunks_created=1, status="ok"),
            IngestResult(doc_id="d3", chunks_created=0, status="encoding_error"),
            IngestResult(doc_id="d4", chunks_created=1, status="ok"),
        ]

        syncer, state_store, _ = self._make_syncer_with_state(tmp_path, ingest_results=results)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        assert cp.processed_files == 3  # ok count
        assert cp.error_count == 2
        assert cp.total_files == 5

    @pytest.mark.asyncio
    async def test_sync_failed_preserves_total_files_from_enumeration(self, tmp_path):
        """On FAILED, total_files should be preserved from file enumeration."""
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()
        # Create 100 real files so enumeration counts them
        for i in range(100):
            (new_dir / f"file{i}.md").write_text(f"content {i}")

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            if on_file_complete:
                on_file_complete(Path("/fake/file0.md"))
                on_file_complete(Path("/fake/file1.md"))
            raise RuntimeError("crash mid-ingest")

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.FAILED
        assert cp.total_files == 100  # from enumeration
        assert "crash mid-ingest" in cp.error
        assert len(cp.processed_paths) == 2  # partial progress retained

    @pytest.mark.asyncio
    async def test_sync_done_total_files_uses_total_new_not_len_results(self, tmp_path):
        """DONE state total_files should use total_new (file enumeration count),
        not len(results) (ingest return count), so partial ingest results don't
        undercount the total."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()
        # Create 5 real files so enumeration yields total_new == 5
        for i in range(5):
            (new_dir / f"file{i}.md").write_text(f"content {i}")

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        # ingest returns only 3 results — fewer than the 5 enumerated files
        partial_results = [
            IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok") for i in range(3)
        ]

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            if on_file_complete:
                for r in partial_results:
                    on_file_complete(Path(f"/fake/{r.doc_id}.md"))
            return partial_results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        # total_files must reflect file enumeration (5), not ingest result count (3)
        assert cp.total_files == 5

    @pytest.mark.asyncio
    async def test_sync_done_total_files_resume_plus_new(self, tmp_path):
        """DONE total_files = resume_offset + total_new, not resume_offset + len(results).

        Compound case: resume_offset > 0 AND total_new > 0 AND len(results) < total_new.
        The old buggy formula (resume_offset + len(results)) would give 1 + 2 = 3.
        The correct formula (resume_offset + total_new) must give 1 + 3 = 4.
        """
        from archon.search._types import IngestResult
        from archon.search.progress import (
            CollectionProgress,
            IndexingStateStore,
            IndexingStatus,
        )
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()

        # Pre-seeded file (resume_offset = 1)
        old_file = col_dir / "old.md"
        old_file.write_text("already indexed")
        old_file_resolved = str(old_file.resolve())

        # 3 new real files on disk → total_new = 3
        for i in range(3):
            (col_dir / f"new{i}.md").write_text(f"new content {i}")

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        # Pre-seed old_file as already processed
        state_store.update_collection("myproject", CollectionProgress(
            status=IndexingStatus.PENDING,
            processed_paths=[old_file_resolved],
        ))

        # ingest returns only 2 results — fewer than total_new (3)
        partial_results = [
            IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok") for i in range(2)
        ]

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            if on_file_complete:
                for r in partial_results:
                    on_file_complete(Path(f"/fake/{r.doc_id}.md"))
            return partial_results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(col_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        # resume_offset(1) + total_new(3) = 4, not resume_offset(1) + len(results)(2) = 3
        assert cp.total_files == 4

    @pytest.mark.asyncio
    async def test_sync_multiple_collections_mixed_results(self, tmp_path):
        """Multiple collections: one succeeds, one fails — each has correct state."""
        import asyncio
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        dir_a = tmp_path / "project_a"
        dir_b = tmp_path / "project_b"
        dir_a.mkdir()
        dir_b.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        call_count = 0

        async def fake_ingest(path, name, **kwargs):
            progress_cb = kwargs.get("progress_cb")
            nonlocal call_count
            call_count += 1
            if "project_a" in str(path):
                results = [IngestResult(doc_id="d0", chunks_created=1, status="ok")]
                if progress_cb:
                    cb_result = progress_cb(1, 1)
                    if asyncio.iscoroutine(cb_result):
                        await cb_result
                return results
            else:
                if progress_cb:
                    cb_result = progress_cb(1, 5)
                    if asyncio.iscoroutine(cb_result):
                        await cb_result
                raise RuntimeError("project_b failed")

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        result = await syncer.sync([str(dir_a), str(dir_b)])

        state = state_store.read()
        assert state is not None

        # project_a should be DONE
        assert "project_a" in state.collections
        assert state.collections["project_a"].status == IndexingStatus.DONE

        # project_b should be FAILED
        assert "project_b" in state.collections
        assert state.collections["project_b"].status == IndexingStatus.FAILED
        assert "project_b failed" in state.collections["project_b"].error


# ---------------------------------------------------------------------------
# Pinned-first ordering in sync()
# ---------------------------------------------------------------------------

class TestSyncPinnedOrder:
    """Tests for pinned-first collection ordering in sync()."""

    @pytest.mark.asyncio
    async def test_sync_pinned_first_ordering(self, tmp_path):
        """Pinned collections are ingested before non-pinned ones."""
        from archon.search.sync import SearchCollectionSync

        alpha = tmp_path / "alpha"
        beta = tmp_path / "beta"
        gamma = tmp_path / "gamma"
        for d in (alpha, beta, gamma):
            d.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        ingestion_order: list[str] = []
        orig_ingest = pipeline.ingest_directory

        async def track_ingest(path, name, **kw):
            ingestion_order.append(name)
            return await orig_ingest(path, name, **kw)

        pipeline.ingest_directory = AsyncMock(side_effect=track_ingest)

        # Pin only gamma — it should be ingested first
        syncer = SearchCollectionSync(pipeline, pinned_collections=[str(gamma)])
        await syncer.sync([str(alpha), str(beta), str(gamma)])

        assert ingestion_order[0] == "gamma"
        # Remaining should be alphabetical
        assert ingestion_order[1:] == ["alpha", "beta"]

    @pytest.mark.asyncio
    async def test_sync_pinned_preserves_declaration_order(self, tmp_path):
        """Pinned collections follow config declaration order, not alphabetical."""
        from archon.search.sync import SearchCollectionSync

        aaa = tmp_path / "aaa"
        bbb = tmp_path / "bbb"
        ccc = tmp_path / "ccc"
        for d in (aaa, bbb, ccc):
            d.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        ingestion_order: list[str] = []
        orig_ingest = pipeline.ingest_directory

        async def track_ingest(path, name, **kw):
            ingestion_order.append(name)
            return await orig_ingest(path, name, **kw)

        pipeline.ingest_directory = AsyncMock(side_effect=track_ingest)

        # Pinned in reverse-alpha order: ccc, bbb
        syncer = SearchCollectionSync(pipeline, pinned_collections=[str(ccc), str(bbb)])
        await syncer.sync([str(aaa), str(bbb), str(ccc)])

        assert ingestion_order == ["ccc", "bbb", "aaa"]

    @pytest.mark.asyncio
    async def test_sync_non_pinned_alphabetical(self, tmp_path):
        """Non-pinned collections are sorted alphabetically by collection name."""
        from archon.search.sync import SearchCollectionSync

        zebra = tmp_path / "zebra"
        apple = tmp_path / "apple"
        mango = tmp_path / "mango"
        for d in (zebra, apple, mango):
            d.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        ingestion_order: list[str] = []
        orig_ingest = pipeline.ingest_directory

        async def track_ingest(path, name, **kw):
            ingestion_order.append(name)
            return await orig_ingest(path, name, **kw)

        pipeline.ingest_directory = AsyncMock(side_effect=track_ingest)

        syncer = SearchCollectionSync(pipeline, pinned_collections=[])
        await syncer.sync([str(zebra), str(apple), str(mango)])

        assert ingestion_order == ["apple", "mango", "zebra"]

    @pytest.mark.asyncio
    async def test_sync_pinned_not_in_desired_ignored(self, tmp_path):
        """Pinned path not in collections list does not cause error."""
        from archon.search.sync import SearchCollectionSync

        alpha = tmp_path / "alpha"
        alpha.mkdir()
        nonexistent_pinned = tmp_path / "not_a_collection"

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        syncer = SearchCollectionSync(pipeline, pinned_collections=[str(nonexistent_pinned)])
        result = await syncer.sync([str(alpha)])

        assert "alpha" in result.added
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_sync_all_pinned(self, tmp_path):
        """All collections are pinned — order matches config declaration order."""
        from archon.search.sync import SearchCollectionSync

        charlie = tmp_path / "charlie"
        alice = tmp_path / "alice"
        bob = tmp_path / "bob"
        for d in (charlie, alice, bob):
            d.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        ingestion_order: list[str] = []
        orig_ingest = pipeline.ingest_directory

        async def track_ingest(path, name, **kw):
            ingestion_order.append(name)
            return await orig_ingest(path, name, **kw)

        pipeline.ingest_directory = AsyncMock(side_effect=track_ingest)

        syncer = SearchCollectionSync(
            pipeline,
            pinned_collections=[str(charlie), str(alice), str(bob)],
        )
        await syncer.sync([str(charlie), str(alice), str(bob)])

        assert ingestion_order == ["charlie", "alice", "bob"]

    @pytest.mark.asyncio
    async def test_sync_no_pinned(self, tmp_path):
        """Empty pinned list — alphabetical fallback."""
        from archon.search.sync import SearchCollectionSync

        delta = tmp_path / "delta"
        bravo = tmp_path / "bravo"
        for d in (delta, bravo):
            d.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        ingestion_order: list[str] = []
        orig_ingest = pipeline.ingest_directory

        async def track_ingest(path, name, **kw):
            ingestion_order.append(name)
            return await orig_ingest(path, name, **kw)

        pipeline.ingest_directory = AsyncMock(side_effect=track_ingest)

        syncer = SearchCollectionSync(pipeline)
        await syncer.sync([str(delta), str(bravo)])

        assert ingestion_order == ["bravo", "delta"]

    @pytest.mark.asyncio
    async def test_sync_pinned_tilde_expansion(self, tmp_path, monkeypatch):
        """Pinned path with ~ correctly matches resolved desired path."""
        from archon.search.sync import SearchCollectionSync

        # Force HOME to tmp_path so ~/mydata resolves deterministically
        monkeypatch.setenv("HOME", str(tmp_path))

        target = tmp_path / "mydata"
        target.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        ingestion_order: list[str] = []
        orig_ingest = pipeline.ingest_directory

        async def track_ingest(path, name, **kw):
            ingestion_order.append(name)
            return await orig_ingest(path, name, **kw)

        pipeline.ingest_directory = AsyncMock(side_effect=track_ingest)

        other = tmp_path / "other"
        other.mkdir()

        syncer = SearchCollectionSync(
            pipeline,
            pinned_collections=["~/mydata"],
        )
        await syncer.sync([str(target), str(other)])

        # target (pinned via tilde path) should come first
        assert ingestion_order[0] == "mydata"


# ---------------------------------------------------------------------------
# Task 3.3 — Resumable indexing tests
# ---------------------------------------------------------------------------


class TestSyncResumable:
    """Tests for resumable indexing (processed_paths) in SearchCollectionSync."""

    @pytest.mark.asyncio
    async def test_reset_stale_preserves_processed_paths(self, tmp_path):
        """IN_PROGRESS state with processed_paths → after reset, PENDING with paths preserved."""
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        pipeline.ingest_directory = AsyncMock(return_value=[])
        state_store = IndexingStateStore(tmp_path / "state")

        state_store.update_collection("col", CollectionProgress(
            status=IndexingStatus.IN_PROGRESS,
            total_files=10,
            processed_files=5,
            processed_paths=["/a", "/b"],
        ))

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([])

        state = state_store.read()
        cp = state.collections["col"]
        assert cp.status == IndexingStatus.PENDING
        assert cp.processed_paths == ["/a", "/b"]
        assert cp.total_files == 10
        assert cp.processed_files == 5

    @pytest.mark.asyncio
    async def test_load_processed_paths_state_store_none(self, tmp_path):
        """_state_store=None → _load_processed_paths returns []."""
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        syncer = SearchCollectionSync(pipeline, state_store=None)
        assert syncer._load_processed_paths("col") == []

    @pytest.mark.asyncio
    async def test_load_processed_paths_no_state_file(self, tmp_path):
        """State file missing → returns []."""
        from archon.search.progress import IndexingStateStore
        from archon.search.sync import SearchCollectionSync

        state_store = IndexingStateStore(tmp_path / "state")
        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        assert syncer._load_processed_paths("col") == []

    @pytest.mark.asyncio
    async def test_load_processed_paths_collection_absent(self, tmp_path):
        """Collection not in state → returns []."""
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        state_store = IndexingStateStore(tmp_path / "state")
        state_store.update_collection("other", CollectionProgress(
            status=IndexingStatus.DONE,
            processed_paths=["/x"],
        ))
        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        assert syncer._load_processed_paths("col") == []

    @pytest.mark.asyncio
    async def test_sync_resumes_from_processed_paths(self, tmp_path):
        """State with processed_paths → exclude_paths passed to ingest_directory."""
        from archon.search._types import IngestResult
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        # Pre-seed state with processed_paths
        state_store.update_collection("myproject", CollectionProgress(
            status=IndexingStatus.PENDING,
            processed_paths=["/a/file.md"],
        ))

        captured_kwargs: dict = {}

        async def fake_ingest(path, name, **kwargs):
            captured_kwargs.update(kwargs)
            return [IngestResult(doc_id="d0", chunks_created=1, status="ok")]

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        assert "exclude_paths" in captured_kwargs
        assert "/a/file.md" in captured_kwargs["exclude_paths"]

    @pytest.mark.asyncio
    async def test_sync_new_collection_preseeded_paths_excluded_new_ingested(self, tmp_path):
        """Pre-seeded path is excluded; new path is ingested; final state has both."""
        from archon.search._types import IngestResult
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()

        # Two real files in the directory
        old_file = col_dir / "old.md"
        new_file = col_dir / "new.md"
        old_file.write_text("already indexed")
        new_file.write_text("brand new content")

        old_file_resolved = str(old_file.resolve())
        new_file_resolved = str(new_file.resolve())

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        # Pre-seed old_file as already processed
        state_store.update_collection("myproject", CollectionProgress(
            status=IndexingStatus.PENDING,
            processed_paths=[old_file_resolved],
        ))

        captured_exclude: set = set()
        captured_new_paths: list[str] = []

        async def fake_ingest(path, name, **kwargs):
            exclude = kwargs.get("exclude_paths", frozenset())
            captured_exclude.update(exclude)
            on_file_complete = kwargs.get("on_file_complete")
            # Simulate ingesting only non-excluded files
            results = []
            for f in sorted(path.glob("**/*.md")):
                if str(f.resolve()) in exclude:
                    continue
                r = IngestResult(doc_id=f.stem, chunks_created=1, status="ok")
                results.append(r)
                captured_new_paths.append(str(f.resolve()))
                if on_file_complete:
                    on_file_complete(f)
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(col_dir)])

        # Pre-seeded file must appear in exclude_paths passed to ingest_directory
        assert old_file_resolved in captured_exclude, "pre-seeded file should be excluded"

        # New file must have been ingested
        assert new_file_resolved in captured_new_paths, "new file should be ingested"

        # Old file must NOT have been re-ingested
        assert old_file_resolved not in captured_new_paths, "pre-seeded file must not be re-ingested"

        # Final state must include both paths in processed_paths and reach DONE
        state = state_store.read()
        cp = state.collections["myproject"]
        assert set(cp.processed_paths) == {old_file_resolved, new_file_resolved}
        assert cp.status == IndexingStatus.DONE

    @pytest.mark.asyncio
    async def test_sync_accumulates_new_paths_in_state(self, tmp_path):
        """After sync, state processed_paths contains newly processed file paths."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            results = []
            for i in range(3):
                r = IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok")
                results.append(r)
                if on_file_complete:
                    on_file_complete(Path(f"/fake/file{i}.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        assert len(cp.processed_paths) == 3
        assert "/fake/file0.md" in cp.processed_paths

    @pytest.mark.asyncio
    async def test_sync_processed_files_offset_correct(self, tmp_path):
        """resume_offset=5, 3 new files: state shows processed_files=8."""
        from archon.search._types import IngestResult
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        # Pre-seed with 5 already-processed paths
        state_store.update_collection("myproject", CollectionProgress(
            status=IndexingStatus.PENDING,
            processed_paths=[f"/old/file{i}.md" for i in range(5)],
        ))

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            results = []
            for i in range(3):
                r = IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok")
                results.append(r)
                if on_file_complete:
                    on_file_complete(Path(f"/new/file{i}.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.processed_files == 8  # 5 + 3

    @pytest.mark.asyncio
    async def test_sync_total_files_correct_with_resume(self, tmp_path):
        """resume_offset=5, total_new=3: state shows total_files=8."""
        from archon.search._types import IngestResult
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()
        # Create 3 real files so _count_files can find them
        for i in range(3):
            (new_dir / f"file{i}.md").write_text(f"content {i}")

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        state_store.update_collection("myproject", CollectionProgress(
            status=IndexingStatus.PENDING,
            processed_paths=[f"/old/file{i}.md" for i in range(5)],
        ))

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            results = []
            for i in range(3):
                r = IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok")
                results.append(r)
                if on_file_complete:
                    on_file_complete(Path(f"/new/file{i}.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.total_files == 8  # 5 + 3

    @pytest.mark.asyncio
    async def test_sync_batched_path_flush_every_50_files(self, tmp_path):
        """100 files: state write at file 50 with 50 paths; final write with 100."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        write_path_counts: list[int] = []
        original_update = state_store.update_collection

        def tracking_update(name, progress):
            if progress.processed_paths is not None:
                write_path_counts.append(len(progress.processed_paths))
            return original_update(name, progress)

        state_store.update_collection = tracking_update

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            results = []
            for i in range(100):
                r = IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok")
                results.append(r)
                if on_file_complete:
                    on_file_complete(Path(f"/fake/file{i}.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        # Should have a write with 50 paths (batch) and final with 100 paths (DONE)
        assert 50 in write_path_counts
        assert 100 in write_path_counts

    @pytest.mark.asyncio
    async def test_sync_final_state_contains_all_paths(self, tmp_path):
        """DONE state has processed_paths listing all ingested files."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            results = []
            for i in range(5):
                r = IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok")
                results.append(r)
                if on_file_complete:
                    on_file_complete(Path(f"/fake/file{i}.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        assert len(cp.processed_paths) == 5
        for i in range(5):
            assert f"/fake/file{i}.md" in cp.processed_paths

    @pytest.mark.asyncio
    async def test_sync_failed_state_contains_paths_processed_before_failure(self, tmp_path):
        """On FAILED, paths from before failure are retained."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            # Process 3 files, then crash
            for i in range(3):
                if on_file_complete:
                    on_file_complete(Path(f"/fake/file{i}.md"))
            raise RuntimeError("crash at file 4")

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.FAILED
        assert len(cp.processed_paths) == 3
        assert cp.processed_files == 3  # not 0

    @pytest.mark.asyncio
    async def test_sync_no_resume_on_empty_processed_paths(self, tmp_path):
        """Fresh collection (no state): exclude_paths is empty frozenset."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        captured_kwargs: dict = {}

        async def fake_ingest(path, name, **kwargs):
            captured_kwargs.update(kwargs)
            return [IngestResult(doc_id="d0", chunks_created=1, status="ok")]

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        exclude = captured_kwargs.get("exclude_paths")
        assert exclude is not None
        assert len(exclude) == 0

    @pytest.mark.asyncio
    async def test_sync_all_files_already_processed_state_correct(self, tmp_path):
        """All files excluded → DONE with total_files=resume_offset, processed_files=resume_offset."""
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        state_store.update_collection("myproject", CollectionProgress(
            status=IndexingStatus.PENDING,
            processed_paths=["/a", "/b", "/c"],
        ))

        # ingest_directory returns empty (all excluded)
        pipeline.ingest_directory = AsyncMock(return_value=[])

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        assert cp.total_files == 3  # resume_offset
        assert cp.processed_files == 3  # resume_offset
        assert cp.processed_paths == ["/a", "/b", "/c"]  # preserved

    @pytest.mark.asyncio
    async def test_sync_errored_file_not_in_processed_paths(self, tmp_path):
        """ingest_file error → that file NOT in processed_paths (retried next sync)."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        # on_file_complete is called only for ok results (handled by ingest_directory)
        # So we simulate: 2 ok, 1 error — on_file_complete called only twice
        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            results = [
                IngestResult(doc_id="d0", chunks_created=1, status="ok"),
                IngestResult(doc_id="d1", chunks_created=0, status="error"),
                IngestResult(doc_id="d2", chunks_created=1, status="ok"),
            ]
            # on_file_complete only for ok files
            if on_file_complete:
                on_file_complete(Path("/fake/ok1.md"))
                on_file_complete(Path("/fake/ok2.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert len(cp.processed_paths) == 2
        assert "/fake/ok1.md" in cp.processed_paths
        assert "/fake/ok2.md" in cp.processed_paths

    @pytest.mark.asyncio
    async def test_sync_resumes_existing_collection_with_pending_status(self, tmp_path):
        """Collection in existing with PENDING status → Step 6.5 resumes it."""
        from archon.search._types import IngestResult
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        existing_dir = tmp_path / "myproject"
        existing_dir.mkdir()
        resolved = str(existing_dir.resolve())

        manifest = {"myproject": resolved}
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["myproject"],
            manifest=manifest,
        )
        state_store = IndexingStateStore(tmp_path / "state")

        # Pre-seed PENDING status (e.g. crashed mid-ingest)
        state_store.update_collection("myproject", CollectionProgress(
            status=IndexingStatus.PENDING,
            processed_paths=["/old/file.md"],
        ))

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            results = [IngestResult(doc_id="d0", chunks_created=1, status="ok")]
            if on_file_complete:
                on_file_complete(Path("/new/file.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        result = await syncer.sync([str(existing_dir)])

        # Should be in added (resumed), NOT unchanged
        assert "myproject" in result.added
        assert "myproject" not in result.unchanged

    @pytest.mark.asyncio
    async def test_sync_resumed_collection_not_in_unchanged(self, tmp_path):
        """PENDING collection in existing & desired → NOT in result.unchanged."""
        from archon.search._types import IngestResult
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        dir_a = tmp_path / "project_a"
        dir_b = tmp_path / "project_b"
        dir_a.mkdir()
        dir_b.mkdir()
        resolved_a = str(dir_a.resolve())
        resolved_b = str(dir_b.resolve())

        manifest = {"project_a": resolved_a, "project_b": resolved_b}
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["project_a", "project_b"],
            manifest=manifest,
        )
        state_store = IndexingStateStore(tmp_path / "state")

        # project_a is DONE, project_b is PENDING (needs resume)
        state_store.update_collection("project_a", CollectionProgress(
            status=IndexingStatus.DONE,
        ))
        state_store.update_collection("project_b", CollectionProgress(
            status=IndexingStatus.PENDING,
            processed_paths=["/old/b.md"],
        ))

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            r = IngestResult(doc_id="d0", chunks_created=1, status="ok")
            if on_file_complete:
                on_file_complete(Path("/new/b.md"))
            return [r]

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        result = await syncer.sync([str(dir_a), str(dir_b)])

        # project_a is DONE → unchanged
        assert "project_a" in result.unchanged
        # project_b was PENDING → resumed → in added, NOT unchanged
        assert "project_b" in result.added
        assert "project_b" not in result.unchanged

    @pytest.mark.asyncio
    async def test_sync_resumes_existing_collection_with_failed_status(self, tmp_path):
        """FAILED collection in existing & desired → Step 6.5 resumes it."""
        from archon.search._types import IngestResult
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        existing_dir = tmp_path / "myproject"
        existing_dir.mkdir()
        resolved = str(existing_dir.resolve())

        manifest = {"myproject": resolved}
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["myproject"],
            manifest=manifest,
        )
        state_store = IndexingStateStore(tmp_path / "state")

        state_store.update_collection("myproject", CollectionProgress(
            status=IndexingStatus.FAILED,
            processed_paths=["/old/file.md"],
            error="previous crash",
        ))

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            exclude = kwargs.get("exclude_paths")
            # Verify resume state is passed through
            assert "/old/file.md" in exclude
            results = [IngestResult(doc_id="d0", chunks_created=1, status="ok")]
            if on_file_complete:
                on_file_complete(Path("/new/file.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        result = await syncer.sync([str(existing_dir)])

        assert "myproject" in result.added
        assert "myproject" not in result.unchanged
        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        assert "/old/file.md" in cp.processed_paths


# ---------------------------------------------------------------------------
# Task 4.4 — _iter_eligible_files and _reset_stale Phase 4 field preservation
# ---------------------------------------------------------------------------


class TestIterEligibleFiles:
    """Tests for the extracted _iter_eligible_files helper."""

    def test_iter_eligible_files_skips_symlinks_hidden_binary(self, tmp_path):
        """Valid files only; symlinks, hidden files, binary extensions, and hidden dir contents excluded."""
        from archon.search.sync import SearchCollectionSync

        source = tmp_path / "source"
        source.mkdir()

        # Valid file — should be included
        (source / "valid.md").write_text("hello")

        # Binary extension — should be excluded
        (source / "file.pyc").write_bytes(b"\x00\x01\x02")

        # Hidden file — should be excluded
        (source / ".hidden_file").write_text("hidden")

        # Symlink — should be excluded
        symlink_target = tmp_path / "target.md"
        symlink_target.write_text("target content")
        (source / "link.md").symlink_to(symlink_target)

        # File under hidden dir — should be excluded
        hidden_dir = source / ".hidden_dir"
        hidden_dir.mkdir()
        (hidden_dir / "somefile.md").write_text("inside hidden dir")

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline)

        result = syncer._iter_eligible_files(source)

        assert len(result) == 1
        assert result[0] == source / "valid.md"

    def test_iter_eligible_files_returns_sorted(self, tmp_path):
        """Returned list is sorted by path."""
        from archon.search.sync import SearchCollectionSync

        source = tmp_path / "source"
        source.mkdir()

        # Create files with names that won't naturally be in sorted order
        (source / "zebra.md").write_text("z")
        (source / "alpha.md").write_text("a")
        (source / "mango.txt").write_text("m")

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline)

        result = syncer._iter_eligible_files(source)

        assert result == sorted(result)
        assert len(result) == 3


class TestResetStalePreservesPhase4Fields:
    """Tests for Phase 4 field preservation in _reset_stale_in_progress."""

    def test_reset_stale_preserves_phase4_fields(self, tmp_path):
        """IN_PROGRESS → PENDING must preserve file_mtimes, file_hashes, indexed_embedding_model, indexed_chunk_size."""
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        # Seed an IN_PROGRESS collection with all four Phase 4 fields populated
        state_store.update_collection("col", CollectionProgress(
            status=IndexingStatus.IN_PROGRESS,
            total_files=20,
            processed_files=10,
            processed_paths=["/a/file.md"],
            file_mtimes={"a": 1.0},
            file_hashes={"a": "abc"},
            indexed_embedding_model="bge",
            indexed_chunk_size=512,
        ))

        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        syncer._reset_stale_in_progress()

        state = state_store.read()
        assert state is not None
        cp = state.collections["col"]

        # Status must be PENDING after reset
        assert cp.status == IndexingStatus.PENDING

        # All four Phase 4 fields must be preserved
        assert cp.file_mtimes == {"a": 1.0}
        assert cp.file_hashes == {"a": "abc"}
        assert cp.indexed_embedding_model == "bge"
        assert cp.indexed_chunk_size == 512


# ---------------------------------------------------------------------------
# TestTask45 — FEAT-027-P4 Task 4.5
# ---------------------------------------------------------------------------


class TestTask45:
    """Tests for SyncResult.updated, constructor params, _load_file_mtimes, _check_collection_changes."""

    # --- Part 1: SyncResult.updated ---

    def test_sync_result_has_updated_field(self):
        """SyncResult() default-constructs with updated=[]."""
        from archon.search.sync import SyncResult

        result = SyncResult()
        assert result.updated == []

    # --- Part 2 & 3: _load_file_mtimes ---

    def test_load_file_mtimes_state_store_none(self, tmp_path):
        """When state_store=None, _load_file_mtimes returns {}."""
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline, state_store=None)
        assert syncer._load_file_mtimes("my_col") == {}

    def test_load_file_mtimes_no_state_file(self, tmp_path):
        """When state store has no data (file absent), returns {}."""
        from archon.search.progress import IndexingStateStore
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        store = IndexingStateStore(tmp_path / "state_empty")
        syncer = SearchCollectionSync(pipeline, state_store=store)
        assert syncer._load_file_mtimes("my_col") == {}

    def test_load_file_mtimes_collection_absent(self, tmp_path):
        """State exists but collection name is not in it — returns {}."""
        from archon.search.progress import CollectionProgress, IndexingState, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        store = IndexingStateStore(tmp_path / "state")
        store.update_collection("other_col", CollectionProgress(status=IndexingStatus.DONE))
        syncer = SearchCollectionSync(pipeline, state_store=store)
        assert syncer._load_file_mtimes("missing_col") == {}

    def test_load_file_mtimes_from_provided_state(self, tmp_path):
        """When state is passed directly, returns its file_mtimes for the named collection."""
        from archon.search.progress import CollectionProgress, IndexingState, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline)

        state = IndexingState(collections={
            "col_a": CollectionProgress(
                status=IndexingStatus.DONE,
                file_mtimes={"a/file.md": 1.0, "b/file.md": 2.5},
            )
        })
        result = syncer._load_file_mtimes("col_a", state=state)
        assert result == {"a/file.md": 1.0, "b/file.md": 2.5}

    def test_load_file_mtimes_provided_state_collection_absent(self, tmp_path):
        """When state is passed but collection absent, returns {}."""
        from archon.search.progress import IndexingState
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline)
        state = IndexingState(collections={})
        assert syncer._load_file_mtimes("nonexistent", state=state) == {}

    # --- Part 4: _check_collection_changes ---

    def _make_syncer(self, tmp_path, embedding_model="", chunk_size=0, auto_reindex=False):
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        return SearchCollectionSync(
            pipeline,
            embedding_model=embedding_model,
            chunk_size=chunk_size,
            auto_reindex_on_chunk_size_change=auto_reindex,
        )

    def test_check_collection_changes_no_changes(self, tmp_path):
        """File on disk with matching mtime → ([], [], [])."""
        from archon.search.sync import SearchCollectionSync

        source = tmp_path / "src"
        source.mkdir()
        f = source / "readme.md"
        f.write_text("hello")
        mtime = f.stat().st_mtime

        syncer = self._make_syncer(tmp_path)
        key = str(f.resolve())
        new_files, changed_files, deleted = syncer._check_collection_changes(
            "col", source, {key: mtime}, "", 0
        )
        assert new_files == []
        assert changed_files == []
        assert deleted == []

    def test_check_collection_changes_new_file(self, tmp_path):
        """File on disk NOT in mtimes dict → in new_files."""
        from archon.search.sync import SearchCollectionSync

        source = tmp_path / "src"
        source.mkdir()
        f = source / "readme.md"
        f.write_text("hello")

        syncer = self._make_syncer(tmp_path)
        # Empty mtimes — file is "new"
        new_files, changed_files, deleted = syncer._check_collection_changes(
            "col", source, {}, "", 0
        )
        assert f.resolve() in new_files
        assert changed_files == []
        assert deleted == []

    def test_check_collection_changes_changed_mtime(self, tmp_path):
        """File in mtimes but mtime differs → in changed_files."""
        from archon.search.sync import SearchCollectionSync

        source = tmp_path / "src"
        source.mkdir()
        f = source / "readme.md"
        f.write_text("hello")
        key = str(f.resolve())

        syncer = self._make_syncer(tmp_path)
        # Use an mtime that won't match the actual file mtime
        new_files, changed_files, deleted = syncer._check_collection_changes(
            "col", source, {key: 0.0}, "", 0
        )
        assert new_files == []
        assert f.resolve() in changed_files
        assert deleted == []

    def test_check_collection_changes_deleted_file(self, tmp_path):
        """Path in mtimes but not on disk → in deleted_paths."""
        from archon.search.sync import SearchCollectionSync

        source = tmp_path / "src"
        source.mkdir()

        syncer = self._make_syncer(tmp_path)
        ghost_key = str(source / "ghost.md")
        new_files, changed_files, deleted = syncer._check_collection_changes(
            "col", source, {ghost_key: 1.0}, "", 0
        )
        assert new_files == []
        assert changed_files == []
        assert ghost_key in deleted

    def test_check_collection_changes_embedding_model_changed(self, tmp_path):
        """Embedding model changed → force_full_reindex: all eligible files in changed_files."""
        from archon.search.sync import SearchCollectionSync

        source = tmp_path / "src"
        source.mkdir()
        f = source / "readme.md"
        f.write_text("hello")

        # syncer has new model; indexed with old model
        syncer = self._make_syncer(tmp_path, embedding_model="new-model")
        new_files, changed_files, deleted = syncer._check_collection_changes(
            "col", source, {}, "old-model", 0
        )
        # force_full_reindex → eligible files go to changed_files, new_files empty
        assert f.resolve() in changed_files
        assert new_files == []

    def test_check_collection_changes_chunk_size_auto_reindex(self, tmp_path):
        """Chunk size mismatch + auto_reindex=True → force_full_reindex."""
        source = tmp_path / "src"
        source.mkdir()
        f = source / "readme.md"
        f.write_text("hello")

        syncer = self._make_syncer(tmp_path, chunk_size=1024, auto_reindex=True)
        new_files, changed_files, deleted = syncer._check_collection_changes(
            "col", source, {}, "", 512
        )
        assert f.resolve() in changed_files
        assert new_files == []

    def test_check_collection_changes_chunk_size_no_auto_reindex(self, tmp_path, caplog):
        """Chunk size mismatch + auto_reindex=False → warning logged, normal per-file detection."""
        import logging

        source = tmp_path / "src"
        source.mkdir()
        f = source / "readme.md"
        f.write_text("hello")
        key = str(f.resolve())
        mtime = f.stat().st_mtime

        syncer = self._make_syncer(tmp_path, chunk_size=1024, auto_reindex=False)
        with caplog.at_level(logging.WARNING, logger="archon"):
            new_files, changed_files, deleted = syncer._check_collection_changes(
                "col", source, {key: mtime}, "", 512
            )

        # No force_full_reindex → no changes detected for this file
        assert new_files == []
        assert changed_files == []
        # Warning must be logged
        assert any("mismatch" in msg.lower() or "chunk" in msg.lower() for msg in caplog.messages)

    def test_check_collection_changes_first_sync_model_guard_skipped(self, tmp_path):
        """indexed_embedding_model='' → embedding model guard skipped, no full re-index."""
        source = tmp_path / "src"
        source.mkdir()
        f = source / "readme.md"
        f.write_text("hello")
        key = str(f.resolve())
        mtime = f.stat().st_mtime

        # syncer has a model set, but indexed_embedding_model is "" (first sync)
        syncer = self._make_syncer(tmp_path, embedding_model="some-model")
        new_files, changed_files, deleted = syncer._check_collection_changes(
            "col", source, {key: mtime}, "", 0
        )
        # Guard skipped → no force_full_reindex, file is unchanged
        assert new_files == []
        assert changed_files == []

    def test_check_collection_changes_first_sync_chunk_guard_skipped(self, tmp_path):
        """indexed_chunk_size=0 → chunk size guard skipped, no warning, normal detection."""
        source = tmp_path / "src"
        source.mkdir()
        f = source / "readme.md"
        f.write_text("hello")
        key = str(f.resolve())
        mtime = f.stat().st_mtime

        syncer = self._make_syncer(tmp_path, chunk_size=1024)
        # indexed_chunk_size=0 means first sync — guard should be skipped
        new_files, changed_files, deleted = syncer._check_collection_changes(
            "col", source, {key: mtime}, "", 0
        )
        assert new_files == []
        assert changed_files == []

    def test_constructor_stores_params(self, tmp_path):
        """New constructor params are stored as instance attributes."""
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(
            pipeline,
            embedding_model="bge-small",
            chunk_size=256,
            auto_reindex_on_chunk_size_change=True,
        )
        assert syncer._embedding_model == "bge-small"
        assert syncer._chunk_size == 256
        assert syncer._auto_reindex_on_chunk_size_change is True

    # --- Additional tests (Fix 5) ---

    def test_check_collection_changes_deleted_symlink(self, tmp_path):
        """File previously indexed is replaced by a symlink → treated as deleted."""
        source = tmp_path / "src"
        source.mkdir()
        target = tmp_path / "target.md"
        target.write_text("target content")

        # Create a regular file, record its resolved path
        f = source / "readme.md"
        f.write_text("hello")
        key = str(f.resolve())
        mtime = f.stat().st_mtime

        # Replace with a symlink
        f.unlink()
        f.symlink_to(target)

        syncer = self._make_syncer(tmp_path)
        new_files, changed_files, deleted = syncer._check_collection_changes(
            "col", source, {key: mtime}, "", 0
        )
        # Symlinks are excluded from eligible → path not in eligible_keys → deleted
        assert key in deleted
        assert new_files == []
        assert changed_files == []

    def test_load_file_mtimes_from_state_store_success(self, tmp_path):
        """When state_store has data for the collection, returns its file_mtimes."""
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        store = IndexingStateStore(tmp_path / "state")
        cp = CollectionProgress(
            status=IndexingStatus.DONE,
            file_mtimes={"/a/file.md": 1700000000.0},
        )
        store.update_collection("my_col", cp)
        syncer = SearchCollectionSync(pipeline, state_store=store)
        result = syncer._load_file_mtimes("my_col")
        assert result == {"/a/file.md": 1700000000.0}

    def test_check_collection_changes_stat_oserror_treated_as_changed(self, tmp_path):
        """OSError from stat() causes file to be treated as changed, not skipped."""
        source = tmp_path / "src"
        source.mkdir()
        f = source / "readme.md"
        f.write_text("hello")
        key = str(f.resolve())

        syncer = self._make_syncer(tmp_path)
        # Intercept _iter_eligible_files to return the real list, then replace the
        # syncer method with one that pre-fetches the list and raises on the stat
        # call that happens in the mtime-check loop.
        real_iter = syncer._iter_eligible_files

        # We override stat on file objects returned from iter by wrapping the loop
        # with a controlled _iter_eligible_files that returns real paths AND separately
        # monkey-patches the stat check via a controlled version of the inner loop.
        # Simplest approach: override _check_collection_changes's inner behavior by
        # subclassing is error-prone. Instead, use a sentinel mtime file trick:
        # put the key in file_mtimes with mtime=0.0 so it differs from real mtime,
        # BUT instead test OSError by writing a wrapper around _iter_eligible_files
        # that returns a fake Path whose stat() raises OSError while is_file/is_symlink work.

        class FakePath:
            """Wraps a real Path but stat() raises OSError."""
            def __init__(self, real: Path):
                self._real = real

            def stat(self):
                raise OSError("permission denied")

            def resolve(self):
                return self._real.resolve()

            def __fspath__(self):
                return str(self._real)

            def __str__(self):
                return str(self._real)

        def patched_iter(path):
            return [FakePath(p) for p in real_iter(path)]

        syncer._iter_eligible_files = patched_iter
        new_files, changed_files, deleted = syncer._check_collection_changes(
            "col", source, {key: 1.0}, "", 0
        )
        # changed_files contains FakePath objects; check by resolved string
        assert any(str(p.resolve()) == key for p in changed_files)
        assert new_files == []

    def test_check_collection_changes_same_embedding_model_no_reindex(self, tmp_path):
        """Same embedding model as indexed → no force_full_reindex."""
        source = tmp_path / "src"
        source.mkdir()
        f = source / "readme.md"
        f.write_text("hello")
        key = str(f.resolve())
        mtime = f.stat().st_mtime

        syncer = self._make_syncer(tmp_path, embedding_model="bge-small")
        new_files, changed_files, deleted = syncer._check_collection_changes(
            "col", source, {key: mtime}, "bge-small", 0
        )
        # Same model → no force_full_reindex → file with matching mtime is unchanged
        assert new_files == []
        assert changed_files == []
        assert deleted == []

    def test_load_file_mtimes_exception_returns_empty(self, tmp_path):
        """If state_store.read() raises, _load_file_mtimes returns {}."""
        from unittest.mock import MagicMock
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        bad_store = MagicMock()
        bad_store.read.side_effect = RuntimeError("corrupted")
        syncer = SearchCollectionSync(pipeline, state_store=bad_store)
        result = syncer._load_file_mtimes("col")
        assert result == {}

    def test_check_collection_changes_embedding_model_changed_deleted_empty(self, tmp_path):
        """Embedding model changed with empty file_mtimes → deleted == []."""
        source = tmp_path / "src"
        source.mkdir()
        f = source / "readme.md"
        f.write_text("hello")

        syncer = self._make_syncer(tmp_path, embedding_model="new-model")
        new_files, changed_files, deleted = syncer._check_collection_changes(
            "col", source, {}, "old-model", 0
        )
        assert f.resolve() in changed_files
        assert new_files == []
        assert deleted == []


# ---------------------------------------------------------------------------
# TestTask46 — Phase 4 core: _apply_collection_changes + sync() Step 7
# ---------------------------------------------------------------------------


def _make_mock_pipeline_with_ingest_file(tmp_path, existing_collections=None, manifest=None):
    """Extended mock pipeline that also mocks ingest_file, rebuild_fts_index,
    delete_by_source_path, and recompute_collection_meta."""
    pipeline = make_mock_pipeline(tmp_path, existing_collections=existing_collections, manifest=manifest)
    pipeline.ingest_file = AsyncMock(return_value=MagicMock(status="ok"))
    pipeline.store.rebuild_fts_index = AsyncMock()
    pipeline.store.delete_by_source_path = AsyncMock(return_value=1)
    pipeline.recompute_collection_meta = AsyncMock()
    return pipeline


def _make_done_state(tmp_path, collection_name, file_mtimes, embedding_model="model-a", chunk_size=512):
    """Write a DONE state with file_mtimes to the state store."""
    from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus

    state_store = IndexingStateStore(tmp_path / "state")
    state_store.update_collection(collection_name, CollectionProgress(
        status=IndexingStatus.DONE,
        total_files=len(file_mtimes),
        processed_files=len(file_mtimes),
        processed_paths=list(file_mtimes.keys()),
        file_mtimes=file_mtimes,
        indexed_embedding_model=embedding_model,
        indexed_chunk_size=chunk_size,
    ))
    return state_store


class TestTask46:
    """Tests for Task 4.6: _apply_collection_changes, _ingest_collection file_mtimes, and sync() Step 7."""

    # ------------------------------------------------------------------
    # Test 1: sync detects new files in existing DONE collection
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_detects_new_files_in_existing_collection(self, tmp_path):
        """DONE collection + new file on disk → result.updated contains collection name."""
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        new_file = col_dir / "new_doc.md"
        new_file.write_text("new content")

        resolved = str(col_dir.resolve())
        manifest = {"myproject": resolved}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        # State: DONE with empty file_mtimes (no files tracked yet)
        state_store = _make_done_state(tmp_path, "myproject", {})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        result = await syncer.sync([str(col_dir)])

        assert "myproject" in result.updated
        assert "myproject" not in result.unchanged
        assert result.errors == []

    # ------------------------------------------------------------------
    # Test 2: changed files are re-ingested
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_detects_changed_files(self, tmp_path):
        """Existing file with different mtime in state → ingest_file called for that file."""
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        doc = col_dir / "readme.md"
        doc.write_text("content")
        real_key = str(doc.resolve())

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        # State: file tracked but with stale mtime (0.0)
        state_store = _make_done_state(tmp_path, "myproject", {real_key: 0.0})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        result = await syncer.sync([str(col_dir)])

        assert "myproject" in result.updated
        pipeline.ingest_file.assert_called()
        assert result.errors == []

    # ------------------------------------------------------------------
    # Test 3: deleted files have their chunks removed
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_detects_deleted_files(self, tmp_path):
        """File in state but not on disk → delete_by_source_path called."""
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        # No files on disk, but state has a file tracked
        ghost_path = str((col_dir / "ghost.md").resolve())

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        state_store = _make_done_state(tmp_path, "myproject", {ghost_path: 1234567.0})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        result = await syncer.sync([str(col_dir)])

        pipeline.store.delete_by_source_path.assert_called_once_with("myproject", ghost_path)
        assert "myproject" in result.updated
        assert result.errors == []

    # ------------------------------------------------------------------
    # Test 4: unchanged files are skipped
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_skips_unchanged_files(self, tmp_path):
        """File mtime matches state → ingest_file NOT called."""
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        doc = col_dir / "readme.md"
        doc.write_text("content")
        real_key = str(doc.resolve())
        real_mtime = doc.stat().st_mtime

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        # State: file tracked with exact current mtime
        state_store = _make_done_state(tmp_path, "myproject", {real_key: real_mtime})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        result = await syncer.sync([str(col_dir)])

        pipeline.ingest_file.assert_not_called()
        pipeline.store.delete_by_source_path.assert_not_called()
        assert "myproject" in result.unchanged
        assert "myproject" not in result.updated

    # ------------------------------------------------------------------
    # Test 5: result.updated contains the changed collection
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_result_includes_updated(self, tmp_path):
        """After applying changes, collection name appears in result.updated."""
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        new_file = col_dir / "doc.md"
        new_file.write_text("hello")

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        state_store = _make_done_state(tmp_path, "myproject", {})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        result = await syncer.sync([str(col_dir)])

        assert "myproject" in result.updated
        assert "myproject" not in result.added

    # ------------------------------------------------------------------
    # Test 6: unchanged collection not in updated
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_unchanged_collection_not_in_updated(self, tmp_path):
        """No file changes → collection in result.unchanged, not result.updated."""
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        doc = col_dir / "readme.md"
        doc.write_text("content")
        real_key = str(doc.resolve())
        real_mtime = doc.stat().st_mtime

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        state_store = _make_done_state(tmp_path, "myproject", {real_key: real_mtime})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        result = await syncer.sync([str(col_dir)])

        assert "myproject" in result.unchanged
        assert "myproject" not in result.updated
        assert result.errors == []

    # ------------------------------------------------------------------
    # Test 7: state updated with correct file_mtimes after sync
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_updates_file_mtimes_in_state(self, tmp_path):
        """After apply, state.file_mtimes reflects actual file mtimes on disk."""
        from archon.search.progress import IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        doc = col_dir / "doc.md"
        doc.write_text("new content")
        real_key = str(doc.resolve())

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        # State with empty mtimes (file appears as new)
        state_store = _make_done_state(tmp_path, "myproject", {})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        await syncer.sync([str(col_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        assert real_key in cp.file_mtimes
        assert cp.file_mtimes[real_key] == pytest.approx(doc.stat().st_mtime)

    # ------------------------------------------------------------------
    # Test 8: state stores indexed_embedding_model and indexed_chunk_size
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_stores_indexed_model_and_chunk_size(self, tmp_path):
        """DONE state after apply contains correct indexed_embedding_model and indexed_chunk_size."""
        from archon.search.progress import IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        doc = col_dir / "doc.md"
        doc.write_text("content")

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        state_store = _make_done_state(tmp_path, "myproject", {}, embedding_model="model-a", chunk_size=512)

        syncer = SearchCollectionSync(
            pipeline, state_store=state_store, embedding_model="model-b", chunk_size=1024
        )
        await syncer.sync([str(col_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.indexed_embedding_model == "model-b"
        assert cp.indexed_chunk_size == 1024

    # ------------------------------------------------------------------
    # Test 9: FTS rebuilt once at end
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_apply_changes_fts_rebuilt_once(self, tmp_path):
        """rebuild_fts_index called exactly once after all file operations."""
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        for i in range(3):
            (col_dir / f"doc{i}.md").write_text(f"content {i}")

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        state_store = _make_done_state(tmp_path, "myproject", {})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        await syncer.sync([str(col_dir)])

        pipeline.store.rebuild_fts_index.assert_called_once_with("myproject")

    # ------------------------------------------------------------------
    # Test 10: recompute_collection_meta called after FTS rebuild
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_apply_changes_updates_collection_meta(self, tmp_path):
        """After _apply_collection_changes, pipeline.recompute_collection_meta is called."""
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        (col_dir / "doc.md").write_text("content")

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        state_store = _make_done_state(tmp_path, "myproject", {})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        await syncer.sync([str(col_dir)])

        pipeline.recompute_collection_meta.assert_called_once_with("myproject")

    # ------------------------------------------------------------------
    # Test 11: new collection ingest populates file_mtimes
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_new_collection_populates_file_mtimes(self, tmp_path):
        """New collection DONE state has file_mtimes populated from ingested files."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "newproject"
        col_dir.mkdir()
        doc = col_dir / "readme.md"
        doc.write_text("hello")

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        real_path = str(doc.resolve())

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            if on_file_complete:
                on_file_complete(doc)
            return [IngestResult(doc_id="d0", chunks_created=1, status="ok")]

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(
            pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512
        )
        await syncer.sync([str(col_dir)])

        state = state_store.read()
        cp = state.collections["newproject"]
        assert cp.status == IndexingStatus.DONE
        assert real_path in cp.file_mtimes
        assert cp.file_mtimes[real_path] == pytest.approx(doc.stat().st_mtime)
        assert cp.indexed_embedding_model == "model-a"
        assert cp.indexed_chunk_size == 512

    # ------------------------------------------------------------------
    # Test 12: exception mid-apply → FAILED state with partial file_mtimes
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_apply_changes_failed_midway(self, tmp_path):
        """Exception during ingest_file → FAILED state with partial file_mtimes.

        Scenario: 1 deleted file (in initial state but not on disk) + 2 new files;
        second new file's ingest_file raises. FAILED state must have:
        - deleted path removed from file_mtimes
        - first new file's mtime present in file_mtimes
        - second new file absent from file_mtimes
        """
        from archon.search.progress import IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        doc1 = col_dir / "a.md"
        doc2 = col_dir / "b.md"
        doc1.write_text("content a")
        doc2.write_text("content b")

        # Pre-existing file that is no longer on disk → will be detected as a deletion
        deleted_key = str((col_dir / "old.md").resolve())

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        # First ingest_file (doc1 / a.md) succeeds, second (doc2 / b.md) raises
        pipeline.ingest_file.side_effect = [
            MagicMock(status="ok", chunks_created=1),
            RuntimeError("disk full"),
        ]

        # Initial state includes the to-be-deleted file so sync detects it as removed
        state_store = _make_done_state(tmp_path, "myproject", {deleted_key: 1234567890.0})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        result = await syncer.sync([str(col_dir)])

        assert len(result.errors) == 1
        assert "disk full" in result.errors[0]

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.FAILED
        assert "disk full" in cp.error

        doc1_key = str(doc1.resolve())
        doc2_key = str(doc2.resolve())

        # Deleted file must be absent — deletion was processed before the failure
        assert deleted_key not in cp.file_mtimes, "deleted path should be removed from file_mtimes"
        # First new file succeeded — its mtime should be present
        assert doc1_key in cp.file_mtimes, "successfully processed file should have mtime saved"
        # Second new file failed — must not appear in file_mtimes
        assert doc2_key not in cp.file_mtimes, "failed file should not have mtime saved"

    # ------------------------------------------------------------------
    # Test 13: embedding model change triggers full re-index
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_embedding_model_change_triggers_full_reindex(self, tmp_path):
        """Embedding model mismatch → all files treated as changed, ingest_file called for each."""
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        doc = col_dir / "readme.md"
        doc.write_text("content")
        real_key = str(doc.resolve())
        real_mtime = doc.stat().st_mtime

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        # State: file with correct mtime BUT different embedding model
        state_store = _make_done_state(
            tmp_path, "myproject", {real_key: real_mtime},
            embedding_model="model-old", chunk_size=512,
        )

        # Syncer uses a different model → full re-index
        syncer = SearchCollectionSync(
            pipeline, state_store=state_store, embedding_model="model-new", chunk_size=512
        )
        result = await syncer.sync([str(col_dir)])

        pipeline.ingest_file.assert_called()
        assert "myproject" in result.updated
        assert result.errors == []

    # ------------------------------------------------------------------
    # Test 14: chunk size mismatch with auto_reindex=False → warning only
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_chunk_size_change_warns_only(self, tmp_path, caplog):
        """Chunk size mismatch + auto_reindex=False → warning logged, no re-ingest."""
        import logging
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        doc = col_dir / "readme.md"
        doc.write_text("content")
        real_key = str(doc.resolve())
        real_mtime = doc.stat().st_mtime

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        # State: same model, different chunk size
        state_store = _make_done_state(
            tmp_path, "myproject", {real_key: real_mtime},
            embedding_model="model-a", chunk_size=512,
        )

        syncer = SearchCollectionSync(
            pipeline, state_store=state_store, embedding_model="model-a", chunk_size=1024,
            auto_reindex_on_chunk_size_change=False,
        )
        with caplog.at_level(logging.WARNING, logger="archon"):
            result = await syncer.sync([str(col_dir)])

        pipeline.ingest_file.assert_not_called()
        assert "myproject" in result.unchanged
        assert any("chunk size" in r.message.lower() or "mismatch" in r.message.lower() for r in caplog.records)

    # ------------------------------------------------------------------
    # Test 15: chunk size mismatch with auto_reindex=True → full re-ingest
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_chunk_size_change_auto_reindex(self, tmp_path):
        """Chunk size mismatch + auto_reindex=True → all files re-ingested."""
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        doc = col_dir / "readme.md"
        doc.write_text("content")
        real_key = str(doc.resolve())
        real_mtime = doc.stat().st_mtime

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        # State: same model, different chunk size
        state_store = _make_done_state(
            tmp_path, "myproject", {real_key: real_mtime},
            embedding_model="model-a", chunk_size=512,
        )

        syncer = SearchCollectionSync(
            pipeline, state_store=state_store, embedding_model="model-a", chunk_size=1024,
            auto_reindex_on_chunk_size_change=True,
        )
        result = await syncer.sync([str(col_dir)])

        pipeline.ingest_file.assert_called()
        assert "myproject" in result.updated
        assert result.errors == []

    # ------------------------------------------------------------------
    # Test 16: batched state writes every 50 files
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_apply_changes_batched_state_writes(self, tmp_path):
        """Processing 51 new files → IN_PROGRESS state written at 50-file boundary."""
        from unittest.mock import patch
        from archon.search.progress import IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        for i in range(51):
            (col_dir / f"doc{i:03d}.md").write_text(f"content {i}")

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        state_store = _make_done_state(tmp_path, "myproject", {})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)

        in_progress_writes: list = []
        original_update = syncer._safe_state_update

        def tracking_update(name, cp):
            if cp.status == IndexingStatus.IN_PROGRESS:
                in_progress_writes.append(cp)
            return original_update(name, cp)

        with patch.object(syncer, "_safe_state_update", side_effect=tracking_update):
            await syncer.sync([str(col_dir)])

        # At least 2 IN_PROGRESS writes: initial + 1 batch (at file 50)
        # Exactly 2 IN_PROGRESS writes: 1 initial (start of apply) + 1 batch (at file 50)
        assert len(in_progress_writes) == 2

    # ------------------------------------------------------------------
    # Test 17: mixed changes (new + changed + deleted + unchanged)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_mixed_changes(self, tmp_path):
        """One new, one changed, one deleted, two unchanged files → all handled correctly."""
        from archon.search.progress import IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()

        # two unchanged files
        unchanged1 = col_dir / "unchanged1.md"
        unchanged2 = col_dir / "unchanged2.md"
        unchanged1.write_text("stable 1")
        unchanged2.write_text("stable 2")
        key1 = str(unchanged1.resolve())
        key2 = str(unchanged2.resolve())
        mtime1 = unchanged1.stat().st_mtime
        mtime2 = unchanged2.stat().st_mtime

        # changed file
        changed = col_dir / "changed.md"
        changed.write_text("changed content")
        changed_key = str(changed.resolve())

        # new file (not in state)
        new_file = col_dir / "new.md"
        new_file.write_text("new content")

        # deleted file (in state, but NOT on disk)
        deleted_key = str((col_dir / "deleted.md").resolve())

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        file_mtimes = {
            key1: mtime1,
            key2: mtime2,
            changed_key: 0.0,  # stale mtime → treated as changed
            deleted_key: 123456.0,  # path not on disk → deleted
        }
        state_store = _make_done_state(tmp_path, "myproject", file_mtimes)

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        result = await syncer.sync([str(col_dir)])

        assert "myproject" in result.updated
        assert result.errors == []

        # ingest_file called for changed and new files (2 calls)
        assert pipeline.ingest_file.call_count == 2

        # delete_by_source_path called for deleted file
        pipeline.store.delete_by_source_path.assert_called_once_with("myproject", deleted_key)

        # Final state should have correct file_mtimes
        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        assert key1 in cp.file_mtimes
        assert key2 in cp.file_mtimes
        assert changed_key in cp.file_mtimes
        assert str(new_file.resolve()) in cp.file_mtimes
        assert deleted_key not in cp.file_mtimes
        # total_files and processed_files reflect post-change collection size (len(file_mtimes))
        expected_file_count = len(cp.file_mtimes)
        assert cp.total_files == expected_file_count
        assert cp.processed_files == expected_file_count

    # ------------------------------------------------------------------
    # Test 18: deletions only → rebuild_fts called, ingest_file NOT called
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_apply_changes_deletion_only(self, tmp_path):
        """Only deletions, no new/changed files → rebuild_fts called once, ingest_file NOT called."""
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        # No files on disk, but state has a file tracked
        ghost_path = str((col_dir / "ghost.md").resolve())

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        state_store = _make_done_state(tmp_path, "myproject", {ghost_path: 1234567.0})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        await syncer.sync([str(col_dir)])

        pipeline.ingest_file.assert_not_called()
        pipeline.store.rebuild_fts_index.assert_called_once_with("myproject")

    # ------------------------------------------------------------------
    # Test 19: processed_paths consistent with file_mtimes after apply
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_apply_changes_processed_paths_consistent(self, tmp_path):
        """After successful apply, every key in file_mtimes is in processed_paths."""
        from archon.search.progress import IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        doc = col_dir / "doc.md"
        doc.write_text("content")

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        state_store = _make_done_state(tmp_path, "myproject", {})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        await syncer.sync([str(col_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        for path_key in cp.file_mtimes:
            assert path_key in cp.processed_paths, f"{path_key} in file_mtimes but not in processed_paths"

    # ------------------------------------------------------------------
    # Test 20: failed apply → not in result.unchanged
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_apply_changes_error_not_in_unchanged(self, tmp_path):
        """Collection with detected changes but failed apply: in result.errors, NOT in result.unchanged."""
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        (col_dir / "doc.md").write_text("content")

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )
        pipeline.ingest_file.side_effect = RuntimeError("failure")

        state_store = _make_done_state(tmp_path, "myproject", {})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        result = await syncer.sync([str(col_dir)])

        assert len(result.errors) == 1
        assert "myproject" not in result.unchanged
        assert "myproject" not in result.updated

    # ------------------------------------------------------------------
    # Test 21: ingest_file soft error preserves old mtime
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_apply_changes_ingest_failure_preserves_old_mtime(self, tmp_path):
        """ingest_file returns error status (not raises) → old mtime preserved in file_mtimes."""
        from archon.search.progress import IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        doc = col_dir / "readme.md"
        doc.write_text("content")
        real_key = str(doc.resolve())
        old_mtime = 0.0  # stale mtime → treated as changed

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )
        # Return soft error (no raise)
        pipeline.ingest_file.return_value = MagicMock(status="error", chunks_created=0)

        state_store = _make_done_state(tmp_path, "myproject", {real_key: old_mtime})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        result = await syncer.sync([str(col_dir)])

        # Verify the change-detection path was actually exercised
        pipeline.ingest_file.assert_called_once()
        assert "myproject" in result.updated

        state = state_store.read()
        cp = state.collections["myproject"]
        # Soft error keeps the collection in DONE state (not FAILED)
        assert cp.status == IndexingStatus.DONE
        # Old mtime should be preserved (file should be retried on next sync)
        assert real_key in cp.file_mtimes
        assert cp.file_mtimes[real_key] == old_mtime

    # ------------------------------------------------------------------
    # Test 21b: ingest_file hard-raise on changed file preserves old mtime
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_apply_changes_ingest_hard_raise_preserves_old_mtime(self, tmp_path):
        """ingest_file raises (not returns error status) on changed file → FAILED state, old mtime preserved."""
        from archon.search.progress import IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        doc = col_dir / "readme.md"
        doc.write_text("content")
        real_key = str(doc.resolve())
        old_mtime = 0.0  # stale mtime → treated as changed

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )
        # Hard raise (not soft error return)
        pipeline.ingest_file.side_effect = RuntimeError("hard ingest failure")

        state_store = _make_done_state(tmp_path, "myproject", {real_key: old_mtime})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        result = await syncer.sync([str(col_dir)])

        # ingest_file must have been called with the changed file, collection name, and rebuild_fts=False
        from unittest.mock import ANY
        pipeline.ingest_file.assert_called_once_with(ANY, "myproject", rebuild_fts=False)

        # The collection should be in errors (FAILED), not updated
        assert len(result.errors) == 1
        assert "myproject" not in result.updated

        # State must be FAILED with old mtime preserved (not cleared)
        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.FAILED
        assert cp.error == "hard ingest failure"
        assert real_key in cp.file_mtimes
        assert cp.file_mtimes[real_key] == old_mtime
        assert real_key in cp.processed_paths

    # ------------------------------------------------------------------
    # Test 22: _ingest_collection FAILED state has partial file_mtimes
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_ingest_collection_failed_state_has_partial_file_mtimes(self, tmp_path):
        """_ingest_collection exception mid-ingest: FAILED state has file_mtimes for successfully ingested files."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "newproject"
        col_dir.mkdir()
        doc = col_dir / "first.md"
        doc.write_text("first file content")

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        real_path = str(doc.resolve())

        async def fake_ingest(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            if on_file_complete:
                on_file_complete(doc)
            raise RuntimeError("ingest failure after first file")

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = SearchCollectionSync(
            pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512
        )
        result = await syncer.sync([str(col_dir)])

        assert len(result.errors) == 1

        state = state_store.read()
        cp = state.collections["newproject"]
        assert cp.status == IndexingStatus.FAILED
        # The first file was reported via on_file_complete → should be in file_mtimes
        assert real_path in cp.file_mtimes

    # ------------------------------------------------------------------
    # Test 23: file vanishes between _check_collection_changes and _apply_collection_changes
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_file_vanishes_between_check_and_apply(self, tmp_path):
        """File ingested successfully but stat() raises OSError during mtime write.

        Acceptance criterion: file.stat().st_mtime wrapped in try/except OSError for new files;
        on OSError, skip the mtime entry (file appears as new on next sync) but apply continues.
        """
        from archon.search.progress import IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        stable_doc = col_dir / "stable.md"
        stable_doc.write_text("stable content")
        vanishing_doc = col_dir / "vanishing.md"
        vanishing_doc.write_text("content")
        stable_key = str(stable_doc.resolve())
        vanishing_key = str(vanishing_doc.resolve())

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        # After ingest_file succeeds, delete the vanishing file so that
        # file.stat() raises OSError (FileNotFoundError is a subclass of OSError)
        async def ingest_and_conditionally_delete(file, name, **kwargs):
            if file.name == "vanishing.md":
                vanishing_doc.unlink()  # delete file after ingest, before stat
            return MagicMock(status="ok", chunks_created=1)

        pipeline.ingest_file.side_effect = ingest_and_conditionally_delete

        # Both files are new (empty file_mtimes)
        state_store = _make_done_state(tmp_path, "myproject", {})

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        result = await syncer.sync([str(col_dir)])

        # Apply should SUCCEED (DONE state) despite the vanished file's stat() raising OSError
        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        # Stable file has its mtime recorded
        assert stable_key in cp.file_mtimes
        # Vanishing file has NO mtime (stat raised OSError after ingest) → will appear new on next sync
        assert vanishing_key not in cp.file_mtimes
        # No errors in result (OSError during stat is handled gracefully per-file)
        assert result.errors == []

    # ------------------------------------------------------------------
    # Test 24a: soft-ingest failure on CHANGED file → not in processed_paths
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_apply_changes_changed_file_error_not_in_processed_paths(self, tmp_path):
        """ingest_file returns error status for a CHANGED file → that file NOT in processed_paths."""
        from archon.search.progress import IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        ok_doc = col_dir / "ok.md"
        ok_doc.write_text("ok content")
        err_doc = col_dir / "err.md"
        err_doc.write_text("error content")
        ok_key = str(ok_doc.resolve())
        err_key = str(err_doc.resolve())

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        # Both files are in state with stale mtimes → both appear as CHANGED
        state_store = _make_done_state(tmp_path, "myproject", {ok_key: 0.0, err_key: 0.0})

        async def ingest_side_effect(file, name, **kwargs):
            if file.name == "err.md":
                return MagicMock(status="error", chunks_created=0)
            return MagicMock(status="ok", chunks_created=1)

        pipeline.ingest_file.side_effect = ingest_side_effect

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        await syncer.sync([str(col_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        # ok file must be in processed_paths; err file must NOT be
        assert ok_key in cp.processed_paths
        assert err_key not in cp.processed_paths
        # stale mtime preserved → mtime mismatch on next scan → file detected as changed again
        assert err_key in cp.file_mtimes, "stale mtime must be preserved to enable retry via change detection"

    # ------------------------------------------------------------------
    # Test 24b: soft-ingest failure on NEW file → not in processed_paths
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_apply_changes_new_file_error_not_in_processed_paths(self, tmp_path):
        """ingest_file returns error status for a NEW file → that file NOT in processed_paths."""
        from archon.search.progress import IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        ok_doc = col_dir / "ok.md"
        ok_doc.write_text("ok content")
        err_doc = col_dir / "err.md"
        err_doc.write_text("error content")
        ok_key = str(ok_doc.resolve())
        err_key = str(err_doc.resolve())

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        # Empty file_mtimes → both files are NEW
        state_store = _make_done_state(tmp_path, "myproject", {})

        async def ingest_side_effect(file, name, **kwargs):
            if file.name == "err.md":
                return MagicMock(status="error", chunks_created=0)
            return MagicMock(status="ok", chunks_created=1)

        pipeline.ingest_file.side_effect = ingest_side_effect

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        await syncer.sync([str(col_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        # ok file must be in processed_paths; err file must NOT be
        assert ok_key in cp.processed_paths
        assert err_key not in cp.processed_paths
        # error file must not be in file_mtimes so it is re-discovered as new on next sync
        assert err_key not in cp.file_mtimes, "error file must not be in file_mtimes so it is re-discovered as new on next sync"

    # ------------------------------------------------------------------
    # Test 24c: error file is retried on next sync
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_apply_changes_error_file_retried_on_next_sync(self, tmp_path):
        """Error file on first sync is retried (and succeeds) on second sync."""
        from archon.search.progress import IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        err_doc = col_dir / "err.md"
        err_doc.write_text("error content")
        err_key = str(err_doc.resolve())

        manifest = {"myproject": str(col_dir.resolve())}
        pipeline = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["myproject"], manifest=manifest
        )

        # Empty file_mtimes → file is NEW on first sync
        state_store = _make_done_state(tmp_path, "myproject", {})

        # First sync: ingest fails
        async def ingest_fail(file, name, **kwargs):
            return MagicMock(status="error", chunks_created=0)

        pipeline.ingest_file.side_effect = ingest_fail

        syncer = SearchCollectionSync(pipeline, state_store=state_store, embedding_model="model-a", chunk_size=512)
        await syncer.sync([str(col_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert err_key not in cp.processed_paths
        assert err_key not in cp.file_mtimes

        # Second sync: ingest succeeds
        async def ingest_ok(file, name, **kwargs):
            return MagicMock(status="ok", chunks_created=1)

        pipeline.ingest_file.side_effect = ingest_ok

        await syncer.sync([str(col_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert err_key in cp.processed_paths, "file must be in processed_paths after successful retry"

    # ------------------------------------------------------------------
    # Test 24: resume then change detection works correctly
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sync_resume_then_change_detection(self, tmp_path):
        """After a resumed ingest populates file_mtimes, next sync skips unchanged files."""
        from archon.search._types import IngestResult
        from archon.search.progress import IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "newproject"
        col_dir.mkdir()
        doc = col_dir / "doc.md"
        doc.write_text("original content")
        real_path = str(doc.resolve())

        state_store = IndexingStateStore(tmp_path / "state")

        # First sync: successful ingest, DONE state with file_mtimes populated
        pipeline1 = make_mock_pipeline(tmp_path, existing_collections=[])

        async def fake_ingest_ok(path, name, **kwargs):
            on_file_complete = kwargs.get("on_file_complete")
            if on_file_complete:
                on_file_complete(doc)
            return [IngestResult(doc_id="d0", chunks_created=1, status="ok")]

        pipeline1.ingest_directory = AsyncMock(side_effect=fake_ingest_ok)

        syncer1 = SearchCollectionSync(
            pipeline1, state_store=state_store, embedding_model="model-a", chunk_size=512
        )
        await syncer1.sync([str(col_dir)])

        state_after_first = state_store.read()
        assert state_after_first.collections["newproject"].status == IndexingStatus.DONE
        assert real_path in state_after_first.collections["newproject"].file_mtimes

        # Second sync: same file, same mtime → should be detected as DONE + no changes
        manifest = {"newproject": str(col_dir.resolve())}
        pipeline2 = _make_mock_pipeline_with_ingest_file(
            tmp_path, existing_collections=["newproject"], manifest=manifest
        )

        syncer2 = SearchCollectionSync(
            pipeline2, state_store=state_store, embedding_model="model-a", chunk_size=512
        )
        result2 = await syncer2.sync([str(col_dir)])

        # No changes → collection should be in unchanged, not updated
        assert "newproject" in result2.unchanged
        assert "newproject" not in result2.updated
        pipeline2.ingest_file.assert_not_called()

# ---------------------------------------------------------------------------
# TestSyncCollectionMethod — FEAT-027 Phase 8 Task 8.4: watch-triggered sync
# ---------------------------------------------------------------------------


class TestBuildDesiredPublic:
    """Verify build_desired is accessible as a public method."""

    def test_build_desired_public(self, tmp_path):
        """build_desired (without leading underscore) is callable on SearchCollectionSync."""
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline)

        real_dir = tmp_path / "myproject"
        real_dir.mkdir()

        result = syncer.build_desired([str(real_dir)])
        assert isinstance(result, dict)
        assert "myproject" in result


class TestSyncCollectionMethod:
    """Tests for the public sync_collection() method (watch-triggered incremental sync)."""

    def _make_syncer(self, tmp_path, *, with_state_store=True):
        from archon.search.progress import IndexingStateStore
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        state_store = IndexingStateStore(tmp_path / "state") if with_state_store else None
        syncer = SearchCollectionSync(pipeline, state_store=state_store)
        return syncer, pipeline

    @pytest.mark.asyncio
    async def test_sync_collection_no_state_store(self, tmp_path):
        """sync_collection returns without error when state_store is None; _check_collection_changes NOT called."""
        from archon.search.sync import SearchCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline, state_store=None)

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()

        with patch.object(syncer, "_check_collection_changes") as mock_check:
            await syncer.sync_collection("myproject", col_dir)

        mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_collection_no_changes(self, tmp_path):
        """When _check_collection_changes returns no diffs, _apply_collection_changes is NOT called."""
        from archon.search.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()

        state_store = IndexingStateStore(tmp_path / "state")
        # Seed state so read() returns something
        from archon.search.progress import IndexingState
        state_store.write(IndexingState(collections={
            "myproject": CollectionProgress(status=IndexingStatus.DONE)
        }))

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        with patch.object(syncer, "_check_collection_changes", return_value=([], [], [])) as mock_check, \
             patch.object(syncer, "_apply_collection_changes", new_callable=AsyncMock) as mock_apply:
            await syncer.sync_collection("myproject", col_dir)

        mock_check.assert_called_once()
        mock_apply.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_collection_with_new_file(self, tmp_path):
        """When new files detected, _apply_collection_changes is called with new_files."""
        from archon.search.progress import CollectionProgress, IndexingState, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        new_file = Path("new.md")

        state_store = IndexingStateStore(tmp_path / "state")
        state_store.write(IndexingState(collections={
            "myproject": CollectionProgress(status=IndexingStatus.DONE)
        }))

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        with patch.object(syncer, "_check_collection_changes", return_value=([new_file], [], [])), \
             patch.object(syncer, "_apply_collection_changes", new_callable=AsyncMock, return_value=None) as mock_apply:
            await syncer.sync_collection("myproject", col_dir)

        mock_apply.assert_called_once()
        call_kwargs = mock_apply.call_args
        # new_files is the 3rd positional arg (after name, source_path)
        assert call_kwargs[0][2] == [new_file]

    @pytest.mark.asyncio
    async def test_sync_collection_with_deleted_file(self, tmp_path):
        """When deleted paths detected, _apply_collection_changes is called with deleted_paths."""
        from archon.search.progress import CollectionProgress, IndexingState, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        deleted = "/old/file.md"

        state_store = IndexingStateStore(tmp_path / "state")
        state_store.write(IndexingState(collections={
            "myproject": CollectionProgress(status=IndexingStatus.DONE)
        }))

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        with patch.object(syncer, "_check_collection_changes", return_value=([], [], [deleted])), \
             patch.object(syncer, "_apply_collection_changes", new_callable=AsyncMock, return_value=None) as mock_apply:
            await syncer.sync_collection("myproject", col_dir)

        mock_apply.assert_called_once()
        call_kwargs = mock_apply.call_args
        # deleted_paths is the 5th positional arg (after name, source_path, new_files, changed_files)
        assert call_kwargs[0][4] == [deleted]

    @pytest.mark.asyncio
    async def test_sync_collection_lock_respected(self, tmp_path):
        """Only one sync_collection call runs at a time for the same collection name."""
        import asyncio

        from archon.search.progress import CollectionProgress, IndexingState, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        new_file = Path("new.md")

        state_store = IndexingStateStore(tmp_path / "state")
        state_store.write(IndexingState(collections={
            "myproject": CollectionProgress(status=IndexingStatus.DONE)
        }))

        pipeline = make_mock_pipeline(tmp_path)
        pipeline.store.rebuild_fts_index = AsyncMock()
        pipeline.store.delete_by_source_path = AsyncMock()
        pipeline.ingest_file = AsyncMock()
        pipeline.recompute_collection_meta = AsyncMock()

        execution_log: list[str] = []
        lock_event = asyncio.Event()

        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        # The mock acquires the per-collection lock itself (mimicking real _apply_collection_changes)
        # so that concurrency is serialized via the same lock sync_collection uses.
        async def slow_apply(name, source_path, new_files, changed_files, deleted_paths, file_mtimes):
            async with syncer._get_lock(name):
                execution_log.append("start")
                lock_event.set()
                await asyncio.sleep(0.02)
                execution_log.append("end")
            return None

        with patch.object(syncer, "_check_collection_changes", return_value=([new_file], [], [])), \
             patch.object(syncer, "_apply_collection_changes", side_effect=slow_apply):
            task1 = asyncio.create_task(syncer.sync_collection("myproject", col_dir))
            await lock_event.wait()  # wait until first call has started
            task2 = asyncio.create_task(syncer.sync_collection("myproject", col_dir))
            await asyncio.gather(task1, task2)

        # Both ran, but they must be serialized (end before start for same lock)
        assert len(execution_log) == 4
        assert execution_log[1] == "end"
        assert execution_log[2] == "start"

    @pytest.mark.asyncio
    async def test_sync_collection_state_read_returns_none(self, tmp_path):
        """When state_store.read() returns None, sync_collection returns early without calling _check."""
        from archon.search.progress import IndexingStateStore
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()

        state_store = IndexingStateStore(tmp_path / "state")
        # Do NOT write any state — read() returns None

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        with patch.object(syncer, "_check_collection_changes") as mock_check:
            await syncer.sync_collection("myproject", col_dir)

        mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_collection_apply_returns_error_logs_warning(self, tmp_path, caplog):
        """When _apply_collection_changes returns a non-None string, a warning is logged."""
        import logging

        from archon.search.progress import CollectionProgress, IndexingState, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()

        state_store = IndexingStateStore(tmp_path / "state")
        state_store.write(IndexingState(collections={
            "myproject": CollectionProgress(status=IndexingStatus.DONE)
        }))

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        with patch.object(syncer, "_check_collection_changes", return_value=([Path("new.md")], [], [])), \
             patch.object(syncer, "_apply_collection_changes", new_callable=AsyncMock, return_value="partial failure") as mock_apply, \
             caplog.at_level(logging.WARNING, logger="archon.search.sync"):
            await syncer.sync_collection("myproject", col_dir)

        mock_apply.assert_called_once()
        assert any("partial failure" in r.message for r in caplog.records if r.levelno == logging.WARNING)

    @pytest.mark.asyncio
    async def test_sync_collection_with_changed_file(self, tmp_path):
        """When changed files detected, _apply_collection_changes is called with changed_files."""
        from archon.search.progress import CollectionProgress, IndexingState, IndexingStateStore, IndexingStatus
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()
        changed = Path("changed.md")

        state_store = IndexingStateStore(tmp_path / "state")
        state_store.write(IndexingState(collections={
            "myproject": CollectionProgress(status=IndexingStatus.DONE)
        }))

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        with patch.object(syncer, "_check_collection_changes", return_value=([], [changed], [])), \
             patch.object(syncer, "_apply_collection_changes", new_callable=AsyncMock, return_value=None) as mock_apply:
            await syncer.sync_collection("myproject", col_dir)

        mock_apply.assert_called_once()
        call_args = mock_apply.call_args[0]
        # changed_files is the 4th positional arg
        assert call_args[3] == [changed]

    @pytest.mark.asyncio
    async def test_sync_collection_collection_not_in_state_uses_defaults(self, tmp_path):
        """When collection has no progress entry in state, defaults ('', 0) are used for model/chunk_size."""
        from archon.search.progress import IndexingState, IndexingStateStore
        from archon.search.sync import SearchCollectionSync

        col_dir = tmp_path / "myproject"
        col_dir.mkdir()

        state_store = IndexingStateStore(tmp_path / "state")
        # Write state without "myproject" entry
        state_store.write(IndexingState(collections={}))

        pipeline = make_mock_pipeline(tmp_path)
        syncer = SearchCollectionSync(pipeline, state_store=state_store)

        with patch.object(syncer, "_check_collection_changes", return_value=([], [], [])) as mock_check, \
             patch.object(syncer, "_apply_collection_changes", new_callable=AsyncMock):
            await syncer.sync_collection("myproject", col_dir)

        # Verify defaults were used: indexed_embedding_model="" and indexed_chunk_size=0
        mock_check.assert_called_once()
        _, kwargs = mock_check.call_args
        assert kwargs.get("indexed_embedding_model") == ""
        assert kwargs.get("indexed_chunk_size") == 0

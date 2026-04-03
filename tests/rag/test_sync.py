"""Tests for archon/rag/sync.py — path_to_collection_name and RagCollectionSync."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon.rag._types import CollectionInfo
from archon.rag.sync import path_to_collection_name


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
# Helpers for RagCollectionSync unit tests
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
# RagCollectionSync unit tests
# ---------------------------------------------------------------------------

class TestRagCollectionSync:
    @pytest.mark.asyncio
    async def test_sync_adds_new_collection(self, tmp_path):
        """Path not in existing collections → ingest_directory called."""
        from archon.rag.sync import RagCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()
        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        syncer = RagCollectionSync(pipeline)
        result = await syncer.sync([str(new_dir)])

        pipeline.ingest_directory.assert_called_once()
        call_args = pipeline.ingest_directory.call_args
        assert call_args[0][0] == new_dir.resolve()
        assert "myproject" in result.added
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_sync_drops_removed_collection(self, tmp_path):
        """Manifest has col not in desired + col in existing → drop called."""
        from archon.rag.sync import RagCollectionSync

        manifest = {"oldcol": "/some/old/path"}
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["oldcol"],
            manifest=manifest,
        )

        syncer = RagCollectionSync(pipeline)
        result = await syncer.sync([])  # empty desired

        pipeline.store.drop_collection.assert_called_once_with("oldcol")
        assert "oldcol" in result.removed
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_sync_skips_unchanged_collection(self, tmp_path):
        """Col in desired and in existing → no ingest, no drop."""
        from archon.rag.sync import RagCollectionSync

        existing_dir = tmp_path / "myproject"
        existing_dir.mkdir()
        resolved = str(existing_dir.resolve())

        manifest = {"myproject": resolved}
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["myproject"],
            manifest=manifest,
        )

        syncer = RagCollectionSync(pipeline)
        result = await syncer.sync([str(existing_dir)])

        pipeline.ingest_directory.assert_not_called()
        pipeline.store.drop_collection.assert_not_called()
        assert "myproject" in result.unchanged
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_sync_resolves_collision(self, tmp_path):
        """Two paths with same basename → parent prefix used."""
        from archon.rag.sync import RagCollectionSync

        dir_a = tmp_path / "alpha" / "sessions"
        dir_b = tmp_path / "beta" / "sessions"
        dir_a.mkdir(parents=True)
        dir_b.mkdir(parents=True)

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        syncer = RagCollectionSync(pipeline)
        result = await syncer.sync([str(dir_a), str(dir_b)])

        assert len(result.added) == 2
        # Names must be distinct
        assert result.added[0] != result.added[1]
        # Both should incorporate parent prefix
        assert all("sessions" in name for name in result.added)

    @pytest.mark.asyncio
    async def test_sync_resolves_three_way_collision(self, tmp_path):
        """Three paths with same basename → all distinct names."""
        from archon.rag.sync import RagCollectionSync

        dirs = []
        for prefix in ("x", "y", "z"):
            d = tmp_path / prefix / "data"
            d.mkdir(parents=True)
            dirs.append(d)

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        syncer = RagCollectionSync(pipeline)
        result = await syncer.sync([str(d) for d in dirs])

        assert len(result.added) == 3
        assert len(set(result.added)) == 3  # all distinct

    @pytest.mark.asyncio
    async def test_sync_resolves_deep_collision_with_hash_fallback(self, tmp_path):
        """Two paths with same parent+basename → hash fallback used."""
        from archon.rag.sync import RagCollectionSync

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
        with patch("archon.rag.sync.path_to_collection_name", return_value="sessions"):
            syncer = RagCollectionSync(pipeline)
            result = await syncer.sync([str(dir_a2), str(dir_b2)])

        # With hash fallback, both names should be distinct
        assert len(set(result.added)) == 2
        # At least one should have a hash suffix
        assert any("_" in name for name in result.added)

    @pytest.mark.asyncio
    async def test_sync_records_ingest_error(self, tmp_path):
        """ingest_directory raises → error in SyncResult.errors, other paths still processed."""
        from archon.rag.sync import RagCollectionSync

        dir_a = tmp_path / "project_a"
        dir_b = tmp_path / "project_b"
        dir_a.mkdir()
        dir_b.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        pipeline.ingest_directory.side_effect = [RuntimeError("disk full"), []]

        syncer = RagCollectionSync(pipeline)
        result = await syncer.sync([str(dir_a), str(dir_b)])

        assert len(result.errors) == 1
        assert "disk full" in result.errors[0]
        assert len(result.added) == 1  # second one succeeded

    @pytest.mark.asyncio
    async def test_sync_preserves_unmanaged_manually_ingested_collection(self, tmp_path):
        """Col in LanceDB but NOT in manifest → appears in skipped, never dropped."""
        from archon.rag.sync import RagCollectionSync

        # "manual" is in LanceDB but not in manifest
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["manual"],
            manifest={},  # empty manifest
        )

        syncer = RagCollectionSync(pipeline)
        result = await syncer.sync([])  # no desired

        pipeline.store.drop_collection.assert_not_called()
        assert "manual" in result.skipped

    @pytest.mark.asyncio
    async def test_sync_records_warning_for_nonexistent_path(self, tmp_path):
        """Path in config but not on disk → in SyncResult.errors, other paths processed."""
        from archon.rag.sync import RagCollectionSync

        real_dir = tmp_path / "real"
        real_dir.mkdir()
        fake_path = str(tmp_path / "nonexistent" / "path")

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        syncer = RagCollectionSync(pipeline)
        result = await syncer.sync([fake_path, str(real_dir)])

        assert any("does not exist" in e for e in result.errors)
        assert len(result.added) == 1  # real_dir was added
        pipeline.ingest_directory.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_with_empty_collections_drops_only_managed(self, tmp_path):
        """collections=[] → only manifest-tracked collections dropped."""
        from archon.rag.sync import RagCollectionSync

        manifest = {"managed": "/some/managed/path"}
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["managed", "unmanaged"],
            manifest=manifest,
        )

        syncer = RagCollectionSync(pipeline)
        result = await syncer.sync([])

        pipeline.store.drop_collection.assert_called_once_with("managed")
        assert "managed" in result.removed
        assert "unmanaged" in result.skipped

    @pytest.mark.asyncio
    async def test_sync_handles_keyerror_on_drop_phantom_manifest_entry(self, tmp_path):
        """Manifest has col, list_collections returns it, drop raises KeyError → WARNING, error in SyncResult, sync continues."""
        from archon.rag.sync import RagCollectionSync

        manifest = {"ghost": "/some/ghost/path"}
        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["ghost"],
            manifest=manifest,
        )
        pipeline.store.drop_collection.side_effect = KeyError("ghost")

        syncer = RagCollectionSync(pipeline)
        with patch("archon.rag.sync.logger") as mock_logger:
            result = await syncer.sync([])

        # Should log a WARNING
        mock_logger.warning.assert_called()
        # Error should be recorded
        assert len(result.errors) == 1

    @pytest.mark.asyncio
    async def test_migration_renames_archon_history_to_derived_name(self, tmp_path):
        """archon-history in LanceDB, sessions not → rename called."""
        from archon.rag.sync import RagCollectionSync

        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["archon-history"],
        )

        syncer = RagCollectionSync(pipeline)
        result = await syncer.sync([])

        pipeline.store.rename_collection.assert_called_once_with("archon-history", "sessions")
        assert result.errors == [] or "archon-history" not in result.errors

    @pytest.mark.asyncio
    async def test_migration_handles_not_implemented_error(self, tmp_path, caplog):
        """rename_collection raises NotImplementedError (LanceDB OSS) → warning, no crash."""
        from archon.rag.sync import RagCollectionSync

        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["archon-history"],
        )
        pipeline.store.rename_collection.side_effect = NotImplementedError("rename_table not supported")

        syncer = RagCollectionSync(pipeline)
        with caplog.at_level(logging.WARNING, logger="archon"):
            result = await syncer.sync([])  # should not crash

        # No exception raised, warning logged
        assert any("rename_table" in msg or "unmanaged" in msg for msg in caplog.messages)

    @pytest.mark.asyncio
    async def test_migration_updates_manifest_on_rename(self, tmp_path):
        """After rename, manifest entry archon-history → sessions."""
        import json
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline)
        await syncer.sync([])

        # Manifest should now have 'sessions' not 'archon-history'
        updated = json.loads((db_path / "sync_manifest.json").read_text())
        assert "sessions" in updated or "archon-history" not in updated

    @pytest.mark.asyncio
    async def test_sync_deduplicates_input_paths(self, tmp_path):
        """Duplicate paths in collections are deduplicated."""
        from archon.rag.sync import RagCollectionSync

        real_dir = tmp_path / "myproject"
        real_dir.mkdir()
        path_str = str(real_dir)

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        syncer = RagCollectionSync(pipeline)
        result = await syncer.sync([path_str, path_str])  # duplicate

        # Should only ingest once
        assert pipeline.ingest_directory.call_count == 1
        assert len(result.added) == 1

    @pytest.mark.asyncio
    async def test_migration_skips_if_both_tables_exist(self, tmp_path, caplog):
        """Both archon-history and sessions exist → WARNING logged, no rename."""
        from archon.rag.sync import RagCollectionSync

        pipeline = make_mock_pipeline(
            tmp_path,
            existing_collections=["archon-history", "sessions"],
        )

        syncer = RagCollectionSync(pipeline)
        with caplog.at_level(logging.WARNING, logger="archon"):
            await syncer.sync([])

        pipeline.store.rename_collection.assert_not_called()
        assert any("archon-history" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# manifest_lookup_by_path tests
# ---------------------------------------------------------------------------

class TestManifestLookupByPath:
    def test_returns_none_when_no_manifest(self, tmp_path):
        from archon.rag.sync import manifest_lookup_by_path

        result = manifest_lookup_by_path(tmp_path / "nonexistent.json", "/some/path")
        assert result is None

    def test_returns_collection_name_for_known_path(self, tmp_path):
        from archon.rag.sync import manifest_lookup_by_path

        manifest_path = tmp_path / "sync_manifest.json"
        real_dir = tmp_path / "myproject"
        real_dir.mkdir()
        resolved = str(real_dir.resolve())
        manifest_path.write_text(json.dumps({"myproject": resolved}))

        result = manifest_lookup_by_path(manifest_path, resolved)
        assert result == "myproject"

    def test_returns_none_for_unknown_path(self, tmp_path):
        from archon.rag.sync import manifest_lookup_by_path

        manifest_path = tmp_path / "sync_manifest.json"
        manifest_path.write_text(json.dumps({"col": "/some/other/path"}))

        result = manifest_lookup_by_path(manifest_path, "/totally/different/path")
        assert result is None

    def test_expands_tilde_in_stored_path(self, tmp_path):
        from archon.rag.sync import manifest_lookup_by_path
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

class TestRagCollectionSyncIntegration:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_sync_integration(self, tmp_path):
        """Add/remove paths → verify LanceDB state matches config (real LanceDB)."""
        import numpy as np

        from archon.rag._types import ChunkRecord
        from archon.rag.embedder import Embedder, EmbedderBackend
        from archon.rag.parser import DocumentParser
        from archon.rag.pipeline import RagPipeline
        from archon.rag.reranker import Reranker, RerankerBackend
        from archon.rag.store import RagStore
        from archon.rag.sync import RagCollectionSync

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
        store = RagStore(db_path)
        await store.connect()

        embedder = Embedder(StubEmbedderBackend())
        reranker = Reranker(StubRerankerBackend())
        chunker = StubChunker()
        parser = DocumentParser()

        pipeline = RagPipeline(
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

        syncer = RagCollectionSync(pipeline)

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
        from archon.rag.sync import RagCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        syncer = RagCollectionSync(pipeline)

        lock1 = syncer._get_lock("col_a")
        lock2 = syncer._get_lock("col_a")

        assert lock1 is lock2

    def test_get_lock_returns_different_lock_for_different_name(self, tmp_path):
        """_get_lock('col_a') and _get_lock('col_b') return different instances."""
        from archon.rag.sync import RagCollectionSync

        pipeline = make_mock_pipeline(tmp_path)
        syncer = RagCollectionSync(pipeline)

        lock_a = syncer._get_lock("col_a")
        lock_b = syncer._get_lock("col_b")

        assert lock_a is not lock_b

    @pytest.mark.asyncio
    async def test_sync_acquires_lock_per_collection(self, tmp_path):
        """Lock is acquired during sync for the collection being ingested."""
        import asyncio
        from archon.rag.sync import RagCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()
        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        lock_was_locked_during_ingest = False

        async def fake_ingest(path, name, **kwargs):
            progress_cb = kwargs.get("progress_cb")
            nonlocal lock_was_locked_during_ingest
            lock = syncer._get_lock(name)
            lock_was_locked_during_ingest = lock.locked()

        pipeline.ingest_directory = fake_ingest

        syncer = RagCollectionSync(pipeline)
        await syncer.sync([str(new_dir)])

        assert lock_was_locked_during_ingest

    @pytest.mark.asyncio
    async def test_concurrent_sync_same_collection_serialized(self, tmp_path):
        """Two concurrent sync() calls on the same collection are serialized."""
        import asyncio
        from archon.rag.sync import RagCollectionSync

        col_dir = tmp_path / "shared"
        col_dir.mkdir()
        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        execution_log: list[str] = []
        start_event = asyncio.Event()

        call_count = 0

        async def fake_ingest(path, name, **kwargs):
            progress_cb = kwargs.get("progress_cb")
            nonlocal call_count
            call_count += 1
            current_call = call_count
            execution_log.append(f"start_{current_call}")
            await asyncio.sleep(0.02)  # simulate work
            execution_log.append(f"end_{current_call}")

        pipeline.ingest_directory = fake_ingest

        syncer = RagCollectionSync(pipeline)

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
        from archon.rag.sync import RagCollectionSync

        dir_a = tmp_path / "col_a"
        dir_b = tmp_path / "col_b"
        dir_a.mkdir()
        dir_b.mkdir()
        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        started: list[str] = []
        both_started = asyncio.Event()

        async def fake_ingest(path, name, **kwargs):
            progress_cb = kwargs.get("progress_cb")
            started.append(name)
            if len(started) == 2:
                both_started.set()
            # Wait until both have started (proving concurrency)
            await asyncio.wait_for(both_started.wait(), timeout=1.0)

        pipeline.ingest_directory = fake_ingest

        syncer = RagCollectionSync(pipeline)

        await asyncio.gather(
            syncer.sync([str(dir_a)]),
            syncer.sync([str(dir_b)]),
        )

        # both_started was set → both ingests ran concurrently
        assert both_started.is_set()
        assert len(started) == 2


class TestManifestRemoveEntry:
    def test_manifest_remove_entry_removes_key(self, tmp_path: Path) -> None:
        from archon.rag.sync import manifest_remove_entry  # noqa: PLC0415

        manifest_path = tmp_path / "sync_manifest.json"
        manifest_path.write_text(json.dumps({"sessions": "/home/user/.archon/sessions", "other": "/data"}))

        manifest_remove_entry(manifest_path, "sessions")

        data = json.loads(manifest_path.read_text())
        assert "sessions" not in data
        assert "other" in data

    def test_manifest_remove_entry_noop_if_missing(self, tmp_path: Path) -> None:
        from archon.rag.sync import manifest_remove_entry  # noqa: PLC0415

        nonexistent = tmp_path / "no_such_manifest.json"
        # Must not raise
        manifest_remove_entry(nonexistent, "sessions")


# ---------------------------------------------------------------------------
# TestSyncProgress — FEAT-027 Task 1.4: sync() progress state integration
# ---------------------------------------------------------------------------

class TestSyncProgress:
    """Tests for IndexingStateStore integration in RagCollectionSync.sync()."""

    def _make_syncer_with_state(self, tmp_path, existing_collections=None, manifest=None, file_count=5, ingest_results=None):
        """Helper: build a RagCollectionSync with a real IndexingStateStore."""
        import asyncio
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        return syncer, state_store, pipeline

    @pytest.mark.asyncio
    async def test_sync_writes_pending_then_in_progress_before_ingest(self, tmp_path):
        """PENDING should be the first state written, then IN_PROGRESS before ingest starts."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        statuses_seen: list[IndexingStatus] = []
        original_update = state_store.update_collection

        def tracking_update(name, progress):
            statuses_seen.append(progress.status)
            return original_update(name, progress)

        state_store.update_collection = tracking_update

        pipeline.ingest_directory = AsyncMock(
            return_value=[IngestResult(doc_id="d0", chunks_created=1, status="ok")],
        )

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        # First write is PENDING, second is IN_PROGRESS
        assert len(statuses_seen) >= 2
        assert statuses_seen[0] == IndexingStatus.PENDING
        assert statuses_seen[1] == IndexingStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_sync_writes_in_progress_during_ingest(self, tmp_path):
        """During ingest, state should be IN_PROGRESS."""
        import asyncio
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        captured_status = None

        async def fake_ingest(path, name, **kwargs):
            progress_cb = kwargs.get("progress_cb")
            nonlocal captured_status
            state = state_store.read()
            if state and name in state.collections:
                captured_status = state.collections[name].status
            return [IngestResult(doc_id="d0", chunks_created=1, status="ok")]

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        assert captured_status == IndexingStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_sync_total_files_set_from_file_enumeration(self, tmp_path):
        """total_files should be set from file enumeration, not from callback."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.total_files == 10
        assert cp.status == IndexingStatus.DONE

    @pytest.mark.asyncio
    async def test_sync_writes_done_after_success(self, tmp_path):
        """After successful ingest, state should be DONE."""
        from archon.rag.progress import IndexingStatus

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
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        pipeline.ingest_directory = AsyncMock(side_effect=RuntimeError("disk full"))

        syncer = RagCollectionSync(pipeline, state_store=state_store)
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
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStatus

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
        from archon.rag._types import IngestResult

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
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        write_calls: list[tuple[str, int]] = []
        original_write = state_store.update_collection

        def tracking_update(name, progress):
            write_calls.append((str(progress.status), progress.processed_files))
            return original_write(name, progress)

        state_store.update_collection = tracking_update

        N = 150

        async def fake_ingest(path, name, **kwargs):
            progress_cb = kwargs.get("progress_cb")
            on_file_complete = kwargs.get("on_file_complete")
            results = [IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok") for i in range(N)]
            for i in range(N):
                if on_file_complete:
                    on_file_complete(Path(f"/fake/file{i}.md"))
                if progress_cb:
                    progress_cb(i + 1, N)
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        # Batched writes from on_file_complete at every 50 files
        in_progress_status = str(IndexingStatus.IN_PROGRESS)
        batched = [pf for st, pf in write_calls if st == in_progress_status and pf > 0]
        assert batched == [50, 100, 150], f"Expected exactly [50, 100, 150], got {batched}"

    @pytest.mark.asyncio
    async def test_sync_batched_writes_boundary_49_files(self, tmp_path):
        """With 49 files, no batched progress writes should happen (only PENDING/IN_PROGRESS/DONE)."""
        import asyncio
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        progress_write_counts: list[int] = []
        original_update = state_store.update_collection

        def tracking_update(name, progress):
            if progress.status == IndexingStatus.IN_PROGRESS:
                progress_write_counts.append(progress.processed_files)
            return original_update(name, progress)

        state_store.update_collection = tracking_update

        N = 49
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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        # Only the initial IN_PROGRESS write (processed_files=0), no batched progress writes
        batched = [c for c in progress_write_counts if c > 0]
        assert len(batched) == 0

    @pytest.mark.asyncio
    async def test_sync_batched_writes_boundary_50_files(self, tmp_path):
        """With exactly 50 files, one batched write from on_file_complete."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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
            progress_cb = kwargs.get("progress_cb")
            on_file_complete = kwargs.get("on_file_complete")
            results = [IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok") for i in range(N)]
            for i in range(N):
                if on_file_complete:
                    on_file_complete(Path(f"/fake/file{i}.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        assert 50 in progress_write_counts
        assert len(progress_write_counts) == 1

    @pytest.mark.asyncio
    async def test_sync_batched_writes_boundary_51_files(self, tmp_path):
        """With 51 files, one batched write at 50 from on_file_complete."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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
            progress_cb = kwargs.get("progress_cb")
            on_file_complete = kwargs.get("on_file_complete")
            results = [IngestResult(doc_id=f"d{i}", chunks_created=1, status="ok") for i in range(N)]
            for i in range(N):
                if on_file_complete:
                    on_file_complete(Path(f"/fake/file{i}.md"))
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        assert progress_write_counts == [50]

    @pytest.mark.asyncio
    async def test_sync_batched_writes_boundary_1_file(self, tmp_path):
        """With 1 file, no batched progress writes."""
        import asyncio
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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
            progress_cb = kwargs.get("progress_cb")
            if progress_cb:
                cb_result = progress_cb(1, 1)
                if asyncio.iscoroutine(cb_result):
                    await cb_result
            return results

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        assert len(progress_write_counts) == 0

    @pytest.mark.asyncio
    async def test_sync_final_write_on_completion(self, tmp_path):
        """Final state write should happen after ingest completes (DONE status)."""
        from archon.rag.progress import IndexingStatus

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

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
        from archon.rag.progress import IndexingStatus

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
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)], progress_cb=caller_cb)

        assert caller_calls == [(1, 3), (2, 3), (3, 3)]

    @pytest.mark.asyncio
    async def test_sync_no_state_store_backward_compat(self, tmp_path):
        """Without state_store, sync works as before — no state files created."""
        from archon.rag.sync import RagCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        pipeline.ingest_directory = AsyncMock(return_value=[])

        syncer = RagCollectionSync(pipeline)  # no state_store
        result = await syncer.sync([str(new_dir)])

        assert "myproject" in result.added
        # No state dir should exist
        assert not (tmp_path / "state").exists()

    @pytest.mark.asyncio
    async def test_sync_resets_stale_in_progress(self, tmp_path):
        """On sync entry, any IN_PROGRESS entries should be reset to PENDING."""
        from archon.rag.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        pipeline.ingest_directory = AsyncMock(return_value=[])
        state_store = IndexingStateStore(tmp_path / "state")

        # Pre-seed stale IN_PROGRESS state
        state_store.update_collection("stale_col", CollectionProgress(
            status=IndexingStatus.IN_PROGRESS,
            total_files=100,
            processed_files=50,
        ))

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([])  # empty desired — just triggers crash recovery

        state = state_store.read()
        assert state is not None
        assert "stale_col" in state.collections
        assert state.collections["stale_col"].status == IndexingStatus.PENDING

    @pytest.mark.asyncio
    async def test_sync_cleans_removed_collections(self, tmp_path):
        """Removed collections should be cleaned from state file."""
        from archon.rag.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        result = await syncer.sync([])  # empty desired

        assert "oldcol" in result.removed
        state = state_store.read()
        assert state is not None
        assert "oldcol" not in state.collections

    @pytest.mark.asyncio
    async def test_sync_state_write_failure_does_not_abort(self, tmp_path):
        """State write failures must not abort sync — sync should continue."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        result = await syncer.sync([str(new_dir)])

        # Sync should still succeed
        assert "myproject" in result.added
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_sync_done_with_error_count(self, tmp_path):
        """DONE state should include both ok and error counts from results."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStatus

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

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
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.FAILED
        assert cp.total_files == 100  # from enumeration
        assert "crash mid-ingest" in cp.error
        assert len(cp.processed_paths) == 2  # partial progress retained

    @pytest.mark.asyncio
    async def test_sync_multiple_collections_mixed_results(self, tmp_path):
        """Multiple collections: one succeeds, one fails — each has correct state."""
        import asyncio
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
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
        from archon.rag.sync import RagCollectionSync

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
        syncer = RagCollectionSync(pipeline, pinned_collections=[str(gamma)])
        await syncer.sync([str(alpha), str(beta), str(gamma)])

        assert ingestion_order[0] == "gamma"
        # Remaining should be alphabetical
        assert ingestion_order[1:] == ["alpha", "beta"]

    @pytest.mark.asyncio
    async def test_sync_pinned_preserves_declaration_order(self, tmp_path):
        """Pinned collections follow config declaration order, not alphabetical."""
        from archon.rag.sync import RagCollectionSync

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
        syncer = RagCollectionSync(pipeline, pinned_collections=[str(ccc), str(bbb)])
        await syncer.sync([str(aaa), str(bbb), str(ccc)])

        assert ingestion_order == ["ccc", "bbb", "aaa"]

    @pytest.mark.asyncio
    async def test_sync_non_pinned_alphabetical(self, tmp_path):
        """Non-pinned collections are sorted alphabetically by collection name."""
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, pinned_collections=[])
        await syncer.sync([str(zebra), str(apple), str(mango)])

        assert ingestion_order == ["apple", "mango", "zebra"]

    @pytest.mark.asyncio
    async def test_sync_pinned_not_in_desired_ignored(self, tmp_path):
        """Pinned path not in collections list does not cause error."""
        from archon.rag.sync import RagCollectionSync

        alpha = tmp_path / "alpha"
        alpha.mkdir()
        nonexistent_pinned = tmp_path / "not_a_collection"

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])

        syncer = RagCollectionSync(pipeline, pinned_collections=[str(nonexistent_pinned)])
        result = await syncer.sync([str(alpha)])

        assert "alpha" in result.added
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_sync_all_pinned(self, tmp_path):
        """All collections are pinned — order matches config declaration order."""
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(
            pipeline,
            pinned_collections=[str(charlie), str(alice), str(bob)],
        )
        await syncer.sync([str(charlie), str(alice), str(bob)])

        assert ingestion_order == ["charlie", "alice", "bob"]

    @pytest.mark.asyncio
    async def test_sync_no_pinned(self, tmp_path):
        """Empty pinned list — alphabetical fallback."""
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline)
        await syncer.sync([str(delta), str(bravo)])

        assert ingestion_order == ["bravo", "delta"]

    @pytest.mark.asyncio
    async def test_sync_pinned_tilde_expansion(self, tmp_path, monkeypatch):
        """Pinned path with ~ correctly matches resolved desired path."""
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(
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
    """Tests for resumable indexing (processed_paths) in RagCollectionSync."""

    @pytest.mark.asyncio
    async def test_reset_stale_preserves_processed_paths(self, tmp_path):
        """IN_PROGRESS state with processed_paths → after reset, PENDING with paths preserved."""
        from archon.rag.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        pipeline.ingest_directory = AsyncMock(return_value=[])
        state_store = IndexingStateStore(tmp_path / "state")

        state_store.update_collection("col", CollectionProgress(
            status=IndexingStatus.IN_PROGRESS,
            total_files=10,
            processed_files=5,
            processed_paths=["/a", "/b"],
        ))

        syncer = RagCollectionSync(pipeline, state_store=state_store)
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
        from archon.rag.sync import RagCollectionSync

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        syncer = RagCollectionSync(pipeline, state_store=None)
        assert syncer._load_processed_paths("col") == []

    @pytest.mark.asyncio
    async def test_load_processed_paths_no_state_file(self, tmp_path):
        """State file missing → returns []."""
        from archon.rag.progress import IndexingStateStore
        from archon.rag.sync import RagCollectionSync

        state_store = IndexingStateStore(tmp_path / "state")
        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        syncer = RagCollectionSync(pipeline, state_store=state_store)
        assert syncer._load_processed_paths("col") == []

    @pytest.mark.asyncio
    async def test_load_processed_paths_collection_absent(self, tmp_path):
        """Collection not in state → returns []."""
        from archon.rag.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

        state_store = IndexingStateStore(tmp_path / "state")
        state_store.update_collection("other", CollectionProgress(
            status=IndexingStatus.DONE,
            processed_paths=["/x"],
        ))
        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        syncer = RagCollectionSync(pipeline, state_store=state_store)
        assert syncer._load_processed_paths("col") == []

    @pytest.mark.asyncio
    async def test_sync_resumes_from_processed_paths(self, tmp_path):
        """State with processed_paths → exclude_paths passed to ingest_directory."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        assert "exclude_paths" in captured_kwargs
        assert "/a/file.md" in captured_kwargs["exclude_paths"]

    @pytest.mark.asyncio
    async def test_sync_accumulates_new_paths_in_state(self, tmp_path):
        """After sync, state processed_paths contains newly processed file paths."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        assert len(cp.processed_paths) == 3
        assert "/fake/file0.md" in cp.processed_paths

    @pytest.mark.asyncio
    async def test_sync_processed_files_offset_correct(self, tmp_path):
        """resume_offset=5, 3 new files: state shows processed_files=8."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.processed_files == 8  # 5 + 3

    @pytest.mark.asyncio
    async def test_sync_total_files_correct_with_resume(self, tmp_path):
        """resume_offset=5, total_new=3: state shows total_files=8."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.total_files == 8  # 5 + 3

    @pytest.mark.asyncio
    async def test_sync_batched_path_flush_every_50_files(self, tmp_path):
        """100 files: state write at file 50 with 50 paths; final write with 100."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        # Should have a write with 50 paths (batch) and final with 100 paths (DONE)
        assert 50 in write_path_counts
        assert 100 in write_path_counts

    @pytest.mark.asyncio
    async def test_sync_final_state_contains_all_paths(self, tmp_path):
        """DONE state has processed_paths listing all ingested files."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
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
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.FAILED
        assert len(cp.processed_paths) == 3
        assert cp.processed_files == 3  # not 0

    @pytest.mark.asyncio
    async def test_sync_no_resume_on_empty_processed_paths(self, tmp_path):
        """Fresh collection (no state): exclude_paths is empty frozenset."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore
        from archon.rag.sync import RagCollectionSync

        new_dir = tmp_path / "myproject"
        new_dir.mkdir()

        pipeline = make_mock_pipeline(tmp_path, existing_collections=[])
        state_store = IndexingStateStore(tmp_path / "state")

        captured_kwargs: dict = {}

        async def fake_ingest(path, name, **kwargs):
            captured_kwargs.update(kwargs)
            return [IngestResult(doc_id="d0", chunks_created=1, status="ok")]

        pipeline.ingest_directory = AsyncMock(side_effect=fake_ingest)

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        exclude = captured_kwargs.get("exclude_paths")
        assert exclude is not None
        assert len(exclude) == 0

    @pytest.mark.asyncio
    async def test_sync_all_files_already_processed_state_correct(self, tmp_path):
        """All files excluded → DONE with total_files=resume_offset, processed_files=resume_offset."""
        from archon.rag.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
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
        from archon.rag._types import IngestResult
        from archon.rag.progress import IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        await syncer.sync([str(new_dir)])

        state = state_store.read()
        cp = state.collections["myproject"]
        assert len(cp.processed_paths) == 2
        assert "/fake/ok1.md" in cp.processed_paths
        assert "/fake/ok2.md" in cp.processed_paths

    @pytest.mark.asyncio
    async def test_sync_resumes_existing_collection_with_pending_status(self, tmp_path):
        """Collection in existing with PENDING status → Step 6.5 resumes it."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        result = await syncer.sync([str(existing_dir)])

        # Should be in added (resumed), NOT unchanged
        assert "myproject" in result.added
        assert "myproject" not in result.unchanged

    @pytest.mark.asyncio
    async def test_sync_resumed_collection_not_in_unchanged(self, tmp_path):
        """PENDING collection in existing & desired → NOT in result.unchanged."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        result = await syncer.sync([str(dir_a), str(dir_b)])

        # project_a is DONE → unchanged
        assert "project_a" in result.unchanged
        # project_b was PENDING → resumed → in added, NOT unchanged
        assert "project_b" in result.added
        assert "project_b" not in result.unchanged

    @pytest.mark.asyncio
    async def test_sync_resumes_existing_collection_with_failed_status(self, tmp_path):
        """FAILED collection in existing & desired → Step 6.5 resumes it."""
        from archon.rag._types import IngestResult
        from archon.rag.progress import CollectionProgress, IndexingStateStore, IndexingStatus
        from archon.rag.sync import RagCollectionSync

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

        syncer = RagCollectionSync(pipeline, state_store=state_store)
        result = await syncer.sync([str(existing_dir)])

        assert "myproject" in result.added
        assert "myproject" not in result.unchanged
        state = state_store.read()
        cp = state.collections["myproject"]
        assert cp.status == IndexingStatus.DONE
        assert "/old/file.md" in cp.processed_paths
        assert "/new/file.md" in cp.processed_paths

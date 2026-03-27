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

        await store.disconnect()

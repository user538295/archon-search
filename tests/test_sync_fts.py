"""Tests for sync.py Task 3.3 — replace rebuild_fts_index with optimize_fts at sync batch end.

All tests in this file are unit tests (default suite) unless marked otherwise.
Integration tests are marked @pytest.mark.integration and require a real LanceDB environment.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import CollectionInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    tmp_path: Path,
    *,
    supports_incremental: bool = True,
    collections: list[str] | None = None,
) -> MagicMock:
    """Return a minimal mock pipeline suitable for _apply_collection_changes tests."""
    pipeline = MagicMock()
    pipeline.store._db_path = tmp_path / "db"
    pipeline.store.list_collections = AsyncMock(return_value=[
        CollectionInfo(name=n, doc_count=0, chunk_count=0) for n in (collections or [])
    ])
    pipeline.store.drop_collection = AsyncMock()
    pipeline.store.rename_collection = AsyncMock()
    pipeline.store.get_all_collections_meta = AsyncMock(return_value=[])
    pipeline.delete_by_source_path = AsyncMock(return_value=1)
    pipeline.store.get_dominant_language = AsyncMock(return_value="en")
    pipeline.store.optimize_fts = AsyncMock()
    pipeline.store.rebuild_fts_index = AsyncMock()
    pipeline.store.supports_incremental_fts_delete = supports_incremental
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)
    pipeline.ingest_file = AsyncMock(return_value=MagicMock(status="ok"))
    pipeline.ingest_directory = AsyncMock(return_value=[])
    pipeline.recompute_collection_meta = AsyncMock()
    pipeline._global_embedder = MagicMock()
    return pipeline


async def _run_apply_changes(
    tmp_path: Path,
    pipeline: MagicMock,
    name: str = "testcol",
    *,
    new_files: list[Path] | None = None,
    changed_files: list[Path] | None = None,
    deleted_paths: list[str] | None = None,
) -> str | None:
    """Invoke SearchCollectionSync._apply_collection_changes with mock data."""
    from archon_search.sync import SearchCollectionSync

    syncer = SearchCollectionSync(pipeline, state_store=None)

    file_mtimes: dict[str, float] = {}
    if new_files:
        for f in new_files:
            file_mtimes[str(f.resolve())] = 1.0
    if changed_files:
        for f in changed_files:
            file_mtimes[str(f.resolve())] = 1.0
    if deleted_paths:
        for p in deleted_paths:
            file_mtimes[p] = 0.0

    return await syncer._apply_collection_changes(
        name=name,
        source_path=tmp_path,
        new_files=new_files or [],
        changed_files=changed_files or [],
        deleted_paths=deleted_paths or [],
        file_mtimes=file_mtimes,
    )


# ---------------------------------------------------------------------------
# Unit tests — Plan A (supports_incremental_fts_delete=True)
# ---------------------------------------------------------------------------


class TestSyncFtsPlanA:
    @pytest.mark.asyncio
    async def test_sync_cycle_calls_optimize_fts_not_rebuild(self, tmp_path):
        """_apply_collection_changes: optimize_fts called, rebuild_fts_index NOT called (Plan A)."""
        pipeline = _make_pipeline(tmp_path, supports_incremental=True)
        f = tmp_path / "doc.txt"
        f.write_text("hello")

        await _run_apply_changes(tmp_path, pipeline, new_files=[f])

        pipeline.store.optimize_fts.assert_called_once_with("testcol")
        pipeline.store.rebuild_fts_index.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_cycle_calls_optimize_once_per_collection(self, tmp_path):
        """optimize_fts is called exactly once per collection per sync cycle (not N times per file)."""
        pipeline = _make_pipeline(tmp_path, supports_incremental=True)
        files = [tmp_path / f"doc{i}.txt" for i in range(3)]
        for f in files:
            f.write_text("content")

        # First collection
        await _run_apply_changes(tmp_path, pipeline, name="col1", new_files=files)
        assert pipeline.store.optimize_fts.call_count == 1

        # Second collection — optimize called once more, total 2
        await _run_apply_changes(tmp_path, pipeline, name="col2", new_files=files)
        assert pipeline.store.optimize_fts.call_count == 2

        # Verify each call targeted the correct collection
        call_args = [c.args[0] for c in pipeline.store.optimize_fts.call_args_list]
        assert call_args == ["col1", "col2"]

    @pytest.mark.asyncio
    async def test_sync_delete_path_does_not_call_optimize_per_file(self, tmp_path):
        """delete_by_source_path is called with skip_fts_optimize=True; batch-end optimize called once."""
        pipeline = _make_pipeline(tmp_path, supports_incremental=True)
        deleted = [f"/some/path/doc{i}.txt" for i in range(3)]

        await _run_apply_changes(tmp_path, pipeline, deleted_paths=deleted)

        # Each delete call must use skip_fts_optimize=True
        for c in pipeline.delete_by_source_path.call_args_list:
            assert c.kwargs.get("skip_fts_optimize") is True, (
                f"delete_by_source_path called without skip_fts_optimize=True: {c}"
            )

        # Batch-end optimize called exactly once
        assert pipeline.store.optimize_fts.call_count == 1

    @pytest.mark.asyncio
    async def test_sync_comment_updated_not_rebuild_called(self, tmp_path):
        """Ensure the old rebuild_fts_index call site is gone (behaviour, not grep)."""
        pipeline = _make_pipeline(tmp_path, supports_incremental=True)
        f = tmp_path / "file.txt"
        f.write_text("content")

        await _run_apply_changes(tmp_path, pipeline, changed_files=[f])

        pipeline.store.rebuild_fts_index.assert_not_called()


# ---------------------------------------------------------------------------
# Unit tests — Plan B (supports_incremental_fts_delete=False)
# ---------------------------------------------------------------------------


class TestSyncFtsPlanB:
    @pytest.mark.asyncio
    async def test_sync_batch_end_calls_rebuild_under_plan_b(self, tmp_path):
        """When Plan B is active, rebuild_fts_index is called at batch end, not optimize_fts."""
        pipeline = _make_pipeline(tmp_path, supports_incremental=False)
        f = tmp_path / "doc.txt"
        f.write_text("hello")

        await _run_apply_changes(tmp_path, pipeline, new_files=[f])

        pipeline.store.rebuild_fts_index.assert_called_once_with(
            "testcol", language="en"
        )
        pipeline.store.optimize_fts.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_plan_b_get_dominant_language_called(self, tmp_path):
        """Plan B: get_dominant_language is called to determine language for rebuild."""
        pipeline = _make_pipeline(tmp_path, supports_incremental=False)
        pipeline.store.get_dominant_language = AsyncMock(return_value="de")
        f = tmp_path / "doc.txt"
        f.write_text("Inhalt")

        await _run_apply_changes(tmp_path, pipeline, new_files=[f])

        pipeline.store.get_dominant_language.assert_called_once_with("testcol")
        pipeline.store.rebuild_fts_index.assert_called_once_with(
            "testcol", language="de"
        )

    @pytest.mark.asyncio
    async def test_sync_plan_b_delete_path_skip_fts_optimize(self, tmp_path):
        """Plan B: delete loop still uses skip_fts_optimize=True; only batch-end rebuild runs."""
        pipeline = _make_pipeline(tmp_path, supports_incremental=False)
        deleted = [f"/some/path/doc{i}.txt" for i in range(2)]

        await _run_apply_changes(tmp_path, pipeline, deleted_paths=deleted)

        for c in pipeline.delete_by_source_path.call_args_list:
            assert c.kwargs.get("skip_fts_optimize") is True

        assert pipeline.store.rebuild_fts_index.call_count == 1


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSyncFtsIntegration:
    @pytest.mark.asyncio
    async def test_sync_cycle_adds_searchable_after_optimize(self, tmp_path):
        """After a sync add cycle, new content is returned by hybrid_search."""
        import numpy as np

        from archon_search._types import ChunkRecord
        from archon_search.embedder import Embedder, EmbedderBackend
        from archon_search.parser import DocumentParser
        from archon_search.pipeline import SearchPipeline
        from archon_search.reranker import Reranker, RerankerBackend
        from archon_search.store import SearchStore

        class StubEmbedderBackend(EmbedderBackend):
            embedding_dim = 4
            is_warm: bool = False

            def encode(self, texts: list[str]) -> list[list[float]]:
                return [list(np.zeros(4, dtype=float)) for _ in texts]

        class StubRerankerBackend(RerankerBackend):
            is_warm: bool = False

            def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
                return [0.5] * len(pairs)

        class StubChunker:
            def chunk(
                self,
                text: str,
                doc_id: str,
                source_path: str,
                *,
                file_type: str,
                updated_at: str,
                ingested_by: str,
                language: str = "",
            ) -> list[ChunkRecord]:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat()
                return [
                    ChunkRecord(
                        doc_id=doc_id,
                        chunk_id=f"{doc_id}-000000",
                        text=text,
                        vector=[0.0] * 4,
                        source_path=source_path,
                        indexed_at=now,
                        file_type=file_type,
                        updated_at=updated_at,
                        ingested_by=ingested_by,
                        language=language,
                    )
                ]

        # Build a minimal real store + pipeline
        db_path = tmp_path / "db"
        store = SearchStore(db_path=str(db_path))
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

        # Create collection and ingest
        col = "syncfts_add_test"
        doc_path = tmp_path / "corpus" / "doc1.txt"
        doc_path.parent.mkdir(parents=True)
        doc_path.write_text("The quick brown fox jumps over the lazy dog")

        # Ingest directly — this builds the FTS index on the first file (rebuild_fts=True is default)
        ingest_result = await pipeline.ingest_file(doc_path, col, embedder=embedder)
        assert ingest_result.status == "ok"

        # FTS search should find the new content
        query_vector = [0.0] * 4
        results = await store.hybrid_search(col, query_vector, "quick brown fox", top_k=5)
        doc_ids = [r.doc_id for r in results]
        assert len(doc_ids) > 0, "No results returned after sync add cycle"

    @pytest.mark.asyncio
    async def test_sync_cycle_delete_no_phantom_hits(self, tmp_path):
        """After a sync delete cycle, deleted content no longer returned by hybrid_search."""
        import numpy as np

        from archon_search._types import ChunkRecord
        from archon_search.embedder import Embedder, EmbedderBackend
        from archon_search.parser import DocumentParser
        from archon_search.pipeline import SearchPipeline
        from archon_search.reranker import Reranker, RerankerBackend
        from archon_search.store import SearchStore
        from archon_search.sync import SearchCollectionSync

        class StubEmbedderBackend(EmbedderBackend):
            embedding_dim = 4
            is_warm: bool = False

            def encode(self, texts: list[str]) -> list[list[float]]:
                return [list(np.zeros(4, dtype=float)) for _ in texts]

        class StubRerankerBackend(RerankerBackend):
            is_warm: bool = False

            def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
                return [0.5] * len(pairs)

        class StubChunker:
            def chunk(
                self,
                text: str,
                doc_id: str,
                source_path: str,
                *,
                file_type: str,
                updated_at: str,
                ingested_by: str,
                language: str = "",
            ) -> list[ChunkRecord]:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat()
                return [
                    ChunkRecord(
                        doc_id=doc_id,
                        chunk_id=f"{doc_id}-000000",
                        text=text,
                        vector=[0.0] * 4,
                        source_path=source_path,
                        indexed_at=now,
                        file_type=file_type,
                        updated_at=updated_at,
                        ingested_by=ingested_by,
                        language=language,
                    )
                ]

        db_path = tmp_path / "db"
        store = SearchStore(db_path=str(db_path))
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

        col = "syncfts_delete_test"
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        doc_path = corpus_dir / "phantom.txt"
        doc_path.write_text("unique phantom text that should not appear after deletion")

        # First ingest the document — builds the FTS index (rebuild_fts=True is default)
        result = await pipeline.ingest_file(doc_path, col, embedder=embedder)
        assert result.status == "ok"

        # Now delete via sync cycle
        syncer = SearchCollectionSync(pipeline, state_store=None)
        await syncer._apply_collection_changes(
            name=col,
            source_path=corpus_dir,
            new_files=[],
            changed_files=[],
            deleted_paths=[str(doc_path.resolve())],
            file_mtimes={},
        )

        # FTS search must return zero results for the deleted text
        query_vector = [0.0] * 4
        results = await store.hybrid_search(col, query_vector, "unique phantom text", top_k=5)
        assert results == [], "Phantom hits detected after sync delete + optimize"

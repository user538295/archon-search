"""Tests for Task 3.2 of C6: pipeline.ingest_directory FTS maintenance.

Unit tests mock the store layer; integration tests use connected_store.
Integration tests are marked @pytest.mark.asyncio and NOT @pytest.mark.integration
because they use the connected_store fixture (which is not ML-heavy).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from archon_search._types import IngestResult
from archon_search.store import ChunkIngestResult


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class MockEmbedderBackend:
    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]


def make_embedder():  # type: ignore[no-untyped-def]
    from archon_search.embedder import Embedder

    return Embedder(MockEmbedderBackend())


def make_pipeline(store: Any):  # type: ignore[no-untyped-def]
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    return SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=None,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )


class FakeStore:
    """Minimal fake store for unit tests."""

    from archon_search.config import SearchConfig

    _config = SearchConfig()

    def __init__(self, *, plan_a: bool = True) -> None:
        self._plan_a = plan_a
        self.optimize_fts = AsyncMock(return_value=None)
        self.rebuild_fts_index = AsyncMock(return_value=None)
        self.get_dominant_language = AsyncMock(return_value="en")
        self.ensure_collection = AsyncMock(return_value=None)
        self.ingest_chunks = AsyncMock(
            return_value=ChunkIngestResult(chunks_ingested=3, needs_recompute=False)
        )
        self.recompute_collection_meta = AsyncMock(return_value=None)
        self.get_collection_meta = AsyncMock(return_value=None)
        self.update_description = AsyncMock(return_value=None)
        self._delete_calls: list[dict[str, Any]] = []

    async def delete_document(
        self,
        collection: str,
        doc_id: str,
        namespace: str = "default",
        *,
        skip_fts_optimize: bool = False,
    ) -> int:
        self._delete_calls.append(
            {
                "collection": collection,
                "doc_id": doc_id,
                "namespace": namespace,
                "skip_fts_optimize": skip_fts_optimize,
            }
        )
        return 0

    @property
    def supports_incremental_fts_delete(self) -> bool:
        return self._plan_a


# ---------------------------------------------------------------------------
# Unit tests — mocked store
# ---------------------------------------------------------------------------


def test_ingest_directory_calls_optimize_once(tmp_path: Any) -> None:
    """ingest_directory() must call optimize_fts exactly once after all files (not N times)."""
    store = FakeStore(plan_a=True)
    pipeline = make_pipeline(store)

    # Create 3 markdown files
    for i in range(3):
        f = tmp_path / f"doc{i}.md"
        f.write_text(f"# Document {i}\n\n" + f"Content for document {i}. " * 30)

    asyncio.run(
        pipeline.ingest_directory(
            tmp_path,
            "mycol",
            embedder=pipeline._global_embedder,
        )
    )

    store.optimize_fts.assert_called_once_with("mycol")
    store.rebuild_fts_index.assert_not_called()


def test_ingest_directory_no_optimize_when_all_fail(tmp_path: Any) -> None:
    """ingest_directory() must NOT call optimize_fts if all files fail to ingest."""
    from archon_search.store import StoreBusyError

    store = FakeStore(plan_a=True)
    # StoreBusyError from delete_document causes ingest_file to return status="error" gracefully

    async def delete_raises(collection, doc_id, namespace="default", *, skip_fts_optimize=False):  # type: ignore[no-untyped-def]
        raise StoreBusyError("busy")

    store.delete_document = delete_raises  # type: ignore[method-assign]
    pipeline = make_pipeline(store)

    # Create 2 files
    for i in range(2):
        f = tmp_path / f"doc{i}.md"
        f.write_text(f"# Document {i}\n\n" + f"Content {i}. " * 30)

    results = asyncio.run(
        pipeline.ingest_directory(
            tmp_path,
            "mycol",
            embedder=pipeline._global_embedder,
        )
    )

    assert all(r.status == "error" for r in results), (
        f"Expected all results to be 'error'; got {[r.status for r in results]!r}"
    )
    store.optimize_fts.assert_not_called()
    store.rebuild_fts_index.assert_not_called()


def test_ingest_directory_calls_rebuild_under_plan_b(tmp_path: Any) -> None:
    """When Plan B is active, ingest_directory must call rebuild_fts_index instead of optimize_fts.

    FakeStore.supports_incremental_fts_delete=False (via plan_a=False) simulates Plan B.
    The pipeline reads store.supports_incremental_fts_delete to branch, so FakeStore's
    property directly controls the path — no module monkeypatching needed.
    """
    store = FakeStore(plan_a=False)
    pipeline = make_pipeline(store)

    for i in range(2):
        f = tmp_path / f"doc{i}.md"
        f.write_text(f"# Document {i}\n\n" + f"Content for {i}. " * 30)

    asyncio.run(
        pipeline.ingest_directory(
            tmp_path,
            "mycol",
            embedder=pipeline._global_embedder,
        )
    )

    store.optimize_fts.assert_not_called()
    store.rebuild_fts_index.assert_called_once()


def test_ingest_directory_fallback_to_rebuild_on_optimize_failure(tmp_path: Any) -> None:
    """When optimize_fts raises in ingest_directory, rebuild_fts_index must be called and a warning logged."""
    store = FakeStore(plan_a=True)
    store.optimize_fts = AsyncMock(side_effect=RuntimeError("optimize exploded"))
    pipeline = make_pipeline(store)

    for i in range(2):
        f = tmp_path / f"doc{i}.md"
        f.write_text(f"# Document {i}\n\n" + f"Content {i}. " * 30)

    warning_records: list[logging.LogRecord] = []

    class CapturingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            warning_records.append(record)

    handler = CapturingHandler(level=logging.WARNING)
    pipeline_logger = logging.getLogger("archon_search.pipeline")
    pipeline_logger.addHandler(handler)
    try:
        asyncio.run(
            pipeline.ingest_directory(
                tmp_path,
                "mycol",
                embedder=pipeline._global_embedder,
            )
        )
    finally:
        pipeline_logger.removeHandler(handler)

    store.rebuild_fts_index.assert_called_once()
    assert any(
        "fallback" in r.getMessage().lower() or "rebuild" in r.getMessage().lower()
        for r in warning_records
    ), f"Expected fallback warning; got: {[r.getMessage() for r in warning_records]!r}"


# ---------------------------------------------------------------------------
# Integration tests — connected_store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_directory_all_files_searchable_after_optimize(
    connected_store: Any, col_name: str, tmp_path: Path
) -> None:
    """After ingest_directory, FTS search must return chunks from all ingested files."""
    pipeline = make_pipeline(connected_store)

    # Create 3 files, each with a unique word
    unique_words: list[str] = []
    for i in range(3):
        word = f"uniquedir{uuid.uuid4().hex[:8]}"
        unique_words.append(word)
        f = tmp_path / f"doc{i}.md"
        f.write_text(f"# File {i}\n\n{word} is a unique term in file {i}.\n" * 5)

    results = await pipeline.ingest_directory(
        tmp_path,
        col_name,
        embedder=pipeline._global_embedder,
    )

    assert len(results) == 3
    assert all(r.status == "ok" for r in results), f"Some ingest results failed: {results!r}"

    query_vec = [0.1] * 4
    all_doc_ids = {r.doc_id for r in results}

    for word in unique_words:
        hits = await connected_store.hybrid_search(col_name, query_vec, word, 10)
        returned_ids = {h.doc_id for h in hits}
        overlap = all_doc_ids & returned_ids
        assert overlap, (
            f"Expected at least one result for word {word!r}; "
            f"got {returned_ids!r} vs expected doc_ids {all_doc_ids!r}"
        )

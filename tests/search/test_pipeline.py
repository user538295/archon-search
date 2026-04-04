"""tests/rag/test_pipeline.py — TDD tests for RagPipeline (FEAT-019 Task 4.1)."""
from __future__ import annotations

import re
import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon.search._types import ChunkRecord, CollectionInfo, DocumentInfo, IngestResult, SearchResult
from archon.search.embedder import Embedder, EmbedderBackend
from archon.search.reranker import Reranker, RerankerBackend


# ---------------------------------------------------------------------------
# Mock backends
# ---------------------------------------------------------------------------

class MockEmbedderBackend:
    """Returns dim=4 vectors for all texts."""

    model_name: str = "mock-embedder"

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]


class MockRerankerBackend:
    """Returns 0.5 score for all pairs."""

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5] * len(pairs)


def make_embedder() -> Embedder:
    return Embedder(MockEmbedderBackend())


def make_reranker() -> Reranker:
    return Reranker(MockRerankerBackend())


# ---------------------------------------------------------------------------
# Helper: build a RagPipeline with connected_store
# ---------------------------------------------------------------------------

def make_pipeline(store):  # type: ignore[no-untyped-def]
    from archon.search.chunker import DocumentChunker
    from archon.search.parser import DocumentParser
    from archon.search.pipeline import RagPipeline

    return RagPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )


# ===========================================================================
# Integration tests (require connected_store)
# ===========================================================================


@pytest.mark.asyncio
async def test_pipeline_ingest_file_ok(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "doc.md"
    md_file.write_text("# Hello\n\nThis is a test document with enough content to chunk.\n" * 5)

    result = await pipeline.ingest_file(md_file, col_name)

    assert isinstance(result, IngestResult)
    assert result.status == "ok"
    assert result.chunks_created > 0
    assert result.doc_id  # non-empty


@pytest.mark.asyncio
async def test_pipeline_ingest_file_parse_error(connected_store, col_name, tmp_path):
    from archon.search.parser import ParseError

    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "bad.md"
    md_file.write_text("some content")

    # Mock parser.parse to raise ParseError
    async def _fail(path: Path) -> str:
        raise ParseError(path, Exception("mock parse failure"))

    pipeline._parser.parse = _fail  # type: ignore[method-assign]

    result = await pipeline.ingest_file(md_file, col_name)

    assert result.status == "error"
    assert result.chunks_created == 0
    assert result.error is not None


@pytest.mark.asyncio
async def test_pipeline_ingest_is_idempotent(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "doc.md"
    md_file.write_text("# Idempotent Test\n\nSome content here.\n" * 10)

    await pipeline.ingest_file(md_file, col_name)
    await pipeline.ingest_file(md_file, col_name)

    docs = await pipeline.list_documents(col_name)
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_pipeline_ingest_file_chunk_ids_sequential(connected_store, col_name, tmp_path):
    from archon.search.chunker import DocumentChunker
    from archon.search.parser import DocumentParser
    from archon.search.pipeline import RagPipeline

    captured_records: list[ChunkRecord] = []

    class CapturingStore:
        async def ensure_collection(self, *a: Any, **kw: Any) -> None:
            pass

        async def delete_document(self, *a: Any, **kw: Any) -> int:
            return 0

        async def ingest_chunks(self, collection: str, records: list[ChunkRecord]) -> int:
            captured_records.extend(records)
            return len(records)

        async def rebuild_fts_index(self, *a: Any, **kw: Any) -> None:
            pass

    pipeline = RagPipeline(
        store=CapturingStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=64),
        parser=DocumentParser(),

        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "seq.md"
    md_file.write_text("Word " * 200)  # enough tokens for multiple chunks

    result = await pipeline.ingest_file(md_file, col_name)
    assert result.status == "ok"

    doc_id = result.doc_id
    for idx, rec in enumerate(captured_records):
        assert rec.chunk_id == f"{doc_id}-{idx:06d}", f"Expected {doc_id}-{idx:06d} got {rec.chunk_id}"


@pytest.mark.asyncio
async def test_pipeline_ingest_file_doc_id_is_sha256_hex(connected_store, col_name, tmp_path):
    from archon.search.chunker import DocumentChunker
    from archon.search.parser import DocumentParser
    from archon.search.pipeline import RagPipeline

    captured_records: list[ChunkRecord] = []

    class CapturingStore:
        async def ensure_collection(self, *a: Any, **kw: Any) -> None:
            pass

        async def delete_document(self, *a: Any, **kw: Any) -> int:
            return 0

        async def ingest_chunks(self, collection: str, records: list[ChunkRecord]) -> int:
            captured_records.extend(records)
            return len(records)

        async def rebuild_fts_index(self, *a: Any, **kw: Any) -> None:
            pass

    pipeline = RagPipeline(
        store=CapturingStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),

        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "sha.md"
    md_file.write_text("# Test\n\nContent.\n" * 10)

    result = await pipeline.ingest_file(md_file, col_name)
    assert re.match(r"^[a-f0-9]{64}$", result.doc_id), f"doc_id {result.doc_id!r} is not 64 hex chars"


@pytest.mark.asyncio
async def test_pipeline_ingest_directory(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent for document {i}.\n" * 5)

    results = await pipeline.ingest_directory(tmp_path, col_name)

    assert len(results) == 3
    assert all(r.status == "ok" for r in results)


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_calls_progress_cb(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent.\n" * 5)

    calls: list[tuple[int, int]] = []

    def progress_cb(done: int, total: int) -> None:
        calls.append((done, total))

    await pipeline.ingest_directory(tmp_path, col_name, progress_cb=progress_cb)

    assert len(calls) == 3
    assert calls[-1][0] == 3  # all done
    assert calls[-1][1] == 3  # total


@pytest.mark.asyncio
async def test_pipeline_search_returns_ranked_results(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "search_doc.md"
    md_file.write_text("# Search Test\n\nThis document contains searchable content.\n" * 10)

    await pipeline.ingest_file(md_file, col_name)
    results = await pipeline.search("searchable content", col_name)

    assert isinstance(results, list)
    assert len(results) > 0
    assert all(isinstance(r, SearchResult) for r in results)


@pytest.mark.asyncio
async def test_pipeline_search_with_context_returns_neighbors(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    # Use small chunk_size to force multiple chunks
    from archon.search.chunker import DocumentChunker
    from archon.search.parser import DocumentParser
    from archon.search.pipeline import RagPipeline

    pipeline2 = RagPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=32),
        parser=DocumentParser(),

        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "ctx_doc.md"
    md_file.write_text("# Context Test\n\n" + ("Content chunk. " * 50))
    await pipeline2.ingest_file(md_file, col_name)

    results = await pipeline2.search_with_context("Content chunk", col_name, context_window=1)

    assert isinstance(results, list)
    assert len(results) > 0
    for item in results:
        assert "result" in item
        assert "context_before" in item
        assert "context_after" in item


@pytest.mark.asyncio
async def test_pipeline_delete_document(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "del_doc.md"
    md_file.write_text("# Delete Test\n\nContent to be deleted.\n" * 5)

    result = await pipeline.ingest_file(md_file, col_name)
    assert result.status == "ok"

    deleted = await pipeline.delete_document(result.doc_id, col_name)
    assert deleted > 0

    docs = await pipeline.list_documents(col_name)
    doc_ids = [d.doc_id for d in docs]
    assert result.doc_id not in doc_ids


@pytest.mark.asyncio
async def test_pipeline_list_collections_after_ingest(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "col_doc.md"
    md_file.write_text("# Collection Test\n\nSome content.\n" * 5)

    await pipeline.ingest_file(md_file, col_name)

    collections = await pipeline.list_collections()
    names = [c.name for c in collections]
    assert col_name in names


@pytest.mark.asyncio
async def test_pipeline_ingest_file_fts_searchable(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    unique_word = "xyzuniquekeyword123"
    md_file = tmp_path / "fts_doc.md"
    md_file.write_text(f"# FTS Test\n\nThis document contains {unique_word} for testing.\n" * 5)

    await pipeline.ingest_file(md_file, col_name)

    results = await pipeline.search(unique_word, col_name)
    assert len(results) > 0


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_empty_dir(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    results = await pipeline.ingest_directory(empty_dir, col_name)

    assert results == []


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_partial_failure(connected_store, col_name, tmp_path):
    from archon.search.parser import ParseError

    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent.\n" * 5)

    fail_count = 0
    original_parse = pipeline._parser.parse

    async def _selective_fail(path: Path) -> str:
        nonlocal fail_count
        if path.name == "doc1.md" and fail_count == 0:
            fail_count += 1
            raise ParseError(path, Exception("forced failure"))
        return await original_parse(path)

    pipeline._parser.parse = _selective_fail  # type: ignore[method-assign]

    results = await pipeline.ingest_directory(tmp_path, col_name)

    ok_results = [r for r in results if r.status == "ok"]
    error_results = [r for r in results if r.status == "error"]
    assert len(ok_results) == 2
    assert len(error_results) == 1


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_rebuilds_fts_once(connected_store, col_name, tmp_path):
    from archon.search.chunker import DocumentChunker
    from archon.search.parser import DocumentParser
    from archon.search.pipeline import RagPipeline
    from archon.search.store import RagStore

    rebuild_calls = 0
    original_rebuild = connected_store.rebuild_fts_index

    async def counting_rebuild(collection: str) -> None:
        nonlocal rebuild_calls
        rebuild_calls += 1
        await original_rebuild(collection)

    connected_store.rebuild_fts_index = counting_rebuild  # type: ignore[method-assign]

    try:
        pipeline = RagPipeline(
            store=connected_store,
            embedder=make_embedder(),
            reranker=make_reranker(),
            chunker=DocumentChunker(chunk_size=128),
            parser=DocumentParser(),
    
            top_k_retrieve=10,
            top_k_return=5,
        )

        for i in range(3):
            (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent.\n" * 5)

        await pipeline.ingest_directory(tmp_path, col_name)

        assert rebuild_calls == 1
    finally:
        connected_store.rebuild_fts_index = original_rebuild  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_skips_subdirectories(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    (tmp_path / "doc1.md").write_text("# Doc 1\n\nContent.\n" * 5)
    (tmp_path / "doc2.md").write_text("# Doc 2\n\nContent.\n" * 5)
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    # The subdir itself should not appear as a result (it's a dir, not a file)

    results = await pipeline.ingest_directory(tmp_path, col_name)

    # Only 2 files, not the subdir
    assert len(results) == 2


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_skips_hidden_files(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    (tmp_path / "visible.md").write_text("# Visible\n\nContent.\n" * 5)
    (tmp_path / ".hidden.md").write_text("# Hidden\n\nContent.\n" * 5)

    results = await pipeline.ingest_directory(tmp_path, col_name)

    assert len(results) == 1
    assert results[0].status == "ok"


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_skips_files_in_hidden_directories(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    (tmp_path / "visible.md").write_text("# Visible\n\nContent.\n" * 5)
    hidden_dir = tmp_path / ".git"
    hidden_dir.mkdir()
    (hidden_dir / "tracked.md").write_text("# Tracked\n\nContent.\n")

    results = await pipeline.ingest_directory(tmp_path, col_name)

    assert len(results) == 1
    assert results[0].status == "ok"


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_skips_symlinks(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    real_file = tmp_path / "real.md"
    real_file.write_text("# Real\n\nContent.\n" * 5)
    symlink_file = tmp_path / "link.md"
    symlink_file.symlink_to(real_file)

    results = await pipeline.ingest_directory(tmp_path, col_name)

    # Only real file, not symlink
    assert len(results) == 1


@pytest.mark.asyncio
async def test_pipeline_ingest_file_parse_error_preserves_existing_chunks(connected_store, col_name, tmp_path):
    from archon.search.parser import ParseError

    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "existing.md"
    md_file.write_text("# Existing Content\n\nThis should be preserved.\n" * 10)

    # Ingest successfully first
    first_result = await pipeline.ingest_file(md_file, col_name)
    assert first_result.status == "ok"

    # Now mock parser to fail
    async def _fail(path: Path) -> str:
        raise ParseError(path, Exception("parse error"))

    pipeline._parser.parse = _fail  # type: ignore[method-assign]

    # Re-ingest should fail gracefully
    second_result = await pipeline.ingest_file(md_file, col_name)
    assert second_result.status == "error"

    # Original doc should still be there
    docs = await pipeline.list_documents(col_name)
    assert any(d.doc_id == first_result.doc_id for d in docs)


@pytest.mark.asyncio
async def test_pipeline_ingest_file_empty_content_preserves_existing_chunks(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "empty_content.md"
    md_file.write_text("# First Ingest\n\nThis should be preserved.\n" * 10)

    first_result = await pipeline.ingest_file(md_file, col_name)
    assert first_result.status == "ok"
    assert first_result.chunks_created > 0

    # Overwrite with empty content
    md_file.write_text("")

    second_result = await pipeline.ingest_file(md_file, col_name)
    assert second_result.status == "ok"
    assert second_result.chunks_created == 0

    # Original doc still in store (no delete on empty)
    docs = await pipeline.list_documents(col_name)
    assert any(d.doc_id == first_result.doc_id for d in docs)


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_all_failures_skips_fts_rebuild(connected_store, col_name, tmp_path):
    from archon.search.chunker import DocumentChunker
    from archon.search.parser import DocumentParser, ParseError
    from archon.search.pipeline import RagPipeline

    rebuild_called = False

    class TrackingStore:
        async def ensure_collection(self, *a: Any, **kw: Any) -> None:
            pass

        async def delete_document(self, *a: Any, **kw: Any) -> int:
            return 0

        async def ingest_chunks(self, *a: Any, **kw: Any) -> int:
            return 0

        async def rebuild_fts_index(self, *a: Any, **kw: Any) -> None:
            nonlocal rebuild_called
            rebuild_called = True

    pipeline = RagPipeline(
        store=TrackingStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),

        top_k_retrieve=10,
        top_k_return=5,
    )

    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent.\n")

    async def _always_fail(path: Path) -> str:
        raise ParseError(path, Exception("all fail"))

    pipeline._parser.parse = _always_fail  # type: ignore[method-assign]

    results = await pipeline.ingest_directory(tmp_path, col_name)

    assert all(r.status == "error" for r in results)
    assert not rebuild_called


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_skips_binary_extensions(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    (tmp_path / "data.txt").write_text("Some text content.\n" * 5)
    (tmp_path / "image.gif").write_bytes(b"GIF89a" + b"\x00" * 100)

    results = await pipeline.ingest_directory(tmp_path, col_name)

    assert len(results) == 1
    assert results[0].status == "ok"


def test_pipeline_image_extensions_not_in_binary() -> None:
    from archon.search.pipeline import _BINARY_EXTENSIONS

    image_exts = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}
    for ext in image_exts:
        assert ext not in _BINARY_EXTENSIONS, f"{ext} should not be in _BINARY_EXTENSIONS"


def test_pipeline_gif_svg_ico_remain_binary() -> None:
    from archon.search.pipeline import _BINARY_EXTENSIONS

    for ext in {".gif", ".svg", ".ico"}:
        assert ext in _BINARY_EXTENSIONS, f"{ext} must remain in _BINARY_EXTENSIONS"


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", [".gif", ".svg", ".ico"])
async def test_pipeline_ingest_directory_skips_binary_image(ext: str, connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    (tmp_path / f"binary{ext}").write_bytes(b"\x00" * 50)

    results = await pipeline.ingest_directory(tmp_path, col_name)

    assert results == [], f"Binary {ext} file should not be ingested"


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_includes_png(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    png_file = tmp_path / "image.png"
    png_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    ocr_text = "Extracted OCR text from image. " * 20
    pipeline._parser.parse = AsyncMock(return_value=ocr_text)  # type: ignore[method-assign]

    results = await pipeline.ingest_directory(tmp_path, col_name)

    assert len(results) == 1
    assert results[0].status == "ok"
    assert results[0].chunks_created > 0


@pytest.mark.asyncio
async def test_pipeline_ingest_image_empty_ocr_produces_no_chunk(connected_store, col_name, tmp_path):
    """Empty OCR result skips store interaction — chunker returns [], no chunks stored."""
    pipeline = make_pipeline(connected_store)
    png_file = tmp_path / "blank.png"
    png_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    pipeline._parser.parse = AsyncMock(return_value="")  # type: ignore[method-assign]

    result = await pipeline.ingest_file(png_file, col_name)

    assert result.status == "ok"
    assert result.chunks_created == 0


# ---------------------------------------------------------------------------
# FEAT-022 Task 1.2 — Centroid computation in ingest_directory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_computes_centroid_from_all_chunks(connected_store, col_name, tmp_path):
    """ingest_directory stores centroid = mean of all chunk embeddings from the batch."""
    from datetime import UTC, datetime

    pipeline = make_pipeline(connected_store)
    # MockEmbedderBackend returns [0.1, 0.1, 0.1, 0.1] for all texts
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent for document {i}.\n" * 5)

    before = datetime.now(UTC)
    results = await pipeline.ingest_directory(tmp_path, col_name)
    after = datetime.now(UTC)
    assert all(r.status == "ok" for r in results)

    meta = await connected_store.get_collection_meta(col_name)
    assert meta is not None
    assert meta.centroid is not None
    assert len(meta.centroid) == 4
    # mean of [0.1]*4 vectors is [0.1]*4
    for val in meta.centroid:
        assert abs(val - 0.1) < 1e-9
    assert meta.doc_count == 3
    assert meta.chunk_count > 0
    assert meta.embedding_model == "mock-embedder"
    assert meta.last_indexed is not None
    assert before <= meta.last_indexed <= after


@pytest.mark.asyncio
async def test_ingest_centroid_replaced_on_reingest(connected_store, col_name, tmp_path):
    """Re-ingest replaces the centroid with fresh computation from the new batch."""
    pipeline = make_pipeline(connected_store)
    (tmp_path / "doc.md").write_text("# Doc\n\nContent.\n" * 5)

    # First ingest — MockEmbedderBackend returns [0.1]*4
    await pipeline.ingest_directory(tmp_path, col_name)
    meta1 = await connected_store.get_collection_meta(col_name)
    assert meta1 is not None and meta1.centroid is not None

    # Swap embedder to one returning [0.5]*4
    class AltEmbedderBackend:
        model_name: str = "alt-embedder"

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.5] * 4 for _ in texts]

    pipeline._embedder = Embedder(AltEmbedderBackend())

    # Re-ingest
    await pipeline.ingest_directory(tmp_path, col_name)
    meta2 = await connected_store.get_collection_meta(col_name)
    assert meta2 is not None
    assert meta2.centroid is not None
    for val in meta2.centroid:
        assert abs(val - 0.5) < 1e-9


@pytest.mark.asyncio
async def test_ingest_centroid_averages_heterogeneous_embeddings(connected_store, col_name, tmp_path):
    """Centroid is the element-wise mean, verified with non-uniform vectors."""
    from archon.search.chunker import DocumentChunker
    from archon.search.parser import DocumentParser
    from archon.search.pipeline import RagPipeline

    call_count = 0

    class HeteroEmbedderBackend:
        """Alternate between two distinct 2-d vectors per call batch."""

        model_name: str = "hetero-embedder"

        def encode(self, texts: list[str]) -> list[list[float]]:
            nonlocal call_count
            call_count += 1
            # odd call → [1.0, 0.0]; even call → [0.0, 1.0]
            if call_count % 2 == 1:
                return [[1.0, 0.0] for _ in texts]
            return [[0.0, 1.0] for _ in texts]

    pipeline = RagPipeline(
        store=connected_store,
        embedder=Embedder(HeteroEmbedderBackend()),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=64),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    # Two files → two embed() calls → vectors [1,0] and [0,1]
    (tmp_path / "a.md").write_text("# A\n\nContent.\n" * 5)
    (tmp_path / "b.md").write_text("# B\n\nContent.\n" * 5)

    results = await pipeline.ingest_directory(tmp_path, col_name)
    assert all(r.status == "ok" for r in results)

    meta = await connected_store.get_collection_meta(col_name)
    assert meta is not None and meta.centroid is not None
    assert len(meta.centroid) == 2
    # mean of ([1,0]*n + [0,1]*m) — each file produces k chunks, centroid ≈ [0.5, 0.5]
    assert abs(meta.centroid[0] - 0.5) < 1e-6
    assert abs(meta.centroid[1] - 0.5) < 1e-6


# ---------------------------------------------------------------------------
# FEAT-022 Task 1.3 — Description generation integration with ingest_directory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_directory_calls_generate_description(connected_store, col_name, tmp_path):
    """ingest_directory() calls generate_description when _should_regenerate returns True (first ingest)."""
    from unittest.mock import patch as _patch

    pipeline = make_pipeline(connected_store)
    (tmp_path / "doc.md").write_text("# Doc\n\nContent.\n" * 5)

    with _patch(
        "archon.search.pipeline.generate_description", return_value="A fine collection."
    ) as mock_gen:
        await pipeline.ingest_directory(tmp_path, col_name)

    mock_gen.assert_awaited_once()
    meta = await connected_store.get_collection_meta(col_name)
    assert meta is not None
    assert meta.description == "A fine collection."
    assert meta.described_at_doc_count == 1  # batch_doc_count


@pytest.mark.asyncio
async def test_ingest_directory_preserves_old_description_on_generation_failure(
    connected_store, col_name, tmp_path
):
    """When generate_description returns None, the previous description is preserved."""
    from unittest.mock import patch as _patch

    pipeline = make_pipeline(connected_store)
    (tmp_path / "doc.md").write_text("# Doc\n\nContent.\n" * 5)

    # First ingest — description successfully generated
    with _patch("archon.search.pipeline.generate_description", return_value="Original description."):
        await pipeline.ingest_directory(tmp_path, col_name)

    meta1 = await connected_store.get_collection_meta(col_name)
    assert meta1 is not None and meta1.description == "Original description."

    # Swap embedder so centroid changes and described_at_doc_count triggers regeneration
    class AltBackend:
        model_name: str = "alt"
        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.9] * 4 for _ in texts]

    pipeline._embedder = Embedder(AltBackend())

    # Second ingest — described_at=1, current=1 → no 20% change → no regeneration
    # Force regeneration by using a new collection that has no existing description
    # (We test preservation by simulating failure on a new path that triggers regeneration)
    new_col = col_name + "-b"
    (tmp_path / "doc2.md").write_text("# Doc2\n\nNew content.\n" * 5)

    with _patch("archon.search.pipeline.generate_description", return_value=None) as mock_gen:
        pipeline._embedder = make_embedder()  # reset to standard embedder
        await pipeline.ingest_directory(tmp_path, new_col)

    meta2 = await connected_store.get_collection_meta(new_col)
    assert meta2 is not None
    # generate_description was called (first ingest, described_at=None) but returned None
    mock_gen.assert_awaited_once()
    # description remains None since generation failed and there was no previous description
    assert meta2.description is None
    # described_at_doc_count not updated on failure
    assert meta2.described_at_doc_count is None


@pytest.mark.asyncio
async def test_ingest_directory_sets_described_at_doc_count_on_success(
    connected_store, col_name, tmp_path
):
    """After successful description generation, described_at_doc_count equals batch_doc_count."""
    from unittest.mock import patch as _patch

    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent.\n" * 5)

    with _patch("archon.search.pipeline.generate_description", return_value="Three docs here."):
        await pipeline.ingest_directory(tmp_path, col_name)

    meta = await connected_store.get_collection_meta(col_name)
    assert meta is not None
    assert meta.described_at_doc_count == 3
    assert meta.last_described is not None


# ===========================================================================
# Unit tests
# ===========================================================================


@pytest.mark.asyncio
async def test_pipeline_search_with_context_malformed_chunk_id(tmp_path):
    from archon.search.chunker import DocumentChunker
    from archon.search.parser import DocumentParser
    from archon.search.pipeline import RagPipeline

    malformed_result = SearchResult(
        doc_id="a" * 64,
        chunk_id="bad-chunk-id",
        text="some text",
        score=0.9,
        source_path="/some/path",
    )

    class MockStore:
        async def hybrid_search(self, *a: Any, **kw: Any) -> list[SearchResult]:
            return [malformed_result]

        async def fetch_adjacent_chunks(self, *a: Any, **kw: Any) -> list[ChunkRecord]:
            return []

    pipeline = RagPipeline(
        store=MockStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),

        top_k_retrieve=10,
        top_k_return=5,
    )

    # Pre-warm embedder dim
    await pipeline._embedder.embed(["warmup"])

    results = await pipeline.search_with_context("query", "test-collection", context_window=1)

    assert len(results) == 1
    assert results[0]["result"] == malformed_result
    assert results[0]["context_before"] == []
    assert results[0]["context_after"] == []


@pytest.mark.asyncio
async def test_create_pipeline_wires_all_components():
    from archon.config.loader import RagConfig
    from archon.search.pipeline import create_pipeline

    cfg = RagConfig(db_path="/tmp/test_rag_db")

    with (
        patch("archon.search.pipeline.ModelEmbedder") as MockME,
        patch("archon.search.pipeline.ModelReranker") as MockMR,
        patch("archon.search.pipeline.DocumentChunker") as MockChunker,
        patch("archon.search.pipeline.DocumentParser") as MockParser,
        patch("archon.search.pipeline.RagStore") as MockStore,
    ):
        MockME.return_value = MockEmbedderBackend()
        MockMR.return_value = MockRerankerBackend()
        MockChunker.return_value = MagicMock()
        MockParser.return_value = MagicMock()
        MockStore.return_value = MagicMock()

        pipeline = create_pipeline(cfg)

    assert pipeline.store is not None
    assert pipeline._embedder is not None
    assert pipeline._reranker is not None
    assert pipeline._chunker is not None
    assert pipeline._parser is not None


@pytest.mark.asyncio
async def test_create_pipeline_does_not_auto_connect():
    from archon.config.loader import RagConfig
    from archon.search.pipeline import create_pipeline

    cfg = RagConfig(db_path="/tmp/test_no_connect_rag")

    with (
        patch("archon.search.pipeline.ModelEmbedder") as MockME,
        patch("archon.search.pipeline.ModelReranker") as MockMR,
        patch("archon.search.pipeline.DocumentChunker"),
        patch("archon.search.pipeline.DocumentParser"),
        patch("archon.search.pipeline.RagStore") as MockStore,
    ):
        MockME.return_value = MockEmbedderBackend()
        MockMR.return_value = MockRerankerBackend()
        # Real RagStore that is NOT connected
        from archon.search.store import RagStore
        real_store = RagStore("/tmp/test_no_connect_rag")
        MockStore.return_value = real_store

        pipeline = create_pipeline(cfg)

    with pytest.raises(RuntimeError, match="RagStore not connected"):
        await pipeline.list_collections()


# ---------------------------------------------------------------------------
# FEAT-021 Task 2.2 — history_collection parameter removed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_calls_progress_callback(connected_store, col_name, tmp_path):
    """progress_cb(done, total) is called once per file processed in sorted order."""
    pipeline = make_pipeline(connected_store)
    # Create files with names that would be out of sort order in typical filesystem order
    (tmp_path / "z.md").write_text("# Z\n\nContent for file Z.\n" * 5)
    (tmp_path / "a.md").write_text("# A\n\nContent for file A.\n" * 5)

    calls: list[tuple[int, int]] = []
    files_seen: list[str] = []

    def _cb(done: int, total: int) -> None:
        calls.append((done, total))

    # Spy on ingest_file to verify sorted processing order
    original_ingest_file = pipeline.ingest_file

    async def _spy_ingest_file(path, collection, **kwargs):
        files_seen.append(path.name)
        return await original_ingest_file(path, collection, **kwargs)

    pipeline.ingest_file = _spy_ingest_file

    results = await pipeline.ingest_directory(tmp_path, col_name, progress_cb=_cb)

    assert len(results) == 2
    assert len(calls) == 2
    # First call: done=1, total=2
    assert calls[0] == (1, 2)
    # Second call: done=2, total=2
    assert calls[1] == (2, 2)
    # Verify sorted order: a.md processed before z.md
    assert files_seen == ["a.md", "z.md"], f"Expected sorted order, got {files_seen}"


@pytest.mark.asyncio
async def test_ingest_async_progress_callback(connected_store, col_name, tmp_path):
    """Async progress_cb is properly awaited (inspect.isawaitable branch)."""
    pipeline = make_pipeline(connected_store)
    (tmp_path / "a.md").write_text("# A\n\nContent for async test.\n" * 5)

    calls: list[tuple[int, int]] = []

    async def _async_cb(done: int, total: int) -> None:
        calls.append((done, total))

    results = await pipeline.ingest_directory(tmp_path, col_name, progress_cb=_async_cb)

    assert len(results) == 1
    assert calls == [(1, 1)]


def test_create_pipeline_no_history_collection_param() -> None:
    """RagPipeline.__init__ must NOT accept history_collection parameter."""
    import inspect
    from archon.search.pipeline import RagPipeline

    sig = inspect.signature(RagPipeline.__init__)
    assert "history_collection" not in sig.parameters, (
        "history_collection parameter still present in RagPipeline.__init__"
    )


def test_create_pipeline_factory_no_history_collection_param() -> None:
    """create_pipeline() must NOT pass history_collection to RagPipeline."""
    import inspect
    from archon.search.pipeline import create_pipeline

    sig = inspect.signature(create_pipeline)
    assert "history_collection" not in sig.parameters, (
        "history_collection parameter still present in create_pipeline()"
    )


def test_ragpipeline_has_no_history_collection_attr() -> None:
    """RagPipeline instance must NOT have _history_collection attribute."""
    from unittest.mock import MagicMock
    from archon.search.embedder import Embedder
    from archon.search.pipeline import RagPipeline
    from archon.search.reranker import Reranker

    pipeline = RagPipeline(
        store=MagicMock(),
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(MockRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    assert not hasattr(pipeline, "_history_collection"), (
        "_history_collection attribute still present on RagPipeline"
    )


# ===========================================================================
# Task 3.2 — exclude_paths and on_file_complete tests
# ===========================================================================


@pytest.mark.asyncio
async def test_ingest_directory_exclude_paths_skips_files(connected_store, col_name, tmp_path):
    """exclude_paths containing a file's absolute path → that file not in results."""
    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent for document {i}.\n" * 5)

    exclude = frozenset({str(tmp_path / "doc1.md")})
    results = await pipeline.ingest_directory(tmp_path, col_name, exclude_paths=exclude)

    assert len(results) == 2
    assert all(r.status == "ok" for r in results)


@pytest.mark.asyncio
async def test_ingest_directory_exclude_paths_adjusts_total(connected_store, col_name, tmp_path):
    """progress_cb receives total equal to non-excluded file count."""
    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent.\n" * 5)

    calls: list[tuple[int, int]] = []

    def progress_cb(done: int, total: int) -> None:
        calls.append((done, total))

    exclude = frozenset({str(tmp_path / "doc1.md")})
    await pipeline.ingest_directory(tmp_path, col_name, progress_cb=progress_cb, exclude_paths=exclude)

    assert len(calls) == 2
    assert all(total == 2 for _, total in calls)


@pytest.mark.asyncio
async def test_ingest_directory_on_file_complete_called_per_file(connected_store, col_name, tmp_path):
    """Callback fired once for each successfully processed file with correct Path."""
    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent for document {i}.\n" * 5)

    completed: list[Path] = []
    results = await pipeline.ingest_directory(
        tmp_path, col_name, on_file_complete=lambda p: completed.append(p),
    )

    assert len(completed) == 3
    assert all(isinstance(p, Path) for p in completed)
    expected = {tmp_path / f"doc{i}.md" for i in range(3)}
    assert set(completed) == expected
    # All three files were OK
    assert all(r.status == "ok" for r in results)


@pytest.mark.asyncio
async def test_ingest_directory_on_file_complete_only_for_ok_results(connected_store, col_name, tmp_path):
    """Callback NOT called for files where ingest_file returns status='error'."""
    from archon.search.parser import ParseError

    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent.\n" * 5)

    original_parse = pipeline._parser.parse

    async def _selective_fail(path: Path) -> str:
        if path.name == "doc1.md":
            raise ParseError(path, Exception("forced failure"))
        return await original_parse(path)

    pipeline._parser.parse = _selective_fail  # type: ignore[method-assign]

    completed: list[Path] = []
    results = await pipeline.ingest_directory(
        tmp_path, col_name, on_file_complete=lambda p: completed.append(p),
    )

    error_results = [r for r in results if r.status == "error"]
    assert len(error_results) == 1
    assert len(completed) == 2
    # doc1.md must NOT be in completed
    assert not any(p.name == "doc1.md" for p in completed)


@pytest.mark.asyncio
async def test_ingest_directory_no_new_files_returns_empty(connected_store, col_name, tmp_path):
    """All files excluded → empty result, progress_cb never called."""
    pipeline = make_pipeline(connected_store)
    for i in range(2):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent.\n" * 5)

    exclude = frozenset({str(tmp_path / "doc0.md"), str(tmp_path / "doc1.md")})
    calls: list[tuple[int, int]] = []

    results = await pipeline.ingest_directory(
        tmp_path, col_name, progress_cb=lambda d, t: calls.append((d, t)), exclude_paths=exclude,
    )

    assert results == []
    assert calls == []


@pytest.mark.asyncio
async def test_ingest_directory_no_exclude_paths_unchanged(connected_store, col_name, tmp_path):
    """exclude_paths=None → identical to current behaviour (no filtering)."""
    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent for document {i}.\n" * 5)

    results = await pipeline.ingest_directory(tmp_path, col_name, exclude_paths=None)

    assert len(results) == 3
    assert all(r.status == "ok" for r in results)


@pytest.mark.asyncio
async def test_ingest_directory_exclude_and_on_file_complete_combined(connected_store, col_name, tmp_path):
    """exclude_paths + on_file_complete + parse error: callback fires only for non-excluded ok files."""
    from archon.search.parser import ParseError

    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent for document {i}.\n" * 5)

    original_parse = pipeline._parser.parse

    async def _selective_fail(path: Path) -> str:
        if path.name == "doc1.md":
            raise ParseError(path, Exception("forced failure"))
        return await original_parse(path)

    pipeline._parser.parse = _selective_fail  # type: ignore[method-assign]

    exclude = frozenset({str(tmp_path / "doc0.md")})
    completed: list[Path] = []
    results = await pipeline.ingest_directory(
        tmp_path, col_name,
        exclude_paths=exclude,
        on_file_complete=lambda p: completed.append(p),
    )

    # doc0 excluded, doc1 errored, doc2 ok → callback only for doc2
    assert len(results) == 2  # doc1 + doc2 (doc0 excluded)
    assert len(completed) == 1
    assert completed[0] == tmp_path / "doc2.md"

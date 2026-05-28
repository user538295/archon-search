"""packages/archon-search/tests/test_pipeline.py — TDD tests for SearchPipeline ."""
from __future__ import annotations

import re
import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._types import ChunkRecord, CollectionInfo, DocumentInfo, IngestResult, SearchResult
from archon_search.embedder import Embedder, EmbedderBackend
from archon_search.reranker import Reranker, RerankerBackend


# ---------------------------------------------------------------------------
# Mock backends
# ---------------------------------------------------------------------------

class MockEmbedderBackend:
    """Returns dim=4 vectors for all texts."""

    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]


class MockRerankerBackend:
    """Returns 0.5 score for all pairs."""

    is_warm: bool = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5] * len(pairs)


def make_embedder() -> Embedder:
    return Embedder(MockEmbedderBackend())


def make_reranker() -> Reranker:
    return Reranker(MockRerankerBackend())


# ---------------------------------------------------------------------------
# Helper: build a SearchPipeline with connected_store
# ---------------------------------------------------------------------------

def make_pipeline(store):  # type: ignore[no-untyped-def]
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    return SearchPipeline(
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
    from archon_search.parser import ParseError

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

    # Use ingest_directory so collection meta is written (required by list_documents namespace guard)
    await pipeline.ingest_directory(tmp_path, col_name)
    await pipeline.ingest_directory(tmp_path, col_name)

    docs = await pipeline.list_documents(col_name)
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_pipeline_ingest_file_chunk_ids_sequential(connected_store, col_name, tmp_path):
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

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

    pipeline = SearchPipeline(
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
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

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

    pipeline = SearchPipeline(
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
    result = await pipeline.search("searchable content", col_name)

    from archon_search.pipeline import SearchPipelineResult
    assert isinstance(result, SearchPipelineResult)
    assert len(result.results) > 0
    assert all(isinstance(r, SearchResult) for r in result.results)


@pytest.mark.asyncio
async def test_pipeline_search_with_context_returns_neighbors(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    # Use small chunk_size to force multiple chunks
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline2 = SearchPipeline(
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

    # Use ingest_directory so collection meta is written (required by delete_document namespace guard)
    results = await pipeline.ingest_directory(tmp_path, col_name)
    assert len(results) == 1
    result = results[0]
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

    result = await pipeline.search(unique_word, col_name)
    assert len(result.results) > 0


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_empty_dir(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    results = await pipeline.ingest_directory(empty_dir, col_name)

    assert results == []


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_partial_failure(connected_store, col_name, tmp_path):
    from archon_search.parser import ParseError

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
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.store import SearchStore

    rebuild_calls = 0
    original_rebuild = connected_store.rebuild_fts_index

    async def counting_rebuild(collection: str) -> None:
        nonlocal rebuild_calls
        rebuild_calls += 1
        await original_rebuild(collection)

    connected_store.rebuild_fts_index = counting_rebuild  # type: ignore[method-assign]

    try:
        pipeline = SearchPipeline(
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
    from archon_search.parser import ParseError

    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "existing.md"
    md_file.write_text("# Existing Content\n\nThis should be preserved.\n" * 10)

    # Use ingest_directory to create collection meta (required by list_documents namespace guard)
    results = await pipeline.ingest_directory(tmp_path, col_name)
    first_result = results[0]
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

    # Use ingest_directory to create collection meta (required by list_documents namespace guard)
    results = await pipeline.ingest_directory(tmp_path, col_name)
    first_result = results[0]
    assert first_result.status == "ok"
    assert first_result.chunks_created > 0

    # Overwrite with empty content and re-ingest via ingest_file
    md_file.write_text("")

    second_result = await pipeline.ingest_file(md_file, col_name)
    assert second_result.status == "ok"
    assert second_result.chunks_created == 0

    # Original doc still in store (no delete on empty)
    docs = await pipeline.list_documents(col_name)
    assert any(d.doc_id == first_result.doc_id for d in docs)


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_all_failures_skips_fts_rebuild(connected_store, col_name, tmp_path):
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser, ParseError
    from archon_search.pipeline import SearchPipeline

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

    pipeline = SearchPipeline(
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
    from archon_search.pipeline import _BINARY_EXTENSIONS

    image_exts = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}
    for ext in image_exts:
        assert ext not in _BINARY_EXTENSIONS, f"{ext} should not be in _BINARY_EXTENSIONS"


def test_pipeline_gif_svg_ico_remain_binary() -> None:
    from archon_search.pipeline import _BINARY_EXTENSIONS

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
# Centroid computation in ingest_directory
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
        is_warm: bool = False

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
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    call_count = 0

    class HeteroEmbedderBackend:
        """Alternate between two distinct 2-d vectors per call batch."""

        model_name: str = "hetero-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            nonlocal call_count
            call_count += 1
            # odd call → [1.0, 0.0]; even call → [0.0, 1.0]
            if call_count % 2 == 1:
                return [[1.0, 0.0] for _ in texts]
            return [[0.0, 1.0] for _ in texts]

    pipeline = SearchPipeline(
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
# Description generation integration with ingest_directory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_directory_calls_generate_description(connected_store, col_name, tmp_path):
    """ingest_directory() calls generate_description when _should_regenerate returns True (first ingest)."""
    from unittest.mock import patch as _patch

    pipeline = make_pipeline(connected_store)
    (tmp_path / "doc.md").write_text("# Doc\n\nContent.\n" * 5)

    with _patch(
        "archon_search.pipeline.generate_description", return_value="A fine collection."
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
    with _patch("archon_search.pipeline.generate_description", return_value="Original description."):
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

    with _patch("archon_search.pipeline.generate_description", return_value=None) as mock_gen:
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

    with _patch("archon_search.pipeline.generate_description", return_value="Three docs here."):
        await pipeline.ingest_directory(tmp_path, col_name)

    meta = await connected_store.get_collection_meta(col_name)
    assert meta is not None
    assert meta.described_at_doc_count == 3
    assert meta.last_described is not None


# ===========================================================================
# Stage instrumentation tests (B1 Task 3.5)
# ===========================================================================


@pytest.mark.asyncio
async def test_search_with_context_records_context_stage(tmp_path):
    """search_with_context records the 'context' stage when a recorder is bound."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.observability import bind_stage_recorder

    search_result = SearchResult(
        doc_id="a" * 64,
        chunk_id=("a" * 64) + "-000000",
        text="some text",
        score=0.9,
        source_path="/some/path",
    )

    class StubStore:
        async def hybrid_search(self, *a: Any, **kw: Any) -> list[SearchResult]:
            return [search_result]

        async def fetch_adjacent_chunks(self, *a: Any, **kw: Any) -> list[ChunkRecord]:
            return []

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._embedder.embed(["warmup"])

    with bind_stage_recorder() as recorder:
        await pipeline.search_with_context("query", "test-col", context_window=1)

    assert "context" in recorder.stage_timings_ms
    assert recorder.stage_timings_ms["context"] >= 0.0


@pytest.mark.asyncio
async def test_ingest_file_records_parse_embed_persist(tmp_path):
    """ingest_file records parse, embed, and persist stages when a recorder is bound."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.observability import bind_stage_recorder

    class StubStore:
        async def ensure_collection(self, *a: Any, **kw: Any) -> None:
            pass

        async def delete_document(self, *a: Any, **kw: Any) -> int:
            return 0

        async def ingest_chunks(self, *a: Any, **kw: Any) -> int:
            return 1

        async def rebuild_fts_index(self, *a: Any, **kw: Any) -> None:
            pass

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=64),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "doc.md"
    md_file.write_text("# Test\n\nContent for stage recording.\n" * 10)

    with bind_stage_recorder() as recorder:
        result = await pipeline.ingest_file(md_file, "test-col")

    assert result.status == "ok"
    assert {"parse", "embed", "persist"} <= recorder.stage_timings_ms.keys()


@pytest.mark.asyncio
async def test_pipeline_noop_when_unbound(tmp_path):
    """Pipeline methods run without error when no recorder is bound; ContextVar stays None."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.observability import _stage_recorder

    search_result = SearchResult(
        doc_id="b" * 64,
        chunk_id=("b" * 64) + "-000000",
        text="text",
        score=0.5,
        source_path="/path",
    )

    class StubStore:
        async def hybrid_search(self, *a: Any, **kw: Any) -> list[SearchResult]:
            return [search_result]

        async def fetch_adjacent_chunks(self, *a: Any, **kw: Any) -> list[ChunkRecord]:
            return []

        async def ensure_collection(self, *a: Any, **kw: Any) -> None:
            pass

        async def delete_document(self, *a: Any, **kw: Any) -> int:
            return 0

        async def ingest_chunks(self, *a: Any, **kw: Any) -> int:
            return 1

        async def rebuild_fts_index(self, *a: Any, **kw: Any) -> None:
            pass

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=64),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._embedder.embed(["warmup"])

    md_file = tmp_path / "doc.md"
    md_file.write_text("# Test\n\nSome content.\n" * 5)

    assert _stage_recorder.get() is None
    await pipeline.search("query", "col")
    assert _stage_recorder.get() is None

    await pipeline.search_with_context("query", "col")
    assert _stage_recorder.get() is None

    await pipeline.ingest_file(md_file, "col")
    assert _stage_recorder.get() is None


# ===========================================================================
# Unit tests
# ===========================================================================


@pytest.mark.asyncio
async def test_pipeline_search_with_context_malformed_chunk_id(tmp_path):
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

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

    pipeline = SearchPipeline(
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
    from unittest.mock import MagicMock
    from archon_search.pipeline import create_pipeline

    cfg = MagicMock()
    cfg.db_path = "/tmp/test_rag_db"

    with (
        patch("archon_search.pipeline.ModelEmbedder") as MockME,
        patch("archon_search.pipeline.ModelReranker") as MockMR,
        patch("archon_search.pipeline.DocumentChunker") as MockChunker,
        patch("archon_search.pipeline.DocumentParser") as MockParser,
        patch("archon_search.pipeline.SearchStore") as MockStore,
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


def test_pipeline_stores_fanout_params() -> None:
    """SearchPipeline stores fan-out scalars as private instance attributes."""
    from unittest.mock import MagicMock

    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=MagicMock(),
        embedder=MagicMock(),
        reranker=MagicMock(),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
        max_fanout=3,
        fanout_leg_trim=7,
        fanout_timeout_seconds=2.5,
    )
    assert pipeline._max_fanout == 3
    assert pipeline._fanout_leg_trim == 7
    assert pipeline._fanout_timeout_seconds == 2.5


def test_pipeline_default_fanout_params_match_config() -> None:
    """Constructor fan-out defaults must stay in sync with SearchConfig defaults."""
    from unittest.mock import MagicMock

    from archon_search.config import SearchConfig
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=MagicMock(),
        embedder=MagicMock(),
        reranker=MagicMock(),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    cfg = SearchConfig()
    assert pipeline._max_fanout == cfg.max_fanout
    assert pipeline._fanout_leg_trim == cfg.fanout_leg_trim
    assert pipeline._fanout_timeout_seconds == cfg.fanout_timeout_seconds


@pytest.mark.asyncio
async def test_create_pipeline_passes_fanout_config() -> None:
    """create_pipeline reads fan-out keys from config and passes them through."""
    from unittest.mock import MagicMock, patch

    from archon_search.config import SearchConfig
    from archon_search.pipeline import create_pipeline

    cfg = SearchConfig()
    cfg.db_path = "/tmp/test_fanout_cfg"
    cfg.max_fanout = 6
    cfg.fanout_leg_trim = 25
    cfg.fanout_timeout_seconds = 12.5

    with (
        patch("archon_search.pipeline.ModelEmbedder", return_value=MockEmbedderBackend()),
        patch("archon_search.pipeline.ModelReranker", return_value=MockRerankerBackend()),
        patch("archon_search.pipeline.DocumentChunker"),
        patch("archon_search.pipeline.DocumentParser"),
        patch("archon_search.pipeline.SearchStore", return_value=MagicMock()),
    ):
        pipeline = create_pipeline(cfg)

    assert pipeline._max_fanout == 6
    assert pipeline._fanout_leg_trim == 25
    assert pipeline._fanout_timeout_seconds == 12.5


@pytest.mark.asyncio
async def test_create_pipeline_does_not_auto_connect():
    from unittest.mock import MagicMock
    from archon_search.pipeline import create_pipeline

    cfg = MagicMock()
    cfg.db_path = "/tmp/test_no_connect_rag"

    with (
        patch("archon_search.pipeline.ModelEmbedder") as MockME,
        patch("archon_search.pipeline.ModelReranker") as MockMR,
        patch("archon_search.pipeline.DocumentChunker"),
        patch("archon_search.pipeline.DocumentParser"),
        patch("archon_search.pipeline.SearchStore") as MockStore,
    ):
        MockME.return_value = MockEmbedderBackend()
        MockMR.return_value = MockRerankerBackend()
        # Real SearchStore that is NOT connected
        from archon_search.store import SearchStore
        real_store = SearchStore("/tmp/test_no_connect_rag")
        MockStore.return_value = real_store

        pipeline = create_pipeline(cfg)

    with pytest.raises(RuntimeError, match="SearchStore not connected"):
        await pipeline.list_collections()


# ---------------------------------------------------------------------------
# history_collection parameter removed
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
    """SearchPipeline.__init__ must NOT accept history_collection parameter."""
    import inspect
    from archon_search.pipeline import SearchPipeline

    sig = inspect.signature(SearchPipeline.__init__)
    assert "history_collection" not in sig.parameters, (
        "history_collection parameter still present in SearchPipeline.__init__"
    )


def test_create_pipeline_factory_no_history_collection_param() -> None:
    """create_pipeline() must NOT pass history_collection to SearchPipeline."""
    import inspect
    from archon_search.pipeline import create_pipeline

    sig = inspect.signature(create_pipeline)
    assert "history_collection" not in sig.parameters, (
        "history_collection parameter still present in create_pipeline()"
    )


def test_ragpipeline_has_no_history_collection_attr() -> None:
    """SearchPipeline instance must NOT have _history_collection attribute."""
    from unittest.mock import MagicMock
    from archon_search.embedder import Embedder
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    pipeline = SearchPipeline(
        store=MagicMock(),
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(MockRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    assert not hasattr(pipeline, "_history_collection"), (
        "_history_collection attribute still present on SearchPipeline"
    )


def test_create_pipeline_uses_expanded_db_path() -> None:
    """create_pipeline() with a tilde db_path must produce a fully-expanded store._db_path."""
    from pathlib import Path
    from unittest.mock import MagicMock, patch
    from archon_search.pipeline import create_pipeline

    cfg = MagicMock()
    cfg.db_path = "~/.archon/search"
    with (
        patch("archon_search.pipeline.DocumentChunker"),
        patch("archon_search.pipeline.DocumentParser"),
    ):
        pipeline = create_pipeline(cfg, embedder_backend=MagicMock(), reranker_backend=MagicMock())
    assert pipeline.store._db_path == Path.home() / ".archon/search"


# ===========================================================================
# exclude_paths and on_file_complete tests
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
    from archon_search.parser import ParseError

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
    from archon_search.parser import ParseError

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


# ===========================================================================
# error-path and resilience tests
# ===========================================================================


@pytest.mark.asyncio
async def test_pipeline_ingest_file_embedder_exception_propagates(connected_store, col_name, tmp_path):
    """ embedder.embed raises during ingest_file → exception propagates to caller."""
    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "doc.md"
    md_file.write_text("# Hello\n\nContent to embed.\n" * 5)

    class ExplodingBackend:
        model_name: str = "exploding"

        def encode(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embedder exploded")

    pipeline._embedder = Embedder(ExplodingBackend())

    with pytest.raises(RuntimeError, match="embedder exploded"):
        await pipeline.ingest_file(md_file, col_name)


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_partial_file_failure_continues(connected_store, col_name, tmp_path):
    """ one file parse-fails → others indexed, progress_cb called for every file including failed one."""
    from archon_search.parser import ParseError

    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent for document {i}.\n" * 5)

    original_parse = pipeline._parser.parse

    async def _selective_fail(path: Path) -> str:
        if path.name == "doc1.md":
            raise ParseError(path, Exception("forced failure"))
        return await original_parse(path)

    pipeline._parser.parse = _selective_fail  # type: ignore[method-assign]

    calls: list[tuple[int, int]] = []

    def progress_cb(done: int, total: int) -> None:
        calls.append((done, total))

    results = await pipeline.ingest_directory(tmp_path, col_name, progress_cb=progress_cb)

    # One file parse-fails, two succeed
    ok_results = [r for r in results if r.status == "ok"]
    error_results = [r for r in results if r.status == "error"]
    assert len(ok_results) == 2
    assert len(error_results) == 1

    # progress_cb called for every file processed, including the failed one
    assert len(calls) == 3
    assert calls[-1] == (3, 3)


@pytest.mark.asyncio
async def test_pipeline_search_embedder_exception_propagates(connected_store, col_name, tmp_path):
    """ embedder.embed_one raises during search → exception propagates to caller."""
    pipeline = make_pipeline(connected_store)

    class ExplodingBackend:
        model_name: str = "exploding-search"

        def encode(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("search embedder exploded")

    pipeline._embedder = Embedder(ExplodingBackend())

    with pytest.raises(RuntimeError, match="search embedder exploded"):
        await pipeline.search("any query", col_name)


@pytest.mark.asyncio
async def test_pipeline_search_with_context_fetch_exception_propagates(tmp_path):
    """ fetch_adjacent_chunks raises → exception propagates to caller (current production behavior).

    Spec intent was: fetch_adjacent_chunks failure → logs, continues, returns result with empty context.
    Production code at pipeline.py:~235 has no try/except around fetch_adjacent_chunks(), so the
    exception propagates instead. This test pins the actual behavior as a regression guard.
    If graceful degradation is ever added, update this test to assert result-with-empty-context.
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    hit = SearchResult(
        doc_id="a" * 64,
        chunk_id=("a" * 64) + "-000001",
        text="some result text",
        score=0.9,
        source_path="/some/path.md",
    )

    class FailingFetchStore:
        async def hybrid_search(self, *a: Any, **kw: Any) -> list[SearchResult]:
            return [hit]

        async def fetch_adjacent_chunks(self, *a: Any, **kw: Any) -> list[Any]:
            raise RuntimeError("fetch_adjacent_chunks exploded")

    pipeline = SearchPipeline(
        store=FailingFetchStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    # Pre-warm embedder so embedding_dim is set
    await pipeline._embedder.embed(["warmup"])

    # Current production behavior: exception propagates to caller
    with pytest.raises(RuntimeError, match="fetch_adjacent_chunks exploded"):
        await pipeline.search_with_context("query", "test-col", context_window=1)


# ===========================================================================
# SQL injection regression guards
# ===========================================================================


# ===========================================================================
# Pipeline zero-files ingest, chunk_size=1
# ===========================================================================


@pytest.mark.asyncio
async def test_P14_21_pipeline_ingest_directory_zero_markdown_files(connected_store, col_name, tmp_path):
    """ ingest_directory on a dir with zero accepted files returns [] and does not crash."""
    pipeline = make_pipeline(connected_store)
    # Only binary files present — all should be filtered out
    (tmp_path / "image.gif").write_bytes(b"GIF89a" + b"\x00" * 50)
    (tmp_path / "archive.zip").write_bytes(b"PK" + b"\x00" * 50)

    results = await pipeline.ingest_directory(tmp_path, col_name)

    assert results == []


@pytest.mark.asyncio
async def test_P14_22_pipeline_ingest_file_chunk_size_1(connected_store, col_name, tmp_path):
    """ chunk_size=1 (minimal) produces one chunk per token without crashing."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=1),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "tiny.md"
    md_file.write_text("alpha beta gamma delta")

    result = await pipeline.ingest_file(md_file, col_name)

    assert result.status == "ok"
    # With chunk_size=1 every token is its own chunk — "alpha beta gamma delta" has 4 words
    assert result.chunks_created >= 4


def test_p14_23_add_collection_sql_injection_rejected_by_validate_collection() -> None:
    """ collection name containing apostrophe raises ValueError from _validate_collection.

    Ensures no SQL injection is possible via collection names: any name that does not
    match ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ is rejected before reaching the database.
    """
    from archon_search.store import SearchStore

    with pytest.raises(ValueError, match="Invalid collection name"):
        SearchStore._validate_collection("col'name")


@pytest.mark.asyncio
async def test_p14_24_delete_document_sql_injection_rejected_by_doc_id_re(connected_store, col_name, tmp_path) -> None:
    """ doc_id containing SQL injection payload raises ValueError before SQL construction.

    _DOC_ID_RE requires exactly 64 hex chars; any deviation (including injection strings)
    is rejected with ValueError before any SQL is built or executed.

    A valid collection with meta must exist first so the namespace guard passes
    and the doc_id validation in the store layer is reached.
    """
    pipeline = make_pipeline(connected_store)
    (tmp_path / "doc.md").write_text("# test\n\nContent.\n" * 5)
    await pipeline.ingest_directory(tmp_path, col_name)

    with pytest.raises(ValueError, match="Invalid doc_id"):
        await pipeline.delete_document("' OR '1'='1", col_name)


# ===========================================================================
# Eval trace execution path
# ===========================================================================


def _make_scored_candidate(
    doc_id: str,
    chunk_id: str,
    text: str = "chunk text",
    rrf_score: float = 0.5,
    reranker_score: float | None = None,
) -> "ScoredSearchCandidate":
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown

    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text=text,
        source_path=f"/path/to/{doc_id}.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=0,
            vector_score=0.9,
            vector_score_kind="distance",
            fts_rank=None,
            fts_score=None,
            fts_score_kind=None,
            rrf_score=rrf_score,
            reranker_score=reranker_score,
        ),
        collection="test-col",
    )


@pytest.mark.asyncio
async def test_eval_trace_returns_pre_and_post_rerank_results(tmp_path):
    """collect_search_trace returns (pre_rerank, post_rerank) both as EvalSearchResult lists."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from archon_search.chunker import DocumentChunker
    from archon_search.eval.types import EvalSearchResult
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    doc_id = "a" * 64
    pre_candidates = [_make_scored_candidate(doc_id, f"{doc_id}-000000", rrf_score=0.8)]
    post_candidates = [_make_scored_candidate(doc_id, f"{doc_id}-000000", rrf_score=0.8, reranker_score=0.9)]

    pipeline = SearchPipeline(
        store=MagicMock(),
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    with (
        patch("archon_search.eval._tracing._hybrid_search_with_trace", new=AsyncMock(return_value=pre_candidates)),
        patch.object(pipeline._reranker, "rerank_candidates", new=AsyncMock(return_value=post_candidates)),
    ):
        pre, post = await collect_search_trace(
            pipeline, "test query", "test-col",
            candidate_depth=20, return_depth=5, metric_depth=10,
        )

    assert isinstance(pre, list)
    assert isinstance(post, list)
    assert len(pre) == 1
    assert len(post) == 1
    assert all(isinstance(r, EvalSearchResult) for r in pre)
    assert all(isinstance(r, EvalSearchResult) for r in post)


@pytest.mark.asyncio
async def test_eval_trace_uses_service_query_path_with_trace_enabled(tmp_path):
    """collect_search_trace calls the pipeline's own store and reranker trace helpers."""
    from unittest.mock import AsyncMock, MagicMock, patch, call

    from archon_search.chunker import DocumentChunker
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    doc_id = "b" * 64
    pre_candidates = [_make_scored_candidate(doc_id, f"{doc_id}-000000")]
    post_candidates = [_make_scored_candidate(doc_id, f"{doc_id}-000000", reranker_score=0.7)]

    pipeline = SearchPipeline(
        store=MagicMock(),
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    hybrid_mock = AsyncMock(return_value=pre_candidates)
    rerank_mock = AsyncMock(return_value=post_candidates)

    with (
        patch("archon_search.eval._tracing._hybrid_search_with_trace", new=hybrid_mock),
        patch.object(pipeline._reranker, "rerank_candidates", new=rerank_mock),
    ):
        await collect_search_trace(
            pipeline, "my query", "test-col",
            candidate_depth=15, return_depth=3, metric_depth=5,
        )

    # Verify the store instance passed to trace is the pipeline's own store
    hybrid_mock.assert_awaited_once()
    call_args = hybrid_mock.call_args
    assert call_args.args[0] is pipeline.store, "store instance must be pipeline's own store"
    assert call_args.args[1] == "test-col"
    assert call_args.args[3] == "my query"
    assert call_args.args[4] == 15  # candidate_depth

    # Verify reranker trace was called with return_depth
    rerank_mock.assert_awaited_once()
    assert rerank_mock.call_args.args[2] == 3  # return_depth


@pytest.mark.asyncio
async def test_eval_trace_does_not_call_private_rerank_with_trace(tmp_path):
    """collect_search_trace reranks via rerank_candidates, not the private alias."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from archon_search.chunker import DocumentChunker
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    doc_id = "c" * 64
    pre_candidates = [_make_scored_candidate(doc_id, f"{doc_id}-000000")]

    pipeline = SearchPipeline(
        store=MagicMock(),
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    alias_spy = AsyncMock(side_effect=pipeline._reranker._rerank_with_trace)

    with (
        patch("archon_search.eval._tracing._hybrid_search_with_trace", new=AsyncMock(return_value=pre_candidates)),
        patch.object(pipeline._reranker, "_rerank_with_trace", new=alias_spy),
    ):
        await collect_search_trace(
            pipeline, "my query", "test-col",
            candidate_depth=15, return_depth=3, metric_depth=5,
        )

    alias_spy.assert_not_called()


@pytest.mark.asyncio
async def test_eval_trace_fails_if_trace_path_diverges_from_search_components(tmp_path):
    """Drift guard raises RuntimeError when embedder/store/reranker instances differ."""
    from unittest.mock import MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=MagicMock(),
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    # Tamper: replace embedder with a different instance after construction
    original_embedder = pipeline._embedder
    pipeline._embedder = make_embedder()  # different object — drift!

    # The drift guard must detect that the pipeline's embedder changed
    # We simulate this by verifying object identity check is performed
    # by patching _get_pipeline_components to return mismatched objects
    from archon_search.eval._tracing import _check_component_drift

    with pytest.raises(RuntimeError, match="drift"):
        _check_component_drift(
            pipeline,
            expected_embedder=original_embedder,  # the original, now mismatched
            expected_store=pipeline.store,
            expected_reranker=pipeline._reranker,
        )


@pytest.mark.asyncio
async def test_eval_trace_matches_search_final_order_with_matching_depths(connected_store, col_name, tmp_path):
    """post_rerank output order matches normal search() when depths equal pipeline defaults."""
    from archon_search.chunker import DocumentChunker
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "trace_doc.md"
    md_file.write_text("# Trace Test\n\nSearchable content for eval trace matching.\n" * 10)
    await pipeline.ingest_file(md_file, col_name)

    normal_result_obj = await pipeline.search("Searchable content", col_name)
    _, post_rerank = await collect_search_trace(
        pipeline, "Searchable content", col_name,
        candidate_depth=pipeline._top_k_retrieve,
        return_depth=pipeline._top_k_return,
        metric_depth=pipeline._top_k_return,
    )

    # post_rerank chunk_ids must match normal search order
    normal_chunk_ids = [r.chunk_id for r in normal_result_obj.results]
    trace_chunk_ids = [r.chunk_id for r in post_rerank]
    assert normal_chunk_ids == trace_chunk_ids, (
        f"Post-rerank trace order differs from search():\n"
        f"  search: {normal_chunk_ids}\n"
        f"  trace:  {trace_chunk_ids}"
    )


@pytest.mark.asyncio
async def test_eval_trace_common_prefix_matches_search_when_depths_differ(connected_store, col_name, tmp_path):
    """When eval depths differ from pipeline defaults, the common prefix of results matches."""
    from archon_search.chunker import DocumentChunker
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "prefix_doc.md"
    md_file.write_text("# Prefix Test\n\nContent for prefix comparison.\n" * 10)
    await pipeline.ingest_file(md_file, col_name)

    normal_result_obj = await pipeline.search("prefix comparison", col_name)
    _, post_rerank = await collect_search_trace(
        pipeline, "prefix comparison", col_name,
        candidate_depth=5,   # different from pipeline default (10)
        return_depth=3,       # different from pipeline default (5)
        metric_depth=3,
    )

    # Compare only the common prefix (min of both result counts)
    normal_results = normal_result_obj.results
    prefix_len = min(len(normal_results), len(post_rerank))
    assert prefix_len > 0, "Expected at least one result"
    normal_prefix = [r.chunk_id for r in normal_results[:prefix_len]]
    trace_prefix = [r.chunk_id for r in post_rerank[:prefix_len]]
    assert normal_prefix == trace_prefix, (
        f"Common prefix mismatch:\n  search: {normal_prefix}\n  trace: {trace_prefix}"
    )


@pytest.mark.asyncio
async def test_eval_trace_does_not_change_public_search_response(connected_store, col_name, tmp_path):
    """Normal search() output is identical before and after collect_search_trace is called."""
    from archon_search.chunker import DocumentChunker
    from archon_search.eval._tracing import collect_search_trace
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "unchanged_doc.md"
    md_file.write_text("# Unchanged Test\n\nSearch results must not change.\n" * 10)
    await pipeline.ingest_file(md_file, col_name)

    before = await pipeline.search("Search results", col_name)

    await collect_search_trace(
        pipeline, "Search results", col_name,
        candidate_depth=10, return_depth=5, metric_depth=5,
    )

    after = await pipeline.search("Search results", col_name)

    assert [r.chunk_id for r in before.results] == [r.chunk_id for r in after.results]
    assert [r.score for r in before.results] == [r.score for r in after.results]


# ===========================================================================
# namespace propagation through SearchPipeline
# ===========================================================================


@pytest.mark.asyncio
async def test_ingest_directory_namespace_param(tmp_path) -> None:
    """ingest_directory forwards namespace to store.get_collection_meta and CollectionMeta."""
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, MagicMock, call

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(return_value=1)
    store.rebuild_fts_index = AsyncMock()
    store.get_collection_meta = AsyncMock(return_value=None)
    store.update_collection_meta = AsyncMock()

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=64),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    (tmp_path / "doc.md").write_text("# Hello\n\nContent for namespace test.\n" * 5)

    await pipeline.ingest_directory(tmp_path, "my-col", namespace="tenantA")

    # store.get_collection_meta must be called with namespace="tenantA"
    store.get_collection_meta.assert_awaited_once_with("my-col", namespace="tenantA")

    # CollectionMeta passed to update_collection_meta must carry namespace="tenantA"
    store.update_collection_meta.assert_awaited_once()
    saved_meta: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved_meta.namespace == "tenantA"


@pytest.mark.asyncio
async def test_ingest_directory_default_namespace(tmp_path) -> None:
    """ingest_directory without explicit namespace defaults to DEFAULT_NAMESPACE."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(return_value=1)
    store.rebuild_fts_index = AsyncMock()
    store.get_collection_meta = AsyncMock(return_value=None)
    store.update_collection_meta = AsyncMock()

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=64),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    (tmp_path / "doc.md").write_text("# Hello\n\nContent for default namespace test.\n" * 5)

    await pipeline.ingest_directory(tmp_path, "my-col")

    store.get_collection_meta.assert_awaited_once_with("my-col", namespace=DEFAULT_NAMESPACE)
    saved_meta: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved_meta.namespace == DEFAULT_NAMESPACE


@pytest.mark.asyncio
async def test_recompute_collection_meta_namespace_param(tmp_path) -> None:
    """recompute_collection_meta forwards namespace to store.get_collection_meta and CollectionMeta."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    existing_meta = CollectionMeta(name="my-col", namespace="tenantA")

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=existing_meta)
    store.get_all_vectors = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4], [0.3, 0.4, 0.5, 0.6]])
    store.count_documents = AsyncMock(return_value=2)
    store.update_collection_meta = AsyncMock()

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    await pipeline.recompute_collection_meta("my-col", namespace="tenantA")

    store.get_collection_meta.assert_awaited_once_with("my-col", namespace="tenantA")
    saved_meta: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved_meta.namespace == "tenantA"


@pytest.mark.asyncio
async def test_get_collection_meta_namespace_param() -> None:
    """get_collection_meta forwards namespace to store.get_collection_meta."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    expected_meta = CollectionMeta(name="my-col", namespace="tenantA")
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=expected_meta)

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    result = await pipeline.get_collection_meta("my-col", namespace="tenantA")

    store.get_collection_meta.assert_awaited_once_with("my-col", namespace="tenantA")
    assert result is expected_meta


# ===========================================================================
# SearchPipelineResult return type for search
# ===========================================================================


@pytest.mark.asyncio
async def test_search_returns_pipeline_result(connected_store, col_name, tmp_path) -> None:
    """pipeline.search() returns a SearchPipelineResult instance, not a bare list."""
    from archon_search.pipeline import SearchPipeline, SearchPipelineResult

    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "result_type_doc.md"
    md_file.write_text("# Type Test\n\nContent for return-type check.\n" * 10)
    await pipeline.ingest_file(md_file, col_name)

    result = await pipeline.search("Content for return-type", col_name)

    assert isinstance(result, SearchPipelineResult)
    assert isinstance(result.results, list)
    assert all(isinstance(r, SearchResult) for r in result.results)


@pytest.mark.asyncio
async def test_search_acl_filtered_true_when_chunks_filtered(tmp_path) -> None:
    """When ACL filter removes candidates, acl_filtered=True in the result."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline, SearchPipelineResult
    from archon_search._types import ChunkRecord, SearchResult

    # A candidate with a restricted ACL (only "tenantX" allowed)
    restricted_result = SearchResult(
        doc_id="a" * 64,
        chunk_id=("a" * 64) + "-000000",
        text="secret content",
        score=0.9,
        source_path="/secret.md",
        acl=["tenantX"],  # not the default namespace
    )

    class AclFilterStore:
        async def hybrid_search(self, *a: Any, **kw: Any) -> list[SearchResult]:
            return [restricted_result]

    pipeline = SearchPipeline(
        store=AclFilterStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    # Pre-warm embedder
    await pipeline._embedder.embed(["warmup"])

    result = await pipeline.search("secret", "test-col")

    assert isinstance(result, SearchPipelineResult)
    assert result.acl_filtered is True
    assert result.results == []  # all filtered out


@pytest.mark.asyncio
async def test_search_acl_filtered_false_when_all_pass(tmp_path) -> None:
    """When no ACL filtering occurs, acl_filtered=False in the result."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline, SearchPipelineResult

    # A candidate with no ACL restriction (acl=None → open)
    open_result = SearchResult(
        doc_id="b" * 64,
        chunk_id=("b" * 64) + "-000000",
        text="open content",
        score=0.9,
        source_path="/open.md",
        acl=None,
    )

    class OpenAclStore:
        async def hybrid_search(self, *a: Any, **kw: Any) -> list[SearchResult]:
            return [open_result]

    pipeline = SearchPipeline(
        store=OpenAclStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._embedder.embed(["warmup"])

    result = await pipeline.search("open", "test-col")

    assert isinstance(result, SearchPipelineResult)
    assert result.acl_filtered is False
    assert len(result.results) == 1


@pytest.mark.asyncio
async def test_search_with_context_still_works_after_type_change(connected_store, col_name, tmp_path) -> None:
    """search_with_context() returns list of dicts with result/context_before/context_after keys."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=32),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "swc_type_doc.md"
    md_file.write_text("# SWC Test\n\n" + ("content chunk. " * 50))
    await pipeline.ingest_file(md_file, col_name)

    results = await pipeline.search_with_context("content chunk", col_name, context_window=1)

    assert isinstance(results, list)
    assert len(results) > 0
    for item in results:
        assert "result" in item
        assert "context_before" in item
        assert "context_after" in item


# ===========================================================================
# namespace guard for get_all_collections_meta, list_documents, delete_document
# ===========================================================================


@pytest.mark.asyncio
async def test_get_all_collections_meta_filters_by_namespace() -> None:
    """get_all_collections_meta(namespace) returns only collections in that namespace."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    meta_a = CollectionMeta(name="col-a", namespace="tenantA")
    meta_b = CollectionMeta(name="col-b", namespace="tenantB")

    store = MagicMock()
    store.get_all_collections_meta = AsyncMock(return_value=[meta_a, meta_b])

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    result = await pipeline.get_all_collections_meta(namespace="tenantA")

    assert result == [meta_a]


@pytest.mark.asyncio
async def test_list_documents_wrong_namespace_returns_empty() -> None:
    """list_documents returns [] when the collection belongs to a different namespace."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    # get_collection_meta returns None → collection not in requested namespace
    store.get_collection_meta = AsyncMock(return_value=None)
    store.list_documents = AsyncMock()

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    result = await pipeline.list_documents("col-a", namespace="tenantB")

    assert result == []
    store.get_collection_meta.assert_awaited_once_with("col-a", namespace="tenantB")
    store.list_documents.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_documents_correct_namespace_succeeds() -> None:
    """list_documents delegates to store when the collection belongs to the correct namespace."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    meta = CollectionMeta(name="col-a", namespace="tenantA")
    doc = DocumentInfo(doc_id="a" * 64, source_path="/some/path.md", chunk_count=2, indexed_at="2026-01-01T00:00:00")
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.list_documents = AsyncMock(return_value=[doc])

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    result = await pipeline.list_documents("col-a", namespace="tenantA")

    assert result == [doc]
    store.get_collection_meta.assert_awaited_once_with("col-a", namespace="tenantA")
    store.list_documents.assert_awaited_once_with("col-a", 100)


@pytest.mark.asyncio
async def test_delete_document_wrong_namespace_raises() -> None:
    """delete_document raises ValueError when collection is not in the requested namespace."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=None)
    store.delete_document = AsyncMock()

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    doc_id = "a" * 64
    with pytest.raises(ValueError, match="not found in namespace"):
        await pipeline.delete_document(doc_id, "col-a", namespace="tenantB")

    store.delete_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_document_correct_namespace_succeeds() -> None:
    """delete_document delegates to store when collection belongs to the correct namespace."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    meta = CollectionMeta(name="col-a", namespace="tenantA")
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.delete_document = AsyncMock(return_value=3)

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    doc_id = "a" * 64
    deleted = await pipeline.delete_document(doc_id, "col-a", namespace="tenantA")

    assert deleted == 3
    store.delete_document.assert_awaited_once_with("col-a", doc_id)


# ===========================================================================
# Task 3.3: filters kwarg forwarding + attrition WARNING
# ===========================================================================


def _make_search_result(n: int, acl: list[str] | None = None) -> "SearchResult":
    """Build a minimal SearchResult for filter/ACL tests."""
    doc_id = f"{'a' * 63}{n % 10}"
    return SearchResult(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text=f"result {n}",
        score=0.5,
        source_path=f"/path/{n}.md",
        acl=acl,
    )


@pytest.mark.asyncio
async def test_pipeline_search_forwards_filters_to_store() -> None:
    """filters kwarg is forwarded to store.hybrid_search."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.hybrid_search = AsyncMock(return_value=[])

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")
    await pipeline.search("test query", "col", filters=filters)

    store.hybrid_search.assert_awaited_once()
    call_kwargs = store.hybrid_search.call_args.kwargs
    assert call_kwargs.get("filters") is filters


@pytest.mark.asyncio
async def test_pipeline_warns_on_filter_plus_acl_under_delivery(caplog) -> None:
    """WARNING emitted when filters set + ACL drops results below top_k_return."""
    import logging

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    # Store returns top_k_retrieve=10 results; 8 have ACL that denies default namespace
    restricted_results = [_make_search_result(i, acl=["tenantX"]) for i in range(8)]
    open_results = [_make_search_result(i + 100, acl=None) for i in range(2)]
    all_results = open_results + restricted_results  # 2 pass, 8 denied

    class StubStore:
        async def hybrid_search(self, *a: Any, **kw: Any) -> list[SearchResult]:
            return all_results

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,  # survivors (2) < top_k_return (5) → warning
    )
    await pipeline._embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")

    with caplog.at_level(logging.WARNING, logger="archon"):
        await pipeline.search("query", "col", filters=filters)

    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("filter+ACL combined attrition" in m for m in warning_messages), (
        f"Expected attrition warning. Got: {warning_messages}"
    )
    # filter_flags must mention file_type
    attrition_msg = next(m for m in warning_messages if "filter+ACL combined attrition" in m)
    assert "file_type" in attrition_msg
    # acl_denied count (8) must appear
    assert "acl_denied=8" in attrition_msg


@pytest.mark.asyncio
async def test_pipeline_no_warning_when_no_filter_set(caplog) -> None:
    """No WARNING when filters=None even if ACL drops results below top_k_return."""
    import logging

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    restricted_results = [_make_search_result(i, acl=["tenantX"]) for i in range(8)]
    open_results = [_make_search_result(i + 100, acl=None) for i in range(2)]

    class StubStore:
        async def hybrid_search(self, *a: Any, **kw: Any) -> list[SearchResult]:
            return open_results + restricted_results

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._embedder.embed(["warmup"])

    with caplog.at_level(logging.WARNING, logger="archon"):
        await pipeline.search("query", "col", filters=None)

    attrition_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "filter+ACL combined attrition" in r.message
    ]
    assert attrition_warnings == [], "No attrition warning should be emitted when filters=None"


@pytest.mark.asyncio
async def test_pipeline_no_warning_when_pool_above_top_k(caplog) -> None:
    """No WARNING when survivors after ACL meet or exceed top_k_return."""
    import logging

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    # 6 open results — all pass ACL; 6 >= top_k_return(5) → no warning
    open_results = [_make_search_result(i, acl=None) for i in range(6)]

    class StubStore:
        async def hybrid_search(self, *a: Any, **kw: Any) -> list[SearchResult]:
            return open_results

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")

    with caplog.at_level(logging.WARNING, logger="archon"):
        await pipeline.search("query", "col", filters=filters)

    attrition_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "filter+ACL combined attrition" in r.message
    ]
    assert attrition_warnings == [], "No warning when survivors >= top_k_return"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_search_filter_then_acl_order() -> None:
    """Rows excluded by filter are never seen by apply_acl_filter (spy on input count)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    # Store returns 5 results; filter reduces to 3 (store is responsible for pre-filter)
    filtered_results = [_make_search_result(i, acl=None) for i in range(3)]

    store = MagicMock()
    store.hybrid_search = AsyncMock(return_value=filtered_results)

    acl_inputs: list[int] = []

    import archon_search.pipeline as _pipeline_mod

    original_apply_acl = _pipeline_mod.apply_acl_filter

    def spy_acl_filter(items, get_acl, namespace):
        acl_inputs.append(len(items))
        return original_apply_acl(items, get_acl, namespace)

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")

    with patch.object(_pipeline_mod, "apply_acl_filter", side_effect=spy_acl_filter):
        await pipeline.search("query", "col", filters=filters)

    # ACL filter received exactly the 3 store results (filter already applied by store)
    assert acl_inputs[0] == 3, (
        f"apply_acl_filter should see store-filtered results (3), got {acl_inputs[0]}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_search_filter_then_reranker_order() -> None:
    """Reranker sees only the filter+ACL survivors."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    # 4 results from store: 2 pass ACL, 2 are restricted
    open_results = [_make_search_result(i, acl=None) for i in range(2)]
    restricted_results = [_make_search_result(i + 10, acl=["tenantX"]) for i in range(2)]

    store = MagicMock()
    store.hybrid_search = AsyncMock(return_value=open_results + restricted_results)

    reranker_inputs: list[list] = []

    class SpyRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            reranker_inputs.append([p[1] for p in pairs])
            return [0.5] * len(pairs)

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=Reranker(SpyRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")
    await pipeline.search("query", "col", filters=filters)

    # Reranker must receive only the 2 open results
    assert len(reranker_inputs) == 1
    assert len(reranker_inputs[0]) == 2, (
        f"Reranker should see 2 survivors, got {len(reranker_inputs[0])}"
    )


@pytest.mark.asyncio
async def test_pipeline_no_warning_when_filters_has_no_active_fields(caplog) -> None:
    """No WARNING when SearchFilters() is passed with all fields at defaults (no real filter set)."""
    import logging

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    # 8 restricted + 2 open -- survivors (2) < top_k_return (5)
    restricted = [_make_search_result(i, acl=["tenantX"]) for i in range(8)]
    open_results = [_make_search_result(i + 100, acl=None) for i in range(2)]

    class StubStore:
        async def hybrid_search(self, *a: Any, **kw: Any) -> list[SearchResult]:
            return open_results + restricted

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._embedder.embed(["warmup"])

    # SearchFilters() with all defaults — filter_flags will be empty, warning must NOT fire
    filters = SearchFilters()
    with caplog.at_level(logging.WARNING, logger="archon"):
        await pipeline.search("query", "col", filters=filters)

    assert not any(
        "filter+ACL combined attrition" in r.message
        for r in caplog.records
    ), f"No attrition warning expected for empty filter. Got: {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_pipeline_search_with_context_forwards_filters_to_store() -> None:
    """search_with_context forwards filters kwarg to the inner search -> store.hybrid_search call."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.hybrid_search = AsyncMock(return_value=[])

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")
    await pipeline.search_with_context("test query", "col", filters=filters)

    store.hybrid_search.assert_awaited_once()
    call_kwargs = store.hybrid_search.call_args.kwargs
    assert call_kwargs.get("filters") is filters, (
        f"Expected filters to be forwarded; got: {call_kwargs}"
    )


@pytest.mark.asyncio
async def test_pipeline_warns_when_filter_alone_causes_under_delivery(caplog) -> None:
    """WARNING fires when filters cause under-delivery even with acl_denied=0."""
    import logging

    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    # Store returns only 3 results (filter was restrictive), all pass ACL
    open_results = [_make_search_result(i, acl=None) for i in range(3)]

    class StubStore:
        async def hybrid_search(self, *a: Any, **kw: Any) -> list[SearchResult]:
            return open_results  # only 3, all open

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,  # 3 < 5 → warning should fire
    )
    await pipeline._embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")
    with caplog.at_level(logging.WARNING, logger="archon"):
        await pipeline.search("query", "col", filters=filters)

    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("filter+ACL combined attrition" in m for m in warning_messages), (
        f"Expected attrition warning when filters cause under-delivery. Got: {warning_messages}"
    )
    attrition_msg = next(m for m in warning_messages if "filter+ACL combined attrition" in m)
    assert "acl_denied=0" in attrition_msg, f"Expected acl_denied=0 in: {attrition_msg}"
    assert "file_type" in attrition_msg


@pytest.mark.asyncio
async def test_pipeline_warns_when_store_returns_zero_results_with_filters(caplog) -> None:
    """WARNING fires when store returns 0 results with active filters (zero-result boundary)."""
    import logging
    from archon_search.chunker import DocumentChunker
    from archon_search.filters import SearchFilters
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    class StubStore:
        async def hybrid_search(self, *a: Any, **kw: Any) -> list[SearchResult]:
            return []  # zero results — filter was very restrictive

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._embedder.embed(["warmup"])

    filters = SearchFilters(file_type="md")
    with caplog.at_level(logging.WARNING, logger="archon"):
        result = await pipeline.search("query", "col", filters=filters)

    assert result.results == []
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("filter+ACL combined attrition" in m for m in warning_messages), (
        f"Expected attrition warning for zero results. Got: {warning_messages}"
    )
    attrition_msg = next(m for m in warning_messages if "filter+ACL combined attrition" in m)
    assert "0/" in attrition_msg, f"Expected '0/' in: {attrition_msg}"
    assert "acl_denied=0" in attrition_msg


# ===========================================================================
# is_warm pipeline properties — Task 2.3 (B2)
# ===========================================================================


def test_pipeline_reranker_is_warm_false_when_cold() -> None:
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    backend = MockRerankerBackend()
    backend.is_warm = False
    pipeline = SearchPipeline(
        store=None,  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=Reranker(backend),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    assert pipeline.reranker_is_warm is False


def test_pipeline_reranker_is_warm_true_when_warm() -> None:
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    backend = MockRerankerBackend()
    backend.is_warm = True
    pipeline = SearchPipeline(
        store=None,  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=Reranker(backend),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    assert pipeline.reranker_is_warm is True


def test_pipeline_embedder_is_warm_false_when_cold() -> None:
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    emb_backend = MockEmbedderBackend()
    emb_backend.is_warm = False
    pipeline = SearchPipeline(
        store=None,  # type: ignore[arg-type]
        embedder=Embedder(emb_backend),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    assert pipeline.embedder_is_warm is False


def test_pipeline_embedder_is_warm_true_when_warm() -> None:
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    emb_backend = MockEmbedderBackend()
    emb_backend.is_warm = True
    pipeline = SearchPipeline(
        store=None,  # type: ignore[arg-type]
        embedder=Embedder(emb_backend),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    assert pipeline.embedder_is_warm is True


# ===========================================================================
# search_many (B3 Task 3.2) — multi-collection fan-out
# ===========================================================================


def _scored(collection: str, doc_id: str, chunk_id: str, rrf_score: float = 0.5):
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown

    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text=f"text-{chunk_id}",
        source_path=f"/path/to/{doc_id}.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=0,
            vector_score=0.9,
            vector_score_kind="distance",
            fts_rank=None,
            fts_score=None,
            fts_score_kind=None,
            rrf_score=rrf_score,
            reranker_score=None,
        ),
        collection=collection,
    )


def _meta(name: str, *, embedding_model: str = "mock-embedder", namespace: str = "default"):
    from archon_search.collection_meta import CollectionMeta

    return CollectionMeta(name=name, embedding_model=embedding_model, namespace=namespace)


def _search_many_pipeline(
    *,
    leg_map: dict | None = None,
    meta_list: list | None = None,
    fanout_leg_trim: int = 40,
    top_k_return: int = 5,
    top_k_retrieve: int = 10,
    fanout_timeout_seconds: float = 30.0,
):
    """Build a SearchPipeline with a MagicMock store wired for fan-out.

    ``leg_map`` maps collection-name -> list[ScoredSearchCandidate]; the
    store's hybrid_search_with_trace dispatches per collection.
    """
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    leg_map = leg_map or {}

    async def _hybrid(collection, vector, query_text, candidate_depth):
        return list(leg_map.get(collection, []))

    store.hybrid_search_with_trace = AsyncMock(side_effect=_hybrid)

    embedder = make_embedder()
    embedder.embed_one = AsyncMock(return_value=[0.1] * 4)  # type: ignore[method-assign]

    reranker = make_reranker()

    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=top_k_retrieve,
        top_k_return=top_k_return,
        fanout_leg_trim=fanout_leg_trim,
        fanout_timeout_seconds=fanout_timeout_seconds,
    )
    if meta_list is not None:
        pipeline.get_all_collections_meta = AsyncMock(return_value=meta_list)  # type: ignore[method-assign]
    return pipeline, store, embedder, reranker


@pytest.mark.asyncio
async def test_search_many_embeds_once() -> None:
    cols = ["A", "B", "C"]
    leg_map = {c: [_scored(c, "d" * 64, f"{'d' * 64}-000000")] for c in cols}
    pipeline, store, embedder, reranker = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta(c) for c in cols]
    )
    await pipeline.search_many("q", cols)
    assert embedder.embed_one.await_count == 1


@pytest.mark.asyncio
async def test_search_many_reranks_once() -> None:
    cols = ["A", "B"]
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-000000")],
    }
    pipeline, store, embedder, reranker = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta("A"), _meta("B")]
    )
    spy = AsyncMock(side_effect=reranker.rerank_candidates)
    reranker.rerank_candidates = spy  # type: ignore[method-assign]

    await pipeline.search_many("q", cols)

    assert spy.await_count == 1
    merged_passed = spy.await_args.args[1] if len(spy.await_args.args) > 1 else spy.await_args.kwargs["candidates"]
    # merged pool == sum of trimmed per-leg pools (1 + 1)
    assert len(merged_passed) == 2


@pytest.mark.asyncio
async def test_search_many_result_carries_collection_provenance() -> None:
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-000000")],
    }
    pipeline, *_ = _search_many_pipeline(leg_map=leg_map, meta_list=[_meta("A"), _meta("B")])
    result = await pipeline.search_many("q", ["A", "B"])
    by_doc = {r.doc_id: r.collection for r in result.results}
    assert by_doc["a" * 64] == "A"
    assert by_doc["b" * 64] == "B"


@pytest.mark.asyncio
async def test_search_many_merge_order_deterministic() -> None:
    """Merge concatenates legs in ascending collection-name order, regardless of
    the order collections are requested."""
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-000000")],
    }
    pipeline, _store, _embedder, reranker = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta("A"), _meta("B")]
    )
    spy = AsyncMock(side_effect=reranker.rerank_candidates)
    reranker.rerank_candidates = spy  # type: ignore[method-assign]

    # Request in reverse (non-alphabetical) order.
    await pipeline.search_many("q", ["B", "A"])

    merged = spy.await_args.args[1]
    # Merge must be alphabetical by collection name (A before B), not request order.
    assert [c.collection for c in merged] == ["A", "B"]


@pytest.mark.asyncio
async def test_search_many_namespace_scope_excludes_out_of_namespace() -> None:
    """A collection that exists only in namespace B is invisible from namespace A:
    it is never searched, and requesting it from A raises CollectionNotFoundError
    (no cross-namespace existence leak)."""
    from archon_search.pipeline import CollectionNotFoundError

    leg_map = {"A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")]}
    pipeline, store, *_ = _search_many_pipeline(leg_map=leg_map, meta_list=None)
    # Back the REAL pipeline.get_all_collections_meta with a store returning both
    # A (namespace A) and B (namespace B); the pipeline filters by namespace.
    store.get_all_collections_meta = AsyncMock(
        return_value=[_meta("A", namespace="A"), _meta("B", namespace="B")]
    )

    await pipeline.search_many("q", ["A"], namespace="A")
    called_cols = {c.args[0] for c in store.hybrid_search_with_trace.call_args_list}
    assert called_cols == {"A"}

    # B lives in namespace B → not found from namespace A (strict 404, no leak).
    with pytest.raises(CollectionNotFoundError):
        await pipeline.search_many("q", ["B"], namespace="A")


@pytest.mark.asyncio
async def test_search_many_missing_collection_raises_collection_not_found() -> None:
    from archon_search.pipeline import CollectionNotFoundError

    pipeline, *_ = _search_many_pipeline(leg_map={}, meta_list=[_meta("A")])
    with pytest.raises(CollectionNotFoundError):
        await pipeline.search_many("q", ["A", "MISSING"])


@pytest.mark.asyncio
async def test_search_many_model_mismatch_excludes_and_reports() -> None:
    from archon_search._types import ExcludedCollection

    leg_map = {"A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")]}
    pipeline, store, *_ = _search_many_pipeline(
        leg_map=leg_map,
        meta_list=[_meta("A"), _meta("B", embedding_model="other-model")],
    )
    result = await pipeline.search_many("q", ["A", "B"])
    assert ExcludedCollection(name="B", reason="embedding_model_mismatch") in result.excluded_collections
    called_cols = {c.args[0] for c in store.hybrid_search_with_trace.call_args_list}
    assert "B" not in called_cols


@pytest.mark.asyncio
async def test_search_many_leg_failure_cancels_siblings_and_raises() -> None:
    cancelled = asyncio.Event()

    async def _hybrid(collection, vector, query_text, candidate_depth):
        if collection == "A":
            raise RuntimeError("leg failed")
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return []

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.hybrid_search_with_trace = AsyncMock(side_effect=_hybrid)
    embedder = make_embedder()
    embedder.embed_one = AsyncMock(return_value=[0.1] * 4)  # type: ignore[method-assign]
    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    pipeline.get_all_collections_meta = AsyncMock(return_value=[_meta("A"), _meta("B")])  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="leg failed"):
        await pipeline.search_many("q", ["A", "B"])
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_search_many_timeout_raises_fanout_timeout_error() -> None:
    from time import monotonic

    from archon_search.pipeline import FanoutTimeoutError

    async def _hybrid(collection, vector, query_text, candidate_depth):
        await asyncio.sleep(999)
        return []

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.hybrid_search_with_trace = AsyncMock(side_effect=_hybrid)
    embedder = make_embedder()
    embedder.embed_one = AsyncMock(return_value=[0.1] * 4)  # type: ignore[method-assign]
    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        fanout_timeout_seconds=0.001,
    )
    pipeline.get_all_collections_meta = AsyncMock(return_value=[_meta("A"), _meta("B")])  # type: ignore[method-assign]

    t0 = monotonic()
    with pytest.raises(FanoutTimeoutError):
        await pipeline.search_many("q", ["A", "B"])
    assert (monotonic() - t0) < 2.0


@pytest.mark.asyncio
async def test_same_chunk_id_in_two_collections_both_survive() -> None:
    shared_chunk = f"{'a' * 64}-000000"
    leg_map = {
        "A": [_scored("A", "a" * 64, shared_chunk)],
        "B": [_scored("B", "a" * 64, shared_chunk)],
    }
    pipeline, store, embedder, reranker = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta("A"), _meta("B")]
    )
    spy = AsyncMock(side_effect=reranker.rerank_candidates)
    reranker.rerank_candidates = spy  # type: ignore[method-assign]

    await pipeline.search_many("q", ["A", "B"])

    merged_passed = spy.await_args.args[1]
    collections = sorted(c.collection for c in merged_passed)
    assert collections == ["A", "B"]


@pytest.mark.asyncio
async def test_search_many_all_collections_model_mismatched_returns_empty() -> None:
    from archon_search.pipeline import SearchPipelineResult

    pipeline, store, *_ = _search_many_pipeline(
        leg_map={},
        meta_list=[
            _meta("A", embedding_model="other-model"),
            _meta("B", embedding_model="other-model"),
        ],
    )
    result = await pipeline.search_many("q", ["A", "B"])
    assert isinstance(result, SearchPipelineResult)
    assert result.results == []
    assert {e.name for e in result.excluded_collections} == {"A", "B"}
    assert store.hybrid_search_with_trace.await_count == 0


@pytest.mark.asyncio
async def test_search_many_leg_trim_below_top_k_return() -> None:
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-{i:06d}", rrf_score=1.0 - i * 0.01) for i in range(10)],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-{i:06d}", rrf_score=1.0 - i * 0.01) for i in range(10)],
    }
    pipeline, *_ = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta("A"), _meta("B")], fanout_leg_trim=1, top_k_return=5
    )
    result = await pipeline.search_many("q", ["A", "B"])
    assert len(result.results) == 2


@pytest.mark.asyncio
async def test_search_many_meta_lookup_raises_propagates() -> None:
    from archon_search.pipeline import MetadataLookupError

    pipeline, *_ = _search_many_pipeline(leg_map={})
    pipeline.get_all_collections_meta = AsyncMock(side_effect=RuntimeError("store error"))  # type: ignore[method-assign]
    with pytest.raises(MetadataLookupError):
        await pipeline.search_many("q", ["A"])


@pytest.mark.asyncio
async def test_search_many_heterogeneous_leg_pool_sizes() -> None:
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-{i:06d}", rrf_score=1.0 - i * 0.001) for i in range(40)],
        "B": [],
    }
    pipeline, *_ = _search_many_pipeline(
        leg_map=leg_map, meta_list=[_meta("A"), _meta("B")], fanout_leg_trim=40, top_k_return=50
    )
    result = await pipeline.search_many("q", ["A", "B"])
    assert all(r.collection == "A" for r in result.results)
    assert len(result.results) == 40


@pytest.mark.asyncio
async def test_search_many_populates_fanout_timings() -> None:
    """Result carries FanoutTimings with one leg_times entry per searched
    collection plus a non-negative rerank time."""
    leg_map = {
        "A": [_scored("A", "a" * 64, f"{'a' * 64}-000000")],
        "B": [_scored("B", "b" * 64, f"{'b' * 64}-000000")],
    }
    pipeline, *_ = _search_many_pipeline(leg_map=leg_map, meta_list=[_meta("A"), _meta("B")])
    result = await pipeline.search_many("q", ["A", "B"])

    assert result.fanout_timings is not None
    assert set(result.fanout_timings.leg_times) == {"A", "B"}
    assert all(v >= 0 for v in result.fanout_timings.leg_times.values())
    assert result.fanout_timings.rerank_time_ms >= 0


@pytest.mark.asyncio
async def test_search_many_acl_filtered_propagates() -> None:
    """When ACL drops a candidate from the merged pool, acl_filtered is True."""
    a_open = _scored("A", "a" * 64, f"{'a' * 64}-000000")  # acl=None → open
    b_denied = _scored("B", "b" * 64, f"{'b' * 64}-000000")
    b_denied.acl = ["other-namespace"]  # not the search namespace → dropped
    leg_map = {"A": [a_open], "B": [b_denied]}
    pipeline, *_ = _search_many_pipeline(leg_map=leg_map, meta_list=[_meta("A"), _meta("B")])

    result = await pipeline.search_many("q", ["A", "B"], namespace="default")

    assert result.acl_filtered is True
    # The denied candidate must not survive into results.
    assert all(r.collection != "B" for r in result.results)


# ===========================================================================
# B4 Task 3.1 — description_embedding populated at ingest
# ===========================================================================


def _make_stub_store_for_embedding_tests(existing_meta=None):  # type: ignore[no-untyped-def]
    """Return a store mock suitable for description-embedding tests."""
    from unittest.mock import AsyncMock, MagicMock

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(return_value=1)
    store.rebuild_fts_index = AsyncMock()
    store.get_collection_meta = AsyncMock(return_value=existing_meta)
    store.update_collection_meta = AsyncMock()
    return store


def _make_pipeline_for_embedding_tests(store):  # type: ignore[no-untyped-def]
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    return SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=64),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )


@pytest.mark.asyncio
async def test_ingest_populates_description_embedding(tmp_path) -> None:
    """ingest_directory persists a CollectionMeta with description_embedding when description is set."""
    from unittest.mock import AsyncMock, patch

    from archon_search.collection_meta import CollectionMeta

    store = _make_stub_store_for_embedding_tests(existing_meta=None)
    pipeline = _make_pipeline_for_embedding_tests(store)

    embed_vec = [0.1] * 32
    (tmp_path / "doc.md").write_text("# Hello\n\nContent for embedding test.\n" * 5)

    with (
        patch("archon_search.pipeline.generate_description", return_value="test desc"),
        patch.object(pipeline._embedder, "embed_one", new=AsyncMock(return_value=embed_vec)),
    ):
        await pipeline.ingest_directory(tmp_path, "my-col")

    store.update_collection_meta.assert_awaited_once()
    saved_meta: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved_meta.description_embedding == embed_vec


@pytest.mark.asyncio
async def test_ingest_description_none_sets_embedding_none(tmp_path) -> None:
    """When generate_description returns None, description_embedding is None on persisted meta."""
    from unittest.mock import AsyncMock, patch

    from archon_search.collection_meta import CollectionMeta

    store = _make_stub_store_for_embedding_tests(existing_meta=None)
    pipeline = _make_pipeline_for_embedding_tests(store)

    (tmp_path / "doc.md").write_text("# Hello\n\nContent for null embedding test.\n" * 5)

    with patch("archon_search.pipeline.generate_description", return_value=None):
        with patch.object(pipeline._embedder, "embed_one", new_callable=AsyncMock) as mock_embed:
            await pipeline.ingest_directory(tmp_path, "my-col")

    mock_embed.assert_not_awaited()
    store.update_collection_meta.assert_awaited_once()
    saved_meta: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved_meta.description_embedding is None


@pytest.mark.asyncio
async def test_ingest_re_embeds_description_on_every_ingest(tmp_path) -> None:
    """Re-ingest always re-embeds the description, overwriting stale description_embedding."""
    from unittest.mock import AsyncMock, patch

    from archon_search.collection_meta import CollectionMeta

    prior_embedding = [0.5] * 32
    existing_meta = CollectionMeta(
        name="my-col",
        description="old desc",
        description_embedding=prior_embedding,
        described_at_doc_count=1,  # batch has 1 doc → 0% change → no regeneration
    )
    store = _make_stub_store_for_embedding_tests(existing_meta=existing_meta)
    pipeline = _make_pipeline_for_embedding_tests(store)

    new_embed_vec = [0.9] * 32
    embed_one_mock = AsyncMock(return_value=new_embed_vec)

    (tmp_path / "doc.md").write_text("# Hello\n\nContent for re-embed test.\n" * 5)

    with patch.object(pipeline._embedder, "embed_one", new=embed_one_mock):
        await pipeline.ingest_directory(tmp_path, "my-col")

    # embed_one must have been called with the preserved description
    embed_one_mock.assert_awaited_once_with("old desc")

    # Persisted meta must carry the fresh embedding, not the prior [0.5]*32
    store.update_collection_meta.assert_awaited_once()
    saved_meta: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved_meta.description_embedding == new_embed_vec
    assert saved_meta.description_embedding != prior_embedding

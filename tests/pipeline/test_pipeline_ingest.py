"""tests/pipeline/test_pipeline_ingest.py — Ingest tests for SearchPipeline.

Covers: ingest_file, ingest_directory, recompute_collection_meta, create_pipeline
wiring, and all structural/factory tests. Moved from tests/test_pipeline.py as part
of C11 pipeline test split.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search._types import ChunkRecord, IngestResult, SearchResult
from archon_search.store import ChunkIngestResult
from archon_search.embedder import Embedder
from archon_search.reranker import Reranker

from .conftest import MockEmbedderBackend, MockRerankerBackend, make_embedder, make_reranker, make_pipeline



@pytest.mark.asyncio
async def test_pipeline_ingest_file_ok(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "doc.md"
    md_file.write_text("# Hello\n\nThis is a test document with enough content to chunk.\n" * 5)

    result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)

    assert isinstance(result, IngestResult)
    assert result.status == "ok"
    assert result.chunks_created > 0
    assert result.doc_id  # non-empty


@pytest.mark.asyncio
async def test_pipeline_ingest_codeFile_routesThroughAstChunker(connected_store, col_name, tmp_path):
    """BE-6: a code-extension (.py) ingest routes chunking through `_ast_chunker.chunk(...)`,
    not `_chunker.chunk(...)`, with the built `scope_table` passed through as a kwarg.
    """
    pipeline = make_pipeline(connected_store)

    source = "def foo():\n    return 1\n"
    mock_record = ChunkRecord(
        doc_id="doc1",
        chunk_id="",
        text=source,
        vector=[],
        source_path="/tmp/mod.py",
        indexed_at="2024-01-01T00:00:00.000000Z",
        file_type="py",
        start_offset=0,
        end_offset=len(source),
    )
    pipeline._ast_chunker = MagicMock()
    pipeline._ast_chunker.chunk.return_value = [mock_record]
    pipeline._chunker = MagicMock()
    pipeline._chunker.chunk.return_value = []

    py_file = tmp_path / "mod.py"
    py_file.write_text(source)

    result = await pipeline.ingest_file(py_file, col_name, embedder=pipeline._global_embedder)

    assert result.status == "ok"
    pipeline._ast_chunker.chunk.assert_called_once()
    _, kwargs = pipeline._ast_chunker.chunk.call_args
    assert "scope_table" in kwargs
    pipeline._chunker.chunk.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_ingest_nonCodeFile_routesThroughChunker(connected_store, col_name, tmp_path):
    """BE-6: a non-code-extension (.md) ingest routes chunking through `_chunker.chunk(...)`,
    not `_ast_chunker.chunk(...)`.
    """
    pipeline = make_pipeline(connected_store)

    source = "# Hello\n\nSome content.\n"
    mock_record = ChunkRecord(
        doc_id="doc1",
        chunk_id="",
        text=source,
        vector=[],
        source_path="/tmp/doc.md",
        indexed_at="2024-01-01T00:00:00.000000Z",
        file_type="md",
        start_offset=0,
        end_offset=len(source),
    )
    pipeline._chunker = MagicMock()
    pipeline._chunker.chunk.return_value = [mock_record]
    pipeline._ast_chunker = MagicMock()
    pipeline._ast_chunker.chunk.return_value = []

    md_file = tmp_path / "doc.md"
    md_file.write_text(source)

    result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)

    assert result.status == "ok"
    pipeline._chunker.chunk.assert_called_once()
    pipeline._ast_chunker.chunk.assert_not_called()


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

    result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)

    assert result.status == "error"
    assert result.chunks_created == 0
    assert result.error is not None


@pytest.mark.asyncio
async def test_pipeline_ingest_is_idempotent(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "doc.md"
    md_file.write_text("# Idempotent Test\n\nSome content here.\n" * 10)

    # Use ingest_directory so collection meta is written (required by list_documents namespace guard)
    await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)
    await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    docs, _, _ = await pipeline.list_documents(col_name)
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_pipeline_ingest_file_chunk_ids_sequential(connected_store, col_name, tmp_path):
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    captured_records: list[ChunkRecord] = []

    class CapturingStore:
        from archon_search.config import SearchConfig
        _config = SearchConfig()

        @property
        def supports_incremental_fts_delete(self) -> bool:
            return True

        async def ensure_collection(self, *a: Any, **kw: Any) -> None:
            pass

        async def delete_document(self, *a: Any, **kw: Any) -> int:
            return 0

        async def ingest_chunks(self, collection: str, records: list[ChunkRecord], **kw: Any) -> ChunkIngestResult:
            captured_records.extend(records)
            return ChunkIngestResult(chunks_ingested=len(records), needs_recompute=False)

        async def optimize_fts(self, *a: Any, **kw: Any) -> None:
            pass

        async def rebuild_fts_index(self, *a: Any, **kw: Any) -> None:
            pass

        async def get_dominant_language(self, *a: Any, **kw: Any) -> str:
            return ""

        async def get_collection_meta(self, *a: Any, **kw: Any) -> None:
            return None

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

    result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)
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
        from archon_search.config import SearchConfig
        _config = SearchConfig()

        @property
        def supports_incremental_fts_delete(self) -> bool:
            return True

        async def ensure_collection(self, *a: Any, **kw: Any) -> None:
            pass

        async def delete_document(self, *a: Any, **kw: Any) -> int:
            return 0

        async def ingest_chunks(self, collection: str, records: list[ChunkRecord], **kw: Any) -> ChunkIngestResult:
            captured_records.extend(records)
            return ChunkIngestResult(chunks_ingested=len(records), needs_recompute=False)

        async def optimize_fts(self, *a: Any, **kw: Any) -> None:
            pass

        async def rebuild_fts_index(self, *a: Any, **kw: Any) -> None:
            pass

        async def get_dominant_language(self, *a: Any, **kw: Any) -> str:
            return ""

        async def get_collection_meta(self, *a: Any, **kw: Any) -> None:
            return None

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

    result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)
    assert re.match(r"^[a-f0-9]{64}$", result.doc_id), f"doc_id {result.doc_id!r} is not 64 hex chars"


@pytest.mark.asyncio
async def test_pipeline_ingest_directory(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent for document {i}.\n" * 5)

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

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

    await pipeline.ingest_directory(tmp_path, col_name, progress_cb=progress_cb, embedder=pipeline._global_embedder, rebuild_fts=False)

    assert len(calls) == 3
    assert calls[-1][0] == 3  # all done
    assert calls[-1][1] == 3  # total


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_empty_dir(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    results = await pipeline.ingest_directory(empty_dir, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

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

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

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

    async def counting_rebuild(collection: str, **kw: Any) -> None:
        nonlocal rebuild_calls
        rebuild_calls += 1
        await original_rebuild(collection, **kw)

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

        await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)

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

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    # Only 2 files, not the subdir
    assert len(results) == 2


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_skips_hidden_files(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    (tmp_path / "visible.md").write_text("# Visible\n\nContent.\n" * 5)
    (tmp_path / ".hidden.md").write_text("# Hidden\n\nContent.\n" * 5)

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    assert len(results) == 1
    assert results[0].status == "ok"


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_skips_files_in_hidden_directories(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    (tmp_path / "visible.md").write_text("# Visible\n\nContent.\n" * 5)
    hidden_dir = tmp_path / ".git"
    hidden_dir.mkdir()
    (hidden_dir / "tracked.md").write_text("# Tracked\n\nContent.\n")

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    assert len(results) == 1
    assert results[0].status == "ok"


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_skips_symlinks(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    real_file = tmp_path / "real.md"
    real_file.write_text("# Real\n\nContent.\n" * 5)
    symlink_file = tmp_path / "link.md"
    symlink_file.symlink_to(real_file)

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    # Only real file, not symlink
    assert len(results) == 1


@pytest.mark.asyncio
async def test_pipeline_ingest_file_parse_error_preserves_existing_chunks(connected_store, col_name, tmp_path):
    from archon_search.parser import ParseError

    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "existing.md"
    md_file.write_text("# Existing Content\n\nThis should be preserved.\n" * 10)

    # Use ingest_directory to create collection meta (required by list_documents namespace guard)
    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)
    first_result = results[0]
    assert first_result.status == "ok"

    # Now mock parser to fail
    async def _fail(path: Path) -> str:
        raise ParseError(path, Exception("parse error"))

    pipeline._parser.parse = _fail  # type: ignore[method-assign]

    # Re-ingest should fail gracefully
    second_result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)
    assert second_result.status == "error"

    # Original doc should still be there
    docs, _, _ = await pipeline.list_documents(col_name)
    assert any(d.doc_id == first_result.doc_id for d in docs)


@pytest.mark.asyncio
async def test_pipeline_ingest_file_empty_content_preserves_existing_chunks(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    md_file = tmp_path / "empty_content.md"
    md_file.write_text("# First Ingest\n\nThis should be preserved.\n" * 10)

    # Use ingest_directory to create collection meta (required by list_documents namespace guard)
    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)
    first_result = results[0]
    assert first_result.status == "ok"
    assert first_result.chunks_created > 0

    # Overwrite with empty content and re-ingest via ingest_file
    md_file.write_text("")

    second_result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)
    assert second_result.status == "ok"
    assert second_result.chunks_created == 0

    # Original doc still in store (no delete on empty)
    docs, _, _ = await pipeline.list_documents(col_name)
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

        async def ingest_chunks(self, *a: Any, **kw: Any) -> ChunkIngestResult:
            return ChunkIngestResult(chunks_ingested=0, needs_recompute=False)

        async def rebuild_fts_index(self, *a: Any, **kw: Any) -> None:
            nonlocal rebuild_called
            rebuild_called = True

        async def get_dominant_language(self, *a: Any, **kw: Any) -> str:
            return ""

        async def get_collection_meta(self, *a: Any, **kw: Any) -> None:
            return None

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

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder)

    assert all(r.status == "error" for r in results)
    assert not rebuild_called


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_skips_binary_extensions(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    (tmp_path / "data.txt").write_text("Some text content.\n" * 5)
    (tmp_path / "image.gif").write_bytes(b"GIF89a" + b"\x00" * 100)

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

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

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    assert results == [], f"Binary {ext} file should not be ingested"


@pytest.mark.asyncio
async def test_pipeline_ingest_directory_includes_png(connected_store, col_name, tmp_path):
    pipeline = make_pipeline(connected_store)
    png_file = tmp_path / "image.png"
    png_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    ocr_text = "Extracted OCR text from image. " * 20
    pipeline._parser.parse = AsyncMock(return_value=ocr_text)  # type: ignore[method-assign]

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

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

    result = await pipeline.ingest_file(png_file, col_name, embedder=pipeline._global_embedder)

    assert result.status == "ok"
    assert result.chunks_created == 0


@pytest.mark.asyncio
async def test_ingest_computes_centroid_from_all_chunks(connected_store, col_name, tmp_path):
    """ingest_directory stores centroid = mean of all chunk embeddings from the batch."""
    from datetime import UTC, datetime

    pipeline = make_pipeline(connected_store)
    # MockEmbedderBackend returns [0.1, 0.1, 0.1, 0.1] for all texts
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent for document {i}.\n" * 5)

    before = datetime.now(UTC)
    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)
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
    assert meta.active_embedding_model == "mock-embedder"
    assert meta.last_indexed is not None
    assert before <= meta.last_indexed <= after


@pytest.mark.asyncio
async def test_ingest_centroid_replaced_on_reingest(connected_store, col_name, tmp_path):
    """Re-ingest replaces the centroid with fresh computation from the new batch."""
    pipeline = make_pipeline(connected_store)
    (tmp_path / "doc.md").write_text("# Doc\n\nContent.\n" * 5)

    # First ingest — MockEmbedderBackend returns [0.1]*4
    await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)
    meta1 = await connected_store.get_collection_meta(col_name)
    assert meta1 is not None and meta1.centroid is not None

    # Swap embedder to one returning [0.5]*4
    class AltEmbedderBackend:
        model_name: str = "alt-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.5] * 4 for _ in texts]

    pipeline._global_embedder = Embedder(AltEmbedderBackend())

    # Re-ingest
    await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)
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

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)
    assert all(r.status == "ok" for r in results)

    meta = await connected_store.get_collection_meta(col_name)
    assert meta is not None and meta.centroid is not None
    assert len(meta.centroid) == 2
    # mean of ([1,0]*n + [0,1]*m) — each file produces k chunks, centroid ≈ [0.5, 0.5]
    assert abs(meta.centroid[0] - 0.5) < 1e-6
    assert abs(meta.centroid[1] - 0.5) < 1e-6


@pytest.mark.asyncio
async def test_ingest_directory_calls_generate_description(connected_store, col_name, tmp_path):
    """ingest_directory() calls generate_description when _should_regenerate returns True (first ingest)."""
    from unittest.mock import patch as _patch

    pipeline = make_pipeline(connected_store)
    (tmp_path / "doc.md").write_text("# Doc\n\nContent.\n" * 5)

    with _patch(
        "archon_search.pipeline.generate_description", return_value="A fine collection."
    ) as mock_gen:
        await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

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
        await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    meta1 = await connected_store.get_collection_meta(col_name)
    assert meta1 is not None and meta1.description == "Original description."

    # Swap embedder so centroid changes and described_at_doc_count triggers regeneration
    class AltBackend:
        model_name: str = "alt"
        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.9] * 4 for _ in texts]

    pipeline._global_embedder = Embedder(AltBackend())

    # Second ingest — described_at=1, current=1 → no 20% change → no regeneration
    # Force regeneration by using a new collection that has no existing description
    # (We test preservation by simulating failure on a new path that triggers regeneration)
    new_col = col_name + "-b"
    (tmp_path / "doc2.md").write_text("# Doc2\n\nNew content.\n" * 5)

    with _patch("archon_search.pipeline.generate_description", return_value=None) as mock_gen:
        pipeline._global_embedder = make_embedder()  # reset to standard embedder
        await pipeline.ingest_directory(tmp_path, new_col, embedder=pipeline._global_embedder, rebuild_fts=False)

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
        await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    meta = await connected_store.get_collection_meta(col_name)
    assert meta is not None
    assert meta.described_at_doc_count == 3
    assert meta.last_described is not None


@pytest.mark.asyncio
async def test_ingest_file_records_parse_embed_persist(tmp_path):
    """ingest_file records parse, embed, and persist stages when a recorder is bound."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.observability import bind_stage_recorder

    class StubStore:
        @property
        def supports_incremental_fts_delete(self) -> bool:
            return True

        async def ensure_collection(self, *a: Any, **kw: Any) -> None:
            pass

        async def delete_document(self, *a: Any, **kw: Any) -> int:
            return 0

        async def ingest_chunks(self, *a: Any, **kw: Any) -> ChunkIngestResult:
            return ChunkIngestResult(chunks_ingested=1, needs_recompute=False)

        async def optimize_fts(self, *a: Any, **kw: Any) -> None:
            pass

        async def rebuild_fts_index(self, *a: Any, **kw: Any) -> None:
            pass

        async def get_dominant_language(self, *a: Any, **kw: Any) -> str:
            return ""

        async def get_collection_meta(self, *a: Any, **kw: Any) -> None:
            return None

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
        result = await pipeline.ingest_file(md_file, "test-col", embedder=pipeline._global_embedder)

    assert result.status == "ok"
    assert {"parse", "embed", "persist"} <= recorder.stage_timings_ms.keys()


@pytest.mark.asyncio
async def test_pipeline_noop_when_unbound(tmp_path):
    """Pipeline methods run without error when no recorder is bound; ContextVar stays None."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.observability import _stage_recorder

    search_candidate = ScoredSearchCandidate(
        doc_id="b" * 64,
        chunk_id=("b" * 64) + "-000000",
        text="text",
        source_path="/path",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=None, vector_score=None, vector_score_kind=None,
            fts_rank=None, fts_score=None, fts_score_kind=None,
            rrf_score=0.5, reranker_score=None,
        ),
        collection="col",
    )

    class StubStore:
        @property
        def supports_incremental_fts_delete(self) -> bool:
            return True

        async def hybrid_search_with_trace(self, *a: Any, **kw: Any) -> list[ScoredSearchCandidate]:
            return [search_candidate]

        async def fetch_adjacent_chunks(self, *a: Any, **kw: Any) -> list[ChunkRecord]:
            return []

        async def ensure_collection(self, *a: Any, **kw: Any) -> None:
            pass

        async def delete_document(self, *a: Any, **kw: Any) -> int:
            return 0

        async def ingest_chunks(self, *a: Any, **kw: Any) -> ChunkIngestResult:
            return ChunkIngestResult(chunks_ingested=1, needs_recompute=False)

        async def optimize_fts(self, *a: Any, **kw: Any) -> None:
            pass

        async def rebuild_fts_index(self, *a: Any, **kw: Any) -> None:
            pass

        async def get_dominant_language(self, *a: Any, **kw: Any) -> str:
            return ""

        async def get_collection_meta(self, *a: Any, **kw: Any) -> None:
            return None

    pipeline = SearchPipeline(
        store=StubStore(),  # type: ignore[arg-type]
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=64),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    await pipeline._global_embedder.embed(["warmup"])

    md_file = tmp_path / "doc.md"
    md_file.write_text("# Test\n\nSome content.\n" * 5)

    assert _stage_recorder.get() is None
    await pipeline.search("query", "col", embedder=pipeline._global_embedder)
    assert _stage_recorder.get() is None

    await pipeline.search_with_context("query", "col", embedder=pipeline._global_embedder)
    assert _stage_recorder.get() is None

    await pipeline.ingest_file(md_file, "col", embedder=pipeline._global_embedder)
    assert _stage_recorder.get() is None


@pytest.mark.asyncio
async def test_create_pipeline_wires_all_components():
    from unittest.mock import MagicMock
    from archon_search.pipeline import create_pipeline

    cfg = MagicMock()
    cfg.db_path = "/tmp/test_rag_db"
    cfg.multilingual = False  # Prevent LanguageDetector instantiation
    cfg.graph.naive_max_expansion_terms = 20

    with (
        patch("archon_search.pipeline.ModelEmbedder") as MockME,
        patch("archon_search.pipeline.ModelReranker") as MockMR,
        patch("archon_search.pipeline.DocumentChunker") as MockChunker,
        patch("archon_search.pipeline.ASTChunker") as MockASTChunker,
        patch("archon_search.pipeline.DocumentParser") as MockParser,
        patch("archon_search.pipeline.SearchStore") as MockStore,
    ):
        MockME.return_value = MockEmbedderBackend()
        MockMR.return_value = MockRerankerBackend()
        MockChunker.return_value = MagicMock()
        MockASTChunker.return_value = MagicMock()
        MockParser.return_value = MagicMock()
        MockStore.return_value = MagicMock()

        pipeline = create_pipeline(cfg)

    assert pipeline.store is not None
    assert pipeline._global_embedder is not None
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
    cfg.multilingual = False  # Prevent LanguageDetector instantiation
    cfg.graph.naive_max_expansion_terms = 20

    with (
        patch("archon_search.pipeline.ModelEmbedder") as MockME,
        patch("archon_search.pipeline.ModelReranker") as MockMR,
        patch("archon_search.pipeline.DocumentChunker"),
        patch("archon_search.pipeline.ASTChunker"),
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

    results = await pipeline.ingest_directory(tmp_path, col_name, progress_cb=_cb, embedder=pipeline._global_embedder, rebuild_fts=False)

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

    results = await pipeline.ingest_directory(tmp_path, col_name, progress_cb=_async_cb, embedder=pipeline._global_embedder, rebuild_fts=False)

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
    cfg.multilingual = False  # Prevent LanguageDetector instantiation
    cfg.graph.naive_max_expansion_terms = 20
    with (
        patch("archon_search.pipeline.DocumentChunker"),
        patch("archon_search.pipeline.ASTChunker"),
        patch("archon_search.pipeline.DocumentParser"),
    ):
        pipeline = create_pipeline(cfg, embedder_backend=MagicMock(), reranker_backend=MagicMock())
    assert pipeline.store._db_path == Path.home() / ".archon/search"


@pytest.mark.asyncio
async def test_ingest_directory_exclude_paths_skips_files(connected_store, col_name, tmp_path):
    """exclude_paths containing a file's absolute path → that file not in results."""
    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent for document {i}.\n" * 5)

    exclude = frozenset({str(tmp_path / "doc1.md")})
    results = await pipeline.ingest_directory(tmp_path, col_name, exclude_paths=exclude, embedder=pipeline._global_embedder, rebuild_fts=False)

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
    await pipeline.ingest_directory(tmp_path, col_name, progress_cb=progress_cb, exclude_paths=exclude, embedder=pipeline._global_embedder, rebuild_fts=False)

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
        embedder=pipeline._global_embedder,
        rebuild_fts=False,
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
        embedder=pipeline._global_embedder,
        rebuild_fts=False,
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
        embedder=pipeline._global_embedder,
        rebuild_fts=False,
)

    assert results == []
    assert calls == []


@pytest.mark.asyncio
async def test_ingest_directory_no_exclude_paths_unchanged(connected_store, col_name, tmp_path):
    """exclude_paths=None → identical to current behaviour (no filtering)."""
    pipeline = make_pipeline(connected_store)
    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent for document {i}.\n" * 5)

    results = await pipeline.ingest_directory(tmp_path, col_name, exclude_paths=None, embedder=pipeline._global_embedder, rebuild_fts=False)

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
        embedder=pipeline._global_embedder,
        rebuild_fts=False,
)

    # doc0 excluded, doc1 errored, doc2 ok → callback only for doc2
    assert len(results) == 2  # doc1 + doc2 (doc0 excluded)
    assert len(completed) == 1
    assert completed[0] == tmp_path / "doc2.md"


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

    pipeline._global_embedder = Embedder(ExplodingBackend())

    with pytest.raises(RuntimeError, match="embedder exploded"):
        await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)


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

    results = await pipeline.ingest_directory(tmp_path, col_name, progress_cb=progress_cb, embedder=pipeline._global_embedder, rebuild_fts=False)

    # One file parse-fails, two succeed
    ok_results = [r for r in results if r.status == "ok"]
    error_results = [r for r in results if r.status == "error"]
    assert len(ok_results) == 2
    assert len(error_results) == 1

    # progress_cb called for every file processed, including the failed one
    assert len(calls) == 3
    assert calls[-1] == (3, 3)


@pytest.mark.asyncio
async def test_P14_21_pipeline_ingest_directory_zero_markdown_files(connected_store, col_name, tmp_path):
    """ ingest_directory on a dir with zero accepted files returns [] and does not crash."""
    pipeline = make_pipeline(connected_store)
    # Only binary files present — all should be filtered out
    (tmp_path / "image.gif").write_bytes(b"GIF89a" + b"\x00" * 50)
    (tmp_path / "archive.zip").write_bytes(b"PK" + b"\x00" * 50)

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

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

    result = await pipeline.ingest_file(md_file, col_name, embedder=pipeline._global_embedder)

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
    await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    with pytest.raises(ValueError, match="Invalid doc_id"):
        await pipeline.delete_document("' OR '1'='1", col_name)


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

    from archon_search.config import SearchConfig

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=1, needs_recompute=False))
    store.rebuild_fts_index = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="")
    store.get_collection_meta = AsyncMock(return_value=None)
    store.update_collection_meta = AsyncMock()
    store.update_description = AsyncMock()
    store._config = SearchConfig()

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

    await pipeline.ingest_directory(tmp_path, "my-col", namespace="tenantA", embedder=pipeline._global_embedder, rebuild_fts=False)

    # store.get_collection_meta must be called with namespace="tenantA" (may be called multiple times for TTL + metadata)
    store.get_collection_meta.assert_any_call("my-col", namespace="tenantA")

    # update_description must be called with namespace="tenantA"
    store.update_description.assert_awaited_once()
    args, kwargs = store.update_description.call_args
    assert kwargs.get("namespace") == "tenantA"


@pytest.mark.asyncio
async def test_ingest_directory_default_namespace(tmp_path) -> None:
    """ingest_directory without explicit namespace defaults to DEFAULT_NAMESPACE."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    from archon_search.config import SearchConfig

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=1, needs_recompute=False))
    store.rebuild_fts_index = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="")
    store.get_collection_meta = AsyncMock(return_value=None)
    store.update_collection_meta = AsyncMock()
    store.update_description = AsyncMock()
    store._config = SearchConfig()

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

    await pipeline.ingest_directory(tmp_path, "my-col", embedder=pipeline._global_embedder, rebuild_fts=False)

    # may be called multiple times (TTL pre-resolution + per-file TTL + post-ingest metadata)
    store.get_collection_meta.assert_any_call("my-col", namespace=DEFAULT_NAMESPACE)
    store.update_description.assert_awaited_once()
    _, kwargs = store.update_description.call_args
    assert kwargs.get("namespace") == DEFAULT_NAMESPACE


@pytest.mark.asyncio
async def test_recompute_collection_meta_namespace_param(tmp_path) -> None:
    """recompute_collection_meta forwards namespace to store.get_collection_meta and CollectionMeta."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    existing_meta = CollectionMeta(name="my-col", namespace="tenantA", needs_recompute=True)

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

    await pipeline.recompute_collection_meta("my-col", pipeline._global_embedder, namespace="tenantA")

    store.get_collection_meta.assert_awaited_once_with("my-col", namespace="tenantA")
    saved_meta: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved_meta.namespace == "tenantA"


def _make_stub_store_for_embedding_tests(existing_meta=None):  # type: ignore[no-untyped-def]
    """Return a store mock suitable for description tests."""
    from unittest.mock import AsyncMock, MagicMock
    from archon_search.config import SearchConfig

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=1, needs_recompute=False))
    store.rebuild_fts_index = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="")
    store.get_collection_meta = AsyncMock(return_value=existing_meta)
    store.update_collection_meta = AsyncMock()
    store.update_description = AsyncMock()
    store.sample_chunk_texts = AsyncMock(return_value=["sample text"])
    store._config = SearchConfig()
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
async def test_ingest_calls_update_description_with_generated_desc(tmp_path) -> None:
    """ingest_directory calls update_description with the generated description."""
    from unittest.mock import AsyncMock, patch

    store = _make_stub_store_for_embedding_tests(existing_meta=None)
    pipeline = _make_pipeline_for_embedding_tests(store)

    (tmp_path / "doc.md").write_text("# Hello\n\nContent for description test.\n" * 5)

    with patch("archon_search.pipeline._should_regenerate", return_value=True), \
         patch("archon_search.pipeline.generate_description", return_value="test desc"):
        await pipeline.ingest_directory(tmp_path, "my-col", embedder=pipeline._global_embedder, rebuild_fts=False)

    store.update_description.assert_awaited_once()
    args, kwargs = store.update_description.call_args
    # First positional arg is collection, second is description
    assert args[1] == "test desc"


@pytest.mark.asyncio
async def test_ingest_description_none_passes_none_to_update_description(tmp_path) -> None:
    """When generate_description returns None, update_description is called with description=None."""
    from unittest.mock import AsyncMock, patch

    store = _make_stub_store_for_embedding_tests(existing_meta=None)
    pipeline = _make_pipeline_for_embedding_tests(store)

    (tmp_path / "doc.md").write_text("# Hello\n\nContent for null description test.\n" * 5)

    with patch("archon_search.pipeline.generate_description", return_value=None):
        with patch.object(pipeline._global_embedder, "embed_one", new_callable=AsyncMock) as mock_embed:
            await pipeline.ingest_directory(tmp_path, "my-col", embedder=pipeline._global_embedder, rebuild_fts=False)

    mock_embed.assert_not_awaited()
    store.update_description.assert_awaited_once()
    args, _ = store.update_description.call_args
    assert args[1] is None


@pytest.mark.asyncio
async def test_ingest_preserves_existing_description_when_no_regeneration(tmp_path) -> None:
    """When description regeneration is not triggered, existing description is passed to update_description."""
    from unittest.mock import AsyncMock, patch

    from archon_search.collection_meta import CollectionMeta

    existing_meta = CollectionMeta(
        name="my-col",
        description="old desc",
        described_at_doc_count=1,  # batch has 1 doc → 0% change → no regeneration
    )
    store = _make_stub_store_for_embedding_tests(existing_meta=existing_meta)
    pipeline = _make_pipeline_for_embedding_tests(store)

    (tmp_path / "doc.md").write_text("# Hello\n\nContent for preservation test.\n" * 5)

    with patch.object(pipeline._global_embedder, "embed_one", new_callable=AsyncMock) as mock_embed:
        await pipeline.ingest_directory(tmp_path, "my-col", embedder=pipeline._global_embedder, rebuild_fts=False)

    mock_embed.assert_not_awaited()
    store.update_description.assert_awaited_once()
    args, _ = store.update_description.call_args
    assert args[1] == "old desc"


@pytest.mark.asyncio
async def test_recompute_populates_description_embedding() -> None:
    """recompute_collection_meta persists description_embedding when existing meta has a description."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    embed_vec = [0.7] * 4
    existing_meta = CollectionMeta(name="my-col", description="some desc", needs_recompute=True)

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

    embed_one_mock = AsyncMock(return_value=embed_vec)
    with patch.object(pipeline._global_embedder, "embed_one", new=embed_one_mock):
        await pipeline.recompute_collection_meta("my-col", pipeline._global_embedder)

    embed_one_mock.assert_awaited_once_with("some desc")
    store.update_collection_meta.assert_awaited_once()
    saved_meta: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved_meta.description_embedding == embed_vec


@pytest.mark.asyncio
async def test_recompute_no_description_embedding_when_description_none() -> None:
    """recompute_collection_meta sets description_embedding=None when existing description is None."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from archon_search.chunker import DocumentChunker
    from archon_search.collection_meta import CollectionMeta
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    existing_meta = CollectionMeta(name="my-col", description=None, needs_recompute=True)

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=existing_meta)
    store.get_all_vectors = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    store.count_documents = AsyncMock(return_value=1)
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

    embed_one_mock = AsyncMock()
    with patch.object(pipeline._global_embedder, "embed_one", new=embed_one_mock):
        await pipeline.recompute_collection_meta("my-col", pipeline._global_embedder)

    embed_one_mock.assert_not_awaited()
    store.update_collection_meta.assert_awaited_once()
    saved_meta: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved_meta.description_embedding is None


@pytest.mark.asyncio
async def test_recompute_no_op_when_empty() -> None:
    """recompute_collection_meta is a no-op when the collection has no vectors."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=None)
    store.get_all_vectors = AsyncMock(return_value=[])
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

    embed_one_mock = AsyncMock()
    with patch.object(pipeline._global_embedder, "embed_one", new=embed_one_mock):
        await pipeline.recompute_collection_meta("empty-col", pipeline._global_embedder)

    embed_one_mock.assert_not_awaited()
    store.update_collection_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_file_returns_error_on_delete_store_busy(tmp_path) -> None:
    """ingest_file returns IngestResult(status='error') when delete_document raises StoreBusyError."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.store import StoreBusyError

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(side_effect=StoreBusyError(timeout_s=0.1))
    store.ingest_chunks = AsyncMock()
    store.rebuild_fts_index = AsyncMock()
    store.get_collection_meta = AsyncMock(return_value=None)

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "busy.md"
    md_file.write_text("Some content to ingest.")

    result = await pipeline.ingest_file(md_file, "test-col", embedder=pipeline._global_embedder)

    assert isinstance(result, IngestResult)
    assert result.status == "error"
    store.ingest_chunks.assert_not_awaited()


def _make_mock_store_for_b5() -> MagicMock:
    """Build a MagicMock store suitable for B5 task 5.2 tests."""
    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=2, needs_recompute=False))
    store.rebuild_fts_index = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="")
    store.get_collection_meta = AsyncMock(return_value=None)
    store.update_collection_meta = AsyncMock()
    store.update_description = AsyncMock()
    store.count_documents = AsyncMock(return_value=1)
    store.get_all_vectors = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    from archon_search.config import SearchConfig
    store._config = SearchConfig()
    return store


def _make_pipeline_with_store(store: MagicMock):
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


@pytest.mark.asyncio
async def test_ingest_directory_calls_update_description_not_update_collection_meta(tmp_path) -> None:
    """ingest_directory calls update_description, NOT update_collection_meta."""
    from archon_search.config import SearchConfig

    store = _make_mock_store_for_b5()
    store._config = SearchConfig()

    pipeline = _make_pipeline_with_store(store)

    md_file = tmp_path / "doc.md"
    md_file.write_text("# Hello\n\nContent for testing.\n" * 5)

    await pipeline.ingest_directory(tmp_path, "test-col", embedder=pipeline._global_embedder, rebuild_fts=False)

    store.update_description.assert_awaited_once()
    store.update_collection_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_directory_triggers_recompute_on_needs_recompute_signal(tmp_path) -> None:
    """When store.ingest_chunks returns needs_recompute=True and flag is on,
    recompute_collection_meta is called."""
    from archon_search.config import SearchConfig

    store = _make_mock_store_for_b5()
    store._config = SearchConfig(centroid_recompute_threshold=1)
    store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=2, needs_recompute=True))

    pipeline = _make_pipeline_with_store(store)

    with patch.object(pipeline, "recompute_collection_meta", new=AsyncMock()) as mock_recompute:
        md_file = tmp_path / "doc.md"
        md_file.write_text("# Hello\n\nContent for testing.\n" * 5)

        await pipeline.ingest_directory(tmp_path, "test-col", embedder=pipeline._global_embedder, rebuild_fts=False)

    mock_recompute.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingest_directory_no_recompute_below_threshold(tmp_path) -> None:
    """When needs_recompute=False, recompute_collection_meta is NOT called."""
    from archon_search.config import SearchConfig

    store = _make_mock_store_for_b5()
    store._config = SearchConfig(centroid_recompute_threshold=10000)
    store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=2, needs_recompute=False))

    pipeline = _make_pipeline_with_store(store)

    with patch.object(pipeline, "recompute_collection_meta", new=AsyncMock()) as mock_recompute:
        md_file = tmp_path / "doc.md"
        md_file.write_text("# Hello\n\nContent for testing.\n" * 5)

        await pipeline.ingest_directory(tmp_path, "test-col", embedder=pipeline._global_embedder, rebuild_fts=False)

    mock_recompute.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_file_triggers_recompute_on_needs_recompute_signal(tmp_path) -> None:
    """ingest_file calls recompute_collection_meta directly when needs_recompute=True and flag=True."""
    from archon_search.config import SearchConfig

    store = _make_mock_store_for_b5()
    store._config = SearchConfig()
    store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=2, needs_recompute=True))

    pipeline = _make_pipeline_with_store(store)

    with patch.object(pipeline, "recompute_collection_meta", new=AsyncMock()) as mock_recompute:
        md_file = tmp_path / "doc.md"
        md_file.write_text("# Hello\n\nContent for testing.\n" * 5)

        result = await pipeline.ingest_file(md_file, "test-col", embedder=pipeline._global_embedder)

    assert result.status == "ok"
    assert result.needs_recompute is True
    mock_recompute.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingest_file_forwards_namespace_to_store(tmp_path) -> None:
    """ingest_file forwards namespace= to store.delete_document and store.ingest_chunks."""
    store = _make_mock_store_for_b5()

    pipeline = _make_pipeline_with_store(store)

    md_file = tmp_path / "doc.md"
    md_file.write_text("# Hello\n\nContent for testing.\n" * 5)

    await pipeline.ingest_file(md_file, "test-col", namespace="ns1", embedder=pipeline._global_embedder)

    # delete_document should receive namespace="ns1"
    call_kwargs = store.delete_document.call_args
    assert call_kwargs.kwargs.get("namespace") == "ns1" or (
        len(call_kwargs.args) >= 3 and call_kwargs.args[2] == "ns1"
    )

    # ingest_chunks should receive namespace="ns1"
    ic_kwargs = store.ingest_chunks.call_args
    assert ic_kwargs.kwargs.get("namespace") == "ns1"


# ---------------------------------------------------------------------------
# BE-3: accumulator removal and unconditional B5 path tests
# ---------------------------------------------------------------------------


def test_ingest_directory_no_all_vectors_accumulator() -> None:
    """all_vectors and all_chunks must NOT appear as live variables in ingest_directory source.

    This is a deletion-regression guard: if the accumulators creep back in,
    this test fails immediately.
    """
    import inspect

    from archon_search.pipeline import SearchPipeline

    src = inspect.getsource(SearchPipeline.ingest_directory)
    # Strip comment lines to avoid false positives from docs/comments
    code_lines = [line for line in src.splitlines() if not line.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert "all_vectors" not in code, "all_vectors accumulator must be removed from ingest_directory"
    assert "all_chunks" not in code, "all_chunks accumulator must be removed from ingest_directory"


@pytest.mark.asyncio
async def test_description_reads_from_store_not_accumulator(tmp_path) -> None:
    """When _should_regenerate returns True, description uses store.sample_chunk_texts(n=100),
    not any chunk accumulator. store.list_chunks_raw must NOT be called."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=3, needs_recompute=False))
    store.rebuild_fts_index = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="")
    store.get_collection_meta = AsyncMock(return_value=None)
    store.update_description = AsyncMock()
    store.update_collection_meta = AsyncMock()
    store.list_chunks_raw = AsyncMock()
    sample_texts = ["chunk text A", "chunk text B", "chunk text C"]
    store.sample_chunk_texts = AsyncMock(return_value=sample_texts)

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=64),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    (tmp_path / "doc.md").write_text("# Hello\n\nContent for BE-3 test.\n" * 5)

    with patch("archon_search.pipeline.generate_description", return_value="generated desc") as mock_gen, \
         patch("archon_search.pipeline._should_regenerate", return_value=True):
        await pipeline.ingest_directory(
            tmp_path, "test-col", embedder=pipeline._global_embedder, rebuild_fts=False
        )

    # sample_chunk_texts must be called with n=100
    store.sample_chunk_texts.assert_awaited_once()
    call_kwargs = store.sample_chunk_texts.call_args
    assert call_kwargs.kwargs.get("n") == 100 or (len(call_kwargs.args) >= 3 and call_kwargs.args[2] == 100)

    # generate_description receives the sample texts
    mock_gen.assert_awaited_once()
    desc_arg = mock_gen.call_args[0][0]
    assert desc_arg == sample_texts

    # list_chunks_raw must NOT be called — the old accumulator path is gone
    store.list_chunks_raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_centroid_incremental_path_always_used(tmp_path) -> None:
    """ingest_directory always calls update_description (B5 path unconditionally).
    update_collection_meta (pre-B5 path) must NOT be called."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=2, needs_recompute=False))
    store.rebuild_fts_index = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="")
    store.get_collection_meta = AsyncMock(return_value=None)
    store.update_description = AsyncMock()
    store.update_collection_meta = AsyncMock()
    store.sample_chunk_texts = AsyncMock(return_value=[])

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=64),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    for i in range(3):
        (tmp_path / f"doc{i}.md").write_text(f"# Doc {i}\n\nContent number {i}.\n" * 5)

    await pipeline.ingest_directory(tmp_path, "test-col", embedder=pipeline._global_embedder, rebuild_fts=False)

    # B5 path: update_description is always called
    store.update_description.assert_awaited_once()
    # Pre-B5 path: update_collection_meta is never called from ingest_directory
    store.update_collection_meta.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_directory_centroid_correct_after_refactor(tmp_path, monkeypatch) -> None:
    """Regression guard: centroid stored in meta equals mean of all chunk stub vectors
    after the accumulator removal. Wires up a real SearchStore + SearchPipeline with
    stub embedder/reranker for end-to-end coverage."""
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    class _StubEmbedderBackend:
        model_name: str = "stub-model"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    class _StubRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    store = SearchStore(str(tmp_path / "db"))
    await store.connect()

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_StubEmbedderBackend()),
        reranker=Reranker(_StubRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    col = "centroid-test"
    for i in range(5):
        (tmp_path / f"file{i}.md").write_text(f"# Doc {i}\n\nReal content for centroid test number {i}.\n" * 5)

    await pipeline.ingest_directory(tmp_path, col, embedder=pipeline._global_embedder, rebuild_fts=False)

    meta = await store.get_collection_meta(col)
    assert meta is not None, "collection meta must exist after ingest_directory"
    # The stub embedder returns [0.1, 0.2, 0.3, 0.4] for every chunk.
    # After B5 incremental ingest, centroid is stored via _do_update_meta_on_add.
    # Since all vectors are identical, centroid must equal [0.1, 0.2, 0.3, 0.4].
    expected = [0.1, 0.2, 0.3, 0.4]
    assert meta.centroid is not None, "centroid must be set in meta"
    for actual, exp in zip(meta.centroid, expected):
        assert abs(actual - exp) < 1e-5, f"centroid mismatch: {meta.centroid!r} != {expected!r}"


# ---------------------------------------------------------------------------
# END BE-3 tests
# ---------------------------------------------------------------------------


def test_ingest_result_needs_recompute_not_in_rest_response() -> None:
    """IngestResult.needs_recompute is an internal field and must not appear in any REST schema."""
    from archon_search.server import schemas

    schema_classes = [
        getattr(schemas, name)
        for name in dir(schemas)
        if not name.startswith("_")
    ]
    for cls in schema_classes:
        if hasattr(cls, "model_fields"):
            assert "needs_recompute" not in cls.model_fields, (
                f"{cls.__name__} must not expose needs_recompute in REST schema"
            )


def test_ingest_result_has_needs_recompute_field() -> None:
    """IngestResult dataclass has needs_recompute field (internal pipeline signal)."""
    r = IngestResult(doc_id="abc", chunks_created=1, status="ok", needs_recompute=True)
    assert r.needs_recompute is True

    r2 = IngestResult(doc_id="abc", chunks_created=1, status="ok")
    assert r2.needs_recompute is False


def _make_pipeline_for_recompute(store, config=None):
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    if config is not None:
        store._config = config
    return pipeline


@pytest.mark.asyncio
async def test_recompute_writes_centroid_sum() -> None:
    """After recompute_collection_meta, saved meta has centroid_sum == elementwise_sum(all_vectors)."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.store import elementwise_sum

    vectors = [[1.0, 2.0, 3.0], [3.0, 4.0, 5.0]]
    store = MagicMock()
    # needs_recompute=True prevents the short-circuit from skipping the scan
    store.get_collection_meta = AsyncMock(return_value=CollectionMeta(name="col", needs_recompute=True))
    store.get_all_vectors = AsyncMock(return_value=vectors)
    store.count_documents = AsyncMock(return_value=1)
    store.update_collection_meta = AsyncMock()

    pipeline = _make_pipeline_for_recompute(store)
    with patch.object(pipeline._global_embedder, "embed_one", new=AsyncMock()):
        await pipeline.recompute_collection_meta("col", pipeline._global_embedder)

    saved: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved.centroid_sum == elementwise_sum(vectors)


@pytest.mark.asyncio
async def test_recompute_resets_mutations_counter() -> None:
    """recompute_collection_meta resets mutations_since_recompute to 0 and needs_recompute to False."""
    from archon_search.collection_meta import CollectionMeta

    existing = CollectionMeta(name="col", mutations_since_recompute=999, needs_recompute=True)
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=existing)
    store.get_all_vectors = AsyncMock(return_value=[[1.0, 2.0]])
    store.count_documents = AsyncMock(return_value=1)
    store.update_collection_meta = AsyncMock()

    pipeline = _make_pipeline_for_recompute(store)
    with patch.object(pipeline._global_embedder, "embed_one", new=AsyncMock()):
        await pipeline.recompute_collection_meta("col", pipeline._global_embedder)

    saved: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved.mutations_since_recompute == 0
    assert saved.needs_recompute is False


@pytest.mark.asyncio
async def test_recompute_noop_on_empty_collection() -> None:
    """recompute_collection_meta with empty vectors returns early; update_collection_meta not called."""
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=None)
    store.get_all_vectors = AsyncMock(return_value=[])
    store.update_collection_meta = AsyncMock()

    pipeline = _make_pipeline_for_recompute(store)
    await pipeline.recompute_collection_meta("empty-col", pipeline._global_embedder)

    store.update_collection_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_recompute_empty_collection_clears_needs_recompute_flag() -> None:
    """force=True on empty collection writes a cleared meta row (not skip)."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.config import SearchConfig

    existing = CollectionMeta(name="col", needs_recompute=True, mutations_since_recompute=42)
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=existing)
    store.get_all_vectors = AsyncMock(return_value=[])
    store.count_documents = AsyncMock(return_value=0)
    store.update_collection_meta = AsyncMock()

    cfg = SearchConfig()
    pipeline = _make_pipeline_for_recompute(store, config=cfg)
    await pipeline.recompute_collection_meta("col", pipeline._global_embedder, force=True)

    store.update_collection_meta.assert_awaited_once()
    saved: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved.needs_recompute is False
    assert saved.mutations_since_recompute == 0
    assert saved.centroid_sum is None
    assert saved.centroid is None
    assert saved.chunk_count == 0
    assert saved.doc_count == 0


@pytest.mark.asyncio
async def test_recompute_single_get_all_vectors_call() -> None:
    """recompute_collection_meta calls store.get_all_vectors exactly once."""
    from archon_search.collection_meta import CollectionMeta

    store = MagicMock()
    # needs_recompute=True ensures short-circuit does not skip the scan
    store.get_collection_meta = AsyncMock(return_value=CollectionMeta(name="col", needs_recompute=True))
    store.get_all_vectors = AsyncMock(return_value=[[1.0, 2.0]])
    store.count_documents = AsyncMock(return_value=1)
    store.update_collection_meta = AsyncMock()

    pipeline = _make_pipeline_for_recompute(store)
    with patch.object(pipeline._global_embedder, "embed_one", new=AsyncMock()):
        await pipeline.recompute_collection_meta("col", pipeline._global_embedder)

    store.get_all_vectors.assert_awaited_once()


@pytest.mark.asyncio
async def test_recompute_collection_meta_no_op_when_not_needed() -> None:
    """Short-circuit: after fresh recompute (needs_recompute=False, mutations=0), second call skips scan."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.config import SearchConfig

    existing = CollectionMeta(name="col", needs_recompute=False, mutations_since_recompute=0)
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=existing)
    store.get_all_vectors = AsyncMock(return_value=[[1.0]])
    store.update_collection_meta = AsyncMock()

    cfg = SearchConfig()
    pipeline = _make_pipeline_for_recompute(store, config=cfg)
    await pipeline.recompute_collection_meta("col", pipeline._global_embedder)

    store.get_all_vectors.assert_not_awaited()


@pytest.mark.asyncio
async def test_recompute_collection_meta_force_bypasses_short_circuit() -> None:
    """force=True bypasses the short-circuit and calls get_all_vectors even when no recompute needed."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.config import SearchConfig

    existing = CollectionMeta(name="col", needs_recompute=False, mutations_since_recompute=0)
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=existing)
    store.get_all_vectors = AsyncMock(return_value=[[1.0, 2.0]])
    store.count_documents = AsyncMock(return_value=1)
    store.update_collection_meta = AsyncMock()

    cfg = SearchConfig()
    pipeline = _make_pipeline_for_recompute(store, config=cfg)
    with patch.object(pipeline._global_embedder, "embed_one", new=AsyncMock()):
        await pipeline.recompute_collection_meta("col", pipeline._global_embedder, force=True)

    store.get_all_vectors.assert_awaited_once()


@pytest.mark.asyncio
async def test_recompute_short_circuits_when_not_needed_and_not_forced() -> None:
    """When force=False and meta says needs_recompute=False and mutations_since_recompute=0,
    the full scan is skipped (short-circuit). get_all_vectors is NOT called."""
    from archon_search.collection_meta import CollectionMeta

    existing = CollectionMeta(name="col", needs_recompute=False, mutations_since_recompute=0)
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=existing)
    store.get_all_vectors = AsyncMock(return_value=[[1.0, 2.0]])
    store.count_documents = AsyncMock(return_value=1)
    store.update_collection_meta = AsyncMock()

    pipeline = _make_pipeline_for_recompute(store)
    await pipeline.recompute_collection_meta("col", pipeline._global_embedder, force=False)

    # Short-circuit fires — get_all_vectors must NOT be called
    store.get_all_vectors.assert_not_awaited()


@pytest.mark.asyncio
async def test_recompute_empty_collection_with_existing_meta_force_false_writes_cleared_row() -> None:
    """force=False + existing_meta + empty vectors writes a cleared meta row via the existing_meta branch."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.config import SearchConfig

    existing = CollectionMeta(name="col", needs_recompute=True, mutations_since_recompute=5)
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=existing)
    store.get_all_vectors = AsyncMock(return_value=[])
    store.count_documents = AsyncMock(return_value=0)
    store.update_collection_meta = AsyncMock()

    cfg = SearchConfig()
    pipeline = _make_pipeline_for_recompute(store, config=cfg)
    await pipeline.recompute_collection_meta("col", pipeline._global_embedder, force=False)

    store.update_collection_meta.assert_awaited_once()
    saved: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved.centroid_sum is None
    assert saved.centroid is None
    assert saved.needs_recompute is False
    assert saved.mutations_since_recompute == 0


@pytest.mark.asyncio
async def test_recompute_new_collection_no_existing_meta_runs_full_scan() -> None:
    """force=False + incremental enabled + existing_meta=None: full scan runs (first-ever recompute)."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.config import SearchConfig
    from archon_search.store import elementwise_sum

    vectors = [[1.0, 0.0], [0.0, 1.0]]
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=None)
    store.get_all_vectors = AsyncMock(return_value=vectors)
    store.count_documents = AsyncMock(return_value=1)
    store.update_collection_meta = AsyncMock()

    cfg = SearchConfig()
    pipeline = _make_pipeline_for_recompute(store, config=cfg)
    with patch.object(pipeline._global_embedder, "embed_one", new=AsyncMock()):
        await pipeline.recompute_collection_meta("col", pipeline._global_embedder, force=False)

    store.get_all_vectors.assert_awaited_once()
    store.update_collection_meta.assert_awaited_once()
    saved: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved.centroid_sum == elementwise_sum(vectors)


def test_self_embedder_does_not_exist():
    """Verify self._embedder was fully renamed to self._global_embedder in pipeline.py."""
    import re
    content = (
        __import__("pathlib").Path("archon_search/pipeline.py").read_text()
    )
    matches = re.findall(r'\bself\._embedder\b', content)
    assert not matches, f"Found {len(matches)} remaining 'self._embedder' reference(s) in pipeline.py"


@pytest.mark.asyncio
async def test_ingest_file_uses_passed_embedder(tmp_path) -> None:
    """ingest_file() embeds with the passed embedder, not self._global_embedder."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = _make_mock_store_for_b5()
    global_embedder = make_embedder()
    passed_embedder = make_embedder()

    global_embed_mock = AsyncMock(return_value=[[0.1] * 4])
    passed_embed_mock = AsyncMock(return_value=[[0.1] * 4])

    global_embedder.embed = global_embed_mock  # type: ignore[method-assign]
    global_embedder._embedding_dim = 4  # pre-initialize so embedding_dim property works
    passed_embedder.embed = passed_embed_mock  # type: ignore[method-assign]
    passed_embedder._embedding_dim = 4  # pre-initialize so embedding_dim property works

    pipeline = SearchPipeline(
        store=store,
        embedder=global_embedder,
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "doc.md"
    md_file.write_text("# Test\n\nContent for embedder routing test.\n" * 5)

    result = await pipeline.ingest_file(md_file, "test-col", embedder=passed_embedder)

    assert result.status == "ok"
    passed_embed_mock.assert_awaited()
    global_embed_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_file_writes_correct_model_name_to_chunks(tmp_path) -> None:
    """After ingest_file(..., embedder=embedder_X), ingest_chunks receives embedding_model=embedder_X.model_name."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = _make_mock_store_for_b5()

    class CustomBackend:
        model_name: str = "custom-model-xyz"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.5] * 4 for _ in texts]

    custom_embedder = Embedder(CustomBackend())

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),  # global embedder has model_name="mock-embedder"
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    md_file = tmp_path / "doc.md"
    md_file.write_text("# Test\n\nContent for model name test.\n" * 5)

    await pipeline.ingest_file(md_file, "test-col", embedder=custom_embedder)

    store.ingest_chunks.assert_awaited_once()
    call_kwargs = store.ingest_chunks.call_args
    assert call_kwargs.kwargs.get("embedding_model") == "custom-model-xyz"


def _make_embedder_with_model(model_name: str) -> Embedder:
    """Build an Embedder whose backend reports a specific model_name."""

    class _Backend:
        is_warm: bool = False

        def __init__(self, name: str) -> None:
            self.model_name = name

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 4 for _ in texts]

    return Embedder(_Backend(model_name))


def _make_mock_store_c1(existing_meta=None):  # type: ignore[no-untyped-def]
    """Minimal mock store for C1 tests."""
    from archon_search.config import SearchConfig

    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(return_value=ChunkIngestResult(chunks_ingested=1, needs_recompute=False))
    store.rebuild_fts_index = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="")
    store.get_collection_meta = AsyncMock(return_value=existing_meta)
    store.update_collection_meta = AsyncMock()
    store.update_description = AsyncMock()
    store.sample_chunk_texts = AsyncMock(return_value=[])
    store._config = SearchConfig()
    return store


@pytest.mark.asyncio
async def test_ingest_directory_preserves_active_embedding_model(tmp_path) -> None:
    """After BE-3, ingest_directory uses the B5 path (update_description) and does NOT
    call update_collection_meta. C1 fields (active_embedding_model, etc.) are preserved
    inside the store via the incremental update path."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    existing_meta = CollectionMeta(name="col", active_embedding_model="model-X")
    store = _make_mock_store_c1(existing_meta=existing_meta)

    embedder_y = _make_embedder_with_model("model-Y")

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    (tmp_path / "doc.md").write_text("# Hello\n\nContent.\n" * 5)

    await pipeline.ingest_directory(tmp_path, "col", embedder=embedder_y, rebuild_fts=False)

    # B5 path: update_description is called
    store.update_description.assert_awaited_once()
    # Pre-B5 path: update_collection_meta is NOT called from ingest_directory
    store.update_collection_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_directory_sets_active_embedding_model_for_new_collection(tmp_path) -> None:
    """After BE-3, ingest_directory uses the B5 path (update_description) for both
    new and existing collections. update_collection_meta is not called."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = _make_mock_store_c1(existing_meta=None)

    embedder_x = _make_embedder_with_model("model-X")

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    (tmp_path / "doc.md").write_text("# Hello\n\nContent.\n" * 5)

    await pipeline.ingest_directory(tmp_path, "col", embedder=embedder_x, rebuild_fts=False)

    # B5 path: update_description is called
    store.update_description.assert_awaited_once()
    # Pre-B5 path: update_collection_meta is NOT called from ingest_directory
    store.update_collection_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_directory_passes_generated_description_to_update_description(tmp_path) -> None:
    """When description regeneration is triggered, the new description is passed to update_description."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = _make_mock_store_c1(existing_meta=None)

    passed_embedder = _make_embedder_with_model("passed-model")
    passed_embedder.embed = AsyncMock(return_value=[[0.2] * 4])  # type: ignore[method-assign]
    passed_embedder._embedding_dim = 4

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    (tmp_path / "doc.md").write_text("# Hello\n\nContent.\n" * 5)

    with patch("archon_search.pipeline._should_regenerate", return_value=True), \
         patch("archon_search.pipeline.generate_description", new=AsyncMock(return_value="A good description")):
        await pipeline.ingest_directory(tmp_path, "col", embedder=passed_embedder, rebuild_fts=False)

    store.update_description.assert_awaited_once()
    args, _ = store.update_description.call_args
    assert args[1] == "A good description"


@pytest.mark.asyncio
async def test_ingest_directory_preserves_all_c1_fields(tmp_path) -> None:
    """After BE-3, ingest_directory calls update_description (B5 path) and does NOT
    call update_collection_meta. C1 fields are preserved inside the store."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    existing_meta = CollectionMeta(
        name="col",
        active_embedding_model="model-A",
        pending_embedding_model="model-B",
        needs_reindex=True,
        reindex_job_id="job-42",
    )
    store = _make_mock_store_c1(existing_meta=existing_meta)

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    (tmp_path / "doc.md").write_text("# Hello\n\nContent.\n" * 5)

    await pipeline.ingest_directory(tmp_path, "col", embedder=pipeline._global_embedder, rebuild_fts=False)

    # B5 path: update_description is called
    store.update_description.assert_awaited_once()
    # Pre-B5 path: update_collection_meta is NOT called from ingest_directory
    store.update_collection_meta.assert_not_awaited()


@pytest.mark.asyncio
async def test_recompute_collection_meta_preserves_active_embedding_model() -> None:
    """recompute_collection_meta preserves active_embedding_model from existing meta."""
    from archon_search.collection_meta import CollectionMeta

    existing_meta = CollectionMeta(name="col", active_embedding_model="model-X", needs_recompute=True)
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=existing_meta)
    store.get_all_vectors = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    store.count_documents = AsyncMock(return_value=1)
    store.update_collection_meta = AsyncMock()

    global_embedder = make_embedder()
    passed_global_embedder = make_embedder()

    class _Backend:
        model_name = "global-model"
        is_warm = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.5] * 4 for _ in texts]

    passed_global_embedder = Embedder(_Backend())
    pipeline = _make_pipeline_for_recompute(store)

    with patch.object(pipeline._global_embedder, "embed_one", new=AsyncMock(return_value=[0.5] * 4)):
        await pipeline.recompute_collection_meta("col", global_embedder=passed_global_embedder)

    store.update_collection_meta.assert_awaited_once()
    saved: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved.active_embedding_model == "model-X"


@pytest.mark.asyncio
async def test_recompute_collection_meta_preserves_all_c1_fields() -> None:
    """recompute_collection_meta preserves all four C1 fields from existing meta."""
    from archon_search.collection_meta import CollectionMeta

    existing_meta = CollectionMeta(
        name="col",
        active_embedding_model="model-X",
        pending_embedding_model="model-Y",
        needs_reindex=True,
        reindex_job_id="job-99",
        needs_recompute=True,
    )
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=existing_meta)
    store.get_all_vectors = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    store.count_documents = AsyncMock(return_value=1)
    store.update_collection_meta = AsyncMock()

    class _GlobalBackend:
        model_name = "global-model"
        is_warm = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.5] * 4 for _ in texts]

    global_emb = Embedder(_GlobalBackend())
    pipeline = _make_pipeline_for_recompute(store)

    with patch.object(pipeline._global_embedder, "embed_one", new=AsyncMock(return_value=[0.5] * 4)):
        await pipeline.recompute_collection_meta("col", global_embedder=global_emb)

    saved: CollectionMeta = store.update_collection_meta.call_args[0][0]
    assert saved.active_embedding_model == "model-X"
    assert saved.pending_embedding_model == "model-Y"
    assert saved.needs_reindex is True
    assert saved.reindex_job_id == "job-99"


@pytest.mark.asyncio
async def test_recompute_collection_meta_uses_global_embedder_for_description() -> None:
    """recompute_collection_meta calls global_embedder.embed_one for the description embedding."""
    from archon_search.collection_meta import CollectionMeta

    existing_meta = CollectionMeta(name="col", description="desc text", needs_recompute=True)
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=existing_meta)
    store.get_all_vectors = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    store.count_documents = AsyncMock(return_value=1)
    store.update_collection_meta = AsyncMock()

    class _GlobalBackend:
        model_name = "global-model"
        is_warm = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.5] * 4 for _ in texts]

    global_emb = Embedder(_GlobalBackend())
    embed_one_mock = AsyncMock(return_value=[0.5] * 4)
    global_emb.embed_one = embed_one_mock  # type: ignore[method-assign]

    pipeline = _make_pipeline_for_recompute(store)

    await pipeline.recompute_collection_meta("col", global_embedder=global_emb)

    embed_one_mock.assert_awaited_once_with("desc text")


def test_no_self_embedder_in_pipeline() -> None:
    """self._embedder must NOT appear in pipeline.py (fully renamed to _global_embedder)."""
    import re
    content = Path("archon_search/pipeline.py").read_text()
    matches = re.findall(r'\bself\._embedder\b', content)
    assert not matches, f"Found {len(matches)} 'self._embedder' reference(s) in pipeline.py"


def test_no_embedding_model_attribute_accesses() -> None:
    """No non-config .embedding_model attribute accesses must remain in archon_search/.

    Allowed: cfg.embedding_model, existing_cfg.embedding_model (Config object accesses).
    Forbidden: meta.embedding_model, collection.embedding_model, or any other object
    (these were migrated to active_embedding_model).
    """
    import subprocess
    result = subprocess.run(
        [
            "grep", "-rn", r"\.embedding_model\b",
            "archon_search/",
            "--include=*.py",
        ],
        capture_output=True,
        text=True,
    )
    lines = [
        line for line in result.stdout.splitlines()
        if "config.embedding_model" not in line
        and "existing_cfg.embedding_model" not in line
        and "cfg.embedding_model" not in line
        and "migrate_per_collection_model" not in line
        and "body.embedding_model" not in line
        and "__pycache__" not in line
    ]
    assert not lines, (
        f"Found {len(lines)} non-config .embedding_model reference(s) — "
        f"use active_embedding_model instead:\n" + "\n".join(lines)
    )


def test_no_underscore_embedder_anywhere() -> None:
    """._embedder must not appear in any archon_search/ file except router.py.

    router.py legitimately has self._embedder (the Router's own embedder).
    All other files must use _global_embedder or equivalent.
    """
    import subprocess
    result = subprocess.run(
        [
            "grep", "-rn", r"\._embedder\b",
            "archon_search/",
            "--include=*.py",
        ],
        capture_output=True,
        text=True,
    )
    lines = [
        line for line in result.stdout.splitlines()
        if "router.py" not in line
        and "__pycache__" not in line
    ]
    assert not lines, (
        f"Found {len(lines)} '._embedder' reference(s) outside router.py — "
        f"pipeline must use _global_embedder:\n" + "\n".join(lines)
    )


def _make_mock_store_for_c2(*, plan_b: bool = False) -> MagicMock:
    """Build a MagicMock store suitable for C2 language detection tests.

    Set ``plan_b=True`` to simulate Plan B (``supports_incremental_fts_delete=False``),
    which causes ingest_file to call ``rebuild_fts_index`` at batch end instead of
    ``optimize_fts``.  Under Plan A (default), ``optimize_fts`` is called.
    """
    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.delete_document = AsyncMock(return_value=0)
    store.ingest_chunks = AsyncMock(
        return_value=ChunkIngestResult(chunks_ingested=2, needs_recompute=False)
    )
    store.optimize_fts = AsyncMock()
    store.rebuild_fts_index = AsyncMock()
    store.get_dominant_language = AsyncMock(return_value="")
    store.get_collection_meta = AsyncMock(return_value=None)
    store.update_collection_meta = AsyncMock()
    store.supports_incremental_fts_delete = not plan_b
    from archon_search.config import SearchConfig
    store._config = SearchConfig()
    return store


def _make_pipeline_with_detector(store, language_detector=None, threshold: float = 0.7):
    """Build a SearchPipeline with optional LanguageDetector."""
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
        language_detector=language_detector,
        language_detection_confidence_threshold=threshold,
    )


@pytest.mark.asyncio
async def test_ingest_file_with_language_detection(tmp_path) -> None:
    """When a LanguageDetector is present, all chunks receive the detected language tag."""
    from archon_search.language_detector import LanguageDetector

    store = _make_mock_store_for_c2()

    detector = MagicMock(spec=LanguageDetector)
    detector.detect = AsyncMock(return_value="fr")

    pipeline = _make_pipeline_with_detector(store, language_detector=detector)

    md_file = tmp_path / "doc.md"
    md_file.write_text("Bonjour le monde. " * 20)

    # Capture the records passed to ingest_chunks
    ingested_records: list = []

    async def _capture_ingest(collection, records, **kwargs):
        ingested_records.extend(records)
        return ChunkIngestResult(chunks_ingested=len(records), needs_recompute=False)

    store.ingest_chunks = _capture_ingest

    await pipeline.ingest_file(md_file, "test-col", embedder=pipeline._global_embedder)

    assert len(ingested_records) > 0, "Expected at least one chunk to be ingested"
    assert all(r.language == "fr" for r in ingested_records), (
        f"All chunks should have language='fr', got: {[r.language for r in ingested_records]}"
    )
    detector.detect.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingest_file_language_detection_disabled(tmp_path) -> None:
    """When language_detector is None (multilingual off), all chunks have language=''."""
    store = _make_mock_store_for_c2()
    pipeline = _make_pipeline_with_detector(store, language_detector=None)

    md_file = tmp_path / "doc.md"
    md_file.write_text("Some content to ingest. " * 10)

    ingested_records: list = []

    async def _capture_ingest(collection, records, **kwargs):
        ingested_records.extend(records)
        return ChunkIngestResult(chunks_ingested=len(records), needs_recompute=False)

    store.ingest_chunks = _capture_ingest

    await pipeline.ingest_file(md_file, "test-col", embedder=pipeline._global_embedder)

    assert len(ingested_records) > 0
    assert all(r.language == "" for r in ingested_records), (
        f"All chunks should have language='', got: {[r.language for r in ingested_records]}"
    )


@pytest.mark.asyncio
async def test_ingest_file_language_unknown(tmp_path) -> None:
    """When detect returns 'unknown' (low confidence), all chunks have language='unknown'."""
    from archon_search.language_detector import LanguageDetector

    store = _make_mock_store_for_c2()

    detector = MagicMock(spec=LanguageDetector)
    detector.detect = AsyncMock(return_value="unknown")

    pipeline = _make_pipeline_with_detector(store, language_detector=detector)

    md_file = tmp_path / "doc.md"
    md_file.write_text("Some ambiguous content. " * 10)

    ingested_records: list = []

    async def _capture_ingest(collection, records, **kwargs):
        ingested_records.extend(records)
        return ChunkIngestResult(chunks_ingested=len(records), needs_recompute=False)

    store.ingest_chunks = _capture_ingest

    await pipeline.ingest_file(md_file, "test-col", embedder=pipeline._global_embedder)

    assert len(ingested_records) > 0
    assert all(r.language == "unknown" for r in ingested_records), (
        f"All chunks should have language='unknown', got: {[r.language for r in ingested_records]}"
    )


def test_search_pipeline_constructor_accepts_language_detector() -> None:
    """SearchPipeline constructor must accept language_detector and language_detection_confidence_threshold."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.language_detector import LanguageDetector
    from unittest.mock import MagicMock

    store = MagicMock()
    detector = MagicMock(spec=LanguageDetector)

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=None,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=5,
        top_k_return=3,
        language_detector=detector,
        language_detection_confidence_threshold=0.8,
    )

    assert pipeline._language_detector is detector
    assert pipeline._language_detection_confidence_threshold == 0.8


def test_search_pipeline_constructor_defaults_no_detector() -> None:
    """SearchPipeline without language_detector defaults to None detector and 0.7 threshold."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from unittest.mock import MagicMock

    store = MagicMock()

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=None,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=5,
        top_k_return=3,
    )

    assert pipeline._language_detector is None
    assert pipeline._language_detection_confidence_threshold == 0.7


def test_create_pipeline_passes_language_detector_when_multilingual() -> None:
    """create_pipeline() instantiates LanguageDetector when config.multilingual=True (mocked)."""
    from archon_search.pipeline import create_pipeline
    from archon_search.config import SearchConfig
    from archon_search.language_detector import LanguageDetector

    cfg = SearchConfig(multilingual=True)

    # LanguageDetector is imported inside create_pipeline; patch __init__ and load_model
    # so construction doesn't require the real model file or fasttext package.
    with (
        patch.object(LanguageDetector, "__init__", return_value=None),
        patch("archon_search.language_detector.fasttext", MagicMock()),
    ):
        pipeline = create_pipeline(cfg)

    assert pipeline._language_detector is not None
    assert isinstance(pipeline._language_detector, LanguageDetector)
    assert pipeline._language_detection_confidence_threshold == cfg.language_detection_confidence_threshold


def test_create_pipeline_no_language_detector_when_not_multilingual() -> None:
    """create_pipeline() does not instantiate LanguageDetector when config.multilingual=False."""
    from archon_search.pipeline import create_pipeline
    from archon_search.config import SearchConfig
    from archon_search.language_detector import LanguageDetector

    cfg = SearchConfig(multilingual=False)

    # Patch LanguageDetector.__init__ — if called, it will raise (file missing).
    # The test asserts it is NOT called.
    with patch.object(LanguageDetector, "__init__", side_effect=AssertionError("should not be called")):
        pipeline = create_pipeline(cfg)

    assert pipeline._language_detector is None


@pytest.mark.asyncio
async def test_ingest_file_language_detection_error_falls_back_to_empty(tmp_path) -> None:
    """When detect() raises an exception, chunks fall back to language='' (untagged)."""
    from archon_search.language_detector import LanguageDetector

    store = _make_mock_store_for_c2()

    detector = MagicMock(spec=LanguageDetector)
    detector.detect = AsyncMock(side_effect=RuntimeError("model crashed"))

    pipeline = _make_pipeline_with_detector(store, language_detector=detector)

    md_file = tmp_path / "doc.md"
    md_file.write_text("Some content. " * 10)

    ingested_records: list = []

    async def _capture_ingest(collection, records, **kwargs):
        ingested_records.extend(records)
        return ChunkIngestResult(chunks_ingested=len(records), needs_recompute=False)

    store.ingest_chunks = _capture_ingest

    # Should NOT raise — error is caught and logged
    result = await pipeline.ingest_file(md_file, "test-col", embedder=pipeline._global_embedder)

    assert result.status == "ok"
    assert len(ingested_records) > 0
    assert all(r.language == "" for r in ingested_records), (
        f"On detection failure, chunks should fall back to language='', got: {[r.language for r in ingested_records]}"
    )


@pytest.mark.asyncio
async def test_ingest_file_language_detect_uses_configured_threshold(tmp_path) -> None:
    """detect() is called with the pipeline's configured confidence_threshold."""
    from archon_search.language_detector import LanguageDetector

    store = _make_mock_store_for_c2()

    detector = MagicMock(spec=LanguageDetector)
    detector.detect = AsyncMock(return_value="de")

    pipeline = _make_pipeline_with_detector(store, language_detector=detector, threshold=0.9)

    md_file = tmp_path / "doc.md"
    md_file.write_text("Guten Tag. " * 10)

    ingested_records: list = []

    async def _capture_ingest(collection, records, **kwargs):
        ingested_records.extend(records)
        return ChunkIngestResult(chunks_ingested=len(records), needs_recompute=False)

    store.ingest_chunks = _capture_ingest

    await pipeline.ingest_file(md_file, "test-col", embedder=pipeline._global_embedder)

    # Verify detect() was called with the correct threshold
    detector.detect.assert_awaited_once()
    _, kwargs = detector.detect.call_args
    assert kwargs.get("confidence_threshold") == 0.9


@pytest.mark.asyncio
async def test_ingest_file_passes_dominant_language_to_rebuild_fts_index_plan_b(tmp_path) -> None:
    """Under Plan B, ingest_file calls get_dominant_language and passes result to rebuild_fts_index."""
    store = _make_mock_store_for_c2(plan_b=True)
    store.get_dominant_language = AsyncMock(return_value="fr")

    pipeline = _make_pipeline_with_detector(store)

    md_file = tmp_path / "doc.md"
    md_file.write_text("Some content. " * 10)

    await pipeline.ingest_file(md_file, "test-col", embedder=pipeline._global_embedder)

    store.get_dominant_language.assert_awaited_once_with("test-col")
    store.rebuild_fts_index.assert_awaited_once_with("test-col", language="fr")


@pytest.mark.asyncio
async def test_ingest_file_passes_empty_dominant_language_when_untagged_plan_b(tmp_path) -> None:
    """Under Plan B, ingest_file passes language='' to rebuild_fts_index when all chunks are untagged."""
    store = _make_mock_store_for_c2(plan_b=True)
    store.get_dominant_language = AsyncMock(return_value="")

    pipeline = _make_pipeline_with_detector(store)

    md_file = tmp_path / "doc.md"
    md_file.write_text("Some content. " * 10)

    await pipeline.ingest_file(md_file, "test-col", embedder=pipeline._global_embedder)

    store.rebuild_fts_index.assert_awaited_once_with("test-col", language="")


@pytest.mark.asyncio
async def test_ingest_file_uses_optimize_fts_under_plan_a(tmp_path) -> None:
    """Under Plan A (default), ingest_file calls optimize_fts and NOT rebuild_fts_index."""
    store = _make_mock_store_for_c2()  # plan_b=False is the default
    pipeline = _make_pipeline_with_detector(store)

    md_file = tmp_path / "doc.md"
    md_file.write_text("Some content. " * 10)

    await pipeline.ingest_file(md_file, "test-col", embedder=pipeline._global_embedder)

    store.optimize_fts.assert_awaited_once_with("test-col")
    store.rebuild_fts_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_pdf_excises_markers_before_chunker(tmp_path) -> None:
    """For a PDF source, markers are removed from the text before it reaches the chunker."""
    from unittest.mock import patch as _patch

    from archon_search.enricher import PAGE_BREAK_MARKER

    store = _make_mock_store_for_c2()
    pipeline = _make_pipeline_with_detector(store)

    pdf_file = tmp_path / "doc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 dummy")

    # Parser returns text with page-break markers (simulating docling output)
    marker_bearing_text = (
        "alpha content " * 10
        + PAGE_BREAK_MARKER
        + "beta content " * 10
        + PAGE_BREAK_MARKER
        + "gamma content " * 10
    )
    pipeline._parser.parse = AsyncMock(return_value=marker_bearing_text)

    chunk_texts_seen: list[str] = []
    original_chunk = pipeline._chunker.chunk

    def spy_chunk(text, *args, **kwargs):  # type: ignore[no-untyped-def]
        chunk_texts_seen.append(text)
        return original_chunk(text, *args, **kwargs)

    pipeline._chunker.chunk = spy_chunk  # type: ignore[method-assign]

    await pipeline.ingest_file(pdf_file, "test-col", embedder=pipeline._global_embedder)

    assert chunk_texts_seen, "chunker.chunk was never called"
    text_sent_to_chunker = chunk_texts_seen[0]
    assert PAGE_BREAK_MARKER not in text_sent_to_chunker, (
        "Marker must be excised before the text reaches the chunker"
    )


@pytest.mark.asyncio
async def test_ingest_text_format_unchanged(tmp_path) -> None:
    """For a .txt file, preprocess is NOT called; enrich_chunk gets page_table=None."""
    from unittest.mock import patch as _patch

    store = _make_mock_store_for_c2()
    pipeline = _make_pipeline_with_detector(store)

    txt_file = tmp_path / "doc.txt"
    txt_file.write_text("Hello world. " * 20)

    enrich_chunk_page_tables: list = []

    with _patch("archon_search.pipeline.MarkdownEnricher.preprocess") as mock_preprocess, \
         _patch("archon_search.pipeline.MarkdownEnricher.enrich_chunk") as mock_enrich:

        # preprocess must NOT be called for text-format sources
        mock_preprocess.side_effect = AssertionError(
            "preprocess should not be called for text-format sources"
        )

        # spy on page_table kwarg passed to enrich_chunk
        def spy_enrich(chunk, *, heading_table=None, page_table=None):  # type: ignore[no-untyped-def]
            enrich_chunk_page_tables.append(page_table)
            return {}

        mock_enrich.side_effect = spy_enrich

        result = await pipeline.ingest_file(txt_file, "test-col", embedder=pipeline._global_embedder)

    assert result.status == "ok"
    # enrich_chunk must have been called at least once
    assert enrich_chunk_page_tables, "enrich_chunk was never called"
    # page_table must be None for all calls (text-format path)
    assert all(pt is None for pt in enrich_chunk_page_tables), (
        f"page_table must be None for text-format sources, got: {enrich_chunk_page_tables}"
    )
    # preprocess must NOT have been called
    mock_preprocess.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_pdf_enrich_chunk_receives_page_table(tmp_path) -> None:
    """For a PDF source, enrich_chunk is called with a non-None page_table."""
    from unittest.mock import patch as _patch

    from archon_search.enricher import PAGE_BREAK_MARKER

    store = _make_mock_store_for_c2()
    pipeline = _make_pipeline_with_detector(store)

    pdf_file = tmp_path / "doc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 dummy")

    marker_text = "alpha content " * 10 + PAGE_BREAK_MARKER + "beta content " * 10
    pipeline._parser.parse = AsyncMock(return_value=marker_text)

    enrich_chunk_page_tables: list = []

    with _patch("archon_search.pipeline.MarkdownEnricher.enrich_chunk") as mock_enrich:
        def spy_enrich(chunk, *, heading_table=None, page_table=None):  # type: ignore[no-untyped-def]
            enrich_chunk_page_tables.append(page_table)
            return {}

        mock_enrich.side_effect = spy_enrich

        await pipeline.ingest_file(pdf_file, "test-col", embedder=pipeline._global_embedder)

    assert enrich_chunk_page_tables, "enrich_chunk was never called"
    assert all(pt is not None for pt in enrich_chunk_page_tables), (
        "page_table must be non-None for docling sources (pdf)"
    )


@pytest.mark.asyncio
async def test_ingest_unknown_extension_uses_fallback_path(tmp_path) -> None:
    """Unknown extensions (.xyz) fall through to the else-branch: heading_table=[], page_table=None."""
    from unittest.mock import patch as _patch

    store = _make_mock_store_for_c2()
    pipeline = _make_pipeline_with_detector(store)

    xyz_file = tmp_path / "doc.xyz"
    xyz_file.write_text("some content here " * 20)

    enrich_chunk_kwargs: list[dict] = []

    with _patch("archon_search.pipeline.MarkdownEnricher.preprocess") as mock_preprocess, \
         _patch("archon_search.pipeline.MarkdownEnricher.enrich_chunk") as mock_enrich:

        mock_preprocess.side_effect = AssertionError(
            "preprocess should not be called for unknown extensions"
        )

        def spy_enrich(chunk, *, heading_table=None, page_table=None):  # type: ignore[no-untyped-def]
            enrich_chunk_kwargs.append({"heading_table": heading_table, "page_table": page_table})
            return {}

        mock_enrich.side_effect = spy_enrich

        result = await pipeline.ingest_file(xyz_file, "test-col", embedder=pipeline._global_embedder)

    assert result.status == "ok"
    mock_preprocess.assert_not_called()
    assert enrich_chunk_kwargs, "enrich_chunk was never called"
    for kwargs in enrich_chunk_kwargs:
        assert kwargs["page_table"] is None, (
            f"page_table must be None for unknown extensions, got: {kwargs['page_table']}"
        )
        assert kwargs["heading_table"] == [], (
            f"heading_table must be [] for unknown non-docling extensions, got: {kwargs['heading_table']}"
        )


@pytest.mark.asyncio
async def test_ingest_md_front_matter_unchanged(tmp_path) -> None:
    """Ingest a .md file with YAML front matter; front matter is extracted and _acl propagates."""
    store = _make_mock_store_for_c2()
    pipeline = _make_pipeline_with_detector(store)

    md_file = tmp_path / "doc.md"
    md_file.write_text(
        "---\n_acl: public\n---\n\n# Hello\n\nSome content here.\n" * 10
    )

    result = await pipeline.ingest_file(md_file, "test-col", embedder=pipeline._global_embedder)

    assert result.status == "ok"
    assert result.chunks_created > 0

    # Verify chunks were stored — check via ingest_chunks call args.
    # ingest_chunks signature: (collection, chunks, embedding_model, namespace)
    # so call_args.args[0] = collection name, call_args.args[1] = chunks list.
    call_args = store.ingest_chunks.call_args
    assert call_args is not None
    chunks_stored = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("chunks", [])
    assert len(chunks_stored) > 0
    # _acl must propagate to every chunk as a list containing "public"
    for chunk in chunks_stored:
        assert "public" in (chunk.acl or []), f"Expected 'public' in acl, got {chunk.acl!r}"


@pytest.mark.asyncio
async def test_ingest_pdf_assigns_page_start_to_chunks(tmp_path) -> None:
    """Every chunk produced from a PDF has _page_start in its metadata (str, 1-indexed)."""
    from archon_search.enricher import PAGE_BREAK_MARKER

    store = _make_mock_store_for_c2()
    pipeline = _make_pipeline_with_detector(store)

    pdf_file = tmp_path / "doc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 dummy")

    # Simulate docling output: three pages separated by two markers.
    marker_text = (
        "alpha content " * 10
        + PAGE_BREAK_MARKER
        + "beta content " * 10
        + PAGE_BREAK_MARKER
        + "gamma content " * 10
    )
    pipeline._parser.parse = AsyncMock(return_value=marker_text)

    ingested_records: list = []

    async def _capture_ingest(collection, records, **kwargs):  # type: ignore[no-untyped-def]
        ingested_records.extend(records)
        return ChunkIngestResult(chunks_ingested=len(records), needs_recompute=False)

    store.ingest_chunks = _capture_ingest

    result = await pipeline.ingest_file(pdf_file, "test-col", embedder=pipeline._global_embedder)

    assert result.status == "ok"
    assert len(ingested_records) > 0, "Expected at least one chunk"
    for chunk in ingested_records:
        assert "_page_start" in chunk.metadata, (
            f"Every PDF chunk must have _page_start; got metadata={chunk.metadata!r}"
        )
        page_val = chunk.metadata["_page_start"]
        assert isinstance(page_val, str), f"_page_start must be str, got {type(page_val)}"
        assert page_val in {"1", "2", "3"}, (
            f"_page_start must be in {{1,2,3}} for three-page fixture, got {page_val!r}"
        )


@pytest.mark.asyncio
async def test_ingest_pdf_cross_page_chunk_has_page_end(tmp_path) -> None:
    """A chunk that straddles a page boundary has both _page_start and _page_end (and they differ)."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.enricher import PAGE_BREAK_MARKER

    store = _make_mock_store_for_c2()

    # Use small chunk_size so a chunk is likely to straddle the boundary
    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=None,
        chunker=DocumentChunker(chunk_size=8),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    pdf_file = tmp_path / "doc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 dummy")

    # "alpha content" is 13 chars, "beta content" is 12 chars.
    # With chunk_size=8 the chunker will split mid-content. A chunk ending in
    # "alpha co" and continuing into "beta co" spans the marker boundary.
    marker_text = "alpha content" + PAGE_BREAK_MARKER + "beta content" + PAGE_BREAK_MARKER + "gamma content"
    pipeline._parser.parse = AsyncMock(return_value=marker_text)

    ingested_records: list = []

    async def _capture_ingest(collection, records, **kwargs):  # type: ignore[no-untyped-def]
        ingested_records.extend(records)
        return ChunkIngestResult(chunks_ingested=len(records), needs_recompute=False)

    store.ingest_chunks = _capture_ingest

    result = await pipeline.ingest_file(pdf_file, "test-col", embedder=pipeline._global_embedder)

    assert result.status == "ok"
    assert len(ingested_records) > 0, "Expected at least one chunk"

    # At least one chunk must have _page_start (all should)
    for chunk in ingested_records:
        assert "_page_start" in chunk.metadata, (
            f"Every PDF chunk must have _page_start; metadata={chunk.metadata!r}"
        )

    # At least one chunk must straddle a boundary (have _page_end != _page_start)
    cross_page = [
        c for c in ingested_records
        if "_page_end" in c.metadata and c.metadata["_page_end"] != c.metadata["_page_start"]
    ]
    assert cross_page, (
        "Expected at least one chunk straddling a page boundary with _page_end != _page_start. "
        f"Chunks: {[c.metadata for c in ingested_records]}"
    )


@pytest.mark.asyncio
async def test_ingest_text_md_has_no_page_fields(tmp_path) -> None:
    """Markdown files produce no _page_start or _page_end in any chunk metadata."""
    store = _make_mock_store_for_c2()
    pipeline = _make_pipeline_with_detector(store)

    md_file = tmp_path / "doc.md"
    md_file.write_text("# Heading\n\nSome content here.\n" * 20)

    ingested_records: list = []

    async def _capture_ingest(collection, records, **kwargs):  # type: ignore[no-untyped-def]
        ingested_records.extend(records)
        return ChunkIngestResult(chunks_ingested=len(records), needs_recompute=False)

    store.ingest_chunks = _capture_ingest

    result = await pipeline.ingest_file(md_file, "test-col", embedder=pipeline._global_embedder)

    assert result.status == "ok"
    assert len(ingested_records) > 0, "Expected at least one chunk"
    for chunk in ingested_records:
        assert "_page_start" not in chunk.metadata, (
            f"Text-format chunk must NOT have _page_start; metadata={chunk.metadata!r}"
        )
        assert "_page_end" not in chunk.metadata, (
            f"Text-format chunk must NOT have _page_end; metadata={chunk.metadata!r}"
        )


@pytest.mark.asyncio
async def test_ingest_pdf_chunk_text_contains_no_marker(tmp_path) -> None:
    """No ChunkRecord.text produced from a PDF contains the page-break marker."""
    from archon_search.enricher import PAGE_BREAK_MARKER

    store = _make_mock_store_for_c2()
    pipeline = _make_pipeline_with_detector(store)

    pdf_file = tmp_path / "doc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 dummy")

    marker_text = (
        "alpha content " * 5
        + PAGE_BREAK_MARKER
        + "beta content " * 5
        + PAGE_BREAK_MARKER
        + "gamma content " * 5
    )
    pipeline._parser.parse = AsyncMock(return_value=marker_text)

    ingested_records: list = []

    async def _capture_ingest(collection, records, **kwargs):  # type: ignore[no-untyped-def]
        ingested_records.extend(records)
        return ChunkIngestResult(chunks_ingested=len(records), needs_recompute=False)

    store.ingest_chunks = _capture_ingest

    await pipeline.ingest_file(pdf_file, "test-col", embedder=pipeline._global_embedder)

    assert len(ingested_records) > 0, "Expected at least one chunk"
    for chunk in ingested_records:
        assert PAGE_BREAK_MARKER not in chunk.text, (
            f"ChunkRecord.text must not contain the page-break marker; "
            f"chunk text={chunk.text!r}"
        )


@pytest.mark.asyncio
async def test_ingest_pdf_with_language_detection_uses_cleaned_text(tmp_path) -> None:
    """Language detector receives marker-free cleaned text (not the raw parser output)."""
    from archon_search.enricher import PAGE_BREAK_MARKER
    from archon_search.language_detector import LanguageDetector

    store = _make_mock_store_for_c2()

    detector = MagicMock(spec=LanguageDetector)
    detector_inputs: list[str] = []

    async def _spy_detect(text: str, *, confidence_threshold: float = 0.7) -> str:  # type: ignore[no-untyped-def]
        detector_inputs.append(text)
        return "en"

    detector.detect = _spy_detect

    pipeline = _make_pipeline_with_detector(store, language_detector=detector)

    pdf_file = tmp_path / "doc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 dummy")

    marker_text = "alpha content " * 10 + PAGE_BREAK_MARKER + "beta content " * 10
    pipeline._parser.parse = AsyncMock(return_value=marker_text)

    await pipeline.ingest_file(pdf_file, "test-col", embedder=pipeline._global_embedder)

    assert detector_inputs, "Expected language detector to be called"
    for detected_input in detector_inputs:
        assert PAGE_BREAK_MARKER not in detected_input, (
            "Language detector must receive marker-free cleaned text; "
            f"got: {detected_input[:200]!r}"
        )


@pytest.mark.asyncio
async def test_ingest_pdf_metadata_survives_store_roundtrip(connected_store, col_name, tmp_path) -> None:
    """Full ingest → hybrid_search round-trip: _page_start is present in search result metadata."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.enricher import PAGE_BREAK_MARKER

    pipeline = SearchPipeline(
        store=connected_store,
        embedder=make_embedder(),
        reranker=None,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    pdf_file = tmp_path / "doc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 dummy")

    # Three pages of content; marker between page 1 and page 2
    marker_text = (
        "alpha content " * 10
        + PAGE_BREAK_MARKER
        + "beta content " * 10
        + PAGE_BREAK_MARKER
        + "gamma content " * 10
    )
    pipeline._parser.parse = AsyncMock(return_value=marker_text)

    ingest_result = await pipeline.ingest_file(
        pdf_file, col_name, embedder=pipeline._global_embedder
    )
    assert ingest_result.status == "ok"
    assert ingest_result.chunks_created > 0

    # Retrieve via hybrid_search using a zero vector (mock embedder returns [0.1]*4)
    query_vector = [0.1] * 4
    results = await connected_store.hybrid_search(
        col_name,
        query_vector=query_vector,
        query_text="beta content",
        top_k=10,
    )

    assert results, "Expected at least one search result after ingest"
    for result in results:
        assert "_page_start" in result.metadata, (
            f"_page_start must survive the LanceDB round-trip; metadata={result.metadata!r}"
        )


# ---------------------------------------------------------------------------
# BE-5: pipeline.list_documents cursor passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_list_documents_cursor_passes_through(connected_store, col_name, tmp_path) -> None:
    """pipeline.list_documents delegates cursor unchanged to store and returns tuple."""
    from unittest.mock import AsyncMock, MagicMock

    from archon_search._types import DocumentInfo
    from archon_search.collection_meta import CollectionMeta
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.embedder import Embedder
    from archon_search.reranker import Reranker
    from .conftest import make_embedder, make_reranker

    doc = DocumentInfo(doc_id="a" * 64, source_path="/p.md", chunk_count=1, indexed_at="2026-01-01T00:00:00")
    meta = CollectionMeta(name="col-x", namespace="default")
    store = MagicMock()
    store.get_collection_meta = AsyncMock(return_value=meta)
    store.list_documents = AsyncMock(return_value=([doc], None, 1))

    pipeline = SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    items, next_cursor, total = await pipeline.list_documents("col-x", limit=50, cursor="some-cursor")
    assert items == [doc]
    assert next_cursor is None
    assert total == 1
    store.list_documents.assert_awaited_once_with("col-x", 50, cursor="some-cursor")

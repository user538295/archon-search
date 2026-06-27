"""Unit and integration tests for the pipeline size guard — E0d BE-3.

Tests the SearchPipeline.ingest_file() size guard introduced in BE-3:
- max_file_mb=0 disables the guard
- file under limit ingests normally
- file exactly at limit is accepted (strictly-greater-than boundary)
- file over limit returns error IngestResult with code="file_too_large"
- symlinks are followed (os.path.getsize follows symlinks)
- directory batch: oversized file errors, under-limit file succeeds (S10)
- watcher path: ingest_file on oversized file returns error, no exception raised (S11)

Mock target notes:
- The guard calls `_file_exceeds_limit` from `archon_search._types`, which uses
  `os.path.getsize` internally → patch `archon_search._types.os.path.getsize`.
- On the error path, pipeline also calls `os.path.getsize` a second time for the
  human-readable message → patch `archon_search.pipeline.os.path.getsize` as well.

Plan: Documentation/Backlog/e0d-pdf-large-file-support-team-plan.md Task BE-3.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from archon_search._types import IngestResult


@contextmanager
def _patch_getsize(return_value=None, side_effect=None):
    """Patch os.path.getsize for the size guard.

    Both _file_exceeds_limit (in _types) and the pipeline's error-path stat call
    use os.path.getsize via 'import os'. Since os.path is a singleton module,
    patching os.path.getsize once covers both call sites uniformly.
    """
    kwargs: dict = {}
    if side_effect is not None:
        kwargs["side_effect"] = side_effect
    else:
        kwargs["return_value"] = return_value
    with patch("os.path.getsize", **kwargs) as mock_getsize:
        yield mock_getsize


# ---------------------------------------------------------------------------
# Helpers — minimal pipeline factory with injectable max_file_mb
# ---------------------------------------------------------------------------


def make_pipeline_with_limit(store, max_file_mb: int = 0):
    """Return a SearchPipeline with the specified max_file_mb guard."""
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    from .conftest import make_embedder, make_reranker

    return SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        max_file_mb=max_file_mb,
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_size_guard_zero_disables_check(connected_store, col_name, tmp_path):
    """S1 / S8 — max_file_mb=0 means no guard; os.path.getsize is never called."""
    pipeline = make_pipeline_with_limit(connected_store, max_file_mb=0)
    big_file = tmp_path / "big.md"
    big_file.write_text("# Hello\n\nThis is a test document.\n" * 20)
    # Assert getsize is never called — the guard block is skipped when max_file_mb=0.
    with _patch_getsize(return_value=500 * 1024 * 1024) as mock_getsize:
        result = await pipeline.ingest_file(big_file, col_name, embedder=pipeline._global_embedder)
    assert result.status == "ok"
    assert result.code is None
    mock_getsize.assert_not_called()


@pytest.mark.asyncio
async def test_size_guard_under_limit_ingests(connected_store, col_name, tmp_path):
    """File under max_file_mb ingests normally (status=ok)."""
    pipeline = make_pipeline_with_limit(connected_store, max_file_mb=100)  # 100 MB limit
    small_file = tmp_path / "small.md"
    small_file.write_text("# Small\n\nSmall document.\n" * 5)

    result = await pipeline.ingest_file(small_file, col_name, embedder=pipeline._global_embedder)

    assert result.status == "ok"
    assert result.code is None


@pytest.mark.asyncio
async def test_size_guard_over_limit_returns_error_result(connected_store, col_name, tmp_path):
    """File over max_file_mb returns IngestResult(status='error', code='file_too_large').

    Uses a 1 MB limit with a file >1 MB created via mock of os.path.getsize to
    avoid writing large files in tests (avoids 500 MB allocation, per learnings.md).
    """
    pipeline = make_pipeline_with_limit(connected_store, max_file_mb=1)  # 1 MB limit
    oversized = tmp_path / "oversized.md"
    oversized.write_bytes(b"x")

    two_mb = 2 * 1024 * 1024
    with _patch_getsize(return_value=two_mb):
        result = await pipeline.ingest_file(oversized, col_name, embedder=pipeline._global_embedder)

    assert result.status == "error"
    assert result.code == "file_too_large"
    assert result.chunks_created == 0
    assert result.error is not None
    assert "2 MB" in result.error
    assert "1 MB" in result.error
    assert "[ingest].max_file_mb" in result.error


@pytest.mark.asyncio
async def test_size_guard_exactly_at_limit_accepted(connected_store, col_name, tmp_path):
    """S5 — file exactly == max_file_mb bytes is accepted (strictly-greater-than boundary)."""
    pipeline = make_pipeline_with_limit(connected_store, max_file_mb=10)  # 10 MB limit
    exact_file = tmp_path / "exact.md"
    exact_file.write_bytes(b"x")

    # Exactly 10 MB in bytes
    ten_mb = 10 * 1024 * 1024
    with _patch_getsize(return_value=ten_mb):
        result = await pipeline.ingest_file(exact_file, col_name, embedder=pipeline._global_embedder)

    # Exactly at limit is NOT rejected (strictly greater-than)
    assert result.status == "ok"
    assert result.code is None


@pytest.mark.asyncio
async def test_size_guard_follows_symlinks(connected_store, col_name, tmp_path):
    """S6 — symlink to oversized file triggers the guard.

    Verifies two things:
    1. os.path.getsize is called with the symlink path (not pre-resolved) — the
       OS call itself follows symlinks, so the pipeline should pass the symlink as-is.
    2. The guard fires when getsize reports an oversized value via the symlink.
    """
    pipeline = make_pipeline_with_limit(connected_store, max_file_mb=1)  # 1 MB limit
    real_file = tmp_path / "real.md"
    real_file.write_bytes(b"x")
    symlink = tmp_path / "link.md"
    symlink.symlink_to(real_file)

    two_mb = 2 * 1024 * 1024
    with _patch_getsize(return_value=two_mb) as mock_getsize:
        result = await pipeline.ingest_file(symlink, col_name, embedder=pipeline._global_embedder)

    assert result.status == "error"
    assert result.code == "file_too_large"
    # Verify getsize was called with the symlink path (not pre-resolved to real_file)
    # so the OS-level symlink dereferencing happens naturally.
    mock_getsize.assert_called_with(symlink)



@pytest.mark.asyncio
async def test_size_guard_oserror_returns_error_result(connected_store, col_name, tmp_path):
    """OSError from os.path.getsize returns error IngestResult, never raises (soft-fail contract)."""
    pipeline = make_pipeline_with_limit(connected_store, max_file_mb=1)
    missing_file = tmp_path / "deleted.md"
    missing_file.write_bytes(b"x")

    with _patch_getsize(side_effect=OSError("file inaccessible")):
        result = await pipeline.ingest_file(missing_file, col_name, embedder=pipeline._global_embedder)

    assert isinstance(result, IngestResult)
    assert result.status == "error"
    assert result.code is None  # OSError path doesn't set code="file_too_large"
    assert "Cannot determine file size for" in result.error
    assert "deleted.md" in result.error


# ---------------------------------------------------------------------------
# Integration tests (real store + pipeline)
# ---------------------------------------------------------------------------


async def _make_real_pipeline_with_limit(tmp_path, monkeypatch, max_file_mb: int):
    """Create a real SearchStore + SearchPipeline with max_file_mb wired via constructor."""
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    store = SearchStore(str(tmp_path / "db"))
    await store.connect()

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        max_file_mb=max_file_mb,
    )
    return store, pipeline


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_guard_no_chunks_on_oversize(tmp_path, monkeypatch):
    """Oversized file returns error IngestResult and writes no chunks to store."""
    store, pipeline = await _make_real_pipeline_with_limit(tmp_path, monkeypatch, max_file_mb=1)

    oversized = tmp_path / "oversized.md"
    oversized.write_bytes(b"x")

    two_mb = 2 * 1024 * 1024
    with _patch_getsize(return_value=two_mb):
        result = await pipeline.ingest_file(oversized, "test-col", embedder=pipeline._global_embedder)

    assert result.status == "error"
    assert result.code == "file_too_large"
    assert result.chunks_created == 0

    count = await store.count_chunks("test-col", namespace="default")
    assert count == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_guard_directory_batch_continues(tmp_path, monkeypatch):
    """S10 — ingest_directory with mixed sizes: oversized file errors, under-limit file succeeds."""
    import hashlib

    store, pipeline = await _make_real_pipeline_with_limit(tmp_path, monkeypatch, max_file_mb=1)

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    small_file = corpus / "small.md"
    small_file.write_text("# Small doc\n\nThis is content for the small file.\n" * 10)
    big_file = corpus / "big.md"
    big_file.write_bytes(b"x")

    two_mb = 2 * 1024 * 1024
    one_kb = 1024

    def _fake_getsize(path):
        if str(path).endswith("big.md"):
            return two_mb
        return one_kb

    with _patch_getsize(side_effect=_fake_getsize):
        results = await pipeline.ingest_directory(
            corpus, "mixed-col", embedder=pipeline._global_embedder
        )

    assert len(results) == 2

    by_path = {r.doc_id: r for r in results}
    big_doc_id = hashlib.sha256(str(big_file.resolve()).encode()).hexdigest()
    small_doc_id = hashlib.sha256(str(small_file.resolve()).encode()).hexdigest()

    assert by_path[big_doc_id].status == "error"
    assert by_path[big_doc_id].code == "file_too_large"
    assert by_path[small_doc_id].status == "ok"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_guard_watcher_path_continues(tmp_path, monkeypatch):
    """S11 — watcher path: pipeline.ingest_file() with oversized file returns error, no exception."""
    store, pipeline = await _make_real_pipeline_with_limit(tmp_path, monkeypatch, max_file_mb=1)

    oversized = tmp_path / "big_watcher.md"
    oversized.write_bytes(b"x")

    two_mb = 2 * 1024 * 1024

    with _patch_getsize(return_value=two_mb):
        result = await pipeline.ingest_file(
            oversized, "watcher-col", embedder=pipeline._global_embedder
        )

    assert isinstance(result, IngestResult)
    assert result.status == "error"
    assert result.code == "file_too_large"

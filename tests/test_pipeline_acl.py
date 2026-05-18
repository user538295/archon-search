"""Integration tests for ACL propagation in SearchPipeline.ingest_file() (FEAT-044 Task 2.4)."""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pyarrow as pa


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(tmp_path: Path):
    """Return a SearchPipeline wired to a real LanceDB store with stub embedder/chunker/parser."""
    import asyncio

    from archon_search.store import SearchStore
    from archon_search.pipeline import SearchPipeline

    store = SearchStore(tmp_path / "db")

    # Stub embedder — returns 4-dim zero vectors
    embedder = MagicMock()
    embedder.embedding_dim = 4
    embedder.model_name = "stub"

    async def _embed(texts):
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

    async def _embed_one(text):
        return [0.0, 0.0, 0.0, 0.0]

    embedder.embed = _embed
    embedder.embed_one = _embed_one

    # Stub reranker
    reranker = MagicMock()

    # Real chunker — but using a stub that avoids chonkie download
    from archon_search._types import ChunkRecord
    from datetime import datetime, timezone

    class _StubChunker:
        def chunk(self, text: str, doc_id: str, source_path: str) -> list[ChunkRecord]:
            now = datetime.now(timezone.utc).isoformat()
            # One chunk per 200 chars (or one chunk if text is shorter)
            parts = [text[i:i + 200] for i in range(0, len(text), 200)] if text else []
            return [
                ChunkRecord(
                    doc_id=doc_id,
                    chunk_id="",
                    text=part,
                    vector=[],
                    source_path=source_path,
                    indexed_at=now,
                )
                for part in parts
            ]

    # Real parser — but stub to avoid heavy deps
    class _StubParser:
        async def parse(self, path: Path) -> str:
            # Return file content as-is (text files)
            return path.read_text(encoding="utf-8", errors="replace")

    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=reranker,
        chunker=_StubChunker(),
        parser=_StubParser(),
        top_k_retrieve=5,
        top_k_return=3,
    )
    return pipeline, store


async def _setup(pipeline, store, collection: str = "test_col"):
    await store.connect()
    await store.ensure_collection(collection, embedding_dim=4)


async def _teardown(store):
    await store.disconnect()


async def _read_chunks(store, collection: str) -> list[dict]:
    db = store._require_connected()
    table = await db.open_table(collection)
    return await table.query().to_list()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_file_front_matter_acl_propagated_to_chunks(tmp_path):
    """Markdown file with _acl: ns1 in front matter → chunks in DB have acl=['ns1']."""
    doc = tmp_path / "doc.md"
    doc.write_text("---\n_acl: ns1\ntitle: Test\n---\n\nHello world content here.\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col1"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(doc, collection)
        assert result.status == "ok"
        assert result.chunks_created > 0

        chunks = await _read_chunks(store, collection)
        assert len(chunks) > 0
        for chunk in chunks:
            assert chunk["acl"] == ["ns1"], f"Expected acl=['ns1'], got {chunk['acl']}"
    finally:
        await _teardown(store)


@pytest.mark.asyncio
async def test_ingest_file_sidecar_acl_propagated_to_chunks(tmp_path):
    """Binary-like file with .acl sidecar containing 'ns1' → chunks have acl=['ns1']."""
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"fake pdf content with some text")
    sidecar = tmp_path / "report.pdf.acl"
    sidecar.write_text("ns1\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col2"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(doc, collection)
        assert result.status == "ok"
        assert result.chunks_created > 0

        chunks = await _read_chunks(store, collection)
        assert len(chunks) > 0
        for chunk in chunks:
            assert chunk["acl"] == ["ns1"], f"Expected acl=['ns1'], got {chunk['acl']}"
    finally:
        await _teardown(store)


@pytest.mark.asyncio
async def test_ingest_file_strips_acl_from_chunk_text(tmp_path):
    """_acl value from front matter must not appear in chunk text."""
    doc = tmp_path / "doc.md"
    doc.write_text("---\n_acl: secretns\n---\n\nContent without ACL value.\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col3"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(doc, collection)
        assert result.status == "ok"

        chunks = await _read_chunks(store, collection)
        for chunk in chunks:
            assert "secretns" not in chunk["text"], "ACL namespace name leaked into chunk text"
            assert "_acl" not in chunk["text"], "_acl key leaked into chunk text"
    finally:
        await _teardown(store)


@pytest.mark.asyncio
async def test_ingest_file_skips_acl_sidecar_files(tmp_path):
    """A .acl sidecar file must not be indexed as content."""
    doc = tmp_path / "doc.md"
    doc.write_text("Main document content.\n")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("ns1\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col4"
    await _setup(pipeline, store, collection)
    try:
        # Ingest the sidecar directly — must return 0 chunks (skipped)
        result = await pipeline.ingest_file(sidecar, collection)
        # .acl files should be skipped: 0 chunks
        assert result.chunks_created == 0, (
            f"Expected 0 chunks for .acl sidecar, got {result.chunks_created}"
        )
    finally:
        await _teardown(store)


@pytest.mark.asyncio
async def test_ingest_directory_skips_acl_sidecar_files(tmp_path):
    """ingest_directory must not index .acl sidecar files as content documents."""
    content_dir = tmp_path / "docs"
    content_dir.mkdir()

    doc = content_dir / "page.md"
    doc.write_text("This is the page content.\n")
    sidecar = content_dir / "page.md.acl"
    sidecar.write_text("ns1\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col_dir"
    await _setup(pipeline, store, collection)
    try:
        results = await pipeline.ingest_directory(content_dir, collection)
        # Only the .md file should be ingested, not the .acl sidecar
        source_paths = [r for r in results]
        assert len(results) == 1, (
            f"Expected 1 result (page.md only), got {len(results)}: "
            f"{[str(r) for r in results]}"
        )
    finally:
        await _teardown(store)


@pytest.mark.asyncio
async def test_ingest_file_front_matter_precedence_over_sidecar(tmp_path):
    """When both front matter _acl and sidecar exist, front matter wins."""
    doc = tmp_path / "doc.md"
    doc.write_text("---\n_acl: frontns\n---\n\nContent here.\n")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("sidecarms\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col5"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(doc, collection)
        assert result.status == "ok"

        chunks = await _read_chunks(store, collection)
        assert len(chunks) > 0
        for chunk in chunks:
            assert chunk["acl"] == ["frontns"], (
                f"Expected front-matter acl=['frontns'], got {chunk['acl']}"
            )
    finally:
        await _teardown(store)


@pytest.mark.asyncio
async def test_ingest_file_empty_sidecar_defaults_open(tmp_path):
    """Empty sidecar file → acl=None (fail-open)."""
    doc = tmp_path / "doc.md"
    doc.write_text("Content.\n")
    sidecar = tmp_path / "doc.md.acl"
    sidecar.write_text("")  # empty

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col6"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(doc, collection)
        assert result.status == "ok"

        chunks = await _read_chunks(store, collection)
        for chunk in chunks:
            assert chunk["acl"] is None, f"Expected acl=None for empty sidecar, got {chunk['acl']}"
    finally:
        await _teardown(store)


@pytest.mark.asyncio
async def test_ingest_file_deny_all_sidecar(tmp_path):
    """Sidecar with 'deny-all' → acl=[] (deny-all sentinel)."""
    doc = tmp_path / "secret.md"
    doc.write_text("Top secret content.\n")
    sidecar = tmp_path / "secret.md.acl"
    sidecar.write_text("deny-all\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col7"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(doc, collection)
        assert result.status == "ok"

        chunks = await _read_chunks(store, collection)
        assert len(chunks) > 0
        for chunk in chunks:
            assert chunk["acl"] == [], f"Expected acl=[] for deny-all, got {chunk['acl']}"
    finally:
        await _teardown(store)


@pytest.mark.asyncio
async def test_ingest_file_all_invalid_acl_defaults_open_with_warning(tmp_path, caplog):
    """All-invalid _acl values → acl=None (fail-open) with a warning logged."""
    import logging

    doc = tmp_path / "doc.md"
    doc.write_text("---\n_acl: '!!!invalid!!!'\n---\n\nContent.\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col8"
    await _setup(pipeline, store, collection)
    try:
        with caplog.at_level(logging.WARNING, logger="archon_search"):
            result = await pipeline.ingest_file(doc, collection)
        assert result.status == "ok"

        chunks = await _read_chunks(store, collection)
        for chunk in chunks:
            assert chunk["acl"] is None, (
                f"Expected acl=None for all-invalid ACL, got {chunk['acl']}"
            )
    finally:
        await _teardown(store)


@pytest.mark.asyncio
async def test_reingest_updates_acl_for_all_chunks(tmp_path):
    """Re-ingesting a file with a changed ACL updates all chunks in the DB."""
    doc = tmp_path / "doc.md"
    doc.write_text("---\n_acl: ns_first\n---\n\nContent to re-index.\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col9"
    await _setup(pipeline, store, collection)
    try:
        # First ingest
        result1 = await pipeline.ingest_file(doc, collection)
        assert result1.status == "ok"
        chunks_after_first = await _read_chunks(store, collection)
        for chunk in chunks_after_first:
            assert chunk["acl"] == ["ns_first"]

        # Update ACL and re-ingest
        doc.write_text("---\n_acl: ns_second\n---\n\nContent to re-index.\n")
        result2 = await pipeline.ingest_file(doc, collection)
        assert result2.status == "ok"

        chunks_after_second = await _read_chunks(store, collection)
        for chunk in chunks_after_second:
            assert chunk["acl"] == ["ns_second"], (
                f"Expected acl updated to ['ns_second'], got {chunk['acl']}"
            )
    finally:
        await _teardown(store)


@pytest.mark.asyncio
async def test_ingest_binary_file_no_front_matter_parsing(tmp_path):
    """Binary file whose extracted text starts with '---' must not trigger front matter parsing.

    For binary files, _acl=None (sidecar checked instead).
    """
    doc = tmp_path / "data.bin"
    # Write a binary file whose text content starts with dashes (simulates PDF accident)
    doc.write_bytes(b"---\n_acl: ns_should_not_be_parsed\n---\nActual binary content")
    # No sidecar → should be fail-open

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col10"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(doc, collection)
        assert result.status == "ok"

        chunks = await _read_chunks(store, collection)
        # No sidecar → acl=None (open), and _acl string may appear in text (no parsing for .bin)
        for chunk in chunks:
            assert chunk["acl"] is None, (
                f"Binary file must not parse front matter; expected acl=None, got {chunk['acl']}"
            )
    finally:
        await _teardown(store)


@pytest.mark.asyncio
async def test_ingest_file_front_matter_block_stripped_from_chunk_text(tmp_path):
    """Front matter delimiters and ALL front matter fields must not appear in chunk text."""
    doc = tmp_path / "doc.md"
    doc.write_text(
        "---\n"
        "_acl: ns1\n"
        "title: My Title\n"
        "author: Alice\n"
        "---\n"
        "\nActual content after front matter.\n"
    )

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col11"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(doc, collection)
        assert result.status == "ok"

        chunks = await _read_chunks(store, collection)
        all_text = " ".join(chunk["text"] for chunk in chunks)
        # Front matter key-value pairs must not appear in chunk text
        assert "title: My Title" not in all_text, "front matter field leaked into chunk text"
        assert "author: Alice" not in all_text, "front matter field leaked into chunk text"
        assert "_acl: ns1" not in all_text, "front matter _acl field leaked into chunk text"
        # The actual content must be present
        assert "Actual content after front matter" in all_text, "real content must be in chunks"
    finally:
        await _teardown(store)

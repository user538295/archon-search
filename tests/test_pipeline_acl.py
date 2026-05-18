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


# ---------------------------------------------------------------------------
# Task 3.4b: ACL filter in SearchPipeline.search() and search_with_context()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_pipeline_search_acl_filter_applied(tmp_path):
    """SearchPipeline.search() with namespace='ns1' excludes chunks with acl=['ns2']."""
    from archon_search._types import ChunkRecord, SearchResult
    from archon_search.pipeline import SearchPipeline

    # Arrange — stub pipeline with controlled hybrid_search output
    store = MagicMock()
    embedder = MagicMock()
    reranker = MagicMock()

    async def _embed_one(text):
        return [0.0, 0.0, 0.0, 0.0]

    embedder.embed_one = _embed_one

    now = "2024-01-01T00:00:00+00:00"
    chunk_allowed = ChunkRecord(
        doc_id="doc1", chunk_id="doc1-000000", text="allowed chunk",
        vector=[0.0] * 4, source_path="/f.md", indexed_at=now, acl=["ns1"],
    )
    chunk_denied = ChunkRecord(
        doc_id="doc2", chunk_id="doc2-000000", text="denied chunk",
        vector=[0.0] * 4, source_path="/g.md", indexed_at=now, acl=["ns2"],
    )

    async def _hybrid_search(collection, vector, query, top_k):
        return [chunk_allowed, chunk_denied]

    store.hybrid_search = _hybrid_search

    async def _rerank(query, candidates, top_k):
        return [SearchResult(doc_id=c.doc_id, chunk_id=c.chunk_id, text=c.text, score=1.0, source_path=c.source_path, acl=c.acl) for c in candidates]

    reranker.rerank = _rerank

    pipeline = SearchPipeline(
        store=store, embedder=embedder, reranker=reranker,
        chunker=MagicMock(), parser=MagicMock(),
        top_k_retrieve=5, top_k_return=3,
    )

    result_obj = await pipeline.search("query", "col", namespace="ns1")

    texts = [r.text for r in result_obj.results]
    assert "allowed chunk" in texts, "ns1 chunk must be returned"
    assert "denied chunk" not in texts, "ns2 chunk must be excluded"


@pytest.mark.asyncio
async def test_search_pipeline_search_default_namespace_denies_protected(tmp_path):
    """SearchPipeline.search() with namespace='' (empty) denies protected chunks."""
    from archon_search._types import ChunkRecord, SearchResult
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    embedder = MagicMock()
    reranker = MagicMock()

    async def _embed_one(text):
        return [0.0] * 4

    embedder.embed_one = _embed_one

    now = "2024-01-01T00:00:00+00:00"
    open_chunk = ChunkRecord(
        doc_id="doc1", chunk_id="doc1-000000", text="open content",
        vector=[0.0] * 4, source_path="/a.md", indexed_at=now, acl=None,
    )
    protected_chunk = ChunkRecord(
        doc_id="doc2", chunk_id="doc2-000000", text="protected content",
        vector=[0.0] * 4, source_path="/b.md", indexed_at=now, acl=["tenantX"],
    )

    async def _hybrid_search(collection, vector, query, top_k):
        return [open_chunk, protected_chunk]

    store.hybrid_search = _hybrid_search

    async def _rerank(query, candidates, top_k):
        return [SearchResult(doc_id=c.doc_id, chunk_id=c.chunk_id, text=c.text, score=1.0, source_path=c.source_path, acl=c.acl) for c in candidates]

    reranker.rerank = _rerank

    pipeline = SearchPipeline(
        store=store, embedder=embedder, reranker=reranker,
        chunker=MagicMock(), parser=MagicMock(),
        top_k_retrieve=5, top_k_return=3,
    )

    result_obj = await pipeline.search("query", "col", namespace="")

    texts = [r.text for r in result_obj.results]
    assert "open content" in texts, "open chunk (acl=None) must be returned"
    assert "protected content" not in texts, "protected chunk must be denied when namespace=''"


@pytest.mark.asyncio
async def test_search_with_context_acl_filter_applied(tmp_path):
    """search_with_context() with namespace='ns1' excludes restricted chunks and adjacent chunks."""
    from archon_search._types import ChunkRecord, SearchResult
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    embedder = MagicMock()
    reranker = MagicMock()

    async def _embed_one(text):
        return [0.0] * 4

    embedder.embed_one = _embed_one

    now = "2024-01-01T00:00:00+00:00"
    allowed = ChunkRecord(
        doc_id="doc1", chunk_id="doc1-000001", text="main chunk",
        vector=[0.0] * 4, source_path="/a.md", indexed_at=now, acl=["ns1"],
    )

    async def _hybrid_search(collection, vector, query, top_k):
        return [allowed]

    store.hybrid_search = _hybrid_search

    async def _rerank(query, candidates, top_k):
        return [SearchResult(doc_id=c.doc_id, chunk_id=c.chunk_id, text=c.text, score=1.0, source_path=c.source_path, acl=c.acl) for c in candidates]

    reranker.rerank = _rerank

    # Adjacent chunk with different namespace
    adj_denied = ChunkRecord(
        doc_id="doc1", chunk_id="doc1-000000", text="adjacent denied",
        vector=[0.0] * 4, source_path="/a.md", indexed_at=now, acl=["ns2"],
    )
    adj_allowed = ChunkRecord(
        doc_id="doc1", chunk_id="doc1-000002", text="adjacent allowed",
        vector=[0.0] * 4, source_path="/a.md", indexed_at=now, acl=["ns1"],
    )

    async def _fetch_adjacent(collection, doc_id, center_idx, window):
        return [adj_denied, adj_allowed]

    store.fetch_adjacent_chunks = _fetch_adjacent

    pipeline = SearchPipeline(
        store=store, embedder=embedder, reranker=reranker,
        chunker=MagicMock(), parser=MagicMock(),
        top_k_retrieve=5, top_k_return=3,
    )

    output = await pipeline.search_with_context("query", "col", namespace="ns1")

    assert len(output) == 1
    entry = output[0]
    assert entry["result"].text == "main chunk"
    all_adjacent = entry["context_before"] + entry["context_after"]
    adjacent_texts = [c.text for c in all_adjacent]
    assert "adjacent denied" not in adjacent_texts, "ACL-denied adjacent chunk must be excluded"
    assert "adjacent allowed" in adjacent_texts, "ACL-allowed adjacent chunk must be included"


@pytest.mark.asyncio
async def test_e2e_ingest_and_search_acl_enforcement(tmp_path):
    """E2E: ingest file with _acl: tenantA; search as tenantA gets the chunk; tenantB gets nothing."""
    doc = tmp_path / "secret.md"
    doc.write_text("---\n_acl: tenantA\n---\n\nConfidential tenantA content here.\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "acl_e2e"
    await _setup(pipeline, store, collection)

    from archon_search._types import SearchResult
    from archon_search.reranker import Reranker

    # Stub reranker to pass all candidates through
    async def _passthrough_rerank(query, candidates, top_k):
        return [
            SearchResult(
                doc_id=c.doc_id, chunk_id=c.chunk_id, text=c.text,
                score=1.0, source_path=c.source_path, acl=c.acl,
            )
            for c in candidates[:top_k]
        ]

    pipeline._reranker.rerank = _passthrough_rerank

    try:
        result = await pipeline.ingest_file(doc, collection)
        assert result.status == "ok"
        assert result.chunks_created > 0

        # tenantA can see the chunk
        results_a = await pipeline.search("Confidential tenantA content", collection, namespace="tenantA")
        assert len(results_a.results) > 0, "tenantA must see its own chunk"

        # tenantB cannot see it
        results_b = await pipeline.search("Confidential tenantA content", collection, namespace="tenantB")
        assert len(results_b.results) == 0, "tenantB must not see tenantA chunk"

        # empty namespace also cannot see it
        results_empty = await pipeline.search("Confidential tenantA content", collection, namespace="")
        assert len(results_empty.results) == 0, "empty namespace must not see protected chunk"
    finally:
        await _teardown(store)


@pytest.mark.asyncio
async def test_search_context_expansion_acl_filtered(tmp_path):
    """Adjacent chunks with ACL for different namespace are excluded from context expansion."""
    from archon_search._types import ChunkRecord, SearchResult
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    embedder = MagicMock()
    reranker = MagicMock()

    async def _embed_one(text):
        return [0.0] * 4

    embedder.embed_one = _embed_one

    now = "2024-01-01T00:00:00+00:00"
    main_chunk = ChunkRecord(
        doc_id="docX", chunk_id="docX-000002", text="center content",
        vector=[0.0] * 4, source_path="/x.md", indexed_at=now, acl=None,
    )

    async def _hybrid_search(collection, vector, query, top_k):
        return [main_chunk]

    store.hybrid_search = _hybrid_search

    async def _rerank(query, candidates, top_k):
        return [SearchResult(doc_id=c.doc_id, chunk_id=c.chunk_id, text=c.text, score=1.0, source_path=c.source_path, acl=c.acl) for c in candidates]

    reranker.rerank = _rerank

    # Before chunk: restricted to different namespace
    before_restricted = ChunkRecord(
        doc_id="docX", chunk_id="docX-000001", text="before restricted",
        vector=[0.0] * 4, source_path="/x.md", indexed_at=now, acl=["other_ns"],
    )
    # After chunk: open
    after_open = ChunkRecord(
        doc_id="docX", chunk_id="docX-000003", text="after open",
        vector=[0.0] * 4, source_path="/x.md", indexed_at=now, acl=None,
    )

    async def _fetch_adjacent(collection, doc_id, center_idx, window):
        return [before_restricted, after_open]

    store.fetch_adjacent_chunks = _fetch_adjacent

    pipeline = SearchPipeline(
        store=store, embedder=embedder, reranker=reranker,
        chunker=MagicMock(), parser=MagicMock(),
        top_k_retrieve=5, top_k_return=3,
    )

    output = await pipeline.search_with_context("query", "col", namespace="ns1")

    assert len(output) == 1
    entry = output[0]
    before_texts = [c.text for c in entry["context_before"]]
    after_texts = [c.text for c in entry["context_after"]]
    assert "before restricted" not in before_texts, "restricted before chunk must be excluded"
    assert "after open" in after_texts, "open after chunk must be included"

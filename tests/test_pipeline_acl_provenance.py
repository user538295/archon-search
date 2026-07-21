"""Unit tests for ACL provenance propagation in SearchPipeline.ingest_file (BE-4).

Covers:
- acl_source set correctly for frontmatter, sidecar, and collection_default cases
- acl_sidecar_path relativized to collection_root or falling back to basename
- acl_warning propagated to every ChunkRecord
- _candidate_to_search_result copies all three provenance fields to SearchResult
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search._types import ChunkRecord, SearchResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_pipeline(tmp_path: Path):
    """Return a SearchPipeline wired to a real LanceDB store with stub embedder/chunker/parser."""
    from archon_search.store import SearchStore
    from archon_search.pipeline import SearchPipeline

    store = SearchStore(tmp_path / "db")

    embedder = MagicMock()
    embedder.embedding_dim = 4
    embedder.model_name = "stub"

    async def _embed(texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    async def _embed_one(text):
        return [0.1, 0.2, 0.3, 0.4]

    embedder.embed = _embed
    embedder.embed_one = _embed_one

    reranker = MagicMock()

    class _StubChunker:
        def chunk(
            self,
            text: str,
            doc_id: str,
            source_path: str,
            *,
            file_type: str = "",
            updated_at: str = "",
            ingested_by: str = "cli",
            language: str = "",
        ) -> list[ChunkRecord]:
            now = datetime.now(timezone.utc).isoformat()
            parts = [text[i : i + 200] for i in range(0, len(text), 200)] if text else []
            return [
                ChunkRecord(
                    doc_id=doc_id,
                    chunk_id="",
                    text=part,
                    vector=[],
                    source_path=source_path,
                    indexed_at=now,
                    file_type=file_type,
                    updated_at=updated_at,
                    ingested_by=ingested_by,  # type: ignore[arg-type]
                    language=language,
                )
                for part in parts
            ]

    class _StubParser:
        async def parse(self, path: Path) -> str:
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
# test_ingest_sets_frontmatter_source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_sets_frontmatter_source(tmp_path):
    """ChunkRecord carries acl_source='frontmatter' after ingesting a doc with _acl: front-matter."""
    doc = tmp_path / "doc.md"
    doc.write_text("---\n_acl: ns1\n---\n\nHello world content here.\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col_fm"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(doc, collection, embedder=pipeline._global_embedder)
        assert result.status == "ok"
        assert result.chunks_created > 0

        chunks = await _read_chunks(store, collection)
        assert chunks
        for chunk in chunks:
            assert chunk["acl_source"] == "frontmatter", (
                f"Expected acl_source='frontmatter', got {chunk['acl_source']!r}"
            )
            assert chunk["acl_sidecar_path"] is None, (
                f"Expected acl_sidecar_path=None for frontmatter, got {chunk['acl_sidecar_path']!r}"
            )
    finally:
        await _teardown(store)


# ---------------------------------------------------------------------------
# test_ingest_sets_sidecar_source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_sets_sidecar_source(tmp_path):
    """ChunkRecord carries acl_source='sidecar' and non-None acl_sidecar_path after ingesting a doc with a .acl sidecar."""
    doc = tmp_path / "report.txt"
    doc.write_text("Report content here.\n")
    sidecar = tmp_path / "report.txt.acl"
    sidecar.write_text("ns1\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col_sc"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(doc, collection, embedder=pipeline._global_embedder)
        assert result.status == "ok"
        assert result.chunks_created > 0

        chunks = await _read_chunks(store, collection)
        assert chunks
        for chunk in chunks:
            assert chunk["acl_source"] == "sidecar", (
                f"Expected acl_source='sidecar', got {chunk['acl_source']!r}"
            )
            assert chunk["acl_sidecar_path"] is not None, (
                "Expected acl_sidecar_path to be non-None for sidecar source"
            )
    finally:
        await _teardown(store)


# ---------------------------------------------------------------------------
# test_ingest_sets_collection_default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_sets_collection_default(tmp_path):
    """ChunkRecord carries acl_source='collection_default' when neither front-matter key nor sidecar file exists."""
    doc = tmp_path / "plain.md"
    doc.write_text("# Plain doc\n\nNo ACL configured at all.\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col_def"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(doc, collection, embedder=pipeline._global_embedder)
        assert result.status == "ok"
        assert result.chunks_created > 0

        chunks = await _read_chunks(store, collection)
        assert chunks
        for chunk in chunks:
            assert chunk["acl_source"] == "collection_default", (
                f"Expected acl_source='collection_default', got {chunk['acl_source']!r}"
            )
            assert chunk["acl_sidecar_path"] is None
    finally:
        await _teardown(store)


# ---------------------------------------------------------------------------
# test_sidecar_path_relative_to_collection_root
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sidecar_path_relative_to_collection_root_when_set(tmp_path):
    """When collection_root is set, acl_sidecar_path is relative (no leading '/') (S14 happy path)."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    doc = corpus / "doc.txt"
    doc.write_text("Document content.\n")
    sidecar = corpus / "doc.txt.acl"
    sidecar.write_text("ns1\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col_rel"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(
            doc,
            collection,
            embedder=pipeline._global_embedder,
            collection_root=corpus,
        )
        assert result.status == "ok"
        assert result.chunks_created > 0

        chunks = await _read_chunks(store, collection)
        assert chunks
        for chunk in chunks:
            assert chunk["acl_source"] == "sidecar"
            sidecar_path = chunk["acl_sidecar_path"]
            assert sidecar_path is not None
            # Must be relative (no leading /)
            assert not sidecar_path.startswith("/"), (
                f"acl_sidecar_path must be relative, got absolute: {sidecar_path!r}"
            )
            # Must be a relative path under corpus
            assert "/" in sidecar_path or sidecar_path == sidecar.name, (
                f"Expected a relative path, got {sidecar_path!r}"
            )
    finally:
        await _teardown(store)


@pytest.mark.asyncio
async def test_sidecar_path_basename_when_collection_root_is_none(tmp_path):
    """When collection_root=None, acl_sidecar_path is basename only, acl_warning contains truncation notice (S14)."""
    doc = tmp_path / "doc.txt"
    doc.write_text("Document content.\n")
    sidecar = tmp_path / "doc.txt.acl"
    sidecar.write_text("ns1\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col_bn"
    await _setup(pipeline, store, collection)
    try:
        # No collection_root → basename fallback path
        result = await pipeline.ingest_file(
            doc,
            collection,
            embedder=pipeline._global_embedder,
            collection_root=None,
        )
        assert result.status == "ok"
        assert result.chunks_created > 0

        chunks = await _read_chunks(store, collection)
        assert chunks
        for chunk in chunks:
            assert chunk["acl_source"] == "sidecar"
            sidecar_path = chunk["acl_sidecar_path"]
            assert sidecar_path is not None
            # Must be basename only — no path separator
            assert "/" not in sidecar_path, (
                f"acl_sidecar_path must be basename-only when collection_root=None, "
                f"got: {sidecar_path!r}"
            )
            assert sidecar_path == "doc.txt.acl", (
                f"Expected basename 'doc.txt.acl', got {sidecar_path!r}"
            )
            # acl_warning must contain the truncation notice
            warnings = chunk.get("acl_warning") or []
            assert any("truncated" in w.lower() for w in warnings), (
                f"Expected truncation notice in acl_warning, got: {warnings!r}"
            )
    finally:
        await _teardown(store)


# ---------------------------------------------------------------------------
# test_candidate_to_search_result_propagates_provenance
# ---------------------------------------------------------------------------


def test_candidate_to_search_result_propagates_provenance():
    """_candidate_to_search_result copies all three provenance fields from ScoredSearchCandidate to SearchResult."""
    from archon_search.pipeline import SearchPipeline

    # Build a minimal pipeline (methods under test are sync; store not needed)
    pipeline = SearchPipeline.__new__(SearchPipeline)

    score_breakdown = SearchScoreBreakdown(
        vector_rank=1,
        vector_score=0.9,
        vector_score_kind="cosine",
        fts_rank=2,
        fts_score=0.8,
        fts_score_kind="bm25",
        rrf_score=0.85,
        reranker_score=0.92,
    )
    candidate = ScoredSearchCandidate(
        doc_id="abc123",
        chunk_id="abc123-000001",
        text="some text",
        source_path="/some/path.md",
        score_breakdown=score_breakdown,
        collection="col1",
        acl=["ns1"],
        acl_source="sidecar",
        acl_sidecar_path="relative/path.md.acl",
        acl_warning=["some warning"],
    )

    result = pipeline._candidate_to_search_result(candidate)

    assert isinstance(result, SearchResult)
    assert result.acl_source == "sidecar"
    assert result.acl_sidecar_path == "relative/path.md.acl"
    assert result.acl_warning == ["some warning"]


def test_candidate_to_search_result_propagates_collection_default():
    """_candidate_to_search_result copies collection_default provenance correctly."""
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline.__new__(SearchPipeline)

    score_breakdown = SearchScoreBreakdown(
        vector_rank=1,
        vector_score=0.9,
        vector_score_kind="cosine",
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=0.8,
        reranker_score=None,
    )
    candidate = ScoredSearchCandidate(
        doc_id="def456",
        chunk_id="def456-000000",
        text="plain text",
        source_path="/plain/doc.md",
        score_breakdown=score_breakdown,
        collection="col2",
        acl=None,
        acl_source="collection_default",
        acl_sidecar_path=None,
        acl_warning=[],
    )

    result = pipeline._candidate_to_search_result(candidate)

    assert result.acl_source == "collection_default"
    assert result.acl_sidecar_path is None
    assert result.acl_warning == []


def test_candidate_to_search_result_propagates_null_provenance():
    """_candidate_to_search_result handles pre-G15 null provenance (acl_source=None) without error (S8)."""
    from archon_search.pipeline import SearchPipeline

    pipeline = SearchPipeline.__new__(SearchPipeline)

    score_breakdown = SearchScoreBreakdown(
        vector_rank=1,
        vector_score=0.7,
        vector_score_kind="cosine",
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=0.7,
        reranker_score=None,
    )
    candidate = ScoredSearchCandidate(
        doc_id="pre123",
        chunk_id="pre123-000000",
        text="pre-g15 text",
        source_path="/old/doc.md",
        score_breakdown=score_breakdown,
        collection="col_old",
        acl=None,
        acl_source=None,
        acl_sidecar_path=None,
        acl_warning=[],
    )

    result = pipeline._candidate_to_search_result(candidate)

    assert result.acl_source is None
    assert result.acl_sidecar_path is None
    assert result.acl_warning == []


# ---------------------------------------------------------------------------
# test_sidecar_path_basename_when_sidecar_outside_collection_root (S14 ValueError arm)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sidecar_path_basename_when_sidecar_outside_collection_root(tmp_path):
    """S14 ValueError arm: sidecar exists but is outside collection_root → basename fallback + truncation warning.

    collection_root is a real Path but the sidecar is located outside it,
    so Path.relative_to raises ValueError → basename-only fallback applies
    and the truncation notice is appended to acl_warning.
    """
    # Sidecar lives under tmp_path directly; collection_root points to a subdirectory
    doc = tmp_path / "doc.txt"
    doc.write_text("Document content.\n")
    sidecar = tmp_path / "doc.txt.acl"
    sidecar.write_text("ns1\n")

    # collection_root is a separate directory — sidecar is OUTSIDE it
    other_root = tmp_path / "other_root"
    other_root.mkdir()

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col_s14_ve"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(
            doc,
            collection,
            embedder=pipeline._global_embedder,
            collection_root=other_root,  # sidecar is outside this root → ValueError
        )
        assert result.status == "ok"
        assert result.chunks_created > 0

        chunks = await _read_chunks(store, collection)
        assert chunks
        for chunk in chunks:
            assert chunk["acl_source"] == "sidecar"
            sidecar_path = chunk["acl_sidecar_path"]
            assert sidecar_path is not None
            # Must be basename only (no path separator)
            assert "/" not in sidecar_path, (
                f"acl_sidecar_path must be basename-only on ValueError, got: {sidecar_path!r}"
            )
            assert sidecar_path == "doc.txt.acl", (
                f"Expected basename 'doc.txt.acl', got {sidecar_path!r}"
            )
            # Truncation notice must be present in acl_warning
            warnings = chunk.get("acl_warning") or []
            assert any("truncated" in w.lower() for w in warnings), (
                f"Expected truncation notice in acl_warning on ValueError arm, got: {warnings!r}"
            )
    finally:
        await _teardown(store)


# ---------------------------------------------------------------------------
# test_ingest_frontmatter_and_sidecar_shadow_warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_frontmatter_and_sidecar_shadow_warning(tmp_path):
    """FIX-5: when both front-matter _acl and a .acl sidecar coexist, frontmatter wins
    and a shadow warning is recorded in acl_warning."""
    # Create a doc with BOTH front-matter _acl AND a sidecar file
    doc = tmp_path / "shadowed.md"
    doc.write_text("---\n_acl: ns1\n---\n\nShadowed doc content.\n")
    sidecar = tmp_path / "shadowed.md.acl"
    sidecar.write_text("ns1\n")

    pipeline, store = _make_pipeline(tmp_path)
    collection = "col_shadow"
    await _setup(pipeline, store, collection)
    try:
        result = await pipeline.ingest_file(doc, collection, embedder=pipeline._global_embedder)
        assert result.status == "ok"
        assert result.chunks_created > 0

        chunks = await _read_chunks(store, collection)
        assert chunks
        for chunk in chunks:
            # Frontmatter takes precedence
            assert chunk["acl_source"] == "frontmatter", (
                f"Expected acl_source='frontmatter' (frontmatter wins over sidecar), "
                f"got {chunk['acl_source']!r}"
            )
            # Shadowed sidecar is not recorded in acl_sidecar_path
            assert chunk["acl_sidecar_path"] is None, (
                f"Expected acl_sidecar_path=None (sidecar shadowed by frontmatter), "
                f"got {chunk['acl_sidecar_path']!r}"
            )
            # Shadow warning must be present
            warnings = chunk.get("acl_warning") or []
            assert warnings, (
                "Expected at least one shadow warning in acl_warning when both "
                "front-matter and sidecar coexist, got empty list"
            )
            assert any("front-matter" in w.lower() or "sidecar" in w.lower() for w in warnings), (
                f"Expected shadow warning to mention 'front-matter' or 'sidecar', "
                f"got: {warnings!r}"
            )
    finally:
        await _teardown(store)

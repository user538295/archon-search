"""Integration tests for ACL provenance propagation (BE-4, G15).

Covers:
- S1: sidecar ingest → search → acl_sidecar_path is relative (not absolute)
- S8: pre-G15 chunk (no provenance columns) returns acl_source=None, no error
- S9: multi-collection search; each result carries its own provenance
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store_and_pipeline(tmp_path: Path):
    """Return (store, pipeline) backed by real LanceDB in tmp_path with stub ML backends."""
    from unittest.mock import MagicMock
    from datetime import datetime, timezone

    from archon_search._types import ChunkRecord
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


    async def _passthrough_rerank(query, candidates, top_k):
        # Return the first top_k candidates unchanged (no real reranking needed for these tests)
        return candidates[:top_k]

    reranker = MagicMock()
    reranker.rerank_candidates = _passthrough_rerank

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
        top_k_retrieve=10,
        top_k_return=5,
    )
    return store, pipeline


# ---------------------------------------------------------------------------
# S1: ingest sidecar doc → search → acl_sidecar_path is relative
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_sidecar_then_search_provenance_round_trip(tmp_path):
    """S1: ingest a doc with sidecar → search → SearchResult carries correct provenance;
    acl_sidecar_path is not an absolute path."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    doc = corpus / "article.txt"
    doc.write_text("This article discusses archon-search and its capabilities.\n")
    sidecar = corpus / "article.txt.acl"
    sidecar.write_text("ns1\n")

    store, pipeline = _make_store_and_pipeline(tmp_path)
    collection = "col_s1"
    await store.connect()
    await store.ensure_collection(collection, embedding_dim=4)
    try:
        ingest_result = await pipeline.ingest_file(
            doc,
            collection,
            embedder=pipeline._global_embedder,
            collection_root=corpus,
        )
        assert ingest_result.status == "ok"
        assert ingest_result.chunks_created > 0

        from archon_search.filters import SearchFilters
        search_result = await pipeline.search(
            "archon-search",
            collection,
            namespace="ns1",
            embedder=pipeline._global_embedder,
            filters=SearchFilters(),
        )
        results = search_result.results

        assert results, "Expected at least one search result after sidecar ingest"
        for r in results:
            assert r.acl_source == "sidecar", (
                f"Expected acl_source='sidecar', got {r.acl_source!r}"
            )
            assert r.acl_sidecar_path is not None
            # Must not be an absolute path
            assert not r.acl_sidecar_path.startswith("/"), (
                f"acl_sidecar_path must not be absolute, got: {r.acl_sidecar_path!r}"
            )
            assert r.acl_sidecar_path != str(sidecar), (
                f"acl_sidecar_path must not equal the absolute filesystem path, "
                f"got: {r.acl_sidecar_path!r}"
            )
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# S8: pre-G15 chunk with no provenance columns → acl_source=None, no error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pre_g15_chunk_source_null(tmp_path):
    """S8: a chunk written with null provenance columns has acl_source=None and acl_warning=None in the DB row.

    The constant-vector stub embedder means search results may be empty due to ANN tie-breaking.
    This test therefore reads the row directly from the chunk table, then verifies the converter
    handles null provenance correctly by calling _candidate_to_search_result directly.
    """
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown

    store, pipeline = _make_store_and_pipeline(tmp_path)
    collection = "col_s8"
    await store.connect()
    await store.ensure_collection(collection, embedding_dim=4)
    try:
        db = store._require_connected()
        table = await db.open_table(collection)

        # Write a row directly that simulates a pre-G15 chunk (all provenance columns null).
        # The schema already has these columns as nullable — null values simulate the
        # pre-G15 state. We use the exact schema fields from SearchStore._schema().
        import hashlib
        doc_id = hashlib.sha256(b"/old/doc.md").hexdigest()
        chunk_id = f"{doc_id}-000000"
        old_row = {
            "doc_id": doc_id,
            "chunk_id": chunk_id,
            "text": "Old pre-G15 chunk content about archon-search.",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "source_path": "/old/doc.md",
            "indexed_at": "2024-01-01T00:00:00.000000Z",
            "file_type": "md",
            "language": "",
            "metadata": "{}",
            "custom_score": None,
            "ingested_by": "cli",
            "updated_at": "2024-01-01T00:00:00.000000Z",
            "acl": None,
            "expires_at": None,
            "scopes": None,
            # Provenance columns set to null — simulates pre-G15 rows
            "acl_source": None,
            "acl_sidecar_path": None,
            "acl_warning": None,
        }
        await table.add([old_row])

        # Read the row directly from the table — non-vacuous assertion that the nullable
        # provenance columns are correctly stored as null and the table is readable without error.
        rows = await table.query().to_list()
        assert rows, "Expected the written row to be retrievable from the chunk table"
        row = next((r for r in rows if r.get("chunk_id") == chunk_id), None)
        assert row is not None, f"Could not find chunk_id={chunk_id!r} in table rows"
        assert row["acl_source"] is None, (
            f"Pre-G15 chunk should have acl_source=None in the DB row, got {row['acl_source']!r}"
        )
        assert row["acl_sidecar_path"] is None, (
            f"Pre-G15 chunk should have acl_sidecar_path=None in the DB row, got {row['acl_sidecar_path']!r}"
        )
        # acl_warning is stored as null (list<utf8> column, pre-G15 rows have no data)
        assert row["acl_warning"] is None, (
            f"Pre-G15 chunk should have acl_warning=None in the DB row, got {row['acl_warning']!r}"
        )

        # Verify the converter handles null provenance correctly without going through the
        # full search path (which is unreliable with constant-vector stubs).
        candidate = ScoredSearchCandidate(
            doc_id=doc_id,
            chunk_id=chunk_id,
            text=old_row["text"],
            source_path=old_row["source_path"],
            score_breakdown=SearchScoreBreakdown(
                vector_rank=1,
                vector_score=0.9,
                vector_score_kind="cosine",
                fts_rank=None,
                fts_score=None,
                fts_score_kind=None,
                rrf_score=0.9,
                reranker_score=None,
            ),
            collection=collection,
            acl=None,
            acl_source=None,        # null — simulates pre-G15
            acl_sidecar_path=None,
            acl_warning=None,       # null — simulates pre-G15
        )
        result = pipeline._candidate_to_search_result(candidate)
        assert result.acl_source is None, (
            f"Pre-G15 chunk should have acl_source=None on search result, got {result.acl_source!r}"
        )
        assert result.acl_warning == [], (
            f"Pre-G15 chunk should have empty acl_warning on search result (null→[]), got {result.acl_warning!r}"
        )
    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# S9: multi-collection search — each result carries its own provenance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multi_collection_each_result_has_own_gate(tmp_path):
    """S9: search_many across 3 collections; merged results each carry their own provenance."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    # Use distinct keywords per doc so FTS can differentiate despite constant vectors.
    # Doc A: sidecar ACL
    doc_a = corpus / "a.txt"
    doc_a.write_text("Sidecar retrieval alpha zeta unique content for archon-search.\n")
    sidecar_a = corpus / "a.txt.acl"
    sidecar_a.write_text("ns1\n")

    # Doc B: front-matter ACL
    doc_b = corpus / "b.md"
    doc_b.write_text("---\n_acl: ns1\n---\n\nFrontmatter retrieval beta gamma unique content for archon-search.\n")

    # Doc C: no ACL (collection_default)
    doc_c = corpus / "c.txt"
    doc_c.write_text("Collection-default retrieval delta omega unique content for archon-search.\n")

    store, pipeline = _make_store_and_pipeline(tmp_path)
    col_a = "col_s9a"
    col_b = "col_s9b"
    col_c = "col_s9c"

    await store.connect()
    await store.ensure_collection(col_a, embedding_dim=4)
    await store.ensure_collection(col_b, embedding_dim=4)
    await store.ensure_collection(col_c, embedding_dim=4)
    try:
        r_a = await pipeline.ingest_file(doc_a, col_a, embedder=pipeline._global_embedder, collection_root=corpus)
        r_b = await pipeline.ingest_file(doc_b, col_b, embedder=pipeline._global_embedder)
        r_c = await pipeline.ingest_file(doc_c, col_c, embedder=pipeline._global_embedder)

        assert r_a.status == "ok" and r_a.chunks_created > 0
        assert r_b.status == "ok" and r_b.chunks_created > 0
        assert r_c.status == "ok" and r_c.chunks_created > 0

        # S9: use search_many to exercise the multi-collection merge path.
        # With constant-vector stubs, results from all 3 collections are merged into one pool.
        # Collections are ingested into the default namespace (ingest_file default), so
        # search_many must also use the default namespace to find them.
        from archon_search.constants import DEFAULT_NAMESPACE
        search_result = await pipeline.search_many(
            "archon-search",
            collections=[col_a, col_b, col_c],
            namespace=DEFAULT_NAMESPACE,
        )
        results = search_result.results
        assert results, "search_many: expected at least one merged result"

        # Each result must carry non-null provenance — the merge path must propagate it.
        for r in results:
            assert r.acl_source is not None, (
                f"search_many result from {r.collection!r} has acl_source=None; "
                "provenance must propagate through the multi-collection merge path"
            )
            assert r.acl_warning is not None, (
                f"search_many result from {r.collection!r} has acl_warning=None; "
                "must be [] or a list, never None"
            )

        # Verify per-collection provenance where we can identify the source collection.
        sources_by_col: dict[str, set[str]] = {}
        for r in results:
            sources_by_col.setdefault(r.collection, set()).add(r.acl_source)

        if col_a in sources_by_col:
            assert "sidecar" in sources_by_col[col_a], (
                f"col_a results should have acl_source='sidecar', got {sources_by_col[col_a]!r}"
            )
        if col_b in sources_by_col:
            assert "frontmatter" in sources_by_col[col_b], (
                f"col_b results should have acl_source='frontmatter', got {sources_by_col[col_b]!r}"
            )
        if col_c in sources_by_col:
            assert "collection_default" in sources_by_col[col_c], (
                f"col_c results should have acl_source='collection_default', got {sources_by_col[col_c]!r}"
            )

    finally:
        await store.disconnect()


# ---------------------------------------------------------------------------
# BE-6 integration tests: fail-open warnings surfaced through full pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_search_acl_gate_warnings_surfaced_for_invalid_frontmatter(tmp_path):
    """S4e: ingest a doc with invalid-type _acl (bool), search with acl_context=true;
    acl_gate.warnings must be non-empty."""
    from archon_search.filters import SearchFilters

    doc = tmp_path / "invalid_fm.md"
    # bool value for _acl is an invalid type — triggers fail-open + warning
    doc.write_text("---\n_acl: true\n---\n\nContent about archon-search.\n")

    store, pipeline = _make_store_and_pipeline(tmp_path)
    collection = "col_be6_fm"
    await store.connect()
    await store.ensure_collection(collection, embedding_dim=4)
    try:
        ingest_result = await pipeline.ingest_file(
            doc,
            collection,
            embedder=pipeline._global_embedder,
        )
        assert ingest_result.status == "ok"
        assert ingest_result.chunks_created > 0

        search_result = await pipeline.search(
            "archon-search",
            collection,
            namespace="default",
            embedder=pipeline._global_embedder,
            filters=SearchFilters(),
        )
        results = search_result.results
        assert results, "Expected search results after ingest"

        # Every result should carry non-empty warnings because _acl=True is invalid
        for r in results:
            assert r.acl_source == "frontmatter", (
                f"Expected acl_source='frontmatter' for frontmatter _acl=True, got {r.acl_source!r}"
            )
            assert r.acl_warning, (
                f"Expected non-empty acl_warning for invalid frontmatter _acl=True, "
                f"got {r.acl_warning!r}"
            )
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_search_acl_gate_warnings_surfaced_for_symlink_sidecar(tmp_path):
    """S4b: ingest a doc with a symlinked sidecar; acl_gate.warnings must be non-empty."""
    from archon_search.filters import SearchFilters

    corpus = tmp_path / "corpus"
    corpus.mkdir()

    doc = corpus / "symlinked.txt"
    doc.write_text("Content about archon-search and symlinks.\n")

    # Create a real file, then a symlink sidecar pointing to it
    real_acl = corpus / "real.acl"
    real_acl.write_text("ns1\n")
    sidecar = corpus / "symlinked.txt.acl"
    sidecar.symlink_to(real_acl)

    store, pipeline = _make_store_and_pipeline(tmp_path)
    collection = "col_be6_sym"
    await store.connect()
    await store.ensure_collection(collection, embedding_dim=4)
    try:
        ingest_result = await pipeline.ingest_file(
            doc,
            collection,
            embedder=pipeline._global_embedder,
        )
        assert ingest_result.status == "ok"
        assert ingest_result.chunks_created > 0

        search_result = await pipeline.search(
            "archon-search",
            collection,
            namespace="default",
            embedder=pipeline._global_embedder,
            filters=SearchFilters(),
        )
        results = search_result.results
        assert results, "Expected search results after ingest"

        for r in results:
            assert r.acl_source == "sidecar", (
                f"Expected acl_source='sidecar' for symlinked sidecar, got {r.acl_source!r}"
            )
            assert r.acl_warning, (
                f"Expected non-empty acl_warning for symlinked sidecar, got {r.acl_warning!r}"
            )
    finally:
        await store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_search_acl_gate_warnings_deny_all_mixed_invalid(tmp_path):
    """S4d: ingest a doc whose sidecar has deny-all mixed with invalid entries;
    acl_gate.warnings must be non-empty (fail-open branch)."""
    from archon_search.filters import SearchFilters

    doc = tmp_path / "deny_mixed.txt"
    doc.write_text("Content about archon-search and deny-all.\n")

    # Sidecar: invalid name first (not the deny-all sentinel position), then deny-all as a
    # non-sentinel entry → both are rejected by is_acl_namespace_valid, all entries invalid
    # → fail-open (None) with warnings (S4d).
    # Note: "deny-all" on the FIRST line would be treated as the deny-all sentinel (acl=[]),
    # so we place !!!bad!!! first so the sentinel check is bypassed and all lines go through
    # the is_acl_namespace_valid loop where deny-all is rejected like any invalid name.
    sidecar = tmp_path / "deny_mixed.txt.acl"
    sidecar.write_text("!!!bad!!!\ndeny-all\n")

    store, pipeline = _make_store_and_pipeline(tmp_path)
    collection = "col_be6_deny"
    await store.connect()
    await store.ensure_collection(collection, embedding_dim=4)
    try:
        ingest_result = await pipeline.ingest_file(
            doc,
            collection,
            embedder=pipeline._global_embedder,
        )
        assert ingest_result.status == "ok"
        assert ingest_result.chunks_created > 0

        search_result = await pipeline.search(
            "archon-search",
            collection,
            namespace="default",
            embedder=pipeline._global_embedder,
            filters=SearchFilters(),
        )
        results = search_result.results
        assert results, "Expected search results — deny+invalid is fail-open, so chunk is accessible"

        for r in results:
            assert r.acl_source == "sidecar", (
                f"Expected acl_source='sidecar', got {r.acl_source!r}"
            )
            assert r.acl_warning, (
                f"Expected non-empty acl_warning for deny-all mixed with invalid entries, "
                f"got {r.acl_warning!r}"
            )
    finally:
        await store.disconnect()

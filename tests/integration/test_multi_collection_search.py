"""B3 Task 8.1 — integration tests for multi-collection search against a REAL LanceDB store.

These exercise ``SearchPipeline.search_many`` (and ``search``) end-to-end with a real,
connected ``SearchStore`` and deterministic mock embedder/reranker backends so the
fan-out, merge, ACL, and rerank seams run against real LanceDB vector + FTS storage.

Run with:
    uv run pytest -m integration tests/integration/test_multi_collection_search.py --no-cov -q
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from archon_search._types import ChunkRecord
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.embedder import Embedder
from archon_search.pipeline import SearchPipeline
from archon_search.reranker import Reranker

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Deterministic backends (mirrors tests/test_pipeline_explain.py)
# ---------------------------------------------------------------------------


class MockEmbedderBackend:
    """Returns fixed dim=4 vectors for all texts."""

    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class DistinctTextRerankerBackend:
    """Returns a distinct, text-deterministic score per candidate text."""

    is_warm: bool = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [
            int(hashlib.sha256(t.encode()).hexdigest(), 16) % 100000 / 100000
            for _, t in pairs
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(store) -> SearchPipeline:  # type: ignore[no-untyped-def]
    return SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
        max_fanout=8,
        fanout_leg_trim=40,
        fanout_timeout_seconds=30.0,
    )


def _make_doc_id(tag: str) -> str:
    """Deterministic 64-hex doc_id from a tag."""
    return hashlib.sha256(f"doc-{tag}".encode()).hexdigest()


def _chunk(doc_id: str, idx: int, text: str, *, dim: int = 4) -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[float(idx + 1)] * dim,
        source_path=f"/tmp/{doc_id[:8]}.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )


def _records(tag: str, n: int) -> list[ChunkRecord]:
    """n ChunkRecords for one doc, sharing query terms so FTS matches."""
    doc_id = _make_doc_id(tag)
    return [
        _chunk(doc_id, i, f"common query terms {tag} unique{i}")
        for i in range(n)
    ]


async def _ingest(
    pipeline: SearchPipeline,
    col: str,
    records: list[ChunkRecord],
    *,
    with_fts: bool = True,
) -> None:
    """Ensure collection, ingest chunks, optionally build FTS, then set CollectionMeta.

    ``recompute_collection_meta`` persists ``embedding_model=pipeline embedder.model_name``
    ("mock-embedder") and ``namespace="default"`` so ``search_many`` won't exclude the
    collection on a model mismatch.
    """
    store = pipeline.store
    await store.ensure_collection(col, 4)
    await store.ingest_chunks(col, records)
    if with_fts:
        await store.rebuild_fts_index(col)
    await pipeline.recompute_collection_meta(col)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_collection_fanout_returns_merged_results_with_provenance(tmp_path):
    """Two-collection fan-out merges results, tags provenance, and reports leg timings."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        pipeline = _make_pipeline(store)
        await _ingest(pipeline, "alpha", _records("alpha", 6))
        await _ingest(pipeline, "beta", _records("beta", 6))

        result = await pipeline.search_many("common query terms", ["alpha", "beta"])

        assert result.results, "expected non-empty merged results"
        assert all(r.collection in {"alpha", "beta"} for r in result.results)
        # Provenance: results from BOTH collections appear.
        seen = {r.collection for r in result.results}
        assert seen == {"alpha", "beta"}, f"expected both collections, got {seen}"

        # Fan-out timings present with a leg entry per queried collection.
        assert result.fanout_timings is not None
        assert set(result.fanout_timings.leg_times) == {"alpha", "beta"}
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_three_collection_fanout_one_without_fts_degrades_gracefully(tmp_path):
    """A leg whose collection lacks an FTS index falls back to vector-only without raising."""
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        pipeline = _make_pipeline(store)
        await _ingest(pipeline, "a", _records("a", 5))
        await _ingest(pipeline, "b", _records("b", 5))
        # Collection "c": skip rebuild_fts_index → FTS absent → vector-only fallback.
        await _ingest(pipeline, "c", _records("c", 5), with_fts=False)

        result = await pipeline.search_many("common query terms", ["a", "b", "c"])

        assert result.results, "expected merged results across all three legs"
        assert all(r.collection in {"a", "b", "c"} for r in result.results)
        # Graceful degradation: the no-FTS leg ("c") still contributes vector-only
        # candidates — its provenance must appear in the merged results.
        seen = {r.collection for r in result.results}
        assert "c" in seen, f"no-FTS leg 'c' contributed no results; got {seen}"
        assert result.fanout_timings is not None
        assert set(result.fanout_timings.leg_times) == {"a", "b", "c"}
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_model_mismatch_collection_excluded_and_not_searched(tmp_path):
    """A collection whose embedding_model differs from the embedder is excluded
    (reported in excluded_collections) and contributes no results."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        pipeline = _make_pipeline(store)
        await _ingest(pipeline, "match", _records("match", 5))
        # Ingest "mismatch", then overwrite its meta with a different embedding_model.
        await _ingest(pipeline, "mismatch", _records("mismatch", 5))
        bad_meta = await store.get_collection_meta("mismatch", namespace=DEFAULT_NAMESPACE)
        assert bad_meta is not None
        await store.update_collection_meta(
            CollectionMeta(
                name="mismatch",
                centroid=bad_meta.centroid,
                description=bad_meta.description,
                doc_count=bad_meta.doc_count,
                chunk_count=bad_meta.chunk_count,
                active_embedding_model="some-other-model",
                namespace=DEFAULT_NAMESPACE,
            )
        )

        result = await pipeline.search_many("common query terms", ["match", "mismatch"])

        assert any(
            e.name == "mismatch" and e.reason == "embedding_model_mismatch"
            for e in result.excluded_collections
        ), result.excluded_collections
        # The excluded collection contributes nothing to the merged results.
        assert all(r.collection != "mismatch" for r in result.results)
        # ...and its leg was never searched (no leg timing recorded for it).
        if result.fanout_timings is not None:
            assert "mismatch" not in result.fanout_timings.leg_times
    finally:
        await store.disconnect()


@pytest.mark.asyncio
async def test_single_item_collections_matches_single_collection_field_subset(tmp_path):
    """Single vs multi over one collection agree on all pre-B3 fields for shared chunks.

    Equality property asserted:
      For the INTERSECTION of chunk_ids returned by ``search("q", "x")`` and
      ``search_many("q", ["x"])``, every pre-B3 ``SearchResult`` field
      (doc_id, chunk_id, text, source_path, file_type, language, indexed_at,
      updated_at, ingested_by, metadata, acl) is IDENTICAL between the two paths,
      and each shared result carries collection == "x". We also assert the
      intersection is non-empty. We deliberately do NOT assert set/order equality
      of the two result lists, because single() uses hybrid_search (top_k_retrieve
      candidates) while search_many() uses hybrid_search_with_trace + per-leg trim,
      so the candidate pools and final ordering can legitimately differ.
    """
    from archon_search.store import SearchStore

    store = SearchStore(tmp_path / "db")
    await store.connect()
    try:
        pipeline = _make_pipeline(store)
        await _ingest(pipeline, "x", _records("x", 6))

        r_single = await pipeline.search("common query terms", "x")
        r_multi = await pipeline.search_many("common query terms", ["x"])

        single_by_chunk = {r.chunk_id: r for r in r_single.results}
        multi_by_chunk = {r.chunk_id: r for r in r_multi.results}

        shared = set(single_by_chunk) & set(multi_by_chunk)
        assert shared, "expected a non-empty intersection of returned chunk_ids"

        pre_b3_fields = (
            "doc_id",
            "chunk_id",
            "text",
            "source_path",
            "file_type",
            "language",
            "indexed_at",
            "updated_at",
            "ingested_by",
            "metadata",
            "acl",
        )
        for chunk_id in shared:
            s = single_by_chunk[chunk_id]
            m = multi_by_chunk[chunk_id]
            for fld in pre_b3_fields:
                assert getattr(s, fld) == getattr(m, fld), (
                    f"field {fld!r} differs for chunk {chunk_id}: "
                    f"single={getattr(s, fld)!r} multi={getattr(m, fld)!r}"
                )
            # Multi-collection path tags provenance; single() leaves it unset ("").
            assert m.collection == "x"
            assert s.collection in ("", "x")
    finally:
        await store.disconnect()

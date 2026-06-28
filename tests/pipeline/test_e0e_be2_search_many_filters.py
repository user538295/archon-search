"""tests/pipeline/test_e0e_be2_search_many_filters.py

BE-2 unit tests: `filters` parameter on search_many() / _fanout_merge_acl().

Covers:
- filters threaded to every hybrid_search_with_trace call site
- glob post-filter per-leg in _fanout_merge_acl
- glob post-filter in RAG Fusion multi-collection merge step
- GLOB_OVERFETCH_FACTOR headroom applied before fanout
- explain() multi-collection unaffected (regression guard)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search.filters import SearchFilters
from archon_search.store_filters import GLOB_OVERFETCH_FACTOR

from .conftest import make_embedder, make_reranker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cand(
    chunk_id: str,
    *,
    collection: str = "col",
    source_path: str = "/path/to/doc.md",
    doc_id: str = "a" * 64,
) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text="text",
        source_path=source_path,
        score_breakdown=SearchScoreBreakdown(
            vector_rank=0,
            vector_score=0.9,
            vector_score_kind="distance",
            fts_rank=None,
            fts_score=None,
            fts_score_kind=None,
            rrf_score=0.5,
            reranker_score=None,
        ),
        collection=collection,
    )


def _meta(name: str, *, active_embedding_model: str = "mock-embedder", namespace: str = "default"):
    from archon_search.collection_meta import CollectionMeta

    return CollectionMeta(name=name, active_embedding_model=active_embedding_model, namespace=namespace)


def _make_pipeline(
    *,
    leg_map: dict | None = None,
    meta_list: list | None = None,
    has_vector_index_map: dict[str, bool] | None = None,
    top_k_retrieve: int = 10,
    top_k_return: int = 5,
    fanout_leg_trim: int = 40,
):
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline

    store = MagicMock()
    leg_map = leg_map or {}
    has_vector_index_map = has_vector_index_map or {}

    async def _hybrid(collection, vector, query_text, candidate_depth, filters=None):
        return list(leg_map.get(collection, []))

    store.hybrid_search_with_trace = AsyncMock(side_effect=_hybrid)
    store.hybrid_search = AsyncMock(return_value=[])
    store.has_vector_index = AsyncMock(
        side_effect=lambda coll: has_vector_index_map.get(coll, True)
    )

    embedder = make_embedder()
    embedder.embed_one = AsyncMock(return_value=[0.1] * 4)  # type: ignore[method-assign]

    pipeline = SearchPipeline(
        store=store,
        embedder=embedder,
        reranker=make_reranker(),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=top_k_retrieve,
        top_k_return=top_k_return,
        fanout_leg_trim=fanout_leg_trim,
    )
    if meta_list is not None:
        pipeline.get_all_collections_meta = AsyncMock(return_value=meta_list)  # type: ignore[method-assign]
    return pipeline, store, embedder


# ---------------------------------------------------------------------------
# Test 1: filters threaded to every leg (standard path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_many_passes_filters_to_each_leg() -> None:
    """hybrid_search_with_trace must be called with the filters kwarg on every leg."""
    leg_map = {
        "A": [_cand(f"{'a' * 64}-000000", collection="A")],
        "B": [_cand(f"{'b' * 64}-000000", collection="B")],
    }
    pipeline, store, _ = _make_pipeline(
        leg_map=leg_map, meta_list=[_meta("A"), _meta("B")]
    )
    filters = SearchFilters(file_type="md")

    await pipeline.search_many("q", ["A", "B"], filters=filters)

    # All legs must receive filters=
    for c in store.hybrid_search_with_trace.call_args_list:
        kwargs = c.kwargs
        assert "filters" in kwargs, "filters kwarg missing on some leg call"
        assert kwargs["filters"] is filters


# ---------------------------------------------------------------------------
# Test 2: no filters → None propagated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_many_no_filters_passes_none() -> None:
    """When filters=None (default), store is called with filters=None."""
    leg_map = {"A": [_cand(f"{'a' * 64}-000000", collection="A")]}
    pipeline, store, _ = _make_pipeline(leg_map=leg_map, meta_list=[_meta("A")])

    await pipeline.search_many("q", ["A"])

    for c in store.hybrid_search_with_trace.call_args_list:
        kwargs = c.kwargs
        assert kwargs.get("filters") is None


# ---------------------------------------------------------------------------
# Test 3: RAG Fusion path — vector-index collections receive filters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_many_rag_fusion_passes_filters_to_each_leg() -> None:
    """With rag_fusion=True, all per-collection variant calls must receive filters=."""
    from archon_search.config import RAGFusionConfig

    leg_map = {
        "A": [_cand(f"{'a' * 64}-000000", collection="A")],
        "B": [_cand(f"{'b' * 64}-000000", collection="B")],
    }
    pipeline, store, _ = _make_pipeline(
        leg_map=leg_map,
        meta_list=[_meta("A"), _meta("B")],
        has_vector_index_map={"A": True, "B": True},
    )
    filters = SearchFilters(language="fr")

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["variant1"])
    rag_config = RAGFusionConfig(enabled=True, num_queries=1)

    await pipeline.search_many(
        "q", ["A", "B"],
        filters=filters,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # 2 collections × 2 queries (original + variant) = 4 calls; each must carry filters
    for c in store.hybrid_search_with_trace.call_args_list:
        assert c.kwargs.get("filters") is filters, (
            f"Call missing filters: {c}"
        )


# ---------------------------------------------------------------------------
# Test 4: RAG Fusion FTS-only collection receives filters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_many_rag_fusion_fts_only_collection_receives_filters() -> None:
    """FTS-only collection (has_vector_index=False) uses single-query path; must still receive filters."""
    from archon_search.config import RAGFusionConfig

    leg_map = {"Fts": [_cand(f"{'f' * 64}-000000", collection="Fts")]}
    pipeline, store, _ = _make_pipeline(
        leg_map=leg_map,
        meta_list=[_meta("Fts")],
        has_vector_index_map={"Fts": False},
    )
    filters = SearchFilters(file_type="py")

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["v1"])
    rag_config = RAGFusionConfig(enabled=True, num_queries=1)

    await pipeline.search_many(
        "q", ["Fts"],
        filters=filters,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # Exactly one call (FTS-only, original query only)
    assert store.hybrid_search_with_trace.call_count == 1
    assert store.hybrid_search_with_trace.call_args.kwargs.get("filters") is filters


# ---------------------------------------------------------------------------
# Test 5: explain() multi-collection unaffected (regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explain_multi_collection_unaffected_by_filters_param() -> None:
    """explain() multi-collection path calls _fanout_merge_acl with filters=None;
    no AttributeError, and results are returned."""
    leg_map = {
        "X": [_cand(f"{'x' * 64}-000000", collection="X")],
        "Y": [_cand(f"{'y' * 64}-000000", collection="Y")],
    }
    pipeline, store, _ = _make_pipeline(
        leg_map=leg_map, meta_list=[_meta("X"), _meta("Y")]
    )

    result = await pipeline.explain("q", collections=["X", "Y"])

    assert result is not None
    # No glob filter should be applied (no source_path_glob in filters=None default)
    assert store.hybrid_search_with_trace.call_count > 0


# ---------------------------------------------------------------------------
# Test 6: glob post-filter removes non-matching paths per-leg (standard path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_many_glob_post_filter_removes_non_matching_per_leg() -> None:
    """Candidates with source_path not matching the glob are removed before merging."""
    md_cand = _cand(f"{'a' * 64}-000000", collection="A", source_path="/docs/guide.md")
    pdf_cand = _cand(f"{'a' * 64}-000001", collection="A", source_path="/docs/guide.pdf")

    leg_map = {"A": [md_cand, pdf_cand]}
    pipeline, _, _ = _make_pipeline(
        leg_map=leg_map, meta_list=[_meta("A")]
    )
    filters = SearchFilters(source_path_glob="*.md")

    result = await pipeline.search_many("q", ["A"], filters=filters)

    source_paths = {r.source_path for r in result.results}
    assert "/docs/guide.md" in source_paths
    assert "/docs/guide.pdf" not in source_paths


# ---------------------------------------------------------------------------
# Test 7: RAG Fusion — glob post-filter applied after multi-collection merge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_many_rag_fusion_glob_post_filter_applied_after_fusion() -> None:
    """Glob filter must run per-collection BEFORE the per-leg trim.

    Setup: fanout_leg_trim=2, 3 candidates per collection.  The two non-matching
    candidates (*.pdf) rank higher than the one matching candidate (*.md) so that
    with the old post-merge filter the pdf files would consume both trim slots and
    the md file would be silently dropped.  With the correct pre-trim filter the
    pdf files are removed first, leaving only the md file which always passes trim.
    """
    from archon_search.config import RAGFusionConfig

    # Per-collection candidate order: pdf1 (rank 0, highest RRF), pdf2 (rank 1),
    # md (rank 2, lowest RRF).  With fanout_leg_trim=2 and old code, md is lost.
    pdf1_a = _cand(f"{'a' * 64}-000000", collection="A", source_path="/a/doc1.pdf")
    pdf2_a = _cand(f"{'a' * 64}-000001", collection="A", source_path="/a/doc2.pdf")
    md_a   = _cand(f"{'a' * 64}-000002", collection="A", source_path="/a/doc.md")

    pdf1_b = _cand(f"{'b' * 64}-000000", collection="B", source_path="/b/doc1.pdf")
    pdf2_b = _cand(f"{'b' * 64}-000001", collection="B", source_path="/b/doc2.pdf")
    md_b   = _cand(f"{'b' * 64}-000002", collection="B", source_path="/b/doc.md")

    leg_map = {"A": [pdf1_a, pdf2_a, md_a], "B": [pdf1_b, pdf2_b, md_b]}
    pipeline, _, _ = _make_pipeline(
        leg_map=leg_map,
        meta_list=[_meta("A"), _meta("B")],
        has_vector_index_map={"A": True, "B": True},
        top_k_return=20,
        fanout_leg_trim=2,  # tight trim: only 2 slots, non-matching must not consume them
    )
    filters = SearchFilters(source_path_glob="*.md")

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=[])
    rag_config = RAGFusionConfig(enabled=True, num_queries=1)

    result = await pipeline.search_many(
        "q", ["A", "B"],
        filters=filters,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    source_paths = {r.source_path for r in result.results}
    # Matching candidates must survive despite being ranked below non-matching ones.
    assert "/a/doc.md" in source_paths, "md from collection A was lost (trim slot wastage)"
    assert "/b/doc.md" in source_paths, "md from collection B was lost (trim slot wastage)"
    # Non-matching candidates must never appear.
    assert "/a/doc1.pdf" not in source_paths
    assert "/a/doc2.pdf" not in source_paths
    assert "/b/doc1.pdf" not in source_paths
    assert "/b/doc2.pdf" not in source_paths


# ---------------------------------------------------------------------------
# Test 8: GLOB_OVERFETCH_FACTOR applied before fanout (standard path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_many_glob_candidate_depth_uses_overfetch_factor() -> None:
    """When filters.source_path_glob is set, candidate_depth >= top_k * GLOB_OVERFETCH_FACTOR."""
    leg_map = {"A": [_cand(f"{'a' * 64}-000000", collection="A")]}
    top_k_retrieve = 10
    pipeline, store, _ = _make_pipeline(
        leg_map=leg_map,
        meta_list=[_meta("A")],
        top_k_retrieve=top_k_retrieve,
    )
    filters = SearchFilters(source_path_glob="*/docs/*.md")

    await pipeline.search_many("q", ["A"], filters=filters)

    call = store.hybrid_search_with_trace.call_args
    actual_depth = call.kwargs.get("candidate_depth") or call.args[3]
    expected_min = top_k_retrieve * GLOB_OVERFETCH_FACTOR
    assert actual_depth >= expected_min, (
        f"candidate_depth {actual_depth} < expected minimum {expected_min}"
    )


# ---------------------------------------------------------------------------
# Test 9: GLOB_OVERFETCH_FACTOR applied in RAG Fusion path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_many_rag_fusion_glob_candidate_depth_uses_overfetch_factor() -> None:
    """With rag_fusion=True and source_path_glob, candidate_depth >= top_k * GLOB_OVERFETCH_FACTOR."""
    from archon_search.config import RAGFusionConfig

    top_k_retrieve = 10
    leg_map = {"A": [_cand(f"{'a' * 64}-000000", collection="A")]}
    pipeline, store, _ = _make_pipeline(
        leg_map=leg_map,
        meta_list=[_meta("A")],
        has_vector_index_map={"A": True},
        top_k_retrieve=top_k_retrieve,
    )
    filters = SearchFilters(source_path_glob="*.py")

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=[])
    rag_config = RAGFusionConfig(enabled=True, num_queries=1)

    await pipeline.search_many(
        "q", ["A"],
        filters=filters,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # All calls (even with empty variants) use increased candidate_depth
    for c in store.hybrid_search_with_trace.call_args_list:
        actual_depth = c.kwargs.get("candidate_depth") or c.args[3]
        expected_min = top_k_retrieve * GLOB_OVERFETCH_FACTOR
        assert actual_depth >= expected_min, (
            f"RAG Fusion call candidate_depth {actual_depth} < {expected_min}"
        )


# ---------------------------------------------------------------------------
# Test 10: RAG Fusion embedding-failure fallback receives filters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_many_rag_fusion_embedding_fallback_receives_filters() -> None:
    """When embedding fails in RAG Fusion path, fallback _fanout_merge_acl gets filters."""
    from archon_search.config import RAGFusionConfig

    leg_map = {"A": [_cand(f"{'a' * 64}-000000", collection="A")]}
    pipeline, store, embedder = _make_pipeline(
        leg_map=leg_map, meta_list=[_meta("A")]
    )
    filters = SearchFilters(file_type="md")

    # Make only the gather-batch calls fail; the subsequent single-query fallback must succeed.
    # The RAG Fusion path embeds [query, variant1] via asyncio.gather — making the 2nd call
    # (the variant) fail is enough to trigger the embedding-failure fallback branch.
    # The fallback then calls embed_one(query) once more (call_count=3), which must succeed.
    call_count = 0

    async def failing_embed(q: str) -> list[float]:
        nonlocal call_count
        call_count += 1
        if call_count == 2:  # fail only the variant embed, not the fallback single-query embed
            raise RuntimeError("embedding failed")
        return [0.1] * 4

    embedder.embed_one = AsyncMock(side_effect=failing_embed)  # type: ignore[method-assign]

    mock_generator = MagicMock()
    mock_generator.generate_variants = AsyncMock(return_value=["v1"])
    rag_config = RAGFusionConfig(enabled=True, num_queries=1)

    await pipeline.search_many(
        "q", ["A"],
        filters=filters,
        rag_fusion=True,
        rag_fusion_generator=mock_generator,
        rag_fusion_config=rag_config,
    )

    # Fallback fanout must still pass filters
    for c in store.hybrid_search_with_trace.call_args_list:
        assert c.kwargs.get("filters") is filters

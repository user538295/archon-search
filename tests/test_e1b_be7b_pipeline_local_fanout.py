"""Unit tests for BE-7b: search_many() local-mode fanout.

Tests:
- Per-collection isolation: 2 collections each with their own community (no cross-merge)
- Mixed match: collection A has community match; collection B has isolated nodes → B falls back to hybrid
- All-stale chunk IDs on one leg → that leg falls back to hybrid; other leg unaffected
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.config import GraphConfig
from archon_search.graph_types import Community, EntityType, GraphNode
from archon_search.pipeline import SearchPipelineResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    store,
    *,
    graph_store=None,
    graph_config=None,
    graph_expander=None,
):
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 4 for _ in texts]

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    return SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        graph_store=graph_store,
        graph_config=graph_config,
        graph_expander=graph_expander,
    )


def _make_community(
    community_id: str = "comm-1",
    chunk_ids: list[str] | None = None,
    entity_ids: list[str] | None = None,
) -> Community:
    return Community(
        community_id=community_id,
        entity_ids=entity_ids or ["entity-1"],
        representative_chunk_ids=chunk_ids if chunk_ids is not None else ["chunk-1"],
        built_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        summary_text=None,
    )


def _make_graph_node(
    node_id: str = "entity-1",
    entity_name: str = "AuthService",
    collection: str = "col-a",
) -> GraphNode:
    return GraphNode(
        id=node_id,
        entity_name=entity_name,
        entity_type=EntityType.system,
        source_doc_id="doc-1",
        collection_name=collection,
    )


def _make_raw_row(
    chunk_id: str = "chunk-1",
    text: str = "some text",
    source_path: str = "/doc.txt",
    doc_id: str = "doc-1",
    acl: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "source_path": source_path,
        "text": text,
        "doc_id": doc_id,
        "namespace": "default",
        "chunk_index": 0,
        "total_chunks": 1,
        "language": "en",
        "acl": acl,
        "metadata": "{}",
        "ingested_by": "cli",
        "file_type": "txt",
        "indexed_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
    }


def _make_scored_candidate(chunk_id: str = "chunk-1", collection: str = "col-a"):
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown

    return ScoredSearchCandidate(
        chunk_id=chunk_id,
        source_path="/doc.txt",
        text="text",
        doc_id="doc-1",
        collection=collection,
        language="en",
        acl=None,
        metadata={},
        score_breakdown=SearchScoreBreakdown(
            vector_rank=None,
            vector_score=None,
            vector_score_kind=None,
            fts_rank=None,
            fts_score=None,
            fts_score_kind=None,
            rrf_score=0.5,
            reranker_score=None,
        ),
        ingested_by="cli",
        file_type="txt",
        indexed_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
    )


def _patch_collections_meta(pipeline, col_names: list[str]):
    """Return a context manager that patches get_all_collections_meta."""
    from archon_search.collection_meta import CollectionMeta

    metas = []
    for name in col_names:
        m = MagicMock(spec=CollectionMeta)
        m.name = name
        m.active_embedding_model = "mock-embedder"
        metas.append(m)

    return patch.object(
        pipeline,
        "get_all_collections_meta",
        new=AsyncMock(return_value=metas),
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_many_local_mode_per_collection_isolation(connected_store, col_name):
    """2 collections with different communities; local mode returns per-collection communities
    without cross-collection merge."""
    col_a = col_name
    col_b = f"{col_name}-b"

    # Collection A: entity "AuthService" in community A, chunk "chunk-a1"
    # Collection B: entity "PaymentService" in community B, chunk "chunk-b1"

    communities_a = [_make_community("comm-a", ["chunk-a1"], ["entity-a1"])]
    communities_b = [_make_community("comm-b", ["chunk-b1"], ["entity-b1"])]

    node_a = _make_graph_node("entity-a1", "AuthService", col_a)
    node_b = _make_graph_node("entity-b1", "PaymentService", col_b)

    rows_a = [_make_raw_row("chunk-a1")]
    rows_b = [_make_raw_row("chunk-b1")]

    graph_store = MagicMock()
    graph_store.find_nodes_by_name = AsyncMock(
        side_effect=lambda coll, ngrams, ns="default": [node_a] if coll == col_a else [node_b]
    )
    graph_store.communities_table_exists = AsyncMock(return_value=True)
    graph_store.get_communities_for_entities = AsyncMock(
        side_effect=lambda coll, eids, ns="default": communities_a if coll == col_a else communities_b
    )

    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(
        connected_store,
        graph_store=graph_store,
        graph_config=graph_config,
    )

    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(
        side_effect=lambda coll, ids: rows_a if coll == col_a else rows_b
    )
    # hybrid_search_with_trace: return empty (community chunks dominate)
    pipeline.store.hybrid_search_with_trace = AsyncMock(return_value=[])
    pipeline._reranker.rerank_candidates = AsyncMock(
        side_effect=lambda q, cands, top_k: cands[:top_k]
    )

    with _patch_collections_meta(pipeline, [col_a, col_b]):
        with patch(
            "archon_search.pipeline.tokenize_and_generate_ngrams",
            return_value=["authservice", "paymentservice"],
        ):
            result = await pipeline.search_many(
                "AuthService PaymentService",
                [col_a, col_b],
                graph_mode="local",
            )

    assert isinstance(result, SearchPipelineResult)
    assert result.graph_expansion_applied is True
    # Both community chunks should be present
    chunk_ids = {r.chunk_id for r in result.results}
    assert "chunk-a1" in chunk_ids, f"col_a community chunk missing; got {chunk_ids}"
    assert "chunk-b1" in chunk_ids, f"col_b community chunk missing; got {chunk_ids}"

    # Verify that community lookup was called per-collection (not cross-merged)
    assert graph_store.get_communities_for_entities.await_count == 2
    call_colls = {call.args[0] for call in graph_store.get_communities_for_entities.call_args_list}
    assert col_a in call_colls
    assert col_b in call_colls

    # Verify get_chunks_by_ids was called with correct chunk IDs per collection
    chunks_calls = {call.args[0]: call.args[1] for call in pipeline.store.get_chunks_by_ids.call_args_list}
    assert col_a in chunks_calls, f"get_chunks_by_ids not called for {col_a}"
    assert col_b in chunks_calls, f"get_chunks_by_ids not called for {col_b}"
    assert "chunk-a1" in chunks_calls[col_a], f"chunk-a1 not fetched for {col_a}"
    assert "chunk-b1" in chunks_calls[col_b], f"chunk-b1 not fetched for {col_b}"


@pytest.mark.asyncio
async def test_search_many_local_mixed_match(connected_store, col_name):
    """Collection A has community match; collection B has no community (isolated nodes).
    Collection A returns community result; collection B falls back to hybrid for that leg."""
    col_a = col_name
    col_b = f"{col_name}-b"

    node_a = _make_graph_node("entity-a1", "AuthService", col_a)
    node_b = _make_graph_node("entity-b1", "AuthService", col_b)
    communities_a = [_make_community("comm-a", ["chunk-a1"], ["entity-a1"])]

    rows_a = [_make_raw_row("chunk-a1")]
    hybrid_rows_b = [_make_scored_candidate("chunk-b-hybrid", col_b)]

    graph_store = MagicMock()
    graph_store.find_nodes_by_name = AsyncMock(
        side_effect=lambda coll, ngrams, ns="default": [node_a] if coll == col_a else [node_b]
    )
    graph_store.communities_table_exists = AsyncMock(return_value=True)
    graph_store.get_communities_for_entities = AsyncMock(
        side_effect=lambda coll, eids, ns="default": communities_a if coll == col_a else []
    )

    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(
        connected_store,
        graph_store=graph_store,
        graph_config=graph_config,
    )

    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(
        side_effect=lambda coll, ids: rows_a if coll == col_a else []
    )
    pipeline.store.hybrid_search_with_trace = AsyncMock(
        side_effect=lambda coll, vec, q, **kw: hybrid_rows_b if coll == col_b else []
    )
    pipeline._reranker.rerank_candidates = AsyncMock(
        side_effect=lambda q, cands, top_k: cands[:top_k]
    )

    with _patch_collections_meta(pipeline, [col_a, col_b]):
        with patch(
            "archon_search.pipeline.tokenize_and_generate_ngrams",
            return_value=["authservice"],
        ):
            result = await pipeline.search_many(
                "AuthService query",
                [col_a, col_b],
                graph_mode="local",
            )

    assert isinstance(result, SearchPipelineResult)
    # Collection A produced community chunks; overall graph_expansion_applied=True
    assert result.graph_expansion_applied is True

    chunk_ids = {r.chunk_id for r in result.results}
    # Community chunk from collection A should be present
    assert "chunk-a1" in chunk_ids
    # Hybrid chunk from collection B should also be present
    assert "chunk-b-hybrid" in chunk_ids


@pytest.mark.asyncio
async def test_search_many_local_one_leg_all_stale_falls_back(connected_store, col_name):
    """Collection A leg has community match but all-stale chunk IDs → falls back to hybrid.
    Collection B leg has a normal community match. No exception raised."""
    col_a = col_name
    col_b = f"{col_name}-b"

    node_a = _make_graph_node("entity-a1", "AuthService", col_a)
    node_b = _make_graph_node("entity-b1", "PaymentService", col_b)
    communities_a = [_make_community("comm-a", ["chunk-a-stale"], ["entity-a1"])]
    communities_b = [_make_community("comm-b", ["chunk-b1"], ["entity-b1"])]

    rows_b = [_make_raw_row("chunk-b1")]
    hybrid_rows_a = [_make_scored_candidate("chunk-a-hybrid", col_a)]

    graph_store = MagicMock()
    graph_store.find_nodes_by_name = AsyncMock(
        side_effect=lambda coll, ngrams, ns="default": [node_a] if coll == col_a else [node_b]
    )
    graph_store.communities_table_exists = AsyncMock(return_value=True)
    graph_store.get_communities_for_entities = AsyncMock(
        side_effect=lambda coll, eids, ns="default": communities_a if coll == col_a else communities_b
    )

    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(
        connected_store,
        graph_store=graph_store,
        graph_config=graph_config,
    )

    pipeline.store = MagicMock()
    # Collection A chunk IDs all stale (empty); collection B returns chunks normally
    pipeline.store.get_chunks_by_ids = AsyncMock(
        side_effect=lambda coll, ids: [] if coll == col_a else rows_b
    )
    # Hybrid search provides fallback for collection A
    pipeline.store.hybrid_search_with_trace = AsyncMock(
        side_effect=lambda coll, vec, q, **kw: hybrid_rows_a if coll == col_a else []
    )
    pipeline._reranker.rerank_candidates = AsyncMock(
        side_effect=lambda q, cands, top_k: cands[:top_k]
    )

    with _patch_collections_meta(pipeline, [col_a, col_b]):
        with patch(
            "archon_search.pipeline.tokenize_and_generate_ngrams",
            return_value=["authservice", "paymentservice"],
        ):
            result = await pipeline.search_many(
                "AuthService PaymentService",
                [col_a, col_b],
                graph_mode="local",
            )

    assert isinstance(result, SearchPipelineResult)
    # No exception raised
    chunk_ids = {r.chunk_id for r in result.results}
    # Collection B community chunk should be present
    assert "chunk-b1" in chunk_ids
    # Collection A hybrid fallback should be present
    assert "chunk-a-hybrid" in chunk_ids
    # graph_expansion_applied=True because collection B had community match
    assert result.graph_expansion_applied is True


@pytest.mark.asyncio
async def test_search_many_local_graph_store_none_falls_through(connected_store, col_name):
    """When graph_store is None, local mode silently falls back to standard search."""
    # Build pipeline without graph_store
    pipeline = _make_pipeline(connected_store, graph_store=None)
    hybrid_cands = [_make_scored_candidate("chunk-std", col_name)]
    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=[])
    pipeline.store.hybrid_search_with_trace = AsyncMock(return_value=hybrid_cands)
    pipeline._reranker.rerank_candidates = AsyncMock(side_effect=lambda q, cands, top_k: cands[:top_k])
    # Need embed_one for standard path
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1] * 4)

    with _patch_collections_meta(pipeline, [col_name]):
        result = await pipeline.search_many("some query", [col_name], graph_mode="local")

    assert isinstance(result, SearchPipelineResult)
    # graph_expansion_applied must be False (standard path was used)
    assert result.graph_expansion_applied is False


@pytest.mark.asyncio
async def test_search_many_local_no_entities_all_legs_hybrid(connected_store, col_name):
    """When no graph entities match in any collection, all legs fall back to hybrid; graph_expansion_applied=False."""
    col_a = col_name
    col_b = f"{col_name}-b"
    hybrid_cands_a = [_make_scored_candidate("chunk-std-a", col_a)]
    hybrid_cands_b = [_make_scored_candidate("chunk-std-b", col_b)]

    graph_store = MagicMock()
    graph_store.find_nodes_by_name = AsyncMock(return_value=[])  # no entities matched
    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(connected_store, graph_store=graph_store, graph_config=graph_config)
    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=[])
    pipeline.store.hybrid_search_with_trace = AsyncMock(
        side_effect=lambda coll, vec, q, **kw: hybrid_cands_a if coll == col_a else hybrid_cands_b
    )
    pipeline._reranker.rerank_candidates = AsyncMock(side_effect=lambda q, cands, top_k: cands[:top_k])

    with _patch_collections_meta(pipeline, [col_a, col_b]):
        with patch("archon_search.pipeline.tokenize_and_generate_ngrams", return_value=["some", "entity"]):
            result = await pipeline.search_many("some entity", [col_a, col_b], graph_mode="local")

    assert isinstance(result, SearchPipelineResult)
    assert result.graph_expansion_applied is False, "graph_expansion_applied must be False when no entities matched"
    # Both hybrid results should appear
    chunk_ids = {r.chunk_id for r in result.results}
    assert "chunk-std-a" in chunk_ids, f"col_a hybrid chunk missing; got {chunk_ids}"
    assert "chunk-std-b" in chunk_ids, f"col_b hybrid chunk missing; got {chunk_ids}"

"""Unit and integration tests for BE-5: SearchPipeline graph_mode dispatch + global mode.

Tests:
- Global mode calls list_community_representatives and get_chunks_by_ids
- GraphCommunitiesNotBuiltError raised when no communities exist
- graph_expansion_applied=True in global mode result
- max_global_candidates cap enforced
- Stale chunk IDs silently skipped (partial return)
- All-stale fallback to _search_standard
- ACL filtering for cross-namespace
- All-ACL-filtered fallback to _search_standard
- Naive mode routed through _search_graph_mode dispatch (regression guard)
- Naive + RAG Fusion uses ORIGINAL (unexpanded) query for variants
- search_many global mode calls list_community_representatives per collection
- Integration: real store + real GraphStore with community data
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search.config import GraphConfig
from archon_search.graph_types import Community
from archon_search.pipeline import GraphCommunitiesNotBuiltError, SearchPipelineResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    store,
    *,
    graph_store=None,
    graph_config=None,
    graph_expander=None,
    graph_extractor=None,
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
        graph_extractor=graph_extractor,
    )


def _make_community(
    community_id: str = "comm-1",
    chunk_ids: list[str] | None = None,
) -> Community:
    return Community(
        community_id=community_id,
        entity_ids=["entity-1"],
        representative_chunk_ids=chunk_ids or ["chunk-1"],
        built_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        summary_text=None,
    )


def _make_raw_row(
    chunk_id: str = "chunk-1",
    text: str = "some text",
    source_path: str = "/doc.txt",
    doc_id: str = "doc-1",
    acl: list[str] | None = None,
) -> dict[str, Any]:
    """Return a raw LanceDB-style chunk row dict."""
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


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_pipeline_global_mode_calls_community_store(connected_store, col_name):
    """graph_mode='global': list_community_representatives called; result returned."""
    communities = [
        _make_community("comm-1", ["chunk-1"]),
        _make_community("comm-2", ["chunk-2"]),
    ]
    rows = [_make_raw_row("chunk-1"), _make_raw_row("chunk-2")]

    graph_store = MagicMock()
    graph_store.list_community_representatives = AsyncMock(return_value=communities)
    graph_config = GraphConfig(enabled=True, max_global_candidates=100)

    pipeline = _make_pipeline(
        connected_store,
        graph_store=graph_store,
        graph_config=graph_config,
    )
    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=rows)
    pipeline._reranker.rerank_candidates = AsyncMock(
        side_effect=lambda q, cands, top_k: cands[:top_k]
    )

    result = await pipeline.search(
        "test query",
        col_name,
        embedder=pipeline._global_embedder,
        graph_mode="global",
    )

    graph_store.list_community_representatives.assert_awaited_once_with(col_name)
    pipeline.store.get_chunks_by_ids.assert_awaited_once()
    assert isinstance(result, SearchPipelineResult)


@pytest.mark.asyncio
async def test_search_pipeline_global_no_communities_raises(connected_store, col_name):
    """graph_mode='global': empty communities → GraphCommunitiesNotBuiltError."""
    graph_store = MagicMock()
    graph_store.list_community_representatives = AsyncMock(return_value=[])
    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(
        connected_store,
        graph_store=graph_store,
        graph_config=graph_config,
    )

    with pytest.raises(GraphCommunitiesNotBuiltError) as exc_info:
        await pipeline.search(
            "test query",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode="global",
        )

    assert exc_info.value.collection == col_name


@pytest.mark.asyncio
async def test_search_pipeline_result_graph_expansion_applied_true(connected_store, col_name):
    """graph_mode='global': result.graph_expansion_applied is True."""
    communities = [_make_community("comm-1", ["chunk-1"])]
    rows = [_make_raw_row("chunk-1")]

    graph_store = MagicMock()
    graph_store.list_community_representatives = AsyncMock(return_value=communities)
    graph_config = GraphConfig(enabled=True, max_global_candidates=100)

    pipeline = _make_pipeline(
        connected_store,
        graph_store=graph_store,
        graph_config=graph_config,
    )
    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=rows)
    pipeline._reranker.rerank_candidates = AsyncMock(
        side_effect=lambda q, cands, top_k: cands[:top_k]
    )

    result = await pipeline.search(
        "test query",
        col_name,
        embedder=pipeline._global_embedder,
        graph_mode="global",
    )

    assert result.graph_expansion_applied is True


@pytest.mark.asyncio
async def test_max_global_candidates_cap_enforced(connected_store, col_name):
    """max_global_candidates=10 caps chunk_ids sent to get_chunks_by_ids."""
    # 4 communities × 50 chunks each = 200 total; cap at 10
    communities = [
        _make_community(f"comm-{i}", [f"chunk-{i}-{j}" for j in range(50)])
        for i in range(4)
    ]
    rows = [_make_raw_row(f"chunk-0-{j}") for j in range(10)]

    graph_store = MagicMock()
    graph_store.list_community_representatives = AsyncMock(return_value=communities)
    graph_config = GraphConfig(enabled=True, max_global_candidates=10)

    pipeline = _make_pipeline(
        connected_store,
        graph_store=graph_store,
        graph_config=graph_config,
    )
    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=rows)
    pipeline._reranker.rerank_candidates = AsyncMock(
        side_effect=lambda q, cands, top_k: cands[:top_k]
    )

    await pipeline.search(
        "test query",
        col_name,
        embedder=pipeline._global_embedder,
        graph_mode="global",
    )

    # get_chunks_by_ids must be called with exactly 10 IDs, not 200
    call_args = pipeline.store.get_chunks_by_ids.call_args
    # positional args: (collection, chunk_ids)
    chunk_ids_passed = call_args[0][1]
    assert len(chunk_ids_passed) == 10


@pytest.mark.asyncio
async def test_stale_chunk_ids_silently_skipped(connected_store, col_name):
    """3 chunk IDs in communities; store returns only 2 rows; reranker called with 2."""
    communities = [_make_community("comm-1", ["chunk-1", "chunk-2", "chunk-3"])]
    rows = [_make_raw_row("chunk-1"), _make_raw_row("chunk-2")]  # chunk-3 missing

    graph_store = MagicMock()
    graph_store.list_community_representatives = AsyncMock(return_value=communities)
    graph_config = GraphConfig(enabled=True, max_global_candidates=100)

    pipeline = _make_pipeline(
        connected_store,
        graph_store=graph_store,
        graph_config=graph_config,
    )
    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=rows)

    rerank_calls: list = []

    async def _capture_rerank(query, cands, top_k):
        rerank_calls.append(list(cands))
        return cands[:top_k]

    pipeline._reranker.rerank_candidates = _capture_rerank

    await pipeline.search(
        "test query",
        col_name,
        embedder=pipeline._global_embedder,
        graph_mode="global",
    )

    assert len(rerank_calls) == 1
    assert len(rerank_calls[0]) == 2


@pytest.mark.asyncio
async def test_all_stale_chunk_ids_falls_back_to_hybrid(connected_store, col_name):
    """get_chunks_by_ids returns [] → _search_standard called; graph_expansion_applied=False."""
    communities = [_make_community("comm-1", ["chunk-stale"])]

    graph_store = MagicMock()
    graph_store.list_community_representatives = AsyncMock(return_value=communities)
    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(
        connected_store,
        graph_store=graph_store,
        graph_config=graph_config,
    )
    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=[])

    dummy_result = SearchPipelineResult(results=[], acl_filtered=False)

    with patch.object(
        pipeline,
        "_search_standard",
        new=AsyncMock(return_value=dummy_result),
    ) as mock_std:
        result = await pipeline.search(
            "test query",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode="global",
        )

    mock_std.assert_awaited_once()
    assert result.graph_expansion_applied is False


@pytest.mark.asyncio
async def test_global_mode_acl_filters_cross_namespace(connected_store, col_name):
    """ACL filtering: only namespace_A chunk reaches reranker; graph_expansion_applied=True."""
    communities = [_make_community("comm-1", ["chunk-A", "chunk-B"])]
    rows = [
        _make_raw_row("chunk-A", acl=["namespace_A"]),
        _make_raw_row("chunk-B", acl=["namespace_B"]),
    ]

    graph_store = MagicMock()
    graph_store.list_community_representatives = AsyncMock(return_value=communities)
    graph_config = GraphConfig(enabled=True, max_global_candidates=100)

    pipeline = _make_pipeline(
        connected_store,
        graph_store=graph_store,
        graph_config=graph_config,
    )
    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=rows)

    rerank_calls: list = []

    async def _capture_rerank(query, cands, top_k):
        rerank_calls.append(list(cands))
        return cands[:top_k]

    pipeline._reranker.rerank_candidates = _capture_rerank

    result = await pipeline.search(
        "test query",
        col_name,
        namespace="namespace_A",
        embedder=pipeline._global_embedder,
        graph_mode="global",
    )

    assert len(rerank_calls) == 1
    assert len(rerank_calls[0]) == 1
    assert rerank_calls[0][0].chunk_id == "chunk-A"
    assert result.graph_expansion_applied is True
    assert result.acl_filtered is True


@pytest.mark.asyncio
async def test_global_mode_all_acl_filtered_falls_back_to_hybrid(connected_store, col_name):
    """All chunks ACL-filtered → _search_standard called; graph_expansion_applied=False."""
    communities = [_make_community("comm-1", ["chunk-B"])]
    rows = [_make_raw_row("chunk-B", acl=["namespace_B"])]

    graph_store = MagicMock()
    graph_store.list_community_representatives = AsyncMock(return_value=communities)
    graph_config = GraphConfig(enabled=True)

    pipeline = _make_pipeline(
        connected_store,
        graph_store=graph_store,
        graph_config=graph_config,
    )
    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=rows)

    dummy_result = SearchPipelineResult(results=[], acl_filtered=False)

    with patch.object(
        pipeline,
        "_search_standard",
        new=AsyncMock(return_value=dummy_result),
    ) as mock_std:
        result = await pipeline.search(
            "test query",
            col_name,
            namespace="namespace_A",
            embedder=pipeline._global_embedder,
            graph_mode="global",
        )

    mock_std.assert_awaited_once()
    assert result.graph_expansion_applied is False


@pytest.mark.asyncio
async def test_naive_mode_routed_through_dispatch(connected_store, col_name):
    """graph_mode='naive' → _search_graph_mode('naive', ...) is called."""
    from archon_search.graph_expander import ExpandedQuery

    expander = MagicMock()
    expander.expand = AsyncMock(
        return_value=ExpandedQuery(
            original_query="test query",
            expanded_text="test query expanded",
            expansion_applied=True,
        )
    )

    pipeline = _make_pipeline(connected_store, graph_expander=expander)

    dummy_result = SearchPipelineResult(results=[], acl_filtered=False)

    with patch.object(
        pipeline,
        "_search_graph_mode",
        new=AsyncMock(return_value="test query expanded"),
    ) as mock_dispatch:
        with patch.object(
            pipeline,
            "_search_standard",
            new=AsyncMock(return_value=dummy_result),
        ):
            result = await pipeline.search(
                "test query",
                col_name,
                embedder=pipeline._global_embedder,
                graph_mode="naive",
            )

    mock_dispatch.assert_awaited_once()
    # First positional arg must be "naive"
    assert mock_dispatch.call_args[0][0] == "naive"
    assert isinstance(result, SearchPipelineResult)


@pytest.mark.asyncio
async def test_naive_plus_rag_fusion_uses_original_query_for_variants(connected_store, col_name):
    """RAG Fusion + graph_mode='naive': variants generated from ORIGINAL (unexpanded) query."""
    from archon_search.config import RAGFusionConfig
    from archon_search.graph_expander import ExpandedQuery

    expander = MagicMock()
    expander.expand = AsyncMock(
        return_value=ExpandedQuery(
            original_query="original",
            expanded_text="original expanded",
            expansion_applied=True,
        )
    )

    pipeline = _make_pipeline(connected_store, graph_expander=expander)

    rag_gen = MagicMock()
    rag_gen.generate_variants = AsyncMock(return_value=["variant query"])
    rag_config = RAGFusionConfig(enabled=True)

    with patch.object(pipeline.store, "has_vector_index", new=AsyncMock(return_value=True)):
        with patch.object(
            pipeline._global_embedder,
            "embed_one",
            new=AsyncMock(return_value=[0.1] * 4),
        ):
            with patch.object(
                pipeline.store,
                "hybrid_search_with_trace",
                new=AsyncMock(return_value=[]),
            ):
                await pipeline.search(
                    "original",
                    col_name,
                    embedder=pipeline._global_embedder,
                    graph_mode="naive",
                    rag_fusion=True,
                    rag_fusion_generator=rag_gen,
                    rag_fusion_config=rag_config,
                )

    # RAG Fusion variants must be generated from the ORIGINAL (unexpanded) query
    rag_gen.generate_variants.assert_awaited_once_with("original")


@pytest.mark.asyncio
async def test_search_many_global_mode_calls_per_collection(connected_store, col_name):
    """search_many(graph_mode='global'): list_community_representatives called per collection."""
    col_b = f"{col_name}-b"

    communities_a = [_make_community("comm-a", ["chunk-a1"])]
    communities_b = [_make_community("comm-b", ["chunk-b1"])]
    rows_a = [_make_raw_row("chunk-a1")]
    rows_b = [_make_raw_row("chunk-b1")]

    graph_store = MagicMock()
    graph_store.list_community_representatives = AsyncMock(
        side_effect=lambda coll: communities_a if coll == col_name else communities_b
    )

    graph_config = GraphConfig(enabled=True, max_global_candidates=100)

    pipeline = _make_pipeline(
        connected_store,
        graph_store=graph_store,
        graph_config=graph_config,
    )

    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(
        side_effect=lambda coll, ids: rows_a if coll == col_name else rows_b
    )
    pipeline._reranker.rerank_candidates = AsyncMock(
        side_effect=lambda q, cands, top_k: cands[:top_k]
    )

    from archon_search.collection_meta import CollectionMeta

    meta_a = MagicMock(spec=CollectionMeta)
    meta_a.name = col_name
    meta_a.active_embedding_model = "mock-embedder"

    meta_b = MagicMock(spec=CollectionMeta)
    meta_b.name = col_b
    meta_b.active_embedding_model = "mock-embedder"

    with patch.object(
        pipeline,
        "get_all_collections_meta",
        new=AsyncMock(return_value=[meta_a, meta_b]),
    ):
        result = await pipeline.search_many(
            "test query",
            [col_name, col_b],
            graph_mode="global",
        )

    assert graph_store.list_community_representatives.await_count == 2
    graph_store.list_community_representatives.assert_any_await(col_name)
    graph_store.list_community_representatives.assert_any_await(col_b)
    assert isinstance(result, SearchPipelineResult)
    assert result.graph_expansion_applied is True


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_global_mode_real_communities(tmp_path, monkeypatch):
    """Integration: real store + real GraphStore with community data → non-empty result."""
    import uuid

    from archon_search.chunker import DocumentChunker
    from archon_search.config import GraphConfig
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.embedder import Embedder
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import Community
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    db_path = tmp_path / "db"
    store = SearchStore(str(db_path))
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

    col = f"test-{uuid.uuid4().hex[:8]}"
    graph_store_path = tmp_path / "graph_db"
    graph_store = GraphStore(graph_store_path)
    await graph_store.connect()

    graph_config = GraphConfig(enabled=True, max_global_candidates=100)

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=5,
        graph_store=graph_store,
        graph_config=graph_config,
    )

    # Ingest a document to create chunks
    doc = tmp_path / "doc.txt"
    doc.write_text("This is a test document about graph retrieval.")
    await pipeline.ingest_file(
        doc,
        col,
        embedder=pipeline._global_embedder,
    )

    # Get chunk IDs from the store
    all_rows = [r async for r in store.list_chunks_raw(col, DEFAULT_NAMESPACE)]
    assert len(all_rows) > 0, "Ingest must have created at least one chunk"
    chunk_id = all_rows[0]["chunk_id"]

    # Create and write a community with that chunk_id
    await graph_store.ensure_communities_table(col)
    community = Community(
        community_id="test-comm-1",
        entity_ids=["entity-1"],
        representative_chunk_ids=[chunk_id],
        built_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        summary_text=None,
    )
    await graph_store.write_communities(col, [community])

    # Search with global mode
    result = await pipeline.search(
        "graph retrieval",
        col,
        embedder=pipeline._global_embedder,
        graph_mode="global",
    )

    assert result.graph_expansion_applied is True
    assert len(result.results) > 0

    await store.disconnect()
    await graph_store.disconnect()

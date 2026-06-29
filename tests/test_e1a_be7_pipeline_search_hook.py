"""Unit tests for BE-7: GraphExpander wired into pipeline.search and pipeline.search_many.

Tests:
- graph_mode="naive" → expander called; expanded query passed to _search_standard
- graph_mode=None → expander not called
- search_many: expander called once per leg with correct collection
- graph_expansion_applied flag set in SearchPipelineResult
- RAG Fusion + graph_mode: expansion on original query, variants from unexpanded
- HyDE (query_vector provided) + graph_mode: expansion applied, graph_expansion_applied=True
- Expanded text reaches embedder (not original query string)
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from archon_search._types import SearchResult
from archon_search.config import GraphConfig
from archon_search.graph_expander import ExpandedQuery
from archon_search.pipeline import SearchPipelineResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pipeline(
    store,
    *,
    graph_expander=None,
    graph_store=None,
    graph_config=None,
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
        graph_expander=graph_expander,
        graph_store=graph_store,
        graph_config=graph_config,
        graph_extractor=graph_extractor,
    )


def _stub_expander(
    expanded_text: str = "AuthService TokenValidator",
    expansion_applied: bool = True,
):
    """Return a mock GraphExpander whose expand() returns a fixed ExpandedQuery."""
    expander = MagicMock()
    expander.expand = AsyncMock(
        return_value=ExpandedQuery(
            original_query="AuthService",
            expanded_text=expanded_text,
            expansion_applied=expansion_applied,
        )
    )
    return expander


def _noop_expander():
    """Return a mock GraphExpander whose expand() returns no-op (no expansion)."""
    expander = MagicMock()
    expander.expand = AsyncMock(
        return_value=ExpandedQuery(
            original_query="foo",
            expanded_text="foo",
            expansion_applied=False,
        )
    )
    return expander


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_with_graph_mode_naive_calls_expander(connected_store, col_name):
    """graph_mode='naive' → expander.expand() called; expanded query passed to _search_standard."""
    expander = _stub_expander(expanded_text="AuthService TokenValidator")
    pipeline = _make_pipeline(connected_store, graph_expander=expander)

    dummy_result = SearchPipelineResult(results=[], acl_filtered=False)

    with patch.object(
        pipeline,
        "_search_standard",
        new=AsyncMock(return_value=dummy_result),
    ) as mock_std:
        await pipeline.search(
            "AuthService",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode="naive",
        )

    expander.expand.assert_awaited_once_with("AuthService", col_name)
    # _search_standard must receive the expanded text, not the original
    assert mock_std.call_args[0][0] == "AuthService TokenValidator"


@pytest.mark.asyncio
async def test_search_without_graph_mode_skips_expander(connected_store, col_name):
    """graph_mode=None → expander.expand() never called."""
    expander = _stub_expander()
    pipeline = _make_pipeline(connected_store, graph_expander=expander)

    dummy_result = SearchPipelineResult(results=[], acl_filtered=False)

    with patch.object(pipeline, "_search_standard", new=AsyncMock(return_value=dummy_result)):
        await pipeline.search(
            "AuthService",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode=None,
        )

    expander.expand.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_graph_expansion_applied_flag(connected_store, col_name):
    """graph_expansion_applied in SearchPipelineResult reflects expansionApplied from expander."""
    expander = _stub_expander(expansion_applied=True)
    pipeline = _make_pipeline(connected_store, graph_expander=expander)

    dummy = SearchPipelineResult(results=[], acl_filtered=False)
    with patch.object(pipeline, "_search_standard", new=AsyncMock(return_value=dummy)):
        result = await pipeline.search(
            "AuthService",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode="naive",
        )

    assert result.graph_expansion_applied is True


@pytest.mark.asyncio
async def test_search_graph_expansion_not_applied_when_no_expansion(connected_store, col_name):
    """graph_expansion_applied=False when expander returns expansion_applied=False."""
    expander = _noop_expander()
    pipeline = _make_pipeline(connected_store, graph_expander=expander)

    dummy = SearchPipelineResult(results=[], acl_filtered=False)
    with patch.object(pipeline, "_search_standard", new=AsyncMock(return_value=dummy)):
        result = await pipeline.search(
            "foo",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode="naive",
        )

    assert result.graph_expansion_applied is False


@pytest.mark.asyncio
async def test_search_expanded_text_reaches_embedder(connected_store, col_name):
    """Embedder must be called with expanded text when graph expansion is applied."""
    expander = _stub_expander(expanded_text="AuthService TokenValidator")
    pipeline = _make_pipeline(connected_store, graph_expander=expander)

    embedded_texts: list[str] = []
    original_embed = pipeline._global_embedder.embed_one

    async def _capture_embed(text: str) -> list[float]:
        embedded_texts.append(text)
        return [0.1] * 4

    with patch.object(pipeline._global_embedder, "embed_one", side_effect=_capture_embed):
        with patch.object(
            pipeline.store,
            "hybrid_search_with_trace",
            new=AsyncMock(return_value=[]),
        ):
            await pipeline.search(
                "AuthService",
                col_name,
                embedder=pipeline._global_embedder,
                graph_mode="naive",
            )

    # The embedder must have been called with the expanded text, not the original
    assert "AuthService TokenValidator" in embedded_texts
    assert "AuthService" not in embedded_texts


@pytest.mark.asyncio
async def test_search_graph_mode_with_rag_fusion_applies_expansion_to_original(
    connected_store, col_name
):
    """RAG Fusion + graph_mode=naive: expansion on original query; RAG Fusion variants from original."""
    expander = _stub_expander(expanded_text="AuthService TokenValidator")
    pipeline = _make_pipeline(connected_store, graph_expander=expander)

    from archon_search.config import RAGFusionConfig
    from archon_search.rag_fusion import RAGFusionGenerator

    mock_rag_gen = MagicMock(spec=RAGFusionGenerator)
    mock_rag_gen.generate_variants = AsyncMock(return_value=["who uses AuthService?"])

    rag_config = RAGFusionConfig(enabled=True)

    with patch.object(
        pipeline.store,
        "has_vector_index",
        new=AsyncMock(return_value=True),  # True so we reach variant generation
    ):
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
                result = await pipeline.search(
                    "AuthService",
                    col_name,
                    embedder=pipeline._global_embedder,
                    graph_mode="naive",
                    rag_fusion=True,
                    rag_fusion_generator=mock_rag_gen,
                    rag_fusion_config=rag_config,
                )

    # Expansion ran on the original query
    expander.expand.assert_awaited_once_with("AuthService", col_name)
    assert result.graph_expansion_applied is True
    # RAG Fusion variants generated from ORIGINAL (not expanded) query
    mock_rag_gen.generate_variants.assert_awaited_once_with("AuthService")


@pytest.mark.asyncio
async def test_search_graph_mode_with_hyde_applies_expansion_to_original(
    connected_store, col_name
):
    """HyDE (pre-computed query_vector) + graph_mode=naive: expansion applied; graph_expansion_applied=True."""
    expander = _stub_expander(expanded_text="AuthService TokenValidator")
    pipeline = _make_pipeline(connected_store, graph_expander=expander)

    dummy = SearchPipelineResult(results=[], acl_filtered=False)
    hyde_vector = [0.9] * 4  # Simulated HyDE-generated vector

    with patch.object(pipeline, "_search_standard", new=AsyncMock(return_value=dummy)) as mock_std:
        result = await pipeline.search(
            "AuthService",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode="naive",
            query_vector=hyde_vector,
        )

    # Graph expansion must run on original query
    expander.expand.assert_awaited_once_with("AuthService", col_name)
    # Result must reflect expansion
    assert result.graph_expansion_applied is True
    # _search_standard gets the expanded text
    assert mock_std.call_args[0][0] == "AuthService TokenValidator"


@pytest.mark.asyncio
async def test_search_many_applies_expansion_per_leg(connected_store, col_name):
    """search_many with graph_mode=naive: expander called once per leg with correct collection name."""
    col2 = f"{col_name}_b"

    expander = MagicMock()
    expander.expand = AsyncMock(
        side_effect=lambda q, coll: ExpandedQuery(
            original_query=q,
            expanded_text=f"{q} extra_{coll}",
            expansion_applied=True,
        )
    )
    pipeline = _make_pipeline(connected_store, graph_expander=expander)

    # Stub metadata lookup so both collections appear valid
    meta1 = MagicMock()
    meta1.name = col_name
    meta1.active_embedding_model = pipeline._global_embedder.model_name

    meta2 = MagicMock()
    meta2.name = col2
    meta2.active_embedding_model = pipeline._global_embedder.model_name

    store_mock = AsyncMock(return_value=[])
    with patch.object(
        pipeline,
        "get_all_collections_meta",
        new=AsyncMock(return_value=[meta1, meta2]),
    ):
        with patch.object(
            pipeline._global_embedder,
            "embed_one",
            new=AsyncMock(return_value=[0.1] * 4),
        ):
            with patch.object(
                pipeline.store,
                "hybrid_search_with_trace",
                new=store_mock,
            ):
                result = await pipeline.search_many(
                    "AuthService",
                    [col_name, col2],
                    graph_mode="naive",
                )

    # Expander must have been called once per collection with the correct collection name
    calls = expander.expand.await_args_list
    assert len(calls) == 2
    called_collections = {c[0][1] for c in calls}  # second positional arg is collection
    assert col_name in called_collections
    assert col2 in called_collections

    assert result.graph_expansion_applied is True

    # Additionally verify each collection received its own expanded text
    store_calls = store_mock.await_args_list
    assert len(store_calls) == 2
    called_query_texts = {call[0][2] for call in store_calls}  # 3rd positional arg is query_text
    assert f"AuthService extra_{col_name}" in called_query_texts
    assert f"AuthService extra_{col2}" in called_query_texts


@pytest.mark.asyncio
async def test_search_pipeline_result_has_graph_expansion_applied_field():
    """SearchPipelineResult must have a graph_expansion_applied field defaulting to False."""
    result = SearchPipelineResult(results=[], acl_filtered=False)
    assert hasattr(result, "graph_expansion_applied")
    assert result.graph_expansion_applied is False

"""Unit and integration tests for BE-7a: SearchPipeline graph_mode=local single-collection path.

Tests:
- No entities matched → fall back to standard hybrid search (graph_expansion_applied=False, S10)
- No communities table (never built) → fall back to standard (graph_expansion_applied=False, WARNING)
- Isolated nodes (entities matched but no community membership) → naive expansion fallback (graph_expansion_applied=True, S9)
- Matched community → representative chunks merged with hybrid candidates, reranked (graph_expansion_applied=True)
- Multiple communities matched → chunk IDs from all communities merged before reranking
- Stale chunk IDs silently skipped (Q6 local path)
- All stale chunk IDs → fall back to _search_standard, WARNING logged (Q6 local path)
- Empty representative_chunk_ids in matched community → fall back to _search_standard, WARNING logged
- ACL filtering on community chunks (S15)
- Integration: real store + real GraphStore with community data
- Integration: ACL filters across namespaces in local mode (S15)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search.config import GraphConfig
from archon_search.filters import SearchFilters
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
    # Use None as sentinel for "use default"; explicit [] means truly empty chunk IDs
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
    collection: str = "test-collection",
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


def _make_scored_candidate(
    chunk_id: str = "hybrid-1",
    text: str = "hybrid text",
    source_path: str = "/hybrid.txt",
    acl: list[str] | None = None,
) -> ScoredSearchCandidate:
    """Return a ScoredSearchCandidate (as returned by hybrid_search_with_trace)."""
    from archon_search._types import IngestedBy
    return ScoredSearchCandidate(
        chunk_id=chunk_id,
        source_path=source_path,
        text=text,
        collection="test-collection",
        doc_id="doc-hybrid",
        language="en",
        file_type="txt",
        indexed_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
        acl=acl,
        metadata={},
        ingested_by="cli",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=1, vector_score=0.9, vector_score_kind="cosine",
            fts_rank=1, fts_score=0.8, fts_score_kind="bm25",
            rrf_score=0.9, reranker_score=None,
        ),
    )


def _make_graph_store_mock(
    *,
    find_nodes_return: list[GraphNode] | None = None,
    communities_table_exists_return: bool = True,
    get_communities_return: list[Community] | None = None,
    list_community_representatives_return: list[Community] | None = None,
) -> MagicMock:
    """Create a mock GraphStore with configurable return values."""
    graph_store = MagicMock()
    graph_store.find_nodes_by_name = AsyncMock(return_value=find_nodes_return or [])
    graph_store.communities_table_exists = AsyncMock(return_value=communities_table_exists_return)
    graph_store.get_communities_for_entities = AsyncMock(return_value=get_communities_return or [])
    graph_store.list_community_representatives = AsyncMock(
        return_value=list_community_representatives_return or []
    )
    return graph_store


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_mode_no_entities_falls_back_to_hybrid(connected_store, col_name):
    """S10: No graph entities recognised → standard hybrid search returned; graph_expansion_applied=False."""
    graph_store = _make_graph_store_mock(find_nodes_return=[])
    graph_config = GraphConfig(enabled=True)
    pipeline = _make_pipeline(connected_store, graph_store=graph_store, graph_config=graph_config)

    dummy_result = SearchPipelineResult(results=[], acl_filtered=False)
    with patch.object(pipeline, "_search_standard", new=AsyncMock(return_value=dummy_result)) as mock_std:
        result = await pipeline.search(
            "AuthService logs",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode="local",
        )

    mock_std.assert_awaited_once()
    graph_store.find_nodes_by_name.assert_awaited_once()
    assert result.graph_expansion_applied is False


@pytest.mark.asyncio
async def test_local_mode_no_communities_table_falls_back_to_hybrid(connected_store, col_name, caplog):
    """Communities table never created → fall back to standard search; WARNING logged."""
    node = _make_graph_node(collection=col_name)
    graph_store = _make_graph_store_mock(
        find_nodes_return=[node],
        communities_table_exists_return=False,
    )
    graph_config = GraphConfig(enabled=True)
    pipeline = _make_pipeline(connected_store, graph_store=graph_store, graph_config=graph_config)

    dummy_result = SearchPipelineResult(results=[], acl_filtered=False)
    with patch.object(pipeline, "_search_standard", new=AsyncMock(return_value=dummy_result)) as mock_std:
        with caplog.at_level(logging.WARNING, logger="archon_search.pipeline"):
            result = await pipeline.search(
                "AuthService logs",
                col_name,
                embedder=pipeline._global_embedder,
                graph_mode="local",
            )

    mock_std.assert_awaited_once()
    assert result.graph_expansion_applied is False
    assert any("communities" in r.message.lower() for r in caplog.records if r.levelno >= logging.WARNING)


@pytest.mark.asyncio
async def test_local_mode_isolated_node_falls_back_to_naive(connected_store, col_name):
    """S9: Entities matched but no community membership → naive expansion fallback; graph_expansion_applied=True."""
    from archon_search.graph_expander import ExpandedQuery

    node = _make_graph_node(collection=col_name)
    graph_store = _make_graph_store_mock(
        find_nodes_return=[node],
        communities_table_exists_return=True,
        get_communities_return=[],  # isolated — no community membership
    )
    graph_config = GraphConfig(enabled=True)

    expander = MagicMock()
    expander.expand = AsyncMock(
        return_value=ExpandedQuery(
            original_query="AuthService logs",
            expanded_text="AuthService logs TokenValidator",
            expansion_applied=True,
        )
    )

    pipeline = _make_pipeline(
        connected_store,
        graph_store=graph_store,
        graph_config=graph_config,
        graph_expander=expander,
    )

    dummy_result = SearchPipelineResult(results=[], acl_filtered=False)
    with patch.object(pipeline, "_search_standard", new=AsyncMock(return_value=dummy_result)):
        result = await pipeline.search(
            "AuthService logs",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode="local",
        )

    # Naive expansion falls back with graph_expansion_applied=True (per spec S9)
    assert result.graph_expansion_applied is True
    expander.expand.assert_awaited_once_with("AuthService logs", col_name, ns="default")


@pytest.mark.asyncio
async def test_local_mode_matched_community_merges_results(connected_store, col_name):
    """Community matched: representative chunks + hybrid candidates merged, reranked, graph_expansion_applied=True."""
    node = _make_graph_node(collection=col_name)
    community = _make_community("comm-1", chunk_ids=["chunk-c1", "chunk-c2"])
    community_rows = [_make_raw_row("chunk-c1"), _make_raw_row("chunk-c2")]
    hybrid_candidates = [_make_scored_candidate("hybrid-1"), _make_scored_candidate("hybrid-2")]

    graph_store = _make_graph_store_mock(
        find_nodes_return=[node],
        communities_table_exists_return=True,
        get_communities_return=[community],
    )
    graph_config = GraphConfig(enabled=True)
    pipeline = _make_pipeline(connected_store, graph_store=graph_store, graph_config=graph_config)

    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=community_rows)
    pipeline.store.hybrid_search_with_trace = AsyncMock(return_value=hybrid_candidates)

    rerank_calls: list[list[ScoredSearchCandidate]] = []

    async def _capture_rerank(query, cands, top_k):
        rerank_calls.append(list(cands))
        return cands[:top_k]

    pipeline._reranker.rerank_candidates = _capture_rerank

    with patch.object(pipeline._global_embedder, "embed_one", new=AsyncMock(return_value=[0.1] * 4)):
        result = await pipeline.search(
            "AuthService logs",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode="local",
        )

    assert result.graph_expansion_applied is True
    assert len(rerank_calls) == 1
    # 2 community chunks + 2 hybrid candidates = 4 total (no overlap)
    assert len(rerank_calls[0]) == 4


@pytest.mark.asyncio
async def test_local_mode_multiple_communities_matched(connected_store, col_name):
    """Query entities span 2 communities: chunk IDs from both merged before reranking; graph_expansion_applied=True."""
    node = _make_graph_node(collection=col_name)
    comm1 = _make_community("comm-1", chunk_ids=["chunk-a"])
    comm2 = _make_community("comm-2", chunk_ids=["chunk-b"])
    community_rows = [_make_raw_row("chunk-a"), _make_raw_row("chunk-b")]

    graph_store = _make_graph_store_mock(
        find_nodes_return=[node],
        communities_table_exists_return=True,
        get_communities_return=[comm1, comm2],
    )
    graph_config = GraphConfig(enabled=True)
    pipeline = _make_pipeline(connected_store, graph_store=graph_store, graph_config=graph_config)

    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=community_rows)
    pipeline.store.hybrid_search_with_trace = AsyncMock(return_value=[])

    rerank_calls: list[list[ScoredSearchCandidate]] = []

    async def _capture_rerank(query, cands, top_k):
        rerank_calls.append(list(cands))
        return cands[:top_k]

    pipeline._reranker.rerank_candidates = _capture_rerank

    with patch.object(pipeline._global_embedder, "embed_one", new=AsyncMock(return_value=[0.1] * 4)):
        result = await pipeline.search(
            "AuthService",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode="local",
        )

    assert result.graph_expansion_applied is True
    assert len(rerank_calls) == 1
    # verify both chunk IDs from both communities were sent to get_chunks_by_ids
    call_args = pipeline.store.get_chunks_by_ids.call_args
    chunk_ids_passed = call_args[0][1]
    assert "chunk-a" in chunk_ids_passed
    assert "chunk-b" in chunk_ids_passed


@pytest.mark.asyncio
async def test_local_mode_stale_chunk_ids_silently_skipped(connected_store, col_name):
    """Q6 local path: 3 chunk IDs; 1 stale → 2 returned by get_chunks_by_ids; reranker gets 2 + hybrid; no error."""
    node = _make_graph_node(collection=col_name)
    community = _make_community("comm-1", chunk_ids=["chunk-1", "chunk-2", "chunk-stale"])
    community_rows = [_make_raw_row("chunk-1"), _make_raw_row("chunk-2")]  # chunk-stale missing

    graph_store = _make_graph_store_mock(
        find_nodes_return=[node],
        communities_table_exists_return=True,
        get_communities_return=[community],
    )
    graph_config = GraphConfig(enabled=True)
    pipeline = _make_pipeline(connected_store, graph_store=graph_store, graph_config=graph_config)

    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=community_rows)
    pipeline.store.hybrid_search_with_trace = AsyncMock(return_value=[])

    rerank_calls: list[list[ScoredSearchCandidate]] = []

    async def _capture_rerank(query, cands, top_k):
        rerank_calls.append(list(cands))
        return cands[:top_k]

    pipeline._reranker.rerank_candidates = _capture_rerank

    with patch.object(pipeline._global_embedder, "embed_one", new=AsyncMock(return_value=[0.1] * 4)):
        result = await pipeline.search(
            "AuthService",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode="local",
        )

    assert result.graph_expansion_applied is True
    assert len(rerank_calls) == 1
    # Only 2 non-stale community chunks (no hybrid candidates)
    assert len(rerank_calls[0]) == 2


@pytest.mark.asyncio
async def test_local_mode_all_stale_chunk_ids_falls_back_to_hybrid(connected_store, col_name, caplog):
    """Q6 local path: all chunk IDs stale → pipeline falls back to _search_standard; WARNING logged."""
    node = _make_graph_node(collection=col_name)
    community = _make_community("comm-1", chunk_ids=["chunk-stale"])

    graph_store = _make_graph_store_mock(
        find_nodes_return=[node],
        communities_table_exists_return=True,
        get_communities_return=[community],
    )
    graph_config = GraphConfig(enabled=True)
    pipeline = _make_pipeline(connected_store, graph_store=graph_store, graph_config=graph_config)

    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=[])  # all stale

    dummy_result = SearchPipelineResult(results=[], acl_filtered=False)
    with patch.object(pipeline, "_search_standard", new=AsyncMock(return_value=dummy_result)) as mock_std:
        with caplog.at_level(logging.WARNING, logger="archon_search.pipeline"):
            result = await pipeline.search(
                "AuthService",
                col_name,
                embedder=pipeline._global_embedder,
                graph_mode="local",
            )

    mock_std.assert_awaited_once()
    assert result.graph_expansion_applied is False
    assert any("stale" in r.message.lower() for r in caplog.records if r.levelno >= logging.WARNING)


@pytest.mark.asyncio
async def test_local_mode_empty_representative_chunk_ids(connected_store, col_name, caplog):
    """Matched community has representative_chunk_ids=[] → falls back to _search_standard; WARNING logged."""
    node = _make_graph_node(collection=col_name)
    community = _make_community("comm-1", chunk_ids=[])  # empty chunk IDs

    graph_store = _make_graph_store_mock(
        find_nodes_return=[node],
        communities_table_exists_return=True,
        get_communities_return=[community],
    )
    graph_config = GraphConfig(enabled=True)
    pipeline = _make_pipeline(connected_store, graph_store=graph_store, graph_config=graph_config)

    dummy_result = SearchPipelineResult(results=[], acl_filtered=False)
    with patch.object(pipeline, "_search_standard", new=AsyncMock(return_value=dummy_result)) as mock_std:
        with caplog.at_level(logging.WARNING, logger="archon_search.pipeline"):
            result = await pipeline.search(
                "AuthService",
                col_name,
                embedder=pipeline._global_embedder,
                graph_mode="local",
            )

    mock_std.assert_awaited_once()
    assert result.graph_expansion_applied is False
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_local_mode_acl_filters_community_chunks_unit(connected_store, col_name):
    """ACL: community chunk for namespace_B filtered out; only namespace_A chunk passes; graph_expansion_applied=True."""
    node = _make_graph_node(collection=col_name)
    community = _make_community("comm-1", chunk_ids=["chunk-A", "chunk-B"])
    community_rows = [
        _make_raw_row("chunk-A", acl=["namespace_A"]),
        _make_raw_row("chunk-B", acl=["namespace_B"]),
    ]

    graph_store = _make_graph_store_mock(
        find_nodes_return=[node],
        communities_table_exists_return=True,
        get_communities_return=[community],
    )
    graph_config = GraphConfig(enabled=True)
    pipeline = _make_pipeline(connected_store, graph_store=graph_store, graph_config=graph_config)

    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=community_rows)
    pipeline.store.hybrid_search_with_trace = AsyncMock(return_value=[])

    rerank_calls: list[list[ScoredSearchCandidate]] = []

    async def _capture_rerank(query, cands, top_k):
        rerank_calls.append(list(cands))
        return cands[:top_k]

    pipeline._reranker.rerank_candidates = _capture_rerank

    with patch.object(pipeline._global_embedder, "embed_one", new=AsyncMock(return_value=[0.1] * 4)):
        result = await pipeline.search(
            "AuthService",
            col_name,
            namespace="namespace_A",
            embedder=pipeline._global_embedder,
            graph_mode="local",
        )

    assert result.graph_expansion_applied is True
    assert result.acl_filtered is True
    assert len(rerank_calls) == 1
    assert len(rerank_calls[0]) == 1
    assert rerank_calls[0][0].chunk_id == "chunk-A"


@pytest.mark.asyncio
async def test_local_mode_all_acl_filtered_falls_back_to_hybrid(connected_store, col_name):
    """All community chunks filtered by ACL → fall back to _search_standard; graph_expansion_applied=False."""
    node = _make_graph_node(collection=col_name)
    community = _make_community("comm-1", chunk_ids=["chunk-B"])
    community_rows = [_make_raw_row("chunk-B", acl=["namespace_B"])]

    graph_store = _make_graph_store_mock(
        find_nodes_return=[node],
        communities_table_exists_return=True,
        get_communities_return=[community],
    )
    graph_config = GraphConfig(enabled=True)
    pipeline = _make_pipeline(connected_store, graph_store=graph_store, graph_config=graph_config)

    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=community_rows)

    dummy_result = SearchPipelineResult(results=[], acl_filtered=False)
    with patch.object(pipeline, "_search_standard", new=AsyncMock(return_value=dummy_result)) as mock_std:
        result = await pipeline.search(
            "AuthService",
            col_name,
            namespace="namespace_A",
            embedder=pipeline._global_embedder,
            graph_mode="local",
        )

    mock_std.assert_awaited_once()
    assert result.graph_expansion_applied is False


@pytest.mark.asyncio
async def test_local_mode_glob_filter_excludes_community_chunks(connected_store, col_name):
    """Step 7: source_path_glob filters community chunks before ACL; non-matching chunks excluded from merge."""
    node = _make_graph_node(collection=col_name)
    community = _make_community("comm-1", chunk_ids=["chunk-match", "chunk-no-match"])
    community_rows = [
        _make_raw_row("chunk-match", source_path="/a/doc.txt"),
        _make_raw_row("chunk-no-match", source_path="/other/doc.txt"),
    ]

    graph_store = _make_graph_store_mock(
        find_nodes_return=[node],
        communities_table_exists_return=True,
        get_communities_return=[community],
    )
    graph_config = GraphConfig(enabled=True)
    pipeline = _make_pipeline(connected_store, graph_store=graph_store, graph_config=graph_config)

    pipeline.store = MagicMock()
    pipeline.store.get_chunks_by_ids = AsyncMock(return_value=community_rows)
    pipeline.store.hybrid_search_with_trace = AsyncMock(return_value=[])

    rerank_calls: list[list[ScoredSearchCandidate]] = []

    async def _capture_rerank(query, cands, top_k):
        rerank_calls.append(list(cands))
        return cands[:top_k]

    pipeline._reranker.rerank_candidates = _capture_rerank

    with patch.object(pipeline._global_embedder, "embed_one", new=AsyncMock(return_value=[0.1] * 4)):
        result = await pipeline.search(
            "AuthService",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode="local",
            filters=SearchFilters(source_path_glob="/a/*.txt"),
        )

    assert result.graph_expansion_applied is True
    assert len(rerank_calls) == 1
    # Only the /a/doc.txt chunk should reach the reranker; /other/doc.txt excluded by glob
    assert len(rerank_calls[0]) == 1
    assert rerank_calls[0][0].chunk_id == "chunk-match"


@pytest.mark.asyncio
async def test_local_mode_graph_store_none_falls_back_to_hybrid(connected_store, col_name):
    """No graph_store configured → local mode falls back to standard search; graph_expansion_applied=False."""
    # graph_store=None simulates a pipeline where graph support is not configured
    pipeline = _make_pipeline(connected_store, graph_store=None, graph_config=GraphConfig(enabled=True))

    dummy_result = SearchPipelineResult(results=[], acl_filtered=False)
    with patch.object(pipeline, "_search_standard", new=AsyncMock(return_value=dummy_result)) as mock_std:
        result = await pipeline.search(
            "AuthService logs",
            col_name,
            embedder=pipeline._global_embedder,
            graph_mode="local",
        )

    mock_std.assert_awaited_once()
    assert result.graph_expansion_applied is False


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_local_mode_real(tmp_path, monkeypatch):
    """Integration: real store + real GraphStore with community; local mode returns community representative chunks."""
    import uuid

    from archon_search.chunker import DocumentChunker
    from archon_search.config import GraphConfig
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.embedder import Embedder
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import Community, GraphNode, EntityType, make_stable_entity_id
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

    graph_config = GraphConfig(enabled=True)

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
    doc.write_text("AuthService handles token validation and authentication.")
    await pipeline.ingest_file(doc, col, embedder=pipeline._global_embedder)

    # Get chunk IDs from the store
    all_rows = [r async for r in store.list_chunks_raw(col, DEFAULT_NAMESPACE)]
    assert len(all_rows) > 0, "Ingest must have created at least one chunk"
    chunk_id = all_rows[0]["chunk_id"]

    # Insert a graph node so find_nodes_by_name can match
    node = GraphNode(
        id=make_stable_entity_id(EntityType.system, "authservice"),
        entity_name="authservice",
        entity_type=EntityType.system,
        source_doc_id="doc-1",
        collection_name=col,
    )
    await graph_store.ensure_graph_tables(col, ns="default")
    await graph_store.write_graph(col, nodes=[node], edges=[], ns="default")

    # Create and write a community with that chunk_id
    community = Community(
        community_id="test-comm-1",
        entity_ids=[node.id],
        representative_chunk_ids=[chunk_id],
        built_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        summary_text=None,
    )
    await graph_store.write_communities(col, [community], ns="default")

    # Search with local mode using a query that contains "authservice"
    result = await pipeline.search(
        "authservice token",
        col,
        embedder=pipeline._global_embedder,
        graph_mode="local",
    )

    assert result.graph_expansion_applied is True
    assert len(result.results) > 0

    await store.disconnect()
    await graph_store.disconnect()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_local_mode_acl_filters_community_chunks(tmp_path, monkeypatch):
    """S15 integration: community chunks from namespace_B not returned when searching from namespace_A."""
    import uuid

    from archon_search.chunker import DocumentChunker
    from archon_search.config import GraphConfig
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.embedder import Embedder
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import Community, GraphNode, EntityType, make_stable_entity_id
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

    graph_config = GraphConfig(enabled=True)

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

    # Ingest two documents with different ACL namespaces
    doc_a = tmp_path / "doc_a.txt"
    doc_a.write_text("AuthService namespace A content")
    doc_b = tmp_path / "doc_b.txt"
    doc_b.write_text("AuthService namespace B content")

    # Ingest with namespace_A ACL — sidecar is <doc_path>.acl
    (tmp_path / "doc_a.txt.acl").write_text("namespace_A")
    await pipeline.ingest_file(doc_a, col, namespace="namespace_A", embedder=pipeline._global_embedder)

    # Get chunk ID for the namespace_A doc (filter by source_path to avoid collision with doc_b)
    rows_all_a = [r async for r in store.list_chunks_raw(col, "namespace_A")
                  if str(doc_a) in r.get("source_path", "")]
    assert len(rows_all_a) > 0, "doc_a must have generated at least one chunk"
    chunk_id_a = rows_all_a[0]["chunk_id"]

    # Add a "namespace_B" chunk via ACL sidecar
    (tmp_path / "doc_b.txt.acl").write_text("namespace_B")
    await pipeline.ingest_file(doc_b, col, namespace="namespace_B", embedder=pipeline._global_embedder)
    rows_all_b = [r async for r in store.list_chunks_raw(col, "namespace_B")
                  if str(doc_b) in r.get("source_path", "")]
    assert len(rows_all_b) > 0, "doc_b must have generated at least one chunk"
    chunk_id_b = rows_all_b[0]["chunk_id"]

    assert chunk_id_a != chunk_id_b, "Test setup error: both docs must have distinct chunk IDs"

    # Create a graph node and a community that includes both chunk IDs
    node = GraphNode(
        id=make_stable_entity_id(EntityType.system, "authservice"),
        entity_name="authservice",
        entity_type=EntityType.system,
        source_doc_id="doc-1",
        collection_name=col,
    )
    await graph_store.ensure_graph_tables(col, ns="default")
    await graph_store.write_graph(col, nodes=[node], edges=[], ns="default")

    # Community includes both chunk IDs
    community = Community(
        community_id="test-comm-acl",
        entity_ids=[node.id],
        representative_chunk_ids=[chunk_id_a, chunk_id_b],
        built_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        summary_text=None,
    )
    await graph_store.write_communities(col, [community], ns="default")

    # Search with local mode from namespace_A — should only get namespace_A chunks
    result = await pipeline.search(
        "authservice",
        col,
        namespace="namespace_A",
        embedder=pipeline._global_embedder,
        graph_mode="local",
    )

    # All results must be accessible to namespace_A
    for r in result.results:
        # chunk_id_b should not appear in results (it belongs to namespace_B)
        assert r.chunk_id != chunk_id_b, "namespace_B chunk should be ACL-filtered out"

    await store.disconnect()
    await graph_store.disconnect()

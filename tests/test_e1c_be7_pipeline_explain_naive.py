"""BE-7: Tests for pipeline.explain() naive graph traversal wiring.

Covers:
- ``test_pipeline_explain_naive_graph_provenance_attached`` (S2)
- ``test_pipeline_explain_naive_dedup_graph_wins`` (S8)
- ``test_pipeline_explain_naive_hybrid_chunks_null_provenance`` (S7)
- ``test_pipeline_explain_naive_real_graph_layer`` (integration)

Scenarios satisfied:
  S2  — naive mode provenance populated on graph-retrieved results
  S7  — mixed results: graph-retrieved carry provenance; hybrid-only carry null
  S8  — dedup: chunk reachable by both → appears once with graph provenance
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._diagnostics import (
    GraphProvenance,
    ScoredSearchCandidate,
    SearchScoreBreakdown,
    TraversalStep,
)
from archon_search.graph_types import EntityType, GraphEdge, GraphNode, RelationshipType
from archon_search.pipeline import ExplainPipelineResult, SearchPipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _breakdown(rrf: float = 0.5) -> SearchScoreBreakdown:
    return SearchScoreBreakdown(
        vector_rank=None,
        vector_score=None,
        vector_score_kind=None,
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=rrf,
        reranker_score=None,
    )


def _candidate(chunk_id: str = "doc1-000000", *, graph_provenance: GraphProvenance | None = None) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id="doc1",
        chunk_id=chunk_id,
        text="hello world",
        source_path="/tmp/foo.md",
        score_breakdown=_breakdown(),
        collection="col",
        graph_provenance=graph_provenance,
    )


def _make_pipeline(*, mock_store: MagicMock | None = None) -> SearchPipeline:
    """Create a minimal mocked SearchPipeline for unit tests."""
    store = mock_store or MagicMock()
    mock_embedder_backend = MagicMock()
    mock_embedder_backend.model_name = "mock"
    mock_embedder_backend.is_warm = False
    mock_embedder_backend.encode = MagicMock(return_value=[[0.1, 0.2, 0.3, 0.4]])

    mock_reranker_backend = MagicMock()
    mock_reranker_backend.is_warm = False
    mock_reranker_backend.predict = MagicMock(return_value=[0.5, 0.4])

    from archon_search.embedder import Embedder
    from archon_search.reranker import Reranker

    return SearchPipeline(
        store=store,
        embedder=Embedder(mock_embedder_backend),
        reranker=Reranker(mock_reranker_backend),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )


def _stub_provenance() -> GraphProvenance:
    return GraphProvenance(steps=[
        TraversalStep(entity="AuthService", entity_id="entity-id-1", relationship="uses")
    ])


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_explain_naive_graph_provenance_attached() -> None:
    """graph_mode='naive': mock graph retrieval returning two candidates with provenance;
    top_results carry GraphProvenance (S2)."""
    prov = _stub_provenance()
    graph_cand_1 = _candidate("doc1-000000", graph_provenance=prov)
    graph_cand_2 = _candidate("doc1-000001", graph_provenance=prov)

    mock_store = MagicMock()
    # hybrid_search_with_trace for original query returns empty (no additional hybrid candidates)
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[])

    pipeline = _make_pipeline(mock_store=mock_store)
    # Stub _explain_naive_graph_candidates to return two candidates with provenance
    pipeline._explain_naive_graph_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[graph_cand_1, graph_cand_2]
    )

    result = await pipeline.explain(
        "test query",
        collection="col",
        graph_mode="naive",
        rerank=False,
    )

    assert result.graph_mode_applied == "naive"
    assert result.rag_fusion_applied is False
    assert len(result.top_results) == 2
    for c in result.top_results:
        assert c.graph_provenance is not None
        assert len(c.graph_provenance.steps) >= 1


@pytest.mark.asyncio
async def test_pipeline_explain_naive_dedup_graph_wins() -> None:
    """Chunk reachable by both graph traversal and hybrid → appears once with graph_provenance (S8)."""
    prov = _stub_provenance()
    graph_cand = _candidate("doc1-000000", graph_provenance=prov)
    # Same chunk_id as the graph candidate, but no provenance (hybrid version)
    hybrid_cand_same_chunk = _candidate("doc1-000000", graph_provenance=None)

    mock_store = MagicMock()
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[hybrid_cand_same_chunk])

    pipeline = _make_pipeline(mock_store=mock_store)
    pipeline._explain_naive_graph_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[graph_cand]
    )

    result = await pipeline.explain(
        "test query",
        collection="col",
        graph_mode="naive",
        rerank=False,
    )

    # chunk appears exactly once
    chunk_ids = [c.chunk_id for c in result.top_results + result.near_misses]
    assert chunk_ids.count("doc1-000000") == 1

    # and it has graph provenance (graph wins)
    all_candidates = result.top_results + result.near_misses
    chunk_a = next(c for c in all_candidates if c.chunk_id == "doc1-000000")
    assert chunk_a.graph_provenance is not None


@pytest.mark.asyncio
async def test_pipeline_explain_naive_hybrid_chunks_null_provenance() -> None:
    """Non-graph candidates in mixed result → graph_provenance is None (S7)."""
    prov = _stub_provenance()
    graph_cand = _candidate("chunk-graph", graph_provenance=prov)
    # Hybrid-only chunk (different chunk_id)
    hybrid_cand = _candidate("chunk-hybrid", graph_provenance=None)

    mock_store = MagicMock()
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[hybrid_cand])

    pipeline = _make_pipeline(mock_store=mock_store)
    pipeline._explain_naive_graph_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[graph_cand]
    )

    result = await pipeline.explain(
        "test query",
        collection="col",
        graph_mode="naive",
        rerank=False,
    )

    all_candidates = result.top_results + result.near_misses
    chunk_ids_with_prov = [c.chunk_id for c in all_candidates if c.graph_provenance is not None]
    chunk_ids_null_prov = [c.chunk_id for c in all_candidates if c.graph_provenance is None]

    assert "chunk-graph" in chunk_ids_with_prov, "graph-retrieved chunk should have provenance (S7)"
    assert "chunk-hybrid" in chunk_ids_null_prov, "hybrid-only chunk should have null provenance (S7)"


# ---------------------------------------------------------------------------
# Integration test — real SearchPipeline + stubbed E1a graph layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_explain_naive_real_graph_layer(tmp_path, monkeypatch) -> None:
    """Real SearchPipeline + stubbed E1a graph layer → graph_mode_applied='naive';
    at least one candidate has non-null graph_provenance.

    The E1a graph layer is stubbed via AsyncMock on pipeline._graph_store so that
    entity matching and edge traversal succeed without real graph data in LanceDB.
    The main SearchStore (LanceDB) and chunking/embedding path are real.
    """
    import hashlib
    from datetime import UTC, datetime

    from archon_search._types import ChunkRecord
    from tests.integration.conftest import make_real_pipeline

    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    # Ingest one chunk so explain has data to query.
    doc_id = hashlib.sha256(b"be7-naive-doc").hexdigest()
    chunk = ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text="AuthService depends on TokenValidator for auth",
        vector=[0.1, 0.2, 0.3, 0.4],
        source_path="/tmp/auth.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )
    await store.ensure_collection("be7-col", embedding_dim=4)
    await store.ingest_chunks("be7-col", [chunk])

    # Stub the E1a graph layer: inject a mock _graph_store that returns real-looking data.
    matched_node = GraphNode(
        id="entity-authservice",
        entity_name="AuthService",
        entity_type=EntityType.system,
        source_doc_id=doc_id,
        collection_name="be7-col",
    )
    neighbour_node = GraphNode(
        id="entity-tokenvalidator",
        entity_name="TokenValidator",
        entity_type=EntityType.system,
        source_doc_id=doc_id,
        collection_name="be7-col",
    )
    edge = GraphEdge(
        id="edge-auth-token",
        source_node_id="entity-authservice",
        target_node_id="entity-tokenvalidator",
        relationship_type=RelationshipType.depends_on,
        source_doc_id=doc_id,
    )

    mock_graph_store = MagicMock()
    mock_graph_store.find_nodes_by_name = AsyncMock(return_value=[matched_node])
    mock_graph_store.get_edges_for_nodes = AsyncMock(return_value=[edge])
    mock_graph_store.get_neighbours = AsyncMock(return_value=[neighbour_node])

    pipeline._graph_store = mock_graph_store  # type: ignore[assignment]

    # Query only contains "AuthService" so "TokenValidator" is a new expansion.
    result = await pipeline.explain(
        "AuthService",
        collection="be7-col",
        graph_mode="naive",
    )

    assert isinstance(result, ExplainPipelineResult)
    assert result.graph_mode_applied == "naive"
    assert result.rag_fusion_applied is False

    all_candidates = result.top_results + result.near_misses
    # At least one candidate must carry non-null graph_provenance (the E1a wiring is live).
    assert any(c.graph_provenance is not None for c in all_candidates), (
        "Expected at least one candidate with non-null graph_provenance after E1a wiring"
    )

    # Verify provenance structure: at least one TraversalStep with relationship set.
    for c in all_candidates:
        if c.graph_provenance is not None:
            assert len(c.graph_provenance.steps) >= 1
            step = c.graph_provenance.steps[0]
            assert step.entity == "AuthService"
            assert step.entity_id == "entity-authservice"
            assert step.relationship == "depends_on"
            break  # verified one is enough


# ---------------------------------------------------------------------------
# Issue 3: provenance survives reranking (rerank=True path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_explain_naive_provenance_survives_reranking() -> None:
    """Graph provenance must still be non-null after reranking merges the candidate pool."""
    prov = _stub_provenance()
    graph_cand = _candidate("doc1-000000", graph_provenance=prov)
    hybrid_cand = _candidate("doc1-000001", graph_provenance=None)

    mock_store = MagicMock()
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[hybrid_cand])

    pipeline = _make_pipeline(mock_store=mock_store)
    # Override predict to accept exactly 2 candidates (graph + hybrid)
    pipeline._reranker._backend.predict = MagicMock(return_value=[0.9, 0.5])  # type: ignore[union-attr]
    pipeline._explain_naive_graph_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[graph_cand]
    )

    # rerank=True is the default — do NOT pass rerank=False
    result = await pipeline.explain(
        "test query",
        collection="col",
        graph_mode="naive",
    )

    assert result.graph_mode_applied == "naive"
    all_candidates = result.top_results + result.near_misses
    # The graph candidate must still carry non-null provenance after reranking
    graph_chunks = [c for c in all_candidates if c.chunk_id == "doc1-000000"]
    assert graph_chunks, "graph candidate should appear in results"
    assert graph_chunks[0].graph_provenance is not None, (
        "graph_provenance must survive the reranking dataclasses.replace path"
    )


# ---------------------------------------------------------------------------
# Issue 4: exception fallback paths in _explain_naive_graph_candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_explain_naive_find_nodes_raises_falls_back_to_standard() -> None:
    """When find_nodes_by_name raises, explain() falls back to standard hybrid (null provenance)."""
    hybrid_cand = _candidate("hybrid-chunk", graph_provenance=None)

    mock_store = MagicMock()
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[hybrid_cand])

    pipeline = _make_pipeline(mock_store=mock_store)

    # Inject a mock graph store where find_nodes_by_name raises
    mock_graph_store = MagicMock()
    mock_graph_store.find_nodes_by_name = AsyncMock(side_effect=RuntimeError("DB unavailable"))
    pipeline._graph_store = mock_graph_store  # type: ignore[assignment]

    result = await pipeline.explain(
        "test query",
        collection="col",
        graph_mode="naive",
        rerank=False,
    )

    assert result.graph_mode_applied == "naive"
    all_candidates = result.top_results + result.near_misses
    assert len(all_candidates) >= 1, "fallback hybrid should return at least one candidate"
    for c in all_candidates:
        assert c.graph_provenance is None, "fallback candidates must have null provenance"


@pytest.mark.asyncio
async def test_pipeline_explain_naive_get_edges_raises_returns_empty_steps() -> None:
    """When get_edges_for_nodes raises, steps=[] → no TraversalStep → _explain_naive returns [] → standard hybrid fallback."""
    hybrid_cand = _candidate("hybrid-chunk", graph_provenance=None)

    mock_store = MagicMock()
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[hybrid_cand])

    pipeline = _make_pipeline(mock_store=mock_store)

    # Inject a mock graph store: find_nodes_by_name succeeds but get_edges_for_nodes raises.
    # Also stub get_neighbours so the code reaches the edges step.
    from archon_search.graph_types import EntityType as ET, GraphNode, RelationshipType as RT

    matched_node = GraphNode(
        id="entity-foo",
        entity_name="AuthService",
        entity_type=ET.system,
        source_doc_id="doc-1",
        collection_name="col",
    )
    neighbour_node = GraphNode(
        id="entity-bar",
        entity_name="TokenValidator",
        entity_type=ET.system,
        source_doc_id="doc-1",
        collection_name="col",
    )

    mock_graph_store = MagicMock()
    mock_graph_store.find_nodes_by_name = AsyncMock(return_value=[matched_node])
    mock_graph_store.get_edges_for_nodes = AsyncMock(side_effect=RuntimeError("edges table missing"))
    mock_graph_store.get_neighbours = AsyncMock(return_value=[neighbour_node])
    pipeline._graph_store = mock_graph_store  # type: ignore[assignment]

    result = await pipeline.explain(
        "AuthService",
        collection="col",
        graph_mode="naive",
        rerank=False,
    )

    assert result.graph_mode_applied == "naive"
    all_candidates = result.top_results + result.near_misses
    # edges=[] → steps=[] → _explain_naive returns [] → standard hybrid fallback
    for c in all_candidates:
        assert c.graph_provenance is None, "fallback candidates must have null provenance when edges raised"

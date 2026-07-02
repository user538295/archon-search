"""BE-8: Tests for pipeline.explain() community-mode (local/global) traversal wiring.

Covers:
- ``test_pipeline_explain_local_community_steps`` (S3)
- ``test_pipeline_explain_global_community_steps`` (S4)
- ``test_pipeline_explain_community_modes_real`` (integration — real pipeline + stubbed E1b)

Scenarios satisfied:
  S3  — local mode: community traversal steps with community_id populated
  S4  — global mode: community traversal steps with community_id populated
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._diagnostics import (
    GraphProvenance,
    ScoredSearchCandidate,
    SearchScoreBreakdown,
    TraversalStep,
)
from archon_search.graph_types import Community, EntityType, GraphNode
from archon_search.pipeline import ExplainPipelineResult, GraphCommunitiesNotBuiltError, SearchPipeline


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


def _candidate(chunk_id: str = "doc1-000000") -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id="doc1",
        chunk_id=chunk_id,
        text="hello world",
        source_path="/tmp/foo.md",
        score_breakdown=_breakdown(),
        collection="col",
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
    mock_reranker_backend.predict = MagicMock(return_value=[0.5])

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


def _community(
    community_id: str,
    entity_ids: list[str],
    chunk_ids: list[str],
) -> Community:
    return Community(
        community_id=community_id,
        entity_ids=entity_ids,
        representative_chunk_ids=chunk_ids,
        built_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Unit tests — local mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_explain_local_community_steps() -> None:
    """graph_mode='local': mock E1b returning community candidates;
    TraversalStep has community_id set, relationship null (S3)."""
    comm = _community("comm-1", ["entity-foo"], ["chunk-a", "chunk-b"])

    # Store returns two chunk rows
    mock_store = MagicMock()
    mock_store.get_chunks_by_ids = AsyncMock(return_value=[
        {
            "doc_id": "doc1",
            "chunk_id": "chunk-a",
            "text": "text a",
            "source_path": "/tmp/a.md",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "collection": "col",
            "acl": None,
            "file_type": None,
            "indexed_at": None,
            "updated_at": None,
            "ingested_by": None,
            "language": None,
            "metadata": None,
        },
        {
            "doc_id": "doc1",
            "chunk_id": "chunk-b",
            "text": "text b",
            "source_path": "/tmp/b.md",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "collection": "col",
            "acl": None,
            "file_type": None,
            "indexed_at": None,
            "updated_at": None,
            "ingested_by": None,
            "language": None,
            "metadata": None,
        },
    ])
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[])

    pipeline = _make_pipeline(mock_store=mock_store)

    # Stub _explain_community_candidates to return two candidates with provenance
    cand_a = _candidate("chunk-a")
    cand_b = _candidate("chunk-b")
    step_a = TraversalStep(entity="EntityFoo", entity_id="entity-foo", community_id="comm-1")
    cand_a.graph_provenance = GraphProvenance(steps=[step_a])
    cand_b.graph_provenance = GraphProvenance(steps=[step_a])

    pipeline._explain_community_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[cand_a, cand_b]
    )

    result = await pipeline.explain(
        "test query",
        collection="col",
        graph_mode="local",
        rerank=False,
    )

    assert isinstance(result, ExplainPipelineResult)
    assert result.graph_mode_applied == "local"
    assert result.rag_fusion_applied is False

    all_candidates = result.top_results + result.near_misses
    assert len(all_candidates) >= 1

    # All community-retrieved candidates must have community_id set in TraversalStep
    for c in all_candidates:
        assert c.graph_provenance is not None, f"chunk {c.chunk_id} should have graph_provenance"
        assert len(c.graph_provenance.steps) >= 1
        step = c.graph_provenance.steps[0]
        assert step.community_id is not None, "TraversalStep must have community_id set (S3)"
        assert step.relationship is None, "relationship should be None in community mode"


# ---------------------------------------------------------------------------
# Unit tests — global mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_explain_global_community_steps() -> None:
    """graph_mode='global': mock E1b returning community candidates;
    TraversalStep has community_id set, relationship null (S4)."""
    cand_a = _candidate("chunk-x")
    step = TraversalStep(entity="comm-99", entity_id="comm-99", community_id="comm-99")
    cand_a.graph_provenance = GraphProvenance(steps=[step])

    mock_store = MagicMock()
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[])

    pipeline = _make_pipeline(mock_store=mock_store)

    pipeline._explain_community_candidates = AsyncMock(  # type: ignore[method-assign]
        return_value=[cand_a]
    )

    result = await pipeline.explain(
        "test query",
        collection="col",
        graph_mode="global",
        rerank=False,
    )

    assert isinstance(result, ExplainPipelineResult)
    assert result.graph_mode_applied == "global"
    assert result.rag_fusion_applied is False

    all_candidates = result.top_results + result.near_misses
    assert len(all_candidates) >= 1

    for c in all_candidates:
        assert c.graph_provenance is not None, f"chunk {c.chunk_id} should have graph_provenance"
        step = c.graph_provenance.steps[0]
        assert step.community_id is not None, "TraversalStep must have community_id set (S4)"
        assert step.relationship is None, "relationship should be None in global mode"


# ---------------------------------------------------------------------------
# Unit tests — fallback: empty community candidates falls back to standard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_explain_local_empty_candidates_fallback() -> None:
    """When _explain_community_candidates returns [], fallback to _explain_standard with graph_mode_applied set."""
    hybrid_cand = _candidate("hybrid-chunk")
    mock_store = MagicMock()
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[hybrid_cand])

    pipeline = _make_pipeline(mock_store=mock_store)
    # Stub: community candidates empty (no entity match / no communities)
    pipeline._explain_community_candidates = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await pipeline.explain(
        "query",
        collection="col",
        graph_mode="local",
        rerank=False,
    )

    assert result.graph_mode_applied == "local"
    # All candidates from fallback have null provenance
    all_candidates = result.top_results + result.near_misses
    for c in all_candidates:
        assert c.graph_provenance is None


# ---------------------------------------------------------------------------
# Integration test — real pipeline + stubbed E1b graph layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_explain_community_modes_real(tmp_path, monkeypatch) -> None:
    """Real SearchPipeline + stubbed E1b community layer.

    (a) graph_mode='local' → graph_mode_applied=='local'; steps carry community_id.
    (b) graph_mode='global' → graph_mode_applied=='global'; steps carry community_id.

    The E1b graph layer is stubbed via mocks on pipeline._graph_store so that
    community lookup succeeds without real graph data in LanceDB.
    """
    import hashlib

    from archon_search._types import ChunkRecord
    from archon_search.graph_types import EntityType
    from tests.integration.conftest import make_real_pipeline

    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    doc_id = hashlib.sha256(b"be8-community-doc").hexdigest()
    chunk = ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text="PaymentService handles all payment transactions",
        vector=[0.1, 0.2, 0.3, 0.4],
        source_path="/tmp/payment.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )
    await store.ensure_collection("be8-col", embedding_dim=4)
    await store.ingest_chunks("be8-col", [chunk])

    chunk_id = f"{doc_id}-000000"
    community = _community("comm-payments", ["entity-pay"], [chunk_id])

    matched_node = GraphNode(
        id="entity-pay",
        entity_name="PaymentService",
        entity_type=EntityType.system,
        source_doc_id=doc_id,
        collection_name="be8-col",
    )

    mock_graph_store = MagicMock()
    mock_graph_store.find_nodes_by_name = AsyncMock(return_value=[matched_node])
    mock_graph_store.communities_table_exists = AsyncMock(return_value=True)
    mock_graph_store.get_communities_for_entities = AsyncMock(return_value=[community])
    mock_graph_store.list_community_representatives = AsyncMock(return_value=[community])

    pipeline._graph_store = mock_graph_store  # type: ignore[assignment]

    # (a) local mode
    result_local = await pipeline.explain(
        "PaymentService",
        collection="be8-col",
        graph_mode="local",
    )

    assert isinstance(result_local, ExplainPipelineResult)
    assert result_local.graph_mode_applied == "local"
    assert result_local.rag_fusion_applied is False

    all_local = result_local.top_results + result_local.near_misses
    assert any(c.graph_provenance is not None for c in all_local), (
        "Expected at least one candidate with graph_provenance in local mode"
    )
    for c in all_local:
        if c.graph_provenance is not None:
            assert len(c.graph_provenance.steps) >= 1
            step = c.graph_provenance.steps[0]
            assert step.community_id == "comm-payments", (
                f"Expected community_id='comm-payments', got {step.community_id!r}"
            )
            assert step.relationship is None

    # (b) global mode
    result_global = await pipeline.explain(
        "PaymentService",
        collection="be8-col",
        graph_mode="global",
    )

    assert isinstance(result_global, ExplainPipelineResult)
    assert result_global.graph_mode_applied == "global"
    assert result_global.rag_fusion_applied is False

    all_global = result_global.top_results + result_global.near_misses
    assert any(c.graph_provenance is not None for c in all_global), (
        "Expected at least one candidate with graph_provenance in global mode"
    )
    for c in all_global:
        if c.graph_provenance is not None:
            assert len(c.graph_provenance.steps) >= 1
            step = c.graph_provenance.steps[0]
            assert step.community_id is not None, "TraversalStep must have community_id set in global mode"
            assert step.relationship is None


# ---------------------------------------------------------------------------
# Unit tests — _explain_community_candidates direct tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_community_candidates_global_no_communities_raises() -> None:
    """When list_community_representatives returns [], raise GraphCommunitiesNotBuiltError."""
    pipeline = _make_pipeline()
    mock_graph_store = MagicMock()
    mock_graph_store.list_community_representatives = AsyncMock(return_value=[])
    pipeline._graph_store = mock_graph_store

    with pytest.raises(GraphCommunitiesNotBuiltError):
        await pipeline._explain_community_candidates("query", "col", "global")


@pytest.mark.asyncio
async def test_explain_community_candidates_local_no_entity_match_returns_empty() -> None:
    """Local mode: when find_nodes_by_name returns [], return []."""
    pipeline = _make_pipeline()
    mock_graph_store = MagicMock()
    mock_graph_store.find_nodes_by_name = AsyncMock(return_value=[])
    pipeline._graph_store = mock_graph_store

    result = await pipeline._explain_community_candidates("query", "col", "local")
    assert result == []


@pytest.mark.asyncio
async def test_explain_community_candidates_local_rep_node_fallback() -> None:
    """When community.entity_ids don't overlap matched_nodes, community_id used as entity_name."""
    comm = Community(
        community_id="comm-1",
        entity_ids=["entity-unmatched"],
        representative_chunk_ids=["chunk-z"],
        summary_text="",
        built_at=None,
    )
    matched_node = GraphNode(
        id="entity-other",
        entity_name="OtherEntity",
        entity_type=EntityType.system,
        source_doc_id="doc1",
        collection_name="col",
    )

    chunk_row = {
        "doc_id": "doc1",
        "chunk_id": "chunk-z",
        "text": "some text",
        "source_path": "/tmp/z.md",
        "vector": [0.1, 0.2, 0.3, 0.4],
        "collection": "col",
        "acl": None, "file_type": None, "indexed_at": None,
        "updated_at": None, "ingested_by": None, "language": None, "metadata": None,
    }

    pipeline = _make_pipeline()
    mock_store = MagicMock()
    mock_store.get_chunks_by_ids = AsyncMock(return_value=[chunk_row])
    pipeline.store = mock_store

    mock_graph_store = MagicMock()
    mock_graph_store.find_nodes_by_name = AsyncMock(return_value=[matched_node])
    mock_graph_store.communities_table_exists = AsyncMock(return_value=True)
    mock_graph_store.get_communities_for_entities = AsyncMock(return_value=[comm])
    pipeline._graph_store = mock_graph_store

    result = await pipeline._explain_community_candidates("OtherEntity", "col", "local")
    # rep_node is None (entity-unmatched not in matched_nodes), so community_id used
    assert len(result) == 1
    step = result[0].graph_provenance.steps[0]
    assert step.entity == "comm-1"  # fallback to community_id
    assert step.entity_id == "comm-1"
    assert step.community_id == "comm-1"


@pytest.mark.asyncio
async def test_pipeline_explain_community_provenance_survives_reranking() -> None:
    """GraphProvenance must still be present after reranking in community mode."""
    cand = _candidate("chunk-comm")
    step = TraversalStep(entity="E", entity_id="e-id", community_id="comm-1")
    cand.graph_provenance = GraphProvenance(steps=[step])

    mock_store = MagicMock()
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[])
    pipeline = _make_pipeline(mock_store=mock_store)
    pipeline._reranker._backend.predict = MagicMock(return_value=[0.9])
    pipeline._explain_community_candidates = AsyncMock(return_value=[cand])  # type: ignore[method-assign]

    result = await pipeline.explain("query", collection="col", graph_mode="local")
    all_candidates = result.top_results + result.near_misses
    comm_chunks = [c for c in all_candidates if c.chunk_id == "chunk-comm"]
    assert comm_chunks
    assert comm_chunks[0].graph_provenance is not None
    assert comm_chunks[0].graph_provenance.steps[0].community_id == "comm-1"


@pytest.mark.asyncio
async def test_pipeline_explain_community_merge_community_wins() -> None:
    """When same chunk appears in community and hybrid results, community provenance wins."""
    cand = _candidate("shared-chunk")
    step = TraversalStep(entity="E", entity_id="e-id", community_id="comm-1")
    cand.graph_provenance = GraphProvenance(steps=[step])

    hybrid_same_chunk = _candidate("shared-chunk")  # no provenance

    mock_store = MagicMock()
    mock_store.hybrid_search_with_trace = AsyncMock(return_value=[hybrid_same_chunk])
    pipeline = _make_pipeline(mock_store=mock_store)
    pipeline._explain_community_candidates = AsyncMock(return_value=[cand])  # type: ignore[method-assign]

    result = await pipeline.explain("query", collection="col", graph_mode="local", rerank=False)
    all_candidates = result.top_results + result.near_misses
    # chunk appears exactly once
    assert sum(1 for c in all_candidates if c.chunk_id == "shared-chunk") == 1
    # and has community provenance
    shared = next(c for c in all_candidates if c.chunk_id == "shared-chunk")
    assert shared.graph_provenance is not None
    assert shared.graph_provenance.steps[0].community_id == "comm-1"


@pytest.mark.asyncio
async def test_explain_community_candidates_no_graph_store_returns_empty() -> None:
    """When _graph_store is None, return [] without raising."""
    pipeline = _make_pipeline()
    pipeline._graph_store = None  # type: ignore[assignment]

    result = await pipeline._explain_community_candidates("query", "col", "local")
    assert result == []

    result = await pipeline._explain_community_candidates("query", "col", "global")
    assert result == []


@pytest.mark.asyncio
async def test_explain_community_candidates_global_cap_respected() -> None:
    """Global mode caps chunk_ids at max_global_candidates; does not overshoot."""
    from archon_search.config import GraphConfig

    # Build a community with 10 chunk IDs and set max_global_candidates=5.
    chunk_ids = [f"chunk-{i}" for i in range(10)]
    comm = _community("comm-big", ["entity-x"], chunk_ids)

    rows = [
        {
            "doc_id": "doc1",
            "chunk_id": f"chunk-{i}",
            "text": f"text {i}",
            "source_path": f"/tmp/{i}.md",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "collection": "col",
            "acl": None, "file_type": None, "indexed_at": None,
            "updated_at": None, "ingested_by": None, "language": None, "metadata": None,
        }
        for i in range(10)
    ]

    pipeline = _make_pipeline()
    mock_store = MagicMock()
    # Track how many chunk_ids the store was asked for.
    requested_chunk_ids: list[str] = []

    async def _capture_get_chunks(collection: str, ids: list[str]) -> list[dict]:
        requested_chunk_ids.extend(ids)
        return [r for r in rows if r["chunk_id"] in ids]

    mock_store.get_chunks_by_ids = _capture_get_chunks
    pipeline.store = mock_store

    mock_graph_store = MagicMock()
    mock_graph_store.list_community_representatives = AsyncMock(return_value=[comm])
    pipeline._graph_store = mock_graph_store

    # Set max_global_candidates = 5 via graph config.
    graph_config = GraphConfig(enabled=True, max_global_candidates=5)
    pipeline._graph_config = graph_config  # type: ignore[assignment]

    result = await pipeline._explain_community_candidates("any query", "col", "global")

    assert len(requested_chunk_ids) == 5, (
        f"Expected exactly 5 chunk IDs requested, got {len(requested_chunk_ids)}: {requested_chunk_ids}"
    )
    assert len(result) == 5


@pytest.mark.asyncio
async def test_explain_community_candidates_local_table_not_exists_raises() -> None:
    """Local mode: communities table does not exist → GraphCommunitiesNotBuiltError (mirrors global)."""
    pipeline = _make_pipeline()
    mock_graph_store = MagicMock()
    matched_node = GraphNode(
        id="entity-1",
        entity_name="PaymentService",
        entity_type=EntityType.system,
        source_doc_id="doc1",
        collection_name="col",
    )
    mock_graph_store.find_nodes_by_name = AsyncMock(return_value=[matched_node])
    mock_graph_store.communities_table_exists = AsyncMock(return_value=False)
    pipeline._graph_store = mock_graph_store

    with pytest.raises(GraphCommunitiesNotBuiltError):
        await pipeline._explain_community_candidates("PaymentService", "col", "local")


@pytest.mark.asyncio
async def test_explain_community_candidates_local_cap_respected() -> None:
    """Local mode caps chunk_ids at _MAX_LOCAL_EXPLAIN_COMMUNITY_CANDIDATES; does not overshoot."""
    from archon_search.pipeline import _MAX_LOCAL_EXPLAIN_COMMUNITY_CANDIDATES

    # Build 3 communities each with 100 chunk IDs → total 300 > cap.
    all_chunk_ids = [f"chunk-{i}" for i in range(300)]
    communities = [
        _community(f"comm-{c}", ["entity-pay"], all_chunk_ids[c * 100 : (c + 1) * 100])
        for c in range(3)
    ]

    # Rows for all chunks.
    def _row(cid: str) -> dict:
        return {
            "doc_id": "doc1", "chunk_id": cid, "text": f"text {cid}",
            "source_path": "/tmp/x.md", "vector": [0.1, 0.2, 0.3, 0.4],
            "collection": "col", "acl": None, "file_type": None, "indexed_at": None,
            "updated_at": None, "ingested_by": None, "language": None, "metadata": None,
        }

    requested_chunk_ids: list[str] = []

    async def _capture_get_chunks(collection: str, ids: list[str]) -> list[dict]:
        requested_chunk_ids.extend(ids)
        return [_row(cid) for cid in ids]

    matched_node = GraphNode(
        id="entity-pay",
        entity_name="PaymentService",
        entity_type=EntityType.system,
        source_doc_id="doc1",
        collection_name="col",
    )

    pipeline = _make_pipeline()
    mock_store = MagicMock()
    mock_store.get_chunks_by_ids = _capture_get_chunks
    pipeline.store = mock_store

    mock_graph_store = MagicMock()
    mock_graph_store.find_nodes_by_name = AsyncMock(return_value=[matched_node])
    mock_graph_store.communities_table_exists = AsyncMock(return_value=True)
    mock_graph_store.get_communities_for_entities = AsyncMock(return_value=communities)
    pipeline._graph_store = mock_graph_store

    result = await pipeline._explain_community_candidates("PaymentService", "col", "local")

    assert len(requested_chunk_ids) <= _MAX_LOCAL_EXPLAIN_COMMUNITY_CANDIDATES, (
        f"Expected at most {_MAX_LOCAL_EXPLAIN_COMMUNITY_CANDIDATES} chunk IDs requested; "
        f"got {len(requested_chunk_ids)}"
    )
    assert len(result) <= _MAX_LOCAL_EXPLAIN_COMMUNITY_CANDIDATES


@pytest.mark.asyncio
async def test_explain_community_candidates_local_overlapping_chunks_first_wins() -> None:
    """When two communities share a representative chunk, the first community's provenance wins."""
    shared_chunk_id = "shared-chunk"

    comm_first = _community("comm-first", ["entity-1"], [shared_chunk_id, "chunk-a"])
    comm_second = _community("comm-second", ["entity-2"], [shared_chunk_id, "chunk-b"])

    rows = [
        {
            "doc_id": "doc1", "chunk_id": shared_chunk_id, "text": "shared text",
            "source_path": "/tmp/s.md", "vector": [0.1, 0.2, 0.3, 0.4],
            "collection": "col", "acl": None, "file_type": None, "indexed_at": None,
            "updated_at": None, "ingested_by": None, "language": None, "metadata": None,
        },
        {
            "doc_id": "doc1", "chunk_id": "chunk-a", "text": "chunk a text",
            "source_path": "/tmp/a.md", "vector": [0.1, 0.2, 0.3, 0.4],
            "collection": "col", "acl": None, "file_type": None, "indexed_at": None,
            "updated_at": None, "ingested_by": None, "language": None, "metadata": None,
        },
        {
            "doc_id": "doc1", "chunk_id": "chunk-b", "text": "chunk b text",
            "source_path": "/tmp/b.md", "vector": [0.1, 0.2, 0.3, 0.4],
            "collection": "col", "acl": None, "file_type": None, "indexed_at": None,
            "updated_at": None, "ingested_by": None, "language": None, "metadata": None,
        },
    ]

    matched_nodes = [
        GraphNode(id="entity-1", entity_name="Entity1", entity_type=EntityType.system, source_doc_id="doc1", collection_name="col"),
        GraphNode(id="entity-2", entity_name="Entity2", entity_type=EntityType.system, source_doc_id="doc1", collection_name="col"),
    ]

    pipeline = _make_pipeline()
    mock_store = MagicMock()
    mock_store.get_chunks_by_ids = AsyncMock(return_value=rows)
    pipeline.store = mock_store

    mock_graph_store = MagicMock()
    mock_graph_store.find_nodes_by_name = AsyncMock(return_value=matched_nodes)
    mock_graph_store.communities_table_exists = AsyncMock(return_value=True)
    mock_graph_store.get_communities_for_entities = AsyncMock(return_value=[comm_first, comm_second])
    pipeline._graph_store = mock_graph_store

    result = await pipeline._explain_community_candidates("Entity1 Entity2", "col", "local")

    # shared-chunk should appear exactly once.
    shared_chunks = [c for c in result if c.chunk_id == shared_chunk_id]
    assert len(shared_chunks) == 1, (
        f"Expected shared-chunk to appear exactly once; got {len(shared_chunks)}"
    )
    # Its provenance should be from comm-first (first-write-wins).
    shared = shared_chunks[0]
    assert shared.graph_provenance is not None
    assert shared.graph_provenance.steps[0].community_id == "comm-first", (
        f"Expected comm-first to win; got {shared.graph_provenance.steps[0].community_id!r}"
    )

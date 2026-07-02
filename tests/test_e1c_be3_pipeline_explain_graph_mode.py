"""BE-3: Tests for pipeline.explain() graph_mode parameter — null pass-through path.

Covers:
- ``pipeline.explain()`` accepts ``graph_mode`` kwarg
- ``ExplainPipelineResult.graph_mode_applied`` is set correctly
- When ``graph_mode`` is non-null, ``rag_fusion_applied=False`` (graph_mode path bypasses RAG Fusion)
- Round-trip: ``ExplainResult.from_candidate()`` with non-null ``GraphProvenance`` serialises correctly

Scenarios: S1 (null pass-through), S13 (explicit null = null).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._diagnostics import (
    GraphProvenance,
    ScoredSearchCandidate,
    SearchScoreBreakdown,
    TraversalStep,
)
from archon_search.pipeline import ExplainPipelineResult, SearchPipeline
from archon_search.server.routes_explain import ExplainResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _breakdown() -> SearchScoreBreakdown:
    return SearchScoreBreakdown(
        vector_rank=None,
        vector_score=None,
        vector_score_kind=None,
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=0.5,
        reranker_score=None,
    )


def _candidate(**kwargs) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id="doc1",
        chunk_id="doc1-000000",
        text="hello world",
        source_path="/tmp/foo.md",
        score_breakdown=_breakdown(),
        collection="col",
        **kwargs,
    )


def _make_pipeline(store=None, *, rag_fusion_generator=None) -> SearchPipeline:
    """Create a minimal mocked SearchPipeline for unit tests."""
    mock_store = store or MagicMock()
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
        store=mock_store,
        embedder=Embedder(mock_embedder_backend),
        reranker=Reranker(mock_reranker_backend),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_explain_graph_mode_none_returns_null_applied() -> None:
    """graph_mode=None → result.graph_mode_applied is None (S1, S13)."""
    pipeline = _make_pipeline()
    # Stub _explain_standard to avoid real store calls.
    expected = ExplainPipelineResult(
        top_results=[],
        near_misses=[],
        acl_filtered=False,
        graph_mode_applied=None,
    )
    pipeline._explain_standard = AsyncMock(return_value=expected)  # type: ignore[method-assign]

    result = await pipeline.explain(
        "test query",
        collection="col",
        graph_mode=None,
    )

    assert result.graph_mode_applied is None


@pytest.mark.asyncio
@pytest.mark.parametrize("graph_mode_value", ["naive", "local", "global"])
async def test_pipeline_explain_graph_mode_stub_returns_applied(graph_mode_value: str) -> None:
    """graph_mode='naive'/'local'/'global' → result.graph_mode_applied set to requested mode;
    all candidates have graph_provenance=None (stub pre-E1a)."""
    candidate = _candidate(graph_provenance=None)
    standard_result = ExplainPipelineResult(
        top_results=[candidate], near_misses=[], acl_filtered=False, graph_mode_applied=None
    )
    pipeline = _make_pipeline()
    pipeline._explain_standard = AsyncMock(return_value=standard_result)  # type: ignore[method-assign]

    result = await pipeline.explain(
        "test query",
        collection="col",
        graph_mode=graph_mode_value,  # type: ignore[arg-type]
    )

    assert result.graph_mode_applied == graph_mode_value
    pipeline._explain_standard.assert_called_once()
    for c in result.top_results:
        assert c.graph_provenance is None


@pytest.mark.asyncio
@pytest.mark.parametrize("graph_mode_value", ["naive", "local", "global"])
async def test_pipeline_explain_graph_mode_sets_rag_fusion_applied_false(graph_mode_value: str) -> None:
    """graph_mode bypasses RAG Fusion: rag_fusion_applied=False regardless of rag_fusion=True flag."""
    pipeline = _make_pipeline()
    # Pre-set rag_fusion_applied=True so the override to False is actually tested.
    standard_result = ExplainPipelineResult(
        top_results=[], near_misses=[], acl_filtered=False, rag_fusion_applied=True
    )
    pipeline._explain_standard = AsyncMock(return_value=standard_result)  # type: ignore[method-assign]

    mock_rf_generator = AsyncMock()
    mock_rf_config = MagicMock()
    mock_rf_config.enabled = True

    result = await pipeline.explain(
        "test query",
        collection="col",
        graph_mode=graph_mode_value,  # type: ignore[arg-type]
        rag_fusion=True,
        rag_fusion_generator=mock_rf_generator,
        rag_fusion_config=mock_rf_config,
    )

    assert result.rag_fusion_applied is False
    assert result.rag_fusion_attempted is False
    mock_rf_generator.generate_variants.assert_not_called()


def test_explain_result_from_candidate_graph_provenance_round_trip() -> None:
    """ScoredSearchCandidate with non-null GraphProvenance → ExplainResult.graph_provenance populated and JSON-serializable."""
    step = TraversalStep(entity="Entity", entity_id="abc123", chunk_id="chunk1")
    prov = GraphProvenance(steps=[step])
    c = _candidate(graph_provenance=prov)

    result = ExplainResult.from_candidate(c)

    assert result.graph_provenance is not None
    assert len(result.graph_provenance.steps) == 1
    assert result.graph_provenance.steps[0].entity == "Entity"
    assert result.graph_provenance.steps[0].entity_id == "abc123"
    assert result.graph_provenance.steps[0].chunk_id == "chunk1"

    # Must serialize to JSON without error.
    serialised = result.model_dump_json()
    parsed = json.loads(serialised)
    assert parsed["graph_provenance"] is not None
    assert parsed["graph_provenance"]["steps"][0]["entity"] == "Entity"
    assert parsed["graph_provenance"]["steps"][0]["entity_id"] == "abc123"
    assert parsed["graph_provenance"]["steps"][0]["chunk_id"] == "chunk1"


# ---------------------------------------------------------------------------
# Integration test — real SearchPipeline with graph_mode=None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_explain_graph_mode_none_real_pipeline(tmp_path, monkeypatch) -> None:
    """Real SearchPipeline with graph_mode=None → result.graph_mode_applied is None.

    Behaviour must be identical to a pre-E1c explain call (S1).
    Collection is created with one ingested document.
    """
    from tests.integration.conftest import make_real_pipeline

    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    # Create a collection and ingest one chunk so explain has something to query.
    import hashlib
    from datetime import UTC, datetime

    from archon_search._types import ChunkRecord

    doc_id = hashlib.sha256(b"doc1").hexdigest()
    chunk = ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text="hello world this is a test document",
        vector=[0.1, 0.2, 0.3, 0.4],
        source_path="/tmp/doc1.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )
    await store.ensure_collection("test-col", embedding_dim=4)
    await store.ingest_chunks("test-col", [chunk])

    result = await pipeline.explain(
        "hello world",
        collection="test-col",
        graph_mode=None,
    )

    assert isinstance(result, ExplainPipelineResult)
    assert result.graph_mode_applied is None
    # All candidates must have graph_provenance=None in the null path.
    for c in result.top_results:
        assert c.graph_provenance is None
    for c in result.near_misses:
        assert c.graph_provenance is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_pipeline_explain_graph_mode_naive_real_pipeline(tmp_path, monkeypatch) -> None:
    """Real SearchPipeline with graph_mode='naive' → result.graph_mode_applied='naive'.

    This test exercises the actual new code path (graph_mode bypass block) with
    a real SearchStore + LanceDB. All candidates still have graph_provenance=None
    in the null pass-through stub (pre-E1a wiring).
    """
    import hashlib
    from datetime import UTC, datetime

    from archon_search._types import ChunkRecord
    from tests.integration.conftest import make_real_pipeline

    store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)

    doc_id = hashlib.sha256(b"doc-naive").hexdigest()
    chunk = ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-000000",
        text="hello world naive graph mode test",
        vector=[0.1, 0.2, 0.3, 0.4],
        source_path="/tmp/doc-naive.md",
        indexed_at=datetime.now(UTC).isoformat(),
    )
    await store.ensure_collection("test-col-naive", embedding_dim=4)
    await store.ingest_chunks("test-col-naive", [chunk])

    result = await pipeline.explain(
        "hello world",
        collection="test-col-naive",
        graph_mode="naive",
    )

    assert isinstance(result, ExplainPipelineResult)
    assert result.graph_mode_applied == "naive"
    assert result.rag_fusion_applied is False
    assert result.rag_fusion_attempted is False
    for c in result.top_results:
        assert c.graph_provenance is None
    for c in result.near_misses:
        assert c.graph_provenance is None


@pytest.mark.asyncio
async def test_pipeline_explain_graph_mode_with_collections_raises() -> None:
    """graph_mode + collections → ValueError (multi-collection graph_mode not supported in E1c)."""
    pipeline = _make_pipeline()

    with pytest.raises(ValueError, match="graph_mode is not supported with multi-collection"):
        await pipeline.explain(
            "test query",
            collections=["col-a", "col-b"],
            graph_mode="naive",
        )

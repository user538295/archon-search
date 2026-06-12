"""TDD tests for SearchPipeline with optional reranker (Task 1.4 — C0 tiered install profiles)."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search.chunker import DocumentChunker
from archon_search.config import SearchConfig
from archon_search.embedder import Embedder, EmbedderBackend
from archon_search.parser import DocumentParser
from archon_search.pipeline import SearchPipeline
from archon_search.reranker import Reranker, RerankerBackend


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class MockEmbedderBackend:
    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]


def make_embedder() -> Embedder:
    return Embedder(MockEmbedderBackend())


def _make_candidate(rrf_score: float = 0.5, reranker_score: float | None = None) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id="doc1",
        chunk_id="doc1-000000",
        text="hello world",
        source_path="/tmp/doc.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=1,
            vector_score=0.9,
            vector_score_kind="similarity",
            fts_rank=None,
            fts_score=None,
            fts_score_kind=None,
            rrf_score=rrf_score,
            reranker_score=reranker_score,
        ),
        collection="col1",
    )


def make_pipeline_no_reranker(store: Any) -> SearchPipeline:
    """Build a SearchPipeline with reranker=None."""
    return SearchPipeline(
        store=store,
        embedder=make_embedder(),
        reranker=None,
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=10,
        top_k_return=3,
    )


def _make_mock_store(candidates: list[ScoredSearchCandidate]) -> Any:
    """Return a minimal mock store that supports hybrid_search and hybrid_search_with_trace."""
    store = MagicMock()
    store._config = SearchConfig()
    store.hybrid_search = AsyncMock(return_value=candidates)
    store.hybrid_search_with_trace = AsyncMock(return_value=candidates)
    store.get_all_collections_meta = AsyncMock(return_value=[])
    return store


# ---------------------------------------------------------------------------
# Test 1: create_pipeline() returns reranker=None when reranker_model is ""
# ---------------------------------------------------------------------------

def test_pipeline_skips_reranker_when_model_is_empty() -> None:
    cfg = SearchConfig(reranker_model="")
    with patch("archon_search.pipeline.ModelReranker") as mock_model_reranker, \
         patch("archon_search.pipeline.SearchStore"), \
         patch("archon_search.pipeline.ModelEmbedder"):
        from archon_search.pipeline import create_pipeline
        pipeline = create_pipeline(cfg)

    mock_model_reranker.assert_not_called()
    assert pipeline._reranker is None


# ---------------------------------------------------------------------------
# Test 2: app.py skips ModelReranker when reranker_model is ""
# ---------------------------------------------------------------------------

def test_app_skips_reranker_when_config_model_empty(tmp_path) -> None:
    from archon_search.server.app import create_app
    from archon_search.jobs import JobStore

    cfg = SearchConfig(reranker_model="")
    # Isolate the jobs file under tmp_path so the test never touches
    # the developer's real ~/.archon-search (C9 Task 2.4 iterative review).
    job_store = JobStore(path=tmp_path / "jobs.json")

    with patch("archon_search.server.app.ModelReranker") as mock_model_reranker, \
         patch("archon_search.server.app.SearchStore"), \
         patch("archon_search.server.app.IndexingStateStore"), \
         patch("archon_search.server.app.ModelEmbedder"):
        create_app(cfg, job_store)

    mock_model_reranker.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: search() without reranker returns results
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_without_reranker_returns_results() -> None:
    candidates = [_make_candidate(rrf_score=0.8), _make_candidate(rrf_score=0.5)]
    store = _make_mock_store(candidates)
    pipeline = make_pipeline_no_reranker(store)

    result = await pipeline.search(query="test", collection="col1", embedder=pipeline._global_embedder)

    assert result is not None
    assert len(result.results) <= pipeline._top_k_return
    assert len(result.results) > 0
    # No AttributeError → reranker_score is None so rrf_score is used
    for r in result.results:
        assert isinstance(r.score, float)


# ---------------------------------------------------------------------------
# Test 4: _candidate_to_search_result() falls back to rrf_score when reranker_score is None
# ---------------------------------------------------------------------------

def test_candidate_to_search_result_uses_rrf_score_when_reranker_none() -> None:
    store = MagicMock()
    store._config = SearchConfig()
    pipeline = make_pipeline_no_reranker(store)

    candidate = _make_candidate(rrf_score=0.75, reranker_score=None)
    result = pipeline._candidate_to_search_result(candidate)

    assert result.score == 0.75


# ---------------------------------------------------------------------------
# Test 5: search_many() without reranker returns results
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_many_without_reranker_returns_results() -> None:
    from archon_search._types import CollectionInfo

    candidates = [_make_candidate(rrf_score=0.7)]
    store = _make_mock_store(candidates)

    from archon_search.constants import DEFAULT_NAMESPACE

    col_meta = MagicMock()
    col_meta.name = "col1"
    col_meta.active_embedding_model = "mock-embedder"
    col_meta.namespace = DEFAULT_NAMESPACE
    store.get_all_collections_meta = AsyncMock(return_value=[col_meta])

    pipeline = make_pipeline_no_reranker(store)

    result = await pipeline.search_many(query="test", collections=["col1"])

    assert result is not None
    assert len(result.results) >= 0
    for r in result.results:
        assert isinstance(r.score, float)


# ---------------------------------------------------------------------------
# Test 6: explain() single-collection without reranker returns results
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explain_without_reranker_returns_results() -> None:
    candidates = [_make_candidate(rrf_score=0.6)]
    store = _make_mock_store(candidates)
    pipeline = make_pipeline_no_reranker(store)

    result = await pipeline.explain(query="test", collection="col1", rerank=True)

    assert result is not None
    assert isinstance(result.top_results, list)
    # No AttributeError should have occurred
    assert not result.acl_filtered


# ---------------------------------------------------------------------------
# Test 7: explain() multi-collection without reranker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explain_multi_collection_without_reranker() -> None:
    candidates = [_make_candidate(rrf_score=0.6)]
    store = _make_mock_store(candidates)

    from archon_search.constants import DEFAULT_NAMESPACE

    col_meta1 = MagicMock()
    col_meta1.name = "col1"
    col_meta1.active_embedding_model = "mock-embedder"
    col_meta1.namespace = DEFAULT_NAMESPACE
    col_meta2 = MagicMock()
    col_meta2.name = "col2"
    col_meta2.active_embedding_model = "mock-embedder"
    col_meta2.namespace = DEFAULT_NAMESPACE
    store.get_all_collections_meta = AsyncMock(return_value=[col_meta1, col_meta2])

    pipeline = make_pipeline_no_reranker(store)

    result = await pipeline.explain(query="test", collections=["col1", "col2"], rerank=True)

    assert result is not None
    assert isinstance(result.top_results, list)
    # No AttributeError should have occurred


# ---------------------------------------------------------------------------
# Test 8: reranker_is_warm returns False when reranker is None
# ---------------------------------------------------------------------------

def test_pipeline_reranker_is_warm_returns_false_when_none() -> None:
    store = MagicMock()
    store._config = SearchConfig()
    pipeline = make_pipeline_no_reranker(store)

    assert pipeline.reranker_is_warm is False


# ---------------------------------------------------------------------------
# Test 9: explain multi-collection with rerank=False and reranker=None does NOT raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explain_multi_collection_rerank_false_no_reranker_does_not_raise() -> None:
    """When reranker is None, explain(collections=[...], rerank=False) must not raise ExplainMultiCollectionNoRerankError."""
    from archon_search.pipeline import DEFAULT_NAMESPACE

    candidate = _make_candidate(rrf_score=0.5)
    store = _make_mock_store([candidate])
    store.hybrid_search_with_trace = AsyncMock(return_value=[candidate])

    col_meta1 = MagicMock()
    col_meta1.name = "col1"
    col_meta1.active_embedding_model = "mock-embedder"
    col_meta1.namespace = DEFAULT_NAMESPACE
    store.get_all_collections_meta = AsyncMock(return_value=[col_meta1])

    pipeline = make_pipeline_no_reranker(store)

    # Must not raise ExplainMultiCollectionNoRerankError
    result = await pipeline.explain(query="test", collections=["col1"], rerank=False)
    assert result is not None

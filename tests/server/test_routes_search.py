"""Tests for POST /search endpoint (Task 2.1 + Task 3.4)."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search._types import SearchResult
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.pipeline import SearchPipeline, SearchPipelineResult
from archon_search.server.app import create_app


def _make_app(tmp_path: Path) -> tuple:
    """Create app and return (app, client) with pipeline mock on app.state."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return app, client


# ---------------------------------------------------------------------------
# create_app() pipeline wiring tests (Task 3.3 / 3.4)
# ---------------------------------------------------------------------------


def test_create_app_has_pipeline_in_state(tmp_path: Path) -> None:
    """create_app() must set app.state.pipeline to a SearchPipeline instance."""
    app, _ = _make_app(tmp_path)
    assert isinstance(app.state.pipeline, SearchPipeline)


def test_pipeline_shares_store_with_app_state(tmp_path: Path) -> None:
    """app.state.pipeline.store must be the same object as app.state.search_store."""
    app, _ = _make_app(tmp_path)
    assert app.state.pipeline.store is app.state.search_store


def _make_pipeline_mock(
    results: list[SearchResult] | None = None,
    acl_filtered: bool = False,
    meta_return=...,  # sentinel — use CollectionMeta by default
    meta_raises: Exception | None = None,
    search_raises: Exception | None = None,
) -> MagicMock:
    """Return a mock SearchPipeline with search() and get_collection_meta() pre-configured."""
    from archon_search.collection_meta import CollectionMeta

    pipeline = MagicMock()

    if meta_raises is not None:
        pipeline.get_collection_meta = AsyncMock(side_effect=meta_raises)
    elif meta_return is ...:
        pipeline.get_collection_meta = AsyncMock(return_value=CollectionMeta(name="col", namespace="default"))
    else:
        pipeline.get_collection_meta = AsyncMock(return_value=meta_return)

    if search_raises is not None:
        pipeline.search = AsyncMock(side_effect=search_raises)
    else:
        pipeline.search = AsyncMock(
            return_value=SearchPipelineResult(results=results or [], acl_filtered=acl_filtered)
        )

    return pipeline


def _make_search_result(n: int = 1) -> SearchResult:
    return SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + f"-{n:06d}",
        text=f"result text {n}",
        score=0.9 - n * 0.1,
        source_path=f"/path/to/doc{n}.md",
    )


# ---------------------------------------------------------------------------
# Task 3.4 — pipeline delegation tests
# ---------------------------------------------------------------------------


def test_search_uses_pipeline_not_inline_logic(tmp_path: Path) -> None:
    """POST /search must call pipeline.search(), not app.state.embedder.embed_one directly."""
    results = [_make_search_result(1)]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)
    # Track that embedder.embed_one is NOT called from the route
    app.state.embedder.embed_one = AsyncMock(side_effect=AssertionError("embed_one called directly"))

    response = client.post("/search", json={"collection": "my-col", "query": "test query"})

    assert response.status_code == 200
    app.state.pipeline.search.assert_called_once()


def test_search_passes_namespace_to_pipeline(tmp_path: Path) -> None:
    """pipeline.search() must be called with the request namespace."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock()

    client.post("/search", json={"collection": "col", "query": "q"})

    call_kwargs = app.state.pipeline.search.call_args
    assert "namespace" in call_kwargs.kwargs
    # default namespace from middleware
    assert call_kwargs.kwargs["namespace"] == "default"


def test_search_returns_acl_filtered_flag(tmp_path: Path) -> None:
    """When pipeline returns acl_filtered=True, response has acl_filtered: true."""
    results = [_make_search_result(1)]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results, acl_filtered=True)

    response = client.post("/search", json={"collection": "col", "query": "q"})

    assert response.status_code == 200
    assert response.json()["acl_filtered"] is True


def test_search_collection_not_found_returns_404(tmp_path: Path) -> None:
    """When get_collection_meta returns None, 404 is returned."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(meta_return=None)

    response = client.post("/search", json={"collection": "nonexistent", "query": "test"})

    assert response.status_code == 404


def test_search_pipeline_error_returns_empty(tmp_path: Path) -> None:
    """When pipeline.search() raises, returns SearchResponse(results=[], acl_filtered=False)."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(search_raises=RuntimeError("search boom"))

    response = client.post("/search", json={"collection": "col", "query": "q"})

    assert response.status_code == 200
    data = response.json()
    assert data["results"] == []
    assert data["acl_filtered"] is False


# ---------------------------------------------------------------------------
# 1. Valid request returns list of results
# ---------------------------------------------------------------------------


def test_search_returns_results(tmp_path: Path) -> None:
    results = [_make_search_result(1), _make_search_result(2)]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post("/search", json={"collection": "my-col", "query": "test query"})

    assert response.status_code == 200
    data = response.json()
    assert "results" in data
    assert len(data["results"]) == 2
    assert data["results"][0]["doc_id"] == results[0].doc_id
    assert data["results"][0]["chunk_id"] == results[0].chunk_id
    assert data["results"][0]["text"] == results[0].text
    assert data["results"][0]["score"] == pytest.approx(results[0].score)
    assert data["results"][0]["source_path"] == results[0].source_path


# ---------------------------------------------------------------------------
# 2. Collection not found → 404
# ---------------------------------------------------------------------------


def test_search_collection_not_found_returns_empty(tmp_path: Path) -> None:
    """Collection not found via namespace check → 404 (not 200+[])."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(meta_return=None)

    response = client.post("/search", json={"collection": "nonexistent", "query": "test"})

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 3. top_k=0 → 422 validation error
# ---------------------------------------------------------------------------


def test_search_invalid_top_k(tmp_path: Path) -> None:
    _, client = _make_app(tmp_path)
    response = client.post("/search", json={"collection": "col", "query": "q", "top_k": 0})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 3b. top_k > 100 → 422 validation error
# ---------------------------------------------------------------------------


def test_search_top_k_exceeds_upper_bound(tmp_path: Path) -> None:
    _, client = _make_app(tmp_path)
    response = client.post("/search", json={"collection": "col", "query": "q", "top_k": 101})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 4. Empty query → 422 validation error
# ---------------------------------------------------------------------------


def test_search_empty_query(tmp_path: Path) -> None:
    _, client = _make_app(tmp_path)
    response = client.post("/search", json={"collection": "col", "query": ""})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 4b. Whitespace-only query → 422 validation error
# ---------------------------------------------------------------------------


def test_search_whitespace_query(tmp_path: Path) -> None:
    _, client = _make_app(tmp_path)
    response = client.post("/search", json={"collection": "col", "query": "   "})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 4c. Empty collection → 422 validation error
# ---------------------------------------------------------------------------


def test_search_empty_collection(tmp_path: Path) -> None:
    _, client = _make_app(tmp_path)
    response = client.post("/search", json={"collection": "", "query": "q"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 4d. Whitespace-only collection → 422 validation error
# ---------------------------------------------------------------------------


def test_search_whitespace_collection(tmp_path: Path) -> None:
    _, client = _make_app(tmp_path)
    response = client.post("/search", json={"collection": "   ", "query": "q"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 5. Exception in pipeline.search() → log WARNING + return []
# ---------------------------------------------------------------------------


def test_search_store_exception_returns_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Exception in pipeline.search() (after successful meta lookup) → log WARNING + return []."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(search_raises=RuntimeError("db failure"))

    with caplog.at_level(logging.WARNING, logger="archon.search"):
        response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 200
    data = response.json()
    assert data["results"] == []
    assert any("search failed" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# 6. top_k field is accepted but does not control pipeline (config-level top_k_return used)
# ---------------------------------------------------------------------------


def test_search_top_k_accepted_but_ignored_by_pipeline(tmp_path: Path) -> None:
    """top_k is accepted in the request body for backward compat but not forwarded to pipeline."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=[_make_search_result(1)])

    response = client.post("/search", json={"collection": "col", "query": "q", "top_k": 3})

    assert response.status_code == 200
    # pipeline.search is called without top_k (uses config-level top_k_return)
    call_kwargs = app.state.pipeline.search.call_args
    assert "top_k" not in call_kwargs.kwargs


# ---------------------------------------------------------------------------
# 7. Pipeline search failure → 200 + []
# ---------------------------------------------------------------------------


def test_search_embedder_failure_returns_empty(tmp_path: Path) -> None:
    """pipeline.search() failure → 200 + [] (pipeline encapsulates embed+rerank)."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(search_raises=RuntimeError("model error"))

    response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 200
    assert response.json()["results"] == []


# ---------------------------------------------------------------------------
# 8. Reranker failure inside pipeline → 200 + []
# ---------------------------------------------------------------------------


def test_search_reranker_failure_returns_empty(tmp_path: Path) -> None:
    """Any exception from pipeline.search() → 200 + [] (reranker failure path)."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(search_raises=ValueError("score count mismatch"))

    response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 200
    assert response.json()["results"] == []


# ---------------------------------------------------------------------------
# 8. Integration: ingest a doc, search, verify result appears
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 9. Route delegates to pipeline, not inline store/reranker logic (Task 3.4)
# ---------------------------------------------------------------------------


def test_search_uses_app_state_pipeline(tmp_path: Path) -> None:
    """POST /search must call app.state.pipeline.search() — no inline store/reranker logic."""
    results = [_make_search_result(1)]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post("/search", json={"collection": "my-col", "query": "test"})

    assert response.status_code == 200
    app.state.pipeline.search.assert_called_once()


# ---------------------------------------------------------------------------
# 10. Same namespace — pipeline.search() is called (not short-circuited)
# ---------------------------------------------------------------------------


def test_search_same_namespace_proceeds(tmp_path: Path) -> None:
    """When get_collection_meta returns a meta row, pipeline.search() is called."""
    results = [_make_search_result(1)]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post("/search", json={"collection": "my-col", "query": "test"})

    assert response.status_code == 200
    app.state.pipeline.search.assert_called_once()


# ---------------------------------------------------------------------------
# 11. Cross-namespace — returns 404 (Task 5.1)
# ---------------------------------------------------------------------------


def test_search_cross_namespace_404(tmp_path: Path) -> None:
    """When get_collection_meta returns None (wrong namespace), response is 404."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(meta_return=None)

    response = client.post("/search", json={"collection": "other-col", "query": "test"})

    assert response.status_code == 404
    app.state.pipeline.search.assert_not_called()


# ---------------------------------------------------------------------------
# 12. Store exception on meta lookup — returns 503 (Task 5.1)
# ---------------------------------------------------------------------------


def test_search_store_exception_returns_503(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """When get_collection_meta raises (LanceDB error), response is 503, not 404 or 200."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(meta_raises=RuntimeError("lancedb failure"))

    with caplog.at_level(logging.ERROR, logger="archon.search"):
        response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 503
    app.state.pipeline.search.assert_not_called()
    assert any("service unavailable" in record.message.lower() or "lancedb" in record.message.lower() or "col" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_search_end_to_end(tmp_path: Path) -> None:
    """Full pipeline: ingest → search → result appears."""
    from archon_search._types import ChunkRecord
    from archon_search.embedder import Embedder, ModelEmbedder
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "BAAI/bge-small-en-v1.5"

    store = SearchStore(config.db_path)
    await store.connect()

    embedder = Embedder(ModelEmbedder(config.embedding_model))
    vector = await embedder.embed_one("hello world")

    chunk = ChunkRecord(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000000",
        text="hello world documentation",
        vector=vector,
        source_path="/docs/hello.md",
        indexed_at="2025-01-01T00:00:00",
    )
    await store.ingest_chunks("testcol", [chunk])
    await store.disconnect()

    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = client.post("/search", json={"collection": "testcol", "query": "hello world"})
    assert response.status_code == 200
    data = response.json()
    results = data["results"]
    assert len(results) >= 1
    assert results[0]["source_path"] == "/docs/hello.md"

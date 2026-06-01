"""Tests for /explain endpoint per-collection embedder dispatch (Task 7.3)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.pipeline import ExplainPipelineResult
from archon_search.server.app import create_app


def _make_app(tmp_path: Path) -> tuple:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return app, client


def _make_explain_result() -> ExplainPipelineResult:
    return ExplainPipelineResult(top_results=[], near_misses=[], acl_filtered=False)


def _make_embedder_cache_mock() -> MagicMock:
    mock_embedder = MagicMock()
    cache = MagicMock()
    cache.get_or_load = AsyncMock(return_value=mock_embedder)
    return cache


# ---------------------------------------------------------------------------
# Task 7.3 — per-collection embedder dispatch for /explain
# ---------------------------------------------------------------------------


def test_explain_single_collection_uses_per_collection_model(tmp_path: Path) -> None:
    """Single-collection explain calls embedder_cache.get_or_load with active_embedding_model."""
    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="model-X")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    response = client.post("/explain", json={"collection": "col", "query": "test"})
    assert response.status_code == 200
    cache.get_or_load.assert_awaited_once_with("model-X")


def test_explain_response_identifies_model_used(tmp_path: Path) -> None:
    """Single-collection explain response body has embedding_model from active_embedding_model."""
    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="model-X")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    response = client.post("/explain", json={"collection": "col", "query": "test"})
    assert response.status_code == 200
    data = response.json()
    assert data["embedding_model"] == "model-X"


def test_explain_multi_collection_uses_global_model(tmp_path: Path) -> None:
    """Multi-collection explain passes embedder=None and uses global config model."""
    app, client = _make_app(tmp_path)
    config = app.state.config
    pipeline = MagicMock()
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    app.state.pipeline = pipeline

    response = client.post(
        "/explain",
        json={"collections": ["col1", "col2"], "query": "test"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["embedding_model"] == config.embedding_model
    # Verify embedder=None was passed (pipeline uses global embedder)
    _, kwargs = pipeline.explain.call_args
    assert kwargs.get("embedder") is None


def test_explain_empty_active_model_falls_back_to_global(tmp_path: Path) -> None:
    """Single-collection explain with active_embedding_model='' falls back to global model."""
    app, client = _make_app(tmp_path)
    config = app.state.config
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    response = client.post("/explain", json={"collection": "col", "query": "test"})
    assert response.status_code == 200
    cache.get_or_load.assert_awaited_once_with(config.embedding_model)
    data = response.json()
    assert data["embedding_model"] == config.embedding_model


def test_explain_routing_path_uses_chosen_collection_model(tmp_path: Path) -> None:
    """Auto-routing explain (no collection param) uses chosen collection's active_embedding_model."""
    from archon_search.collection_meta import CollectionMeta

    app, client = _make_app(tmp_path)
    config = app.state.config
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="model-X", centroid=[1.0, 0.0])
    pipeline.get_all_collections_meta = AsyncMock(return_value=[meta])
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[1.0, 0.0])
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    response = client.post("/explain", json={"query": "test"})
    assert response.status_code == 200
    cache.get_or_load.assert_awaited_once_with("model-X")
    data = response.json()
    assert data["embedding_model"] == "model-X"

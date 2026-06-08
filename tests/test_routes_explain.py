"""Tests for /explain endpoint per-collection embedder dispatch (Task 7.3) and HyDE schema fields (Task 3.2)."""
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
from archon_search.server.routes_explain import ExplainRequest, ExplainResponse


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


# ---------------------------------------------------------------------------
# Task 3.2 — HyDE schema fields on ExplainRequest / ExplainResponse
# ---------------------------------------------------------------------------


def test_explain_request_hyde_default_false() -> None:
    """ExplainRequest without hyde defaults to False."""
    req = ExplainRequest(query="test", collection="col")
    assert req.hyde is False


def test_explain_request_accepts_hyde_true() -> None:
    """ExplainRequest with hyde=True validates without error."""
    req = ExplainRequest(query="test", collection="col", hyde=True)
    assert req.hyde is True


def test_explain_response_has_hyde_applied() -> None:
    """ExplainResponse defaults hyde_applied to False."""
    from archon_search.pipeline import ExplainPipelineResult as EPR

    result = EPR(top_results=[], near_misses=[], acl_filtered=False)
    resp = ExplainResponse.from_pipeline_result(
        rerank=True,
        collection="col",
        routing=None,
        result=result,
    )
    assert resp.hyde_applied is False


def test_explain_response_hyde_applied_true() -> None:
    """ExplainResponse.from_pipeline_result accepts hyde_applied=True."""
    from archon_search.pipeline import ExplainPipelineResult as EPR

    result = EPR(top_results=[], near_misses=[], acl_filtered=False)
    resp = ExplainResponse.from_pipeline_result(
        rerank=True,
        collection="col",
        routing=None,
        result=result,
        hyde_applied=True,
    )
    assert resp.hyde_applied is True


def test_explain_endpoint_response_includes_hyde_applied(tmp_path: Path) -> None:
    """POST /explain response body includes hyde_applied field."""
    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    response = client.post("/explain", json={"collection": "col", "query": "test"})
    assert response.status_code == 200
    data = response.json()
    assert "hyde_applied" in data
    assert data["hyde_applied"] is False


# ---------------------------------------------------------------------------
# Task 4.3 — Wire resolve_hyde_vector into routes_explain.py handler
# ---------------------------------------------------------------------------


def test_explain_hyde_true_passes_vector(tmp_path: Path) -> None:
    """hyde=true: resolve_hyde_vector returns a vector → pipeline.explain called with query_vector;
    response has hyde_applied=True."""
    from unittest.mock import patch, AsyncMock as AM

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    hyde_vector = [0.1, 0.2, 0.3, 0.4]

    with patch(
        "archon_search.server.routes_explain.resolve_hyde_vector",
        new=AM(return_value=(hyde_vector, True)),
    ):
        response = client.post("/explain", json={"collection": "col", "query": "test", "hyde": True})

    assert response.status_code == 200
    data = response.json()
    assert data["hyde_applied"] is True

    # Verify pipeline.explain was called with query_vector=hyde_vector
    _, kwargs = pipeline.explain.call_args
    assert kwargs.get("query_vector") == hyde_vector


def test_explain_hyde_false_passes_none(tmp_path: Path) -> None:
    """hyde=false: resolve_hyde_vector returns (None, False) → pipeline.explain called with
    query_vector=None; response has hyde_applied=False."""
    from unittest.mock import patch, AsyncMock as AM

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    with patch(
        "archon_search.server.routes_explain.resolve_hyde_vector",
        new=AM(return_value=(None, False)),
    ):
        response = client.post("/explain", json={"collection": "col", "query": "test", "hyde": False})

    assert response.status_code == 200
    data = response.json()
    assert data["hyde_applied"] is False

    _, kwargs = pipeline.explain.call_args
    assert kwargs.get("query_vector") is None


def test_explain_hyde_package_not_installed_returns_422(tmp_path: Path) -> None:
    """If resolve_hyde_vector raises RuntimeError (package not installed), respond with 422."""
    from unittest.mock import patch, AsyncMock as AM

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    app.state.pipeline = pipeline

    with patch(
        "archon_search.server.routes_explain.resolve_hyde_vector",
        side_effect=RuntimeError("Install archon-search[hyde] to use HyDE"),
    ):
        response = client.post("/explain", json={"collection": "col", "query": "test", "hyde": True})

    assert response.status_code == 422
    assert "archon-search[hyde]" in response.json()["detail"]


def test_explain_multi_collection_hyde_true_passes_vector(tmp_path: Path) -> None:
    """Multi-collection fanout: hyde=True passes query_vector=hyde_vector to pipeline.explain();
    response has hyde_applied=True."""
    from unittest.mock import patch, AsyncMock as AM

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    app.state.pipeline = pipeline

    hyde_vector = [0.5, 0.6, 0.7, 0.8]

    with patch(
        "archon_search.server.routes_explain.resolve_hyde_vector",
        new=AM(return_value=(hyde_vector, True)),
    ):
        response = client.post(
            "/explain",
            json={"collections": ["col1", "col2"], "query": "test", "hyde": True},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["hyde_applied"] is True

    _, kwargs = pipeline.explain.call_args
    assert kwargs.get("query_vector") == hyde_vector


def test_explain_collectionless_routing_hyde_true_passes_vector(tmp_path: Path) -> None:
    """Collectionless routing path: hyde=True passes query_vector=hyde_vector to pipeline.explain();
    response has hyde_applied=True."""
    from unittest.mock import patch, AsyncMock as AM

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="", centroid=[1.0, 0.0])
    pipeline.get_all_collections_meta = AsyncMock(return_value=[meta])
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[1.0, 0.0])
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    hyde_vector = [0.9, 0.1]

    with patch(
        "archon_search.server.routes_explain.resolve_hyde_vector",
        new=AM(return_value=(hyde_vector, True)),
    ):
        response = client.post("/explain", json={"query": "test", "hyde": True})

    assert response.status_code == 200
    data = response.json()
    assert data["hyde_applied"] is True

    _, kwargs = pipeline.explain.call_args
    assert kwargs.get("query_vector") == hyde_vector


# ---------------------------------------------------------------------------
# Task 3.2 — RAG Fusion schema fields on ExplainRequest / ExplainResponse
# ---------------------------------------------------------------------------


def test_explain_request_rag_fusion_default_false() -> None:
    """ExplainRequest without rag_fusion defaults to False."""
    req = ExplainRequest(query="test", collection="col")
    assert req.rag_fusion is False


def test_explain_request_accepts_rag_fusion_true() -> None:
    """ExplainRequest with rag_fusion=True validates without error."""
    req = ExplainRequest(query="test", collection="col", rag_fusion=True)
    assert req.rag_fusion is True


def test_explain_response_has_rag_fusion_fields() -> None:
    """ExplainResponse has all five RAG Fusion fields with correct defaults."""
    from archon_search.pipeline import ExplainPipelineResult as EPR
    from archon_search.server.routes_explain import ExplainResponse

    result = EPR(top_results=[], near_misses=[], acl_filtered=False)
    resp = ExplainResponse.from_pipeline_result(
        rerank=True,
        collection="col",
        routing=None,
        result=result,
    )
    assert resp.rag_fusion_applied is False
    assert resp.rag_fusion_queries_used == 0
    assert resp.rag_fusion_attempted is False
    assert resp.rag_fusion_failure_reason is None
    assert resp.rag_fusion_sub_queries is None


def test_rag_fusion_sub_query_result_schema() -> None:
    """RagFusionSubQueryResult validates fields correctly."""
    from archon_search.server.routes_explain import RagFusionSubQueryResult

    obj = RagFusionSubQueryResult(variant_index=0, result_count=3, top_doc_ids=["a", "b", "c"])
    assert obj.variant_index == 0
    assert obj.result_count == 3
    assert obj.top_doc_ids == ["a", "b", "c"]


def test_explain_from_pipeline_result_threads_rag_fusion() -> None:
    """from_pipeline_result sets RAG Fusion fields when provided."""
    from archon_search.pipeline import ExplainPipelineResult as EPR, RagFusionSubQueryInfo
    from archon_search.server.routes_explain import ExplainResponse

    sub_query_results = [
        RagFusionSubQueryInfo(variant_index=0, result_count=3, top_doc_ids=["doc1", "doc2", "doc3"]),
        RagFusionSubQueryInfo(variant_index=1, result_count=2, top_doc_ids=["doc4", "doc5"]),
    ]
    result = EPR(
        top_results=[],
        near_misses=[],
        acl_filtered=False,
        rag_fusion_applied=True,
        rag_fusion_queries_used=2,
        rag_fusion_attempted=True,
        rag_fusion_failure_reason=None,
        rag_fusion_sub_query_results=sub_query_results,
    )
    resp = ExplainResponse.from_pipeline_result(
        rerank=True,
        collection="col",
        routing=None,
        result=result,
        rag_fusion_applied=True,
        rag_fusion_queries_used=2,
        rag_fusion_attempted=True,
        rag_fusion_sub_query_results=sub_query_results,
    )
    assert resp.rag_fusion_applied is True
    assert resp.rag_fusion_queries_used == 2
    assert resp.rag_fusion_attempted is True
    assert resp.rag_fusion_failure_reason is None
    assert resp.rag_fusion_sub_queries is not None
    assert len(resp.rag_fusion_sub_queries) == 2
    assert resp.rag_fusion_sub_queries[0].variant_index == 0
    assert resp.rag_fusion_sub_queries[0].result_count == 3
    assert resp.rag_fusion_sub_queries[1].variant_index == 1

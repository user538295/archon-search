"""Tests for /explain endpoint per-collection embedder dispatch (Task 7.3) and HyDE schema fields (Task 3.2)."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.pipeline import ExplainPipelineResult
from archon_search.server.app import create_app
from archon_search.server.routes_explain import ExplainRequest, ExplainResponse
from tests._hyde_stubs import rest_hyde_generating, stub_hyde_generator


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
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="model-X", centroid=[1.0, 0.0])
    pipeline.get_all_collections_meta = AsyncMock(return_value=[meta])
    pipeline.global_embedder = MagicMock()
    pipeline.global_embedder.embed_one = AsyncMock(return_value=[1.0, 0.0])
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
# Task 4.3 — Wire HyDE into routes_explain.py handler
# ---------------------------------------------------------------------------


def test_explain_hyde_true_passes_vector(tmp_path: Path) -> None:
    """hyde=true: HyDE generates a vector → pipeline.explain called with query_vector;
    response has hyde_applied=True."""

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    hyde_vector = [0.1, 0.2, 0.3, 0.4]

    with rest_hyde_generating(app, return_value=hyde_vector):
        response = client.post("/explain", json={"collection": "col", "query": "test", "hyde": True})

    assert response.status_code == 200
    data = response.json()
    assert data["hyde_applied"] is True

    # Verify pipeline.explain was called with query_vector=hyde_vector
    _, kwargs = pipeline.explain.call_args
    assert kwargs.get("query_vector") == hyde_vector


def test_explain_hyde_false_passes_none(tmp_path: Path) -> None:
    """hyde=false: HyDE is not resolved → pipeline.explain called with
    query_vector=None; response has hyde_applied=False."""

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    with rest_hyde_generating(app, return_value=None) as generate:
        response = client.post("/explain", json={"collection": "col", "query": "test", "hyde": False})

    assert response.status_code == 200
    generate.assert_not_awaited()
    data = response.json()
    assert data["hyde_applied"] is False

    _, kwargs = pipeline.explain.call_args
    assert kwargs.get("query_vector") is None


def test_explain_hyde_package_not_installed_returns_422(tmp_path: Path) -> None:
    """If HyDE generation raises HyDEDependencyError (package not installed), respond with 422.

    The detail is the fixed install message; the exception's install command (which
    describes the server's own install) stays in the log.
    """

    import archon_search.server.routes_explain as routes_explain_mod

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock()
    app.state.pipeline = pipeline

    dependency_error = routes_explain_mod.HyDEDependencyError(
        "Install archon-search[hyde] to use HyDE (pip install 'archon-search[hyde]')"
    )
    with rest_hyde_generating(app, side_effect=dependency_error):
        response = client.post("/explain", json={"collection": "col", "query": "test", "hyde": True})
        multi_response = client.post("/explain", json={"collections": ["col", "c2"], "query": "test", "hyde": True})

    assert response.status_code == 422
    assert response.json() == {"detail": "Install archon-search[hyde] to use HyDE"}
    assert multi_response.status_code == 422
    assert multi_response.json() == {"detail": "Install archon-search[hyde] to use HyDE"}
    pipeline.explain.assert_not_awaited()


_SLOW_HYDE_SECONDS = 0.5


async def _slow_generate(*_args: object, **_kwargs: object) -> list[float]:
    await asyncio.sleep(_SLOW_HYDE_SECONDS)
    return [0.1, 0.2]


def test_explain_stage_timings_exclude_the_hyde_call(tmp_path: Path) -> None:
    """HyDE (LLM call + hypothesis embed) is not a retrieval stage: ``total`` excludes it.

    HyDE resolves inside the timed window now that it waits for the explained
    collection's embedder; the window pauses around it so ``stage_timings_ms`` keeps
    its pre-existing meaning.
    """
    app, client = _make_app(tmp_path)
    app.state.config.observability.stage_timings_enabled = True
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default", active_embedding_model="")
    )
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    app.state.pipeline = pipeline

    with rest_hyde_generating(app, side_effect=_slow_generate):
        response = client.post("/explain", json={"collection": "col", "query": "test", "hyde": True})

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["hyde_applied"] is True
    assert 0 <= data["stage_timings_ms"]["total"] < _SLOW_HYDE_SECONDS * 1000.0, data["stage_timings_ms"]


def test_explain_multi_collection_stage_timings_exclude_the_hyde_call(tmp_path: Path) -> None:
    """C3-T-5: the fan-out ``total`` excludes HyDE's LLM call too."""
    app, client = _make_app(tmp_path)
    app.state.config.observability.stage_timings_enabled = True
    pipeline = MagicMock()
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    app.state.pipeline = pipeline

    with rest_hyde_generating(app, side_effect=_slow_generate):
        response = client.post("/explain", json={"collections": ["col1", "col2"], "query": "test", "hyde": True})

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["hyde_applied"] is True
    assert 0 <= data["stage_timings_ms"]["total"] < _SLOW_HYDE_SECONDS * 1000.0, data["stage_timings_ms"]


def test_explain_multi_collection_hyde_true_passes_vector(tmp_path: Path) -> None:
    """Multi-collection fanout: hyde=True passes query_vector=hyde_vector to pipeline.explain();
    response has hyde_applied=True."""

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    app.state.pipeline = pipeline

    hyde_vector = [0.5, 0.6, 0.7, 0.8]

    with rest_hyde_generating(app, return_value=hyde_vector):
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

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="", centroid=[1.0, 0.0])
    pipeline.get_all_collections_meta = AsyncMock(return_value=[meta])
    pipeline.global_embedder = MagicMock()
    pipeline.global_embedder.embed_one = AsyncMock(return_value=[1.0, 0.0])
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    hyde_vector = [0.9, 0.1]

    with rest_hyde_generating(app, return_value=hyde_vector):
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
    from archon_search.pipeline import ExplainPipelineResult as EPR
    from archon_search.pipeline import RagFusionSubQueryInfo
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


# ---------------------------------------------------------------------------
# Task 4.3 — Wire RAG Fusion into routes_explain.py handler
# ---------------------------------------------------------------------------


def test_explain_rag_fusion_true_skips_hyde(tmp_path: Path) -> None:
    """rag_fusion=True (and enabled in config) and hyde=True: HyDE generate NOT called; hyde_applied=False.

    Mutual exclusion only holds when RAG Fusion can actually run (S272) — the config
    kill-switch must be on, otherwise HyDE must not be suppressed (see
    test_explain_rag_fusion_requested_but_disabled_hyde_still_applies below).
    """

    app, client = _make_app(tmp_path)
    app.state.config.rag_fusion.enabled = True
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    with rest_hyde_generating(app, return_value=[0.1, 0.2]) as mock_hyde:
        response = client.post(
            "/explain",
            json={"collection": "col", "query": "test", "rag_fusion": True, "hyde": True},
        )

    assert response.status_code == 200
    data = response.json()
    # HyDE must be suppressed when rag_fusion=True
    assert data["hyde_applied"] is False
    # HyDE generate must NOT be called
    mock_hyde.assert_not_awaited()


def test_explain_rag_fusion_requested_but_disabled_hyde_still_applies(tmp_path: Path) -> None:
    """S272: rag_fusion=True with [rag_fusion] disabled must not suppress a real HyDE request."""

    app, client = _make_app(tmp_path)
    assert app.state.config.rag_fusion.enabled is False  # default kill-switch stays off
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    with rest_hyde_generating(app, return_value=[0.1, 0.2]) as mock_hyde:
        response = client.post(
            "/explain",
            json={"collection": "col", "query": "test", "rag_fusion": True, "hyde": True},
        )

    assert response.status_code == 200
    data = response.json()
    # RAG Fusion never "wins" when it's disabled by config — HyDE must run normally.
    assert data["hyde_applied"] is True
    mock_hyde.assert_awaited_once()


def test_explain_rag_fusion_true_passes_to_pipeline(tmp_path: Path) -> None:
    """rag_fusion=True: pipeline.explain called with rag_fusion fields; response carries all five new fields."""
    from archon_search.pipeline import ExplainPipelineResult as EPR
    from archon_search.pipeline import RagFusionSubQueryInfo

    sub_query_results = [
        RagFusionSubQueryInfo(variant_index=0, result_count=5, top_doc_ids=["d1", "d2"]),
        RagFusionSubQueryInfo(variant_index=1, result_count=3, top_doc_ids=["d3"]),
    ]
    pipeline_result = EPR(
        top_results=[],
        near_misses=[],
        acl_filtered=False,
        rag_fusion_applied=True,
        rag_fusion_queries_used=2,
        rag_fusion_attempted=True,
        rag_fusion_failure_reason=None,
        rag_fusion_sub_query_results=sub_query_results,
    )
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=pipeline_result)
    cache = _make_embedder_cache_mock()
    app, client = _make_app(tmp_path)
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    response = client.post("/explain", json={"collection": "col", "query": "test", "rag_fusion": True})
    assert response.status_code == 200
    data = response.json()
    assert data["rag_fusion_applied"] is True
    assert data["rag_fusion_queries_used"] == 2
    assert data["rag_fusion_attempted"] is True
    assert data["rag_fusion_failure_reason"] is None
    assert data["rag_fusion_sub_queries"] is not None
    assert len(data["rag_fusion_sub_queries"]) == 2

    # Verify pipeline.explain was called with rag_fusion=True
    _, kwargs = pipeline.explain.call_args
    assert kwargs.get("rag_fusion") is True


def test_explain_rag_fusion_failure_reason_in_response(tmp_path: Path) -> None:
    """Pipeline result with rag_fusion_attempted=True and failure_reason: response maps both through."""
    from archon_search.pipeline import ExplainPipelineResult as EPR

    pipeline_result = EPR(
        top_results=[],
        near_misses=[],
        acl_filtered=False,
        rag_fusion_applied=False,
        rag_fusion_queries_used=0,
        rag_fusion_attempted=True,
        rag_fusion_failure_reason="timeout",
        rag_fusion_sub_query_results=None,
    )
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=pipeline_result)
    cache = _make_embedder_cache_mock()
    app, client = _make_app(tmp_path)
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    response = client.post("/explain", json={"collection": "col", "query": "test", "rag_fusion": True})
    assert response.status_code == 200
    data = response.json()
    assert data["rag_fusion_attempted"] is True
    assert data["rag_fusion_failure_reason"] == "timeout"


def test_explain_rag_fusion_sub_queries_mapped_to_schema(tmp_path: Path) -> None:
    """rag_fusion_sub_query_results on pipeline result → rag_fusion_sub_queries in response (mapped by field name)."""
    from archon_search.pipeline import ExplainPipelineResult as EPR
    from archon_search.pipeline import RagFusionSubQueryInfo

    sub_query_results = [
        RagFusionSubQueryInfo(variant_index=0, result_count=3, top_doc_ids=["a", "b", "c"]),
        RagFusionSubQueryInfo(variant_index=1, result_count=2, top_doc_ids=["b", "c"]),
    ]
    pipeline_result = EPR(
        top_results=[],
        near_misses=[],
        acl_filtered=False,
        rag_fusion_applied=True,
        rag_fusion_queries_used=1,
        rag_fusion_attempted=True,
        rag_fusion_sub_query_results=sub_query_results,
    )
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=pipeline_result)
    cache = _make_embedder_cache_mock()
    app, client = _make_app(tmp_path)
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    response = client.post("/explain", json={"collection": "col", "query": "test", "rag_fusion": True})
    assert response.status_code == 200
    data = response.json()
    assert data["rag_fusion_sub_queries"] is not None
    assert len(data["rag_fusion_sub_queries"]) == 2
    assert data["rag_fusion_sub_queries"][0]["variant_index"] == 0
    assert data["rag_fusion_sub_queries"][0]["result_count"] == 3
    assert data["rag_fusion_sub_queries"][0]["top_doc_ids"] == ["a", "b", "c"]
    assert data["rag_fusion_sub_queries"][1]["variant_index"] == 1


def test_explain_rag_fusion_package_not_installed_returns_422(tmp_path: Path) -> None:
    """Pipeline raises RAGFusionDependencyError (package not installed): response is 422."""
    from archon_search.rag_fusion import (
        RAG_FUSION_DEPENDENCY_DETAIL,
        RAGFusionDependencyError,
    )

    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(side_effect=RAGFusionDependencyError("Install archon-search[rag_fusion]"))
    cache = _make_embedder_cache_mock()
    app, client = _make_app(tmp_path)
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    response = client.post("/explain", json={"collection": "col", "query": "test", "rag_fusion": True})
    assert response.status_code == 422
    assert response.json() == {"detail": RAG_FUSION_DEPENDENCY_DETAIL}


def test_explain_rag_fusion_false_hyde_still_works(tmp_path: Path) -> None:
    """rag_fusion=False, hyde=True: HyDE generate IS called (mutual exclusion is directional)."""

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    meta = CollectionMeta(name="col", namespace="default", active_embedding_model="")
    pipeline.get_collection_meta = AsyncMock(return_value=meta)
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    cache = _make_embedder_cache_mock()
    app.state.pipeline = pipeline
    app.state.embedder_cache = cache

    hyde_vector = [0.1, 0.2, 0.3]
    with rest_hyde_generating(app, return_value=hyde_vector) as mock_hyde:
        response = client.post(
            "/explain",
            json={"collection": "col", "query": "test", "rag_fusion": False, "hyde": True},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["hyde_applied"] is True
    mock_hyde.assert_awaited_once()
    _, kwargs = pipeline.explain.call_args
    assert kwargs.get("query_vector") == hyde_vector


def test_explain_multi_collection_rag_fusion_passes_to_pipeline(tmp_path: Path) -> None:
    """Multi-collection path: rag_fusion=True passes rag_fusion params to pipeline.explain."""
    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    app.state.pipeline = pipeline

    response = client.post(
        "/explain",
        json={"collections": ["col1", "col2"], "query": "test", "rag_fusion": True},
    )
    assert response.status_code == 200
    _, kwargs = pipeline.explain.call_args
    assert kwargs.get("rag_fusion") is True
    assert "rag_fusion_generator" in kwargs
    assert "rag_fusion_config" in kwargs


def test_explain_multi_collection_rag_fusion_package_not_installed_returns_422(tmp_path: Path) -> None:
    """Multi-collection path: RAGFusionDependencyError from pipeline returns 422."""
    from archon_search.rag_fusion import (
        RAG_FUSION_DEPENDENCY_DETAIL,
        RAGFusionDependencyError,
    )

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.explain = AsyncMock(side_effect=RAGFusionDependencyError("Install archon-search[rag_fusion]"))
    app.state.pipeline = pipeline

    response = client.post(
        "/explain",
        json={"collections": ["col1", "col2"], "query": "test", "rag_fusion": True},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": RAG_FUSION_DEPENDENCY_DETAIL}


_LEAKY_RAG_FUSION_HINT = "uv tool install /opt/private/wheelhouse 'archon-search[rag_fusion]'"


@pytest.mark.parametrize("target", [{"collection": "col"}, {"collections": ["col1", "col2"]}])
def test_explain_rag_fusion_dependency_error_keeps_the_install_command_in_the_log(
    tmp_path: Path, target: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """G2: fixed 422 text on the wire; the install command for the server's own env is logged."""
    import logging

    from archon_search.rag_fusion import (
        RAG_FUSION_DEPENDENCY_DETAIL,
        RAGFusionDependencyError,
    )

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default", active_embedding_model="")
    )
    pipeline.explain = AsyncMock(
        side_effect=RAGFusionDependencyError(f"{RAG_FUSION_DEPENDENCY_DETAIL} ({_LEAKY_RAG_FUSION_HINT})")
    )
    app.state.pipeline = pipeline
    app.state.embedder_cache = _make_embedder_cache_mock()

    with caplog.at_level(logging.WARNING, logger="archon_search.server.routes_explain"):
        response = client.post("/explain", json={"query": "test", "rag_fusion": True, **target})

    assert response.status_code == 422
    assert response.json() == {"detail": "Install archon-search[rag_fusion] to use RAG Fusion"}
    assert _LEAKY_RAG_FUSION_HINT in caplog.text


def _hyde_explain_app(tmp_path: Path, **generate: object) -> tuple:
    """App whose real HyDE path runs against :func:`stub_hyde_generator` (``**generate``)."""
    app, client = _make_app(tmp_path)
    app.state.config.hyde.enabled = True
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default", active_embedding_model="")
    )
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    app.state.pipeline = pipeline
    collection_embedder = MagicMock(name="collection-embedder", is_warm=False)
    app.state.embedder_cache = MagicMock(get_or_load=AsyncMock(return_value=collection_embedder))
    app.state.hyde_generator = stub_hyde_generator(**generate)
    return app, client, pipeline, collection_embedder


_LEAKY_HYDE_FAILURE = "ONNXRuntimeError: /Users/ops/.archon-search/models/onnx/model.onnx failed to load"


@pytest.mark.parametrize("target", [{"collection": "col"}, {"collections": ["col1", "col2"]}])
def test_explain_non_dependency_hyde_failure_is_a_sanitized_500(tmp_path: Path, target: dict) -> None:
    """C2-T-2: any other HyDE exception falls to the catch-all — fixed "explain failed", no raw text."""
    app, client, pipeline, _embedder = _hyde_explain_app(tmp_path, side_effect=RuntimeError(_LEAKY_HYDE_FAILURE))
    client = TestClient(app, raise_server_exceptions=False, headers=client.headers)

    response = client.post("/explain", json={"query": "test", "hyde": True, **target})

    assert response.status_code == 500
    assert response.json() == {"detail": "explain failed"}
    pipeline.explain.assert_not_awaited()


_SHORT_SEARCH_BUDGET_SECONDS = 0.05
_SILENT_PROVIDER_SECONDS = 30.0
_BOUNDED_CALL_CEILING_SECONDS = 5.0


def test_explain_hyde_is_bounded_by_the_search_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """C2-T-3/C2-A-11: /explain passes its own budget, so a silent LLM cannot park the request."""
    import asyncio
    import time

    import archon_search.server.routes_explain as routes_explain_mod

    generation_started = asyncio.Event()

    async def _never_answers(*_args: object, **_kwargs: object) -> list[float]:
        generation_started.set()
        await asyncio.sleep(_SILENT_PROVIDER_SECONDS)
        raise AssertionError("the HyDE call was not cancelled by the search budget")

    _app, client, pipeline, _embedder = _hyde_explain_app(tmp_path, side_effect=_never_answers)
    monkeypatch.setattr(routes_explain_mod, "_SEARCH_TIMEOUT_SECONDS", _SHORT_SEARCH_BUDGET_SECONDS)

    started = time.monotonic()
    response = client.post("/explain", json={"collection": "col", "query": "test", "hyde": True})
    elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    # Presence anchor: the budget cut a generation that started, not a skipped one.
    assert generation_started.is_set(), "HyDE generation never started"
    assert elapsed < _BOUNDED_CALL_CEILING_SECONDS, f"/explain waited {elapsed:.1f}s on the HyDE call"
    assert response.json()["hyde_applied"] is False
    assert pipeline.explain.await_args.kwargs["query_vector"] is None


def test_explain_hyde_still_runs_while_the_collection_embedder_is_cold(tmp_path: Path) -> None:
    """C2-I-1: pipeline.explain embeds on the cold model regardless, so skipping HyDE saved no wait.

    HyDE embeds with that same cold embedder instead — the build is waited on once.
    """
    hyde_vector = [0.4, 0.5]
    app, client, pipeline, collection_embedder = _hyde_explain_app(tmp_path, return_value=hyde_vector)
    generate = app.state.hyde_generator.generate

    response = client.post("/explain", json={"collection": "col", "query": "test", "hyde": True})

    assert response.status_code == 200, response.text
    assert response.json()["hyde_applied"] is True
    assert generate.await_args.kwargs["embedder"] is collection_embedder
    explain_kwargs = pipeline.explain.await_args.kwargs
    assert explain_kwargs["query_vector"] == hyde_vector
    assert explain_kwargs["embedder"] is collection_embedder
    pipeline.warmup_models.assert_not_called()

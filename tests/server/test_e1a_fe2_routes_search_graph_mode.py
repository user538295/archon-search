"""Tests for FE-2: graph_mode field on SearchRequest / SearchResponse and route handler guard.

Covers:
- SearchRequest accepts graph_mode="naive" and None
- SearchResponse has graph_expansion_applied field
- graph_mode="naive" forwarded to pipeline.search / pipeline.search_many
- 422 when graph_mode="naive" and graph.enabled=False
- 422 when graph_mode has invalid Literal value ("local" / "global")
- POST /explain with graph_mode extra field returns 422 via Pydantic extra="forbid"
- expansion_used includes graph_expansion_applied
- graph_expansion_applied=True appears in response when expander fires
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search._types import SearchResult
from archon_search.config import GraphConfig, SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.pipeline import SearchPipeline, SearchPipelineResult
from archon_search.server.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path, *, graph_enabled: bool = False) -> tuple:
    import sys
    import types

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.graph = GraphConfig(enabled=graph_enabled)
    job_store = JobStore(path=tmp_path / "jobs.json")

    # When graph is enabled but spaCy is not installed, create_app raises ConfigError.
    # Stub spacy in sys.modules so the startup check passes.
    original_spacy = sys.modules.get("spacy")
    spacy_stub_injected = False
    if graph_enabled and original_spacy is None:
        spacy_stub = types.ModuleType("spacy")
        sys.modules["spacy"] = spacy_stub  # type: ignore[assignment]
        spacy_stub_injected = True

    try:
        with patch("archon_search.chunker.DocumentChunker.__init__", return_value=None):
            app = create_app(config, job_store)
    finally:
        if spacy_stub_injected:
            sys.modules.pop("spacy", None)
            if original_spacy is not None:
                sys.modules["spacy"] = original_spacy

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {key}"})
    return app, client


def _make_pipeline_mock(
    results: list[SearchResult] | None = None,
    acl_filtered: bool = False,
    meta_return=...,
    graph_expansion_applied: bool = False,
) -> MagicMock:
    from archon_search.collection_meta import CollectionMeta

    pipeline = MagicMock()
    if meta_return is ...:
        pipeline.get_collection_meta = AsyncMock(return_value=CollectionMeta(name="col", namespace="default"))
    else:
        pipeline.get_collection_meta = AsyncMock(return_value=meta_return)
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(
            results=results or [],
            acl_filtered=acl_filtered,
            graph_expansion_applied=graph_expansion_applied,
        )
    )
    pipeline.search_many = AsyncMock(
        return_value=SearchPipelineResult(
            results=results or [],
            acl_filtered=acl_filtered,
            graph_expansion_applied=graph_expansion_applied,
        )
    )
    return pipeline


# ---------------------------------------------------------------------------
# Unit tests — SearchRequest schema
# ---------------------------------------------------------------------------


def test_search_request_graph_mode_field() -> None:
    """SearchRequest accepts graph_mode='naive' and None."""
    from archon_search.server.routes_search import SearchRequest

    req_naive = SearchRequest(collection="col", query="q", graph_mode="naive")
    assert req_naive.graph_mode == "naive"

    req_none = SearchRequest(collection="col", query="q", graph_mode=None)
    assert req_none.graph_mode is None

    req_default = SearchRequest(collection="col", query="q")
    assert req_default.graph_mode is None


def test_post_search_graph_mode_invalid_value_returns_422(tmp_path: Path) -> None:
    """graph_mode='local' or 'global' → 422 via Pydantic Literal validation."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock()

    for invalid in ("local", "global", "unknown"):
        response = client.post("/search", json={"collection": "col", "query": "q", "graph_mode": invalid})
        assert response.status_code == 422, f"Expected 422 for graph_mode={invalid!r}"


# ---------------------------------------------------------------------------
# Unit tests — SearchResponse schema
# ---------------------------------------------------------------------------


def test_search_response_has_graph_expansion_applied_field() -> None:
    """SearchResponse has graph_expansion_applied field; defaults to False."""
    from archon_search.server.routes_search import SearchResponse

    resp = SearchResponse(results=[], acl_filtered=False)
    assert resp.graph_expansion_applied is False


# ---------------------------------------------------------------------------
# Unit tests — handler body guard (graph disabled → 422)
# ---------------------------------------------------------------------------


def test_post_search_graph_mode_422_when_disabled(tmp_path: Path) -> None:
    """graph.enabled=False + graph_mode='naive' → 422 with error message."""
    app, client = _make_app(tmp_path, graph_enabled=False)
    app.state.pipeline = _make_pipeline_mock()

    response = client.post("/search", json={"collection": "col", "query": "q", "graph_mode": "naive"})

    assert response.status_code == 422
    body = response.json()
    detail = body.get("detail", "")
    assert "graph" in detail.lower() or "enabled" in detail.lower(), (
        f"Expected detail to mention 'graph' or 'enabled', got: {detail!r}"
    )


# ---------------------------------------------------------------------------
# Unit tests — graph_mode forwarded to pipeline
# ---------------------------------------------------------------------------


def test_post_search_graph_mode_forwarded_to_pipeline(tmp_path: Path) -> None:
    """graph_mode='naive' in request; assert pipeline.search called with graph_mode='naive'."""
    app, client = _make_app(tmp_path, graph_enabled=True)
    pipeline_mock = _make_pipeline_mock()
    app.state.pipeline = pipeline_mock

    response = client.post("/search", json={"collection": "col", "query": "q", "graph_mode": "naive"})

    assert response.status_code == 200
    call_kwargs = pipeline_mock.search.call_args.kwargs
    assert call_kwargs.get("graph_mode") == "naive"


def test_post_search_many_graph_mode_forwarded_to_pipeline(tmp_path: Path) -> None:
    """graph_mode='naive' on multi-collection request; assert pipeline.search_many called with graph_mode='naive'."""
    app, client = _make_app(tmp_path, graph_enabled=True)
    pipeline_mock = _make_pipeline_mock()
    app.state.pipeline = pipeline_mock

    response = client.post("/search", json={"collections": ["a", "b"], "query": "q", "graph_mode": "naive"})

    assert response.status_code == 200
    call_kwargs = pipeline_mock.search_many.call_args.kwargs
    assert call_kwargs.get("graph_mode") == "naive"


# ---------------------------------------------------------------------------
# Unit tests — expansion_used includes graph_expansion_applied
# ---------------------------------------------------------------------------


def test_expansion_used_includes_graph_expansion(tmp_path: Path) -> None:
    """graph_mode=naive, expander returns expansionApplied=True; SearchResponse.expansion_used==True."""
    app, client = _make_app(tmp_path, graph_enabled=True)
    pipeline_mock = _make_pipeline_mock(graph_expansion_applied=True)
    app.state.pipeline = pipeline_mock

    response = client.post("/search", json={"collection": "col", "query": "q", "graph_mode": "naive"})

    assert response.status_code == 200
    data = response.json()
    assert data["expansion_used"] is True


def test_expansion_used_false_when_graph_not_expanded(tmp_path: Path) -> None:
    """graph_mode=naive requested but expander finds no neighbours; expansion_used remains False."""
    app, client = _make_app(tmp_path, graph_enabled=True)
    pipeline_mock = _make_pipeline_mock(graph_expansion_applied=False)
    app.state.pipeline = pipeline_mock

    response = client.post("/search", json={"collection": "col", "query": "q", "graph_mode": "naive"})

    assert response.status_code == 200
    data = response.json()
    assert data["expansion_used"] is False


# ---------------------------------------------------------------------------
# Integration test — graph_expansion_applied appears in response
# ---------------------------------------------------------------------------


def test_post_search_graph_expansion_applied_in_response(tmp_path: Path) -> None:
    """stub expander; POST with graph_mode='naive'; assert graph_expansion_applied==True in response."""
    app, client = _make_app(tmp_path, graph_enabled=True)
    pipeline_mock = _make_pipeline_mock(graph_expansion_applied=True)
    app.state.pipeline = pipeline_mock

    response = client.post("/search", json={"collection": "col", "query": "q", "graph_mode": "naive"})

    assert response.status_code == 200
    data = response.json()
    assert data["graph_expansion_applied"] is True


def test_post_search_many_graph_expansion_applied_in_response(tmp_path: Path) -> None:
    """Multi-collection path: graph_expansion_applied=True appears in response."""
    app, client = _make_app(tmp_path, graph_enabled=True)
    pipeline_mock = _make_pipeline_mock(graph_expansion_applied=True)
    app.state.pipeline = pipeline_mock

    response = client.post("/search", json={"collections": ["a", "b"], "query": "q", "graph_mode": "naive"})

    assert response.status_code == 200
    data = response.json()
    assert data["graph_expansion_applied"] is True
    assert data["expansion_used"] is True


def test_post_search_many_graph_mode_422_when_disabled(tmp_path: Path) -> None:
    """Multi-collection path: graph.enabled=False + graph_mode='naive' → 422."""
    app, client = _make_app(tmp_path, graph_enabled=False)
    app.state.pipeline = _make_pipeline_mock()

    response = client.post("/search", json={"collections": ["a", "b"], "query": "q", "graph_mode": "naive"})

    assert response.status_code == 422
    body = response.json()
    detail = body.get("detail", "")
    assert "graph" in detail.lower() or "enabled" in detail.lower()


# ---------------------------------------------------------------------------
# Unit test — POST /explain with graph_mode returns 422 via extra="forbid"
# ---------------------------------------------------------------------------


def test_post_explain_graph_mode_422(tmp_path: Path) -> None:
    """POST /explain with graph_mode='NAIVE' (wrong case) → 422 via Pydantic Literal validation.

    BE-2 added graph_mode as a Literal["naive","local","global"] field on ExplainRequest.
    The previous version of this test checked that graph_mode was rejected as an extra field;
    now that graph_mode is a known field, we instead verify that an invalid literal value is
    rejected with a 422 validation error.
    """
    app, client = _make_app(tmp_path)

    response = client.post(
        "/explain",
        json={
            "query": "q",
            "graph_mode": "NAIVE",
        },
    )

    assert response.status_code == 422
    body = response.json()
    detail_str = str(body.get("detail", ""))
    assert "graph_mode" in detail_str.lower() or "literal" in detail_str.lower() or "naive" in detail_str.lower(), (
        f"Expected 422 about invalid graph_mode value, got: {detail_str!r}"
    )

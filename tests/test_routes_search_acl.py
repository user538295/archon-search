"""Tests for SearchResponse schema and ACL field isolation (Task 3.3 + 3.4)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search._types import SearchResult
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.pipeline import SearchPipelineResult
from archon_search.server.app import create_app
from archon_search.server.routes_search import SearchResponse, SearchResultSchema


# ---------------------------------------------------------------------------
# Task 3.3 — schema unit tests
# ---------------------------------------------------------------------------


def test_search_response_schema_fields() -> None:
    result = SearchResponse(results=[], acl_filtered=False).model_dump()
    assert result == {"results": [], "acl_filtered": False}


def test_search_result_schema_no_acl_field() -> None:
    assert "acl" not in SearchResultSchema.model_fields


def test_search_response_is_never_bare_array() -> None:
    keys = set(SearchResponse.model_fields.keys())
    assert "results" in keys
    assert "acl_filtered" in keys


# ---------------------------------------------------------------------------
# Task 3.4 — integration tests: ACL filter + SearchResponse envelope
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path) -> tuple:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return app, client


def _make_result(n: int = 1, acl: list[str] | None = None) -> SearchResult:
    return SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + f"-{n:06d}",
        text=f"result text {n}",
        score=0.9 - n * 0.1,
        source_path=f"/path/to/doc{n}.md",
        acl=acl,
    )


def _make_pipeline_mock(
    results: list[SearchResult] | None = None,
    acl_filtered: bool = False,
    meta_return: CollectionMeta | None = ...,  # type: ignore[assignment]
) -> MagicMock:
    pipeline = MagicMock()
    if meta_return is ...:
        pipeline.get_collection_meta = AsyncMock(
            return_value=CollectionMeta(name="col", namespace="default")
        )
    else:
        pipeline.get_collection_meta = AsyncMock(return_value=meta_return)
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(results=results or [], acl_filtered=acl_filtered)
    )
    return pipeline


def test_search_returns_search_response_envelope(tmp_path: Path) -> None:
    """Response body has 'results' and 'acl_filtered' keys — not a bare array."""
    app, client = _make_app(tmp_path)
    results = [_make_result(1)]
    app.state.pipeline = _make_pipeline_mock(results=results)
    response = client.post("/search", json={"collection": "col", "query": "test"})
    assert response.status_code == 200
    data = response.json()
    assert "results" in data
    assert "acl_filtered" in data
    assert isinstance(data["results"], list)


def test_search_acl_null_always_returned(tmp_path: Path) -> None:
    """Chunk with acl=None is included for any namespace (fail-open)."""
    app, client = _make_app(tmp_path)
    result = _make_result(1, acl=None)
    app.state.pipeline = _make_pipeline_mock(results=[result], acl_filtered=False)
    response = client.post("/search", json={"collection": "col", "query": "test"})
    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) == 1
    assert data["acl_filtered"] is False


def test_search_acl_match_returned(tmp_path: Path) -> None:
    """Chunk with acl=['default'] and caller namespace='default' → included."""
    app, client = _make_app(tmp_path)
    result = _make_result(1, acl=["default"])
    app.state.pipeline = _make_pipeline_mock(results=[result], acl_filtered=False)
    response = client.post("/search", json={"collection": "col", "query": "test"})
    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) == 1
    assert data["acl_filtered"] is False


def test_search_acl_no_match_excluded(tmp_path: Path) -> None:
    """Chunk with acl=['ns1'] and caller namespace='default' → excluded (pipeline handles ACL)."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=[], acl_filtered=True)
    response = client.post("/search", json={"collection": "col", "query": "test"})
    assert response.status_code == 200
    data = response.json()
    assert data["results"] == []
    assert data["acl_filtered"] is True


def test_search_acl_deny_all_excluded(tmp_path: Path) -> None:
    """Chunk with acl=[] (deny-all) → excluded for all callers (pipeline handles ACL)."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=[], acl_filtered=True)
    response = client.post("/search", json={"collection": "col", "query": "test"})
    assert response.status_code == 200
    data = response.json()
    assert data["results"] == []
    assert data["acl_filtered"] is True


def test_search_acl_filtered_false_when_no_drops(tmp_path: Path) -> None:
    """All chunks allowed → acl_filtered=false."""
    app, client = _make_app(tmp_path)
    results = [_make_result(1, acl=None), _make_result(2, acl=None)]
    app.state.pipeline = _make_pipeline_mock(results=results, acl_filtered=False)
    response = client.post("/search", json={"collection": "col", "query": "test"})
    assert response.status_code == 200
    assert response.json()["acl_filtered"] is False


def test_search_missing_namespace_denies_protected(tmp_path: Path) -> None:
    """Caller with empty namespace → ACL-protected chunks denied (pipeline handles filtering)."""
    app, client = _make_app(tmp_path)
    # Pipeline returns empty results + acl_filtered=True (as if it filtered based on namespace)
    app.state.pipeline = _make_pipeline_mock(results=[], acl_filtered=True)
    response = client.post("/search", json={"collection": "col", "query": "test"})
    assert response.status_code == 200
    data = response.json()
    assert data["acl_filtered"] is True


def test_search_result_schema_no_acl_field_in_response(tmp_path: Path) -> None:
    """Response results items don't have an 'acl' field."""
    app, client = _make_app(tmp_path)
    result = _make_result(1, acl=["default"])
    app.state.pipeline = _make_pipeline_mock(results=[result])
    response = client.post("/search", json={"collection": "col", "query": "test"})
    assert response.status_code == 200
    data = response.json()
    if data["results"]:
        assert "acl" not in data["results"][0]

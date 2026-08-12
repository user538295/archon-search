"""tests/server/test_e0e_be3_search_filters.py

BE-3: Remove SearchRequest v1 restriction; wire filters + applied_filters through POST /search.

Plan: e0e-multi-collection-filters-team-plan.md, task BE-3.

Tests:
- test_search_request_collections_with_filters_now_valid (unit)
- test_search_request_invalid_filter_still_422 (unit)
- test_post_search_single_collection_with_filter_applied_filters_echoed (unit)
- test_post_search_single_collection_no_filter_applied_filters_null (unit)
- test_post_search_multi_collection_with_file_type_filter (handler)
- test_post_search_multi_collection_no_filters_applied_filters_null (handler)
- test_post_search_multi_collection_all_empty_after_filter (handler)
- test_post_search_applied_filters_datetime_serialization (handler)
- test_post_search_multi_collection_rag_fusion_and_filters_both_passed (handler)
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from archon_search.filters import SearchFilters
from archon_search.pipeline import SearchPipelineResult
from archon_search.server.routes_search import SearchRequest


# ---------------------------------------------------------------------------
# Helpers (mirror _make_app / _make_multi_pipeline_mock from test_routes_search)
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path) -> tuple:
    from unittest.mock import patch

    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app
    from fastapi.testclient import TestClient

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    with patch("archon_search.chunker.DocumentChunker.__init__", return_value=None):
        app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {key}"})
    return app, client


def _make_multi_pipeline_mock(
    *,
    search_many_return: SearchPipelineResult | None = None,
    search_many_raises: Exception | None = None,
) -> MagicMock:
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock()
    if search_many_raises is not None:
        pipeline.search_many = AsyncMock(side_effect=search_many_raises)
    else:
        pipeline.search_many = AsyncMock(
            return_value=search_many_return
            or SearchPipelineResult(results=[], acl_filtered=False)
        )
    return pipeline


def _make_single_pipeline_mock(
    *,
    results=None,
    acl_filtered: bool = False,
) -> MagicMock:
    from archon_search.collection_meta import CollectionMeta

    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(
            results=results or [], acl_filtered=acl_filtered
        )
    )
    pipeline._global_embedder = MagicMock()
    return pipeline


# ---------------------------------------------------------------------------
# Unit tests — SearchRequest validator
# ---------------------------------------------------------------------------


def test_search_request_collections_with_filters_now_valid() -> None:
    """After removing the v1 restriction, SearchRequest with collections + filters is valid."""
    req = SearchRequest(
        collections=["a", "b"],
        query="q",
        filters=SearchFilters(file_type="md"),
    )
    assert req.collections == ["a", "b"]
    assert req.filters is not None
    assert req.filters.file_type == "md"


def test_search_request_invalid_filter_still_422() -> None:
    """Invalid filter values (empty file_type) still raise ValidationError even in multi-collection (S7)."""
    with pytest.raises(ValidationError):
        SearchRequest(
            collections=["a"],
            query="q",
            filters=SearchFilters(file_type=""),
        )


# ---------------------------------------------------------------------------
# Unit test — single-collection applied_filters echoed
# ---------------------------------------------------------------------------


def test_post_search_single_collection_with_filter_applied_filters_echoed(tmp_path: Path) -> None:
    """Single-collection search with filters returns applied_filters.language = 'en' in response."""
    from archon_search._types import SearchResult

    result = SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000001",
        text="some text",
        score=0.9,
        source_path="/docs/hello.md",
        language="en",
    )
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_single_pipeline_mock(results=[result])

    response = client.post(
        "/search",
        json={"collection": "col", "query": "q", "filters": {"language": "en"}},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["applied_filters"] is not None, "applied_filters must not be null for filtered request"
    assert data["applied_filters"]["language"] == "en"

    # Verify filter was actually forwarded to pipeline.search(), not just echoed
    app.state.pipeline.search.assert_called_once()
    call_kwargs = app.state.pipeline.search.call_args.kwargs
    assert call_kwargs.get("filters") is not None, "pipeline.search must receive filters kwarg"
    assert call_kwargs["filters"].language == "en", (
        f"pipeline.search must receive filters.language='en', got: {call_kwargs.get('filters')}"
    )


# ---------------------------------------------------------------------------
# Integration tests — multi-collection handler wiring (TestClient + mock pipeline)
# ---------------------------------------------------------------------------


def test_post_search_multi_collection_with_file_type_filter(tmp_path: Path) -> None:
    """POST /search with collections + filters returns 200; filters passed to search_many;
    applied_filters.file_type == 'md' (dot stripped/normalised by SearchFilters)."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(results=[], acl_filtered=False)
    )

    # Send file_type with dot prefix — SearchFilters normalises it to 'md'
    response = client.post(
        "/search",
        json={"collections": ["docs", "code"], "query": "q", "filters": {"file_type": ".md"}},
    )

    assert response.status_code == 200
    data = response.json()
    # applied_filters must be echoed
    assert data["applied_filters"] is not None
    assert data["applied_filters"]["file_type"] == "md"

    # pipeline.search_many must have been called with filters
    app.state.pipeline.search_many.assert_called_once()
    call_kwargs = app.state.pipeline.search_many.call_args.kwargs
    assert "filters" in call_kwargs
    assert call_kwargs["filters"] is not None
    assert call_kwargs["filters"].file_type == "md"


def test_post_search_multi_collection_no_filters_applied_filters_null(tmp_path: Path) -> None:
    """POST /search with collections but no filters returns applied_filters: null (S3, S11)."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(results=[], acl_filtered=False)
    )

    response = client.post(
        "/search",
        json={"collections": ["a", "b"], "query": "q"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["applied_filters"] is None, (
        f"applied_filters must be null when no filters sent, got: {data['applied_filters']}"
    )

    # pipeline must receive filters=None, not a default-constructed SearchFilters()
    app.state.pipeline.search_many.assert_called_once()
    assert app.state.pipeline.search_many.call_args.kwargs["filters"] is None


def test_post_search_single_collection_no_filter_applied_filters_null(tmp_path: Path) -> None:
    """Single-collection search with no filters returns applied_filters: null."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_single_pipeline_mock()

    response = client.post(
        "/search",
        json={"collection": "col", "query": "q"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["applied_filters"] is None, (
        f"applied_filters must be null when no filters sent, got: {data['applied_filters']}"
    )


def test_post_search_multi_collection_all_empty_after_filter(tmp_path: Path) -> None:
    """POST /search with collections + filter returning no results: 200, results: [], applied_filters non-null (S5)."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(results=[], acl_filtered=False)
    )

    response = client.post(
        "/search",
        json={"collections": ["a", "b"], "query": "q", "filters": {"file_type": "md"}},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["results"] == []
    assert data["applied_filters"] is not None, "applied_filters must be set even when results are empty"
    assert data["applied_filters"]["file_type"] == "md"


def test_post_search_applied_filters_datetime_serialization(tmp_path: Path) -> None:
    """POST /search with indexed_after filter: applied_filters.indexed_after serialises correctly in JSON."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(results=[], acl_filtered=False)
    )

    response = client.post(
        "/search",
        json={"collections": ["a", "b"], "query": "q", "filters": {"indexed_after": "2024-01-15"}},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["applied_filters"] is not None
    # indexed_after must appear as a serialised datetime string (not null, not omitted)
    indexed_after_val = data["applied_filters"].get("indexed_after")
    assert indexed_after_val is not None, "indexed_after must be present in applied_filters"
    # Must be a string (ISO datetime serialisation)
    assert isinstance(indexed_after_val, str), f"indexed_after must be a string, got: {type(indexed_after_val)}"
    from datetime import datetime, timezone
    parsed = datetime.fromisoformat(indexed_after_val.replace("Z", "+00:00"))
    assert parsed == datetime(2024, 1, 15, 0, 0, 0, tzinfo=timezone.utc), (
        f"indexed_after must serialise to midnight UTC, got: {indexed_after_val}"
    )


def test_post_search_multi_collection_rag_fusion_and_filters_both_passed(tmp_path: Path) -> None:
    """rag_fusion=True and filters are not mutually exclusive at the handler level.

    The rag_fusion=True branch short-circuits before resolve_hyde_vector is called,
    so no HyDE patch is needed here.
    """
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(results=[], acl_filtered=False)
    )

    response = client.post(
        "/search",
        json={"collections": ["a", "b"], "query": "q", "filters": {"file_type": "py"}, "rag_fusion": True},
    )

    assert response.status_code == 200
    call_kwargs = app.state.pipeline.search_many.call_args.kwargs
    assert call_kwargs["filters"] is not None
    assert call_kwargs["filters"].file_type == "py"
    assert call_kwargs["rag_fusion"] is True


def test_post_search_missing_collection_does_not_carry_applied_filters(tmp_path: Path) -> None:
    """404 error response from a missing collection does NOT carry applied_filters.

    Error responses are JSONResponse (not SearchResponse), so applied_filters
    must be absent from the JSON body.
    """
    app, client = _make_app(tmp_path)
    # Raise CollectionNotFoundError to trigger the 404 path
    from archon_search.pipeline import CollectionNotFoundError
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_raises=CollectionNotFoundError("ghost-col")
    )

    response = client.post(
        "/search",
        json={"collections": ["ghost-col"], "query": "q", "filters": {"file_type": "md"}},
    )

    assert response.status_code == 404, f"expected 404, got {response.status_code}"
    data = response.json()
    assert "applied_filters" not in data, (
        f"404 error response must not carry applied_filters, got: {data}"
    )


def test_post_search_multi_collection_language_filter_accepted(tmp_path: Path) -> None:
    """POST /search with collections + language filter returns 200 (previously 422 for the MCP path;
    the REST path also previously rejected this via the v1 restriction).

    Verifies the documented claim: language filter is now usable with multi-collection fan-out.
    """
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(results=[], acl_filtered=False)
    )

    response = client.post(
        "/search",
        json={"collections": ["docs", "code"], "query": "q", "filters": {"language": "en"}},
    )

    assert response.status_code == 200, (
        f"collections + language filter must return 200 (v1 restriction removed), "
        f"got {response.status_code}: {response.text}"
    )
    data = response.json()
    assert data["applied_filters"] is not None
    assert data["applied_filters"]["language"] == "en"

"""Tests for POST /search with A2 metadata filters."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch

from archon_search._types import SearchResult
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.pipeline import SearchPipelineResult
from archon_search.server.app import create_app


def _make_app(tmp_path: Path) -> tuple:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    # Patch DocumentChunker to avoid chonkie tokenizer download (same pattern as test_routes_explain.py)
    with patch("archon_search.server.app.DocumentChunker") as mock_chunker_cls:
        mock_chunker_cls.return_value = MagicMock()
        app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return app, client


def _make_pipeline_mock(
    results: list[SearchResult] | None = None,
    acl_filtered: bool = False,
    meta_return=...,
) -> MagicMock:
    from archon_search.collection_meta import CollectionMeta

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


def _make_search_result(
    n: int = 1,
    file_type: str = "md",
    language: str | None = None,
    metadata: dict | None = None,
) -> SearchResult:
    return SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + f"-{n:06d}",
        text=f"result text {n}",
        score=0.9 - n * 0.1,
        source_path=f"/path/to/doc{n}.md",
        file_type=file_type,
        language=language,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# Filter forwarding tests
# ---------------------------------------------------------------------------


def test_post_search_with_file_type_filter_returns_filtered_results(tmp_path: Path) -> None:
    """POST /search with filters.file_type calls pipeline.search() with filters set."""
    results = [_make_search_result(1, file_type="md")]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post(
        "/search",
        json={
            "collection": "col",
            "query": "test",
            "filters": {"file_type": "md"},
        },
    )

    assert response.status_code == 200
    call_kwargs = app.state.pipeline.search.call_args
    filters_arg = call_kwargs.kwargs.get("filters")
    assert filters_arg is not None
    assert filters_arg.file_type == "md"


def test_post_search_invalid_filter_returns_422_with_validator_message(tmp_path: Path) -> None:
    """Invalid filter (language=en) → 422 with validation message."""
    _, client = _make_app(tmp_path)

    response = client.post(
        "/search",
        json={
            "collection": "col",
            "query": "test",
            "filters": {"language": "en"},
        },
    )

    assert response.status_code == 422
    assert "C2" in response.text


def test_post_search_no_filter_unchanged_behavior(tmp_path: Path) -> None:
    """POST /search without filters= works exactly as before (no regression)."""
    results = [_make_search_result(1)]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post(
        "/search",
        json={"collection": "col", "query": "test"},
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) == 1


def test_search_response_includes_language_field(tmp_path: Path) -> None:
    """SearchResultSchema must include language field."""
    results = [_make_search_result(1, language="en")]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post("/search", json={"collection": "col", "query": "q"})

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert "language" in result
    assert result["language"] == "en"


def test_search_response_language_none_when_not_set(tmp_path: Path) -> None:
    """SearchResultSchema language=None when not set on SearchResult."""
    results = [_make_search_result(1, language=None)]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post("/search", json={"collection": "col", "query": "q"})

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result.get("language") is None


def test_search_response_omits_custom_metadata_when_include_metadata_false(tmp_path: Path) -> None:
    """When include_metadata=False (default), metadata dict is empty in response."""
    results = [_make_search_result(1, metadata={"key": "secret_value"})]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post(
        "/search",
        json={"collection": "col", "query": "q", "filters": {"include_metadata": False}},
    )

    assert response.status_code == 200
    result_data = response.json()["results"][0]
    assert result_data["metadata"] == {}


def test_search_response_includes_custom_metadata_when_include_metadata_true(tmp_path: Path) -> None:
    """When include_metadata=True, metadata dict is included in response."""
    results = [_make_search_result(1, metadata={"key": "visible_value"})]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post(
        "/search",
        json={"collection": "col", "query": "q", "filters": {"include_metadata": True}},
    )

    assert response.status_code == 200
    result_data = response.json()["results"][0]
    assert result_data["metadata"] == {"key": "visible_value"}


def test_search_metadata_omitted_when_no_filters(tmp_path: Path) -> None:
    """When no filters at all, metadata defaults to empty (include_metadata=False by default)."""
    results = [_make_search_result(1, metadata={"key": "hidden"})]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post("/search", json={"collection": "col", "query": "q"})

    assert response.status_code == 200
    result_data = response.json()["results"][0]
    # No filters → include_metadata=False → metadata suppressed
    assert result_data["metadata"] == {}

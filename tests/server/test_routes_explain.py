"""Tests for POST /explain endpoint (Task 3.1)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.pipeline import ExplainPipelineResult
from archon_search.server.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path) -> tuple:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    # Patch DocumentChunker to avoid chonkie tokenizer download
    with patch("archon_search.server.app.DocumentChunker") as mock_chunker_cls:
        mock_chunker_cls.return_value = MagicMock()
        app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return app, client


def _make_breakdown(rrf: float = 0.1, reranker: float | None = None) -> SearchScoreBreakdown:
    return SearchScoreBreakdown(
        vector_rank=0,
        vector_score=0.5,
        vector_score_kind="distance",
        fts_rank=None,
        fts_score=None,
        fts_score_kind=None,
        rrf_score=rrf,
        reranker_score=reranker,
    )


def _make_candidate(
    doc_id: str = "a" * 64,
    chunk_id_suffix: str = "000000",
    rrf: float = 0.1,
    reranker: float | None = None,
) -> ScoredSearchCandidate:
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{chunk_id_suffix}",
        text="some text",
        source_path="/path/doc.md",
        score_breakdown=_make_breakdown(rrf=rrf, reranker=reranker),
        collection="my-col",
    )


def _make_pipeline_mock(
    meta_return=...,
    all_meta_return: list[CollectionMeta] | None = None,
    explain_return: ExplainPipelineResult | None = None,
    meta_raises: Exception | None = None,
    explain_raises: Exception | None = None,
    embed_vector: list[float] | None = None,
) -> MagicMock:
    pipeline = MagicMock()

    default_meta = CollectionMeta(name="my-col", namespace="default")

    if meta_raises is not None:
        pipeline.get_collection_meta = AsyncMock(side_effect=meta_raises)
    elif meta_return is ...:
        pipeline.get_collection_meta = AsyncMock(return_value=default_meta)
    else:
        pipeline.get_collection_meta = AsyncMock(return_value=meta_return)

    pipeline.get_all_collections_meta = AsyncMock(
        return_value=all_meta_return if all_meta_return is not None else [default_meta]
    )

    default_explain = ExplainPipelineResult(
        top_results=[_make_candidate()],
        near_misses=[],
        acl_filtered=False,
    )

    if explain_raises is not None:
        pipeline.explain = AsyncMock(side_effect=explain_raises)
    else:
        pipeline.explain = AsyncMock(return_value=explain_return or default_explain)

    # Embedder on pipeline
    embedder = MagicMock()
    embedder.embed_one = AsyncMock(return_value=embed_vector or [1.0, 0.0])
    embedder.model_name = "test-model"
    pipeline._embedder = embedder

    return pipeline


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_post_explain_without_auth_returns_401(tmp_path: Path) -> None:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    with patch("archon_search.server.app.DocumentChunker") as mock_chunker_cls:
        mock_chunker_cls.return_value = MagicMock()
        app = create_app(config, job_store)
    client = TestClient(app)  # no auth headers

    response = client.post("/explain", json={"query": "hello"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_post_explain_empty_query_returns_422(tmp_path: Path) -> None:
    _, client = _make_app(tmp_path)
    response = client.post("/explain", json={"query": ""})
    assert response.status_code == 422


def test_post_explain_top_k_above_100_returns_422(tmp_path: Path) -> None:
    _, client = _make_app(tmp_path)
    response = client.post("/explain", json={"query": "q", "top_k": 101})
    assert response.status_code == 422


def test_post_explain_top_k_below_1_returns_422(tmp_path: Path) -> None:
    _, client = _make_app(tmp_path)
    response = client.post("/explain", json={"query": "q", "top_k": 0})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 404 cases
# ---------------------------------------------------------------------------


def test_post_explain_pinned_collection_not_found_returns_404(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(meta_return=None)

    response = client.post("/explain", json={"query": "hello", "collection": "no-such-col"})
    assert response.status_code == 404


def test_post_explain_collectionless_no_collections_returns_404(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(all_meta_return=[])

    response = client.post("/explain", json={"query": "hello"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 500 cases
# ---------------------------------------------------------------------------


def test_post_explain_store_failure_returns_500(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(
        explain_raises=RuntimeError("store boom")
    )

    response = client.post("/explain", json={"query": "hello", "collection": "my-col"})
    assert response.status_code == 500


def test_post_explain_reranker_failure_returns_500(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(
        explain_raises=ValueError("reranker score count mismatch")
    )

    response = client.post("/explain", json={"query": "hello", "collection": "my-col"})
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# Telemetry failure does not abort response
# ---------------------------------------------------------------------------


def test_post_explain_telemetry_writer_failure_does_not_abort_response(
    tmp_path: Path,
) -> None:
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock()

    # Install a broken writer
    broken_writer = MagicMock()
    broken_writer.enqueue = MagicMock(side_effect=RuntimeError("writer boom"))
    app.state.telemetry_writer = broken_writer

    response = client.post("/explain", json={"query": "hello", "collection": "my-col"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Happy path integration
# ---------------------------------------------------------------------------


def test_post_explain_pinned_collection_happy_path(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    candidate = _make_candidate(rrf=0.15, reranker=0.9)
    explain_result = ExplainPipelineResult(
        top_results=[candidate],
        near_misses=[],
        acl_filtered=False,
    )
    app.state.pipeline = _make_pipeline_mock(explain_return=explain_result)

    response = client.post(
        "/explain",
        json={"query": "what is archon?", "collection": "my-col", "top_k": 5},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["collection"] == "my-col"
    assert data["routing"] is None
    assert data["acl_filtered"] is False
    assert len(data["results"]) == 1
    assert data["results"][0]["score"] == pytest.approx(0.9)
    assert data["near_misses"] == []


def test_post_explain_collectionless_includes_routing_block(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    meta = CollectionMeta(
        name="my-col",
        namespace="default",
        centroid=[1.0, 0.0],
        embedding_model="test-model",
    )
    app.state.pipeline = _make_pipeline_mock(
        all_meta_return=[meta],
        explain_return=ExplainPipelineResult(
            top_results=[_make_candidate()],
            near_misses=[],
            acl_filtered=False,
        ),
    )

    response = client.post("/explain", json={"query": "what is archon?"})

    assert response.status_code == 200
    data = response.json()
    assert data["routing"] is not None
    assert data["routing"]["invoked"] is True
    assert data["routing"]["chosen_collection"] == "my-col"
    assert len(data["routing"]["candidates"]) >= 1


def test_post_explain_near_miss_no_text_field(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    near = _make_candidate(doc_id="b" * 64, rrf=0.05)
    explain_result = ExplainPipelineResult(
        top_results=[_make_candidate()],
        near_misses=[near],
        acl_filtered=False,
    )
    app.state.pipeline = _make_pipeline_mock(explain_return=explain_result)

    response = client.post(
        "/explain",
        json={"query": "test", "collection": "my-col"},
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["near_misses"]) == 1
    assert "text" not in data["near_misses"][0]

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


def test_post_explain_error_path_emits_error_telemetry(tmp_path: Path) -> None:
    """Error paths must call writer.enqueue with a from_error entry (not abort the response)."""
    from archon_search.telemetry.entry import TelemetryEntry
    from archon_search.telemetry.writer import TelemetryWriter

    enqueued: list[TelemetryEntry] = []
    writer = MagicMock(spec=TelemetryWriter)
    writer.enqueue.side_effect = lambda entry: enqueued.append(entry)

    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(explain_raises=RuntimeError("pipeline fail"))
    app.state.telemetry_writer = writer

    response = client.post("/explain", json={"query": "error test", "collection": "my-col"})

    assert response.status_code == 500
    assert len(enqueued) == 1
    entry = enqueued[0]
    assert entry.status != "ok"
    assert entry.endpoint == "explain"


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


# ---------------------------------------------------------------------------
# AC9: Telemetry emits no query text
# ---------------------------------------------------------------------------


def test_post_explain_telemetry_emits_no_query(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from archon_search.telemetry.entry import TelemetryEntry
    from archon_search.telemetry.writer import TelemetryWriter

    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock()

    mock_writer = MagicMock(spec=TelemetryWriter)
    app.state.telemetry_writer = mock_writer

    unique_query = "UNIQUE_SENTINEL_QUERY_AC9_XYZ"
    response = client.post(
        "/explain",
        json={"query": unique_query, "collection": "my-col"},
    )

    assert response.status_code == 200
    mock_writer.enqueue.assert_called_once()
    entry: TelemetryEntry = mock_writer.enqueue.call_args[0][0]

    entry_dict = entry.model_dump()
    assert "query" not in entry_dict
    assert unique_query not in str(entry_dict)
    assert entry.endpoint == "explain"
    assert isinstance(entry.result_count, int)


# ---------------------------------------------------------------------------
# AC8: ACL filtering — only same-namespace collections appear in routing.candidates
# ---------------------------------------------------------------------------


def test_post_explain_routing_candidates_acl_filtered(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)

    meta_ns = CollectionMeta(
        name="col-ns",
        namespace="default",
        centroid=[1.0, 0.0],
        embedding_model="test-model",
    )
    meta_other = CollectionMeta(
        name="col-other",
        namespace="other-ns",
        centroid=[0.0, 1.0],
        embedding_model="test-model",
    )

    app.state.pipeline = _make_pipeline_mock(
        all_meta_return=[meta_ns, meta_other],
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
    candidate_names = [c["collection"] for c in data["routing"]["candidates"]]
    assert "col-ns" in candidate_names
    assert "col-other" not in candidate_names


# ---------------------------------------------------------------------------
# AC11: acl_filtered=True propagates to response
# ---------------------------------------------------------------------------


def test_post_explain_acl_filtered_flag_true(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    explain_result = ExplainPipelineResult(
        top_results=[_make_candidate()],
        near_misses=[],
        acl_filtered=True,
    )
    app.state.pipeline = _make_pipeline_mock(explain_return=explain_result)

    response = client.post(
        "/explain",
        json={"query": "what is archon?", "collection": "my-col"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["acl_filtered"] is True


# ---------------------------------------------------------------------------
# AC8: Collectionless + all collections in wrong namespace → 404
# ---------------------------------------------------------------------------


def test_post_explain_collectionless_all_acl_filtered_returns_404(tmp_path: Path) -> None:
    """When all available collections belong to a different namespace, return 404."""
    app, client = _make_app(tmp_path)
    # All collections are in "other-ns", not the caller's "default" namespace
    other_meta = CollectionMeta(name="col-other", namespace="other-ns")
    app.state.pipeline = _make_pipeline_mock(
        all_meta_return=[other_meta],
        explain_return=ExplainPipelineResult(
            top_results=[_make_candidate()],
            near_misses=[],
            acl_filtered=False,
        ),
    )

    response = client.post("/explain", json={"query": "test query"})

    assert response.status_code == 404
    assert response.json()["detail"] == "no collections available"


# ---------------------------------------------------------------------------
# Fix 6: 503 tests for meta-lookup exception paths
# ---------------------------------------------------------------------------


def test_post_explain_pinned_meta_lookup_exception_returns_503(tmp_path: Path) -> None:
    """When get_collection_meta raises an Exception, pinned path returns 503."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(
        meta_raises=RuntimeError("database connection lost")
    )

    response = client.post("/explain", json={"query": "hello", "collection": "my-col"})
    assert response.status_code == 503
    assert response.json()["detail"] == "service unavailable"


def test_post_explain_collectionless_router_failure_returns_503(tmp_path: Path) -> None:
    """When get_all_collections_meta raises an Exception, collectionless path returns 503."""
    app, client = _make_app(tmp_path)

    pipeline = _make_pipeline_mock()
    pipeline.get_all_collections_meta = AsyncMock(side_effect=RuntimeError("meta store down"))
    app.state.pipeline = pipeline

    response = client.post("/explain", json={"query": "hello"})
    assert response.status_code == 503
    assert response.json()["detail"] == "service unavailable"


# ---------------------------------------------------------------------------
# Fix 7: Embedding failure 500 test
# ---------------------------------------------------------------------------


def test_post_explain_collectionless_embedding_failure_returns_500(tmp_path: Path) -> None:
    """When embed_one raises a non-ValueError Exception, collectionless path returns 500."""
    app, client = _make_app(tmp_path)

    meta = CollectionMeta(
        name="my-col",
        namespace="default",
        centroid=[1.0, 0.0],
        embedding_model="test-model",
    )
    pipeline = _make_pipeline_mock(all_meta_return=[meta])
    pipeline._embedder.embed_one = AsyncMock(side_effect=RuntimeError("CUDA OOM"))
    app.state.pipeline = pipeline

    response = client.post("/explain", json={"query": "hello"})
    assert response.status_code == 500
    assert response.json()["detail"] == "internal server error"


# ---------------------------------------------------------------------------
# Fix 9: Verify chosen_below_threshold field
# ---------------------------------------------------------------------------


def test_post_explain_collectionless_routing_block_fields(tmp_path: Path) -> None:
    """routing block includes confidence_threshold and chosen_below_threshold as bool."""
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
    routing = data["routing"]
    assert routing is not None

    # confidence_threshold equals the config default
    from archon_search.config import SearchConfig
    default_threshold = SearchConfig().routing_confidence_threshold
    assert routing["confidence_threshold"] == pytest.approx(default_threshold)

    # chosen_below_threshold is a boolean
    assert isinstance(routing["chosen_below_threshold"], bool)


# ---------------------------------------------------------------------------
# Fix 2: Timeout returns 504
# ---------------------------------------------------------------------------


def test_post_explain_pipeline_timeout_returns_504(tmp_path: Path) -> None:
    """When pipeline.explain times out, the endpoint returns HTTP 504."""
    import asyncio as _asyncio

    app, client = _make_app(tmp_path)

    async def _slow(*args: object, **kwargs: object) -> None:
        raise _asyncio.TimeoutError()

    pipeline = _make_pipeline_mock()
    pipeline.explain = AsyncMock(side_effect=_asyncio.TimeoutError())
    app.state.pipeline = pipeline

    response = client.post("/explain", json={"query": "hello", "collection": "my-col"})
    assert response.status_code == 504

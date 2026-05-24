"""Tests for POST /search endpoint ( + )."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search._types import SearchResult
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.pipeline import SearchPipeline, SearchPipelineResult
from archon_search.server.app import create_app


def _make_app(tmp_path: Path) -> tuple:
    """Create app and return (app, client) with pipeline mock on app.state.

    DocumentChunker.__init__ is patched to skip gpt2 tokenizer download so
    tests pass in network-restricted environments.  Tests that replace
    app.state.pipeline immediately after this call are unaffected; tests that
    inspect the real pipeline (isinstance, store identity) still work because
    SearchPipeline is constructed normally — only the embedded chunker is a
    stub that must not be invoked.
    """
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    with patch("archon_search.chunker.DocumentChunker.__init__", return_value=None):
        app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, raise_server_exceptions=False, headers={"Authorization": f"Bearer {key}"})
    return app, client


# ---------------------------------------------------------------------------
# create_app pipeline wiring tests ( / 3.4)
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
# pipeline delegation tests
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


def test_search_pipeline_error_returns_500(tmp_path: Path) -> None:
    """When pipeline.search() raises → HTTP 500 (bare re-raise; plain text body from ServerErrorMiddleware)."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(search_raises=RuntimeError("search boom"))

    response = client.post("/search", json={"collection": "col", "query": "q"})

    assert response.status_code == 500


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
# 5. Exception in pipeline.search() → HTTP 500 with structured error log
# ---------------------------------------------------------------------------


def test_search_store_exception_returns_500(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Exception in pipeline.search() (after successful meta lookup) → HTTP 500 with standard error envelope."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(search_raises=RuntimeError("db failure"))

    with caplog.at_level(logging.ERROR, logger="archon.search"):
        response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 500
    assert any("search pipeline failed" in record.message for record in caplog.records)


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
# 7. Pipeline search failure → HTTP 500
# ---------------------------------------------------------------------------


def test_search_embedder_failure_returns_500(tmp_path: Path) -> None:
    """pipeline.search() failure → HTTP 500 (bare re-raise; plain text body from ServerErrorMiddleware)."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(search_raises=RuntimeError("model error"))

    response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 500


# ---------------------------------------------------------------------------
# 8. Reranker failure inside pipeline → HTTP 500
# ---------------------------------------------------------------------------


def test_search_reranker_failure_returns_500(tmp_path: Path) -> None:
    """Any exception from pipeline.search() → HTTP 500 (bare re-raise; plain text body from ServerErrorMiddleware)."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(search_raises=ValueError("score count mismatch"))

    response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 500


# ---------------------------------------------------------------------------
# 8. Integration: ingest a doc, search, verify result appears
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 9. Route delegates to pipeline, not inline store/reranker logic 
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
# 11. Cross-namespace — returns 404 
# ---------------------------------------------------------------------------


def test_search_cross_namespace_404(tmp_path: Path) -> None:
    """When get_collection_meta returns None (wrong namespace), response is 404."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(meta_return=None)

    response = client.post("/search", json={"collection": "other-col", "query": "test"})

    assert response.status_code == 404
    app.state.pipeline.search.assert_not_called()


# ---------------------------------------------------------------------------
# 12. Store exception on meta lookup — returns 503 
# ---------------------------------------------------------------------------


def test_search_store_exception_returns_503(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """When get_collection_meta raises (LanceDB error), response is 503, not 404 or 200.

    Also verifies that the 503 meta-lookup failure path does NOT enqueue a telemetry
    entry (telemetry is reserved for the search-execution failure paths).
    """
    from archon_search.telemetry.writer import TelemetryWriter

    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(meta_raises=RuntimeError("lancedb failure"))
    writer_mock = MagicMock(spec=TelemetryWriter)
    app.state.telemetry_writer = writer_mock

    with caplog.at_level(logging.ERROR, logger="archon.search"):
        response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 503
    app.state.pipeline.search.assert_not_called()
    assert any("service unavailable" in record.message.lower() or "lancedb" in record.message.lower() or "col" in record.message for record in caplog.records)
    # 503 meta-lookup path must not enqueue telemetry.
    assert writer_mock.enqueue.call_count == 0


# ---------------------------------------------------------------------------
# 13. Pipeline failure 500 body is plain-text (not JSON) — A3/CON-5
# ---------------------------------------------------------------------------


def test_search_pipeline_failure_500_body_is_plain_text(tmp_path: Path) -> None:
    """POST /search pipeline failure → HTTP 500 with plain-text body from Starlette
    ServerErrorMiddleware (bare re-raise).  Callers must NOT call .json() on the
    500 body; it is not a JSON envelope.
    """
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(search_raises=RuntimeError("store down"))

    response = client.post("/search", json={"collection": "col", "query": "q"})

    assert response.status_code == 500
    # Body must be plain-text "Internal Server Error" — NOT a JSON envelope.
    assert response.text == "Internal Server Error"
    assert "text/plain" in response.headers.get("content-type", "")


def test_search_pipeline_failure_500_body_is_not_json_parseable(tmp_path: Path) -> None:
    """Calling response.json() on a pipeline-failure 500 raises JSONDecodeError.
    Documents the key breaking-change behavior introduced in A3.
    """
    import json

    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(search_raises=ValueError("embedder crash"))

    response = client.post("/search", json={"collection": "col", "query": "q"})

    assert response.status_code == 500
    with pytest.raises((json.JSONDecodeError, Exception)):
        response.json()


# ---------------------------------------------------------------------------
# 14. Timeout 504 body contains JSON detail — A3/CON-5
# ---------------------------------------------------------------------------


def test_search_timeout_504_body_has_search_timed_out_detail(tmp_path: Path) -> None:
    """POST /search timeout → HTTP 504 with JSON body {"detail": "Search timed out"}.
    Unlike the 500 bare-re-raise, the 504 is raised via HTTPException which FastAPI
    serialises as a JSON envelope — safe to call .json() on.
    """
    import archon_search.server.routes_search as routes_search_module

    app, client = _make_app(tmp_path)

    # Patch the timeout constant to a near-zero value so the test is fast.
    original_timeout = routes_search_module._SEARCH_TIMEOUT_SECONDS
    routes_search_module._SEARCH_TIMEOUT_SECONDS = 0.01

    import asyncio

    async def _slow_search(*args, **kwargs):
        await asyncio.sleep(60)

    app.state.pipeline = _make_pipeline_mock()
    app.state.pipeline.search = _slow_search  # type: ignore[assignment]

    try:
        response = client.post("/search", json={"collection": "col", "query": "q"})
    finally:
        routes_search_module._SEARCH_TIMEOUT_SECONDS = original_timeout

    assert response.status_code == 504
    body = response.json()
    assert body["detail"] == "Search timed out"


def test_search_timeout_detail_differs_from_route_timeout_detail(tmp_path: Path) -> None:
    """The /search 504 detail string 'Search timed out' is distinct from /route's
    'routing timed out' — both endpoints are tested to have their own detail string.
    Regression guard: they must never accidentally share the same string.
    """
    import archon_search.server.routes_search as routes_search_module
    import asyncio

    app, client = _make_app(tmp_path)
    original_timeout = routes_search_module._SEARCH_TIMEOUT_SECONDS
    routes_search_module._SEARCH_TIMEOUT_SECONDS = 0.01

    async def _slow_search(*args, **kwargs):
        await asyncio.sleep(60)

    app.state.pipeline = _make_pipeline_mock()
    app.state.pipeline.search = _slow_search  # type: ignore[assignment]

    try:
        response = client.post("/search", json={"collection": "col", "query": "q"})
    finally:
        routes_search_module._SEARCH_TIMEOUT_SECONDS = original_timeout

    assert response.status_code == 504
    body = response.json()
    # Must be the /search-specific string, NOT the /route string.
    assert body["detail"] == "Search timed out"
    assert body["detail"] != "routing timed out"


# ---------------------------------------------------------------------------
# 15. 503 and 404 response body formats — A3/CON-5
# ---------------------------------------------------------------------------


def test_search_meta_lookup_failure_503_body_format(tmp_path: Path) -> None:
    """503 from meta-lookup failure → JSON body {"detail": "service unavailable"}."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(meta_raises=RuntimeError("lancedb gone"))

    response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 503
    body = response.json()
    assert body["detail"] == "service unavailable"


def test_search_collection_not_found_404_body_format(tmp_path: Path) -> None:
    """404 from cross-namespace / missing collection → JSON body {"detail": "collection not found"}."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(meta_return=None)

    response = client.post("/search", json={"collection": "missing", "query": "test"})

    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "collection not found"


# ---------------------------------------------------------------------------
# 16. Regression: empty results (200) is a healthy pipeline signal — A3/CON-5
# ---------------------------------------------------------------------------


def test_search_empty_results_200_is_success_not_error(tmp_path: Path) -> None:
    """HTTP 200 with results=[] means the pipeline ran successfully but found no
    matching documents — it is NOT a failure signal.  Regression guard for CON-5:
    pre-A3 this was the failure-downgrade path; post-A3 it is purely a success path.
    """
    app, client = _make_app(tmp_path)
    # Pipeline succeeds but returns no hits.
    app.state.pipeline = _make_pipeline_mock(results=[], acl_filtered=False)

    response = client.post("/search", json={"collection": "col", "query": "no matches"})

    assert response.status_code == 200
    body = response.json()
    assert body["results"] == []
    assert body["acl_filtered"] is False
    # pipeline.search must have been called (not short-circuited by an error handler)
    app.state.pipeline.search.assert_called_once()


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

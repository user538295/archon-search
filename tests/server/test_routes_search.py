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
    pipeline.warmup_models = AsyncMock()

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


def test_search_response_carries_collection_provenance(tmp_path: Path) -> None:
    """The collection field on SearchResult must round-trip through the HTTP JSON body."""
    result = SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000001",
        text="result text",
        score=0.9,
        source_path="/path/to/doc.md",
        collection="my-col",
    )
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=[result])

    response = client.post("/search", json={"collection": "my-col", "query": "q"})

    assert response.status_code == 200
    assert response.json()["results"][0]["collection"] == "my-col"


def test_search_response_excluded_collections_empty_on_single_collection(tmp_path: Path) -> None:
    """Single-collection /search must emit an empty excluded_collections envelope."""
    results = [_make_search_result(1)]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post("/search", json={"collection": "col", "query": "q"})

    assert response.status_code == 200
    assert response.json()["excluded_collections"] == []


def test_search_collection_not_found_returns_404(tmp_path: Path) -> None:
    """When get_collection_meta returns None, 404 is returned."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(meta_return=None)

    response = client.post("/search", json={"collection": "nonexistent", "query": "test"})

    assert response.status_code == 404
    assert "code" not in response.json()


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
    body = response.json()
    assert body["code"] == "metadata_store_error"
    assert "metadata store" in body["detail"]
    app.state.pipeline.search.assert_not_called()
    assert any("service unavailable" in record.message.lower() or "lancedb" in record.message.lower() or "col" in record.message for record in caplog.records)
    # 503 meta-lookup path must not enqueue telemetry.
    assert writer_mock.enqueue.call_count == 0


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Task 4.1: SearchFilters embedded in SearchRequest
# ---------------------------------------------------------------------------


def test_post_search_with_file_type_filter_returns_filtered_results(tmp_path: Path) -> None:
    """filters field reaches pipeline.search() as a SearchFilters instance."""
    from archon_search.filters import SearchFilters

    results = [_make_search_result(1)]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post(
        "/search",
        json={"collection": "col", "query": "q", "filters": {"file_type": "md"}},
    )

    assert response.status_code == 200
    call_kwargs = app.state.pipeline.search.call_args
    assert "filters" in call_kwargs.kwargs
    filters = call_kwargs.kwargs["filters"]
    assert isinstance(filters, SearchFilters), f"Expected SearchFilters, got {type(filters)}"
    assert filters.file_type == "md"


def test_post_search_all_filter_types_forwarded(tmp_path: Path) -> None:
    """source_path_prefix, source_path_glob, indexed_after, indexed_before all reach pipeline."""
    from archon_search.filters import SearchFilters

    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock()

    response = client.post(
        "/search",
        json={
            "collection": "col",
            "query": "q",
            "filters": {
                "source_path_prefix": "/docs/",
                "source_path_glob": "*.md",
                "indexed_after": "2025-01-01",
                "indexed_before": "2025-12-31",
            },
        },
    )

    assert response.status_code == 200
    filters = app.state.pipeline.search.call_args.kwargs["filters"]
    assert isinstance(filters, SearchFilters)
    assert filters.source_path_prefix == "/docs/"
    assert filters.source_path_glob == "*.md"
    assert filters.indexed_after is not None
    assert filters.indexed_before is not None


def test_post_search_invalid_filter_returns_422_with_validator_message(tmp_path: Path) -> None:
    """Pydantic validation errors on SearchFilters surface as HTTP 422 with detail."""
    _, client = _make_app(tmp_path)

    # empty file_type — message should mention file_type
    resp = client.post("/search", json={"collection": "col", "query": "q", "filters": {"file_type": ""}})
    assert resp.status_code == 422
    detail = str(resp.json().get("detail", ""))
    assert detail, "422 response must have a non-empty detail field"

    # indexed_after > indexed_before — message should mention the ordering constraint
    resp = client.post(
        "/search",
        json={
            "collection": "col",
            "query": "q",
            "filters": {"indexed_after": "2025-12-31", "indexed_before": "2025-01-01"},
        },
    )
    assert resp.status_code == 422
    assert resp.json().get("detail"), "422 must include detail for date-range inversion"

    # invalid language code (too long — "english" is 7 chars, exceeds 2–3 letter constraint)
    resp = client.post("/search", json={"collection": "col", "query": "q", "filters": {"language": "english"}})
    assert resp.status_code == 422
    assert resp.json().get("detail"), "422 must include detail for invalid language code"


def test_post_search_no_filter_unchanged_behavior(tmp_path: Path) -> None:
    """Omitting filters field is backward-compatible — results still returned."""
    results = [_make_search_result(1)]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post("/search", json={"collection": "col", "query": "q"})

    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) == 1


def test_post_search_metadata_suppressed_by_default(tmp_path: Path) -> None:
    """No filters → include_metadata defaults to False → metadata stripped from ALL results."""
    results = [
        SearchResult(
            doc_id="a" * 64,
            chunk_id="a" * 64 + f"-{i:06d}",
            text=f"text {i}",
            score=0.9 - i * 0.1,
            source_path="/tmp/doc.md",
            metadata={"k": f"v{i}"},
        )
        for i in range(3)
    ]
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=results)

    response = client.post("/search", json={"collection": "col", "query": "q"})

    assert response.status_code == 200
    for r in response.json()["results"]:
        assert r["metadata"] == {}, f"metadata not suppressed: {r['metadata']}"


def test_post_search_include_metadata_true_passes_through(tmp_path: Path) -> None:
    """include_metadata=True in filters → metadata present in response."""
    result = SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000001",
        text="some text",
        score=0.9,
        source_path="/tmp/doc.md",
        metadata={"author": "alice"},
    )
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=[result])

    response = client.post(
        "/search",
        json={"collection": "col", "query": "q", "filters": {"include_metadata": True}},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["results"][0]["metadata"] == {"author": "alice"}


def test_post_search_unknown_collection_returns_404_not_422(tmp_path: Path) -> None:
    """Unknown collection → 404, not 422 (not a validation error)."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(meta_return=None)

    response = client.post(
        "/search",
        json={"collection": "no-such-col", "query": "q", "filters": {"file_type": "pdf"}},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "collection not found"



@pytest.mark.integration
async def test_search_filter_excludes_everything_returns_200_empty(tmp_path: Path) -> None:
    """Filters that exclude all rows → 200 with empty results list."""
    from archon_search._types import ChunkRecord
    from archon_search.collection_meta import CollectionMeta
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
        file_type="md",
    )
    await store.ensure_collection("filtercol", len(vector))
    await store.ingest_chunks("filtercol", [chunk])
    await store.update_collection_meta(
        CollectionMeta(name="filtercol", active_embedding_model=config.embedding_model, namespace="default")
    )
    await store.disconnect()

    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    with TestClient(app, headers={"Authorization": f"Bearer {key}"}) as client:
        response = client.post(
            "/search",
            json={"collection": "filtercol", "query": "hello world", "filters": {"file_type": "pdf"}},
        )
    assert response.status_code == 200
    assert response.json()["results"] == []


@pytest.mark.integration
async def test_include_metadata_false_suppresses_metadata_end_to_end(tmp_path: Path) -> None:
    """include_metadata=False suppresses metadata; True makes it present."""
    from archon_search._types import ChunkRecord
    from archon_search.collection_meta import CollectionMeta
    from archon_search.embedder import Embedder, ModelEmbedder
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "BAAI/bge-small-en-v1.5"

    store = SearchStore(config.db_path)
    await store.connect()

    embedder = Embedder(ModelEmbedder(config.embedding_model))
    vector = await embedder.embed_one("metadata test")

    chunk = ChunkRecord(
        doc_id="b" * 64,
        chunk_id="b" * 64 + "-000000",
        text="metadata test document",
        vector=vector,
        source_path="/docs/meta.md",
        indexed_at="2025-01-01T00:00:00",
        metadata={"author": "tester"},
    )
    await store.ensure_collection("metacol", len(vector))
    await store.ingest_chunks("metacol", [chunk])
    await store.update_collection_meta(
        CollectionMeta(name="metacol", active_embedding_model=config.embedding_model, namespace="default")
    )
    await store.disconnect()

    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    with TestClient(app, headers={"Authorization": f"Bearer {key}"}) as client:
        # Without include_metadata — metadata should be empty (suppressed)
        resp_no_meta = client.post("/search", json={"collection": "metacol", "query": "metadata test"})
        assert resp_no_meta.status_code == 200
        results_no_meta = resp_no_meta.json()["results"]
        assert len(results_no_meta) >= 1
        assert results_no_meta[0]["metadata"] == {}

        # With include_metadata=True — metadata should be present
        resp_with_meta = client.post(
            "/search",
            json={"collection": "metacol", "query": "metadata test", "filters": {"include_metadata": True}},
        )
    assert resp_with_meta.status_code == 200
    results_with_meta = resp_with_meta.json()["results"]
    assert len(results_with_meta) >= 1
    assert results_with_meta[0]["metadata"].get("author") == "tester"


@pytest.mark.integration
async def test_search_end_to_end(tmp_path: Path) -> None:
    """Full pipeline: ingest → search → result appears."""
    from archon_search._types import ChunkRecord
    from archon_search.collection_meta import CollectionMeta
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
    await store.ensure_collection("testcol", len(vector))
    await store.ingest_chunks("testcol", [chunk])
    await store.update_collection_meta(
        CollectionMeta(name="testcol", active_embedding_model=config.embedding_model, namespace="default")
    )
    await store.disconnect()

    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    # Context-manager form runs the lifespan, which connects app.state.search_store.
    with TestClient(app, headers={"Authorization": f"Bearer {key}"}) as client:
        response = client.post("/search", json={"collection": "testcol", "query": "hello world"})
    assert response.status_code == 200
    data = response.json()
    results = data["results"]
    assert len(results) >= 1
    assert results[0]["source_path"] == "/docs/hello.md"


# ---------------------------------------------------------------------------
# B3 Task 4.1: multi-collection search (SearchRequest validator + handler)
# ---------------------------------------------------------------------------

from pydantic import ValidationError

from archon_search._types import ExcludedCollection
from archon_search.filters import SearchFilters
from archon_search.pipeline import (
    CollectionNotFoundError,
    FanoutTimeoutError,
    MetadataLookupError,
)
from archon_search.server.routes_search import (
    SearchRequest,
)
from archon_search.server.schemas import ExcludedCollectionSchema  # noqa: F401


# --- validator unit tests --------------------------------------------------


def test_search_request_both_fields_is_422() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(collection="x", collections=["y"], query="q")


def test_search_request_neither_field_is_422() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="q")


def test_search_request_empty_collections_is_422() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(collections=[], query="q")


def test_search_request_over_max_fanout_parses_ok() -> None:
    # Fanout check moved to route handler (reads config.max_fanout); Pydantic no longer rejects.
    req = SearchRequest(collections=[f"c{i}" for i in range(9)], query="q")
    assert len(req.collections) == 9


def test_search_request_whitespace_entry_is_422() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(collections=["  "], query="q")


def test_search_request_deduplicates() -> None:
    req = SearchRequest(collections=["a", "a", "b"], query="q")
    assert req.collections == ["a", "b"]


def test_search_request_strips_then_dedupes() -> None:
    """Whitespace is stripped per-item before dedup, so ' a' and 'a ' collapse."""
    req = SearchRequest(collections=[" a", "a ", " b "], query="q")
    assert req.collections == ["a", "b"]


def test_search_request_exactly_max_fanout_is_valid() -> None:
    req = SearchRequest(collections=[f"c{i}" for i in range(8)], query="q")
    assert len(req.collections) == 8


def test_search_request_single_collection_still_valid() -> None:
    req = SearchRequest(collection="x", query="q")
    assert req.collection == "x"
    assert req.collections is None


def test_search_request_single_item_collections_is_valid() -> None:
    req = SearchRequest(collections=["x"], query="q")
    assert len(req.collections) == 1


# NOTE: test_search_request_collections_with_filters_now_valid covers this invariant in
# tests/server/test_e0e_be3_search_filters.py (E0e BE-3).


# --- handler integration tests ---------------------------------------------


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


def test_search_handler_multi_collection_calls_search_many(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(results=[], acl_filtered=False)
    )

    response = client.post("/search", json={"collections": ["a", "b"], "query": "q"})

    assert response.status_code == 200
    app.state.pipeline.search_many.assert_called_once()
    # The multi-collection branch must NOT run the single-collection meta pre-check.
    app.state.pipeline.get_collection_meta.assert_not_called()


def test_search_handler_missing_collection_returns_404(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_raises=CollectionNotFoundError(["x"])
    )

    response = client.post("/search", json={"collections": ["x"], "query": "q"})

    assert response.status_code == 404
    assert response.json()["detail"] == "collection not found"


def test_search_handler_fanout_timeout_returns_504(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(search_many_raises=FanoutTimeoutError())

    response = client.post("/search", json={"collections": ["a", "b"], "query": "q"})

    assert response.status_code == 504
    assert response.json()["detail"] == "Search timed out"


def test_search_handler_meta_lookup_failure_returns_503(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_raises=MetadataLookupError(RuntimeError("x"))
    )

    response = client.post("/search", json={"collections": ["a", "b"], "query": "q"})

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "metadata_store_error"
    assert "metadata store" in body["detail"]


def test_search_response_includes_excluded_collections(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(
            results=[],
            acl_filtered=False,
            excluded_collections=[
                ExcludedCollection(name="b", reason="embedding_model_mismatch")
            ],
        )
    )

    response = client.post("/search", json={"collections": ["a", "b"], "query": "q"})

    assert response.status_code == 200
    excluded = response.json()["excluded_collections"]
    assert {"name": "b", "reason": "embedding_model_mismatch"} in excluded


def test_search_response_json_includes_collection_key(tmp_path: Path) -> None:
    result = SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + "-000001",
        text="text",
        score=0.5,
        source_path="/path/doc.md",
        collection="a",
    )
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(results=[result], acl_filtered=False)
    )

    response = client.post("/search", json={"collections": ["a"], "query": "q"})

    assert response.status_code == 200
    assert response.json()["results"][0]["collection"] == "a"


def test_search_handler_multi_collection_emits_search_multi_telemetry(tmp_path: Path) -> None:
    """The multi-collection /search path enqueues a search_multi telemetry entry
    with fanout_count = requested - excluded and the correct excluded_count."""
    from archon_search._types import ExcludedCollection
    from archon_search.telemetry.entry import EndpointKind

    app, client = _make_app(tmp_path)
    writer = MagicMock()
    app.state.telemetry_writer = writer
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(
            results=[_make_search_result(1)],
            acl_filtered=False,
            excluded_collections=[ExcludedCollection(name="c", reason="embedding_model_mismatch")],
        )
    )

    response = client.post("/search", json={"collections": ["a", "b", "c"], "query": "q"})

    assert response.status_code == 200
    writer.enqueue.assert_called_once()
    entry = writer.enqueue.call_args.args[0]
    assert entry.endpoint == EndpointKind.search_multi
    assert entry.fanout_count == 2  # 3 requested - 1 excluded
    assert entry.excluded_count == 1
    assert entry.result_count == 1
    assert entry.collections == ["a", "b", "c"]


def test_openapi_language_field_not_nullable(tmp_path):
    """SearchResultSchema.language must not be nullable in OpenAPI schema."""
    app, _ = _make_app(tmp_path)
    sync_client = TestClient(app)
    resp = sync_client.get("/openapi.json")
    schema = resp.json()
    search_result = schema.get("components", {}).get("schemas", {}).get("SearchResultSchema", {})
    lang_prop = search_result.get("properties", {}).get("language", {})
    # After C2 Task 2.3: language is str = "" (not nullable), so anyOf with null must not appear
    if "anyOf" in lang_prop:
        types_in_anyof = [t.get("type") for t in lang_prop["anyOf"]]
        assert "null" not in types_in_anyof, f"language field must not be nullable, got: {lang_prop}"
    else:
        assert lang_prop.get("type") == "string", f"language field must be string type, got: {lang_prop}"


# ---------------------------------------------------------------------------
# C4 Task 3.1: SearchRequest.hyde + SearchResponse.hyde_applied
# ---------------------------------------------------------------------------


def test_search_request_hyde_default_false() -> None:
    """SearchRequest without hyde field should default to False."""
    req = SearchRequest(query="q", collection="c")
    assert req.hyde is False


def test_search_request_accepts_hyde_true() -> None:
    """SearchRequest with hyde=True should validate without error."""
    req = SearchRequest(query="q", collection="c", hyde=True)
    assert req.hyde is True


def test_search_response_has_hyde_applied() -> None:
    """SearchResponse without hyde_applied should default to False."""
    from archon_search.server.routes_search import SearchResponse

    resp = SearchResponse(results=[], acl_filtered=False)
    assert resp.hyde_applied is False


# ---------------------------------------------------------------------------
# C4 Task 4.2: routes_search.py handler wiring for resolve_hyde_vector
# ---------------------------------------------------------------------------


def test_search_hyde_true_passes_vector_to_pipeline(tmp_path: Path) -> None:
    """hyde=true: resolve_hyde_vector returns a vector → pipeline.search called with it; response hyde_applied=True."""
    app, client = _make_app(tmp_path)
    results = [_make_search_result(1)]
    pipeline_mock = _make_pipeline_mock(results=results)
    app.state.pipeline = pipeline_mock

    hyde_vector = [0.1, 0.2, 0.3]
    with patch(
        "archon_search.server.routes_search.resolve_hyde_vector",
        new=AsyncMock(return_value=(hyde_vector, True)),
    ):
        response = client.post("/search", json={"collection": "col", "query": "q", "hyde": True})

    assert response.status_code == 200
    data = response.json()
    assert data["hyde_applied"] is True
    call_kwargs = pipeline_mock.search.call_args.kwargs
    assert call_kwargs["query_vector"] == hyde_vector


def test_search_hyde_fallback_passes_none(tmp_path: Path) -> None:
    """hyde=true but resolve_hyde_vector returns (None, False) → pipeline.search called with query_vector=None; hyde_applied=False."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock()

    with patch(
        "archon_search.server.routes_search.resolve_hyde_vector",
        new=AsyncMock(return_value=(None, False)),
    ):
        response = client.post("/search", json={"collection": "col", "query": "q", "hyde": True})

    assert response.status_code == 200
    assert response.json()["hyde_applied"] is False
    call_kwargs = app.state.pipeline.search.call_args.kwargs
    assert call_kwargs["query_vector"] is None


def test_search_hyde_package_not_installed_returns_422(tmp_path: Path) -> None:
    """resolve_hyde_vector raises RuntimeError (package missing) → 422 response."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock()

    with patch(
        "archon_search.server.routes_search.resolve_hyde_vector",
        new=AsyncMock(side_effect=RuntimeError("Install archon-search[hyde] to use HyDE")),
    ):
        response = client.post("/search", json={"collection": "col", "query": "q", "hyde": True})

    assert response.status_code == 422
    assert "hyde" in response.json()["detail"].lower() or "install" in response.json()["detail"].lower()


def test_search_many_hyde_true(tmp_path: Path) -> None:
    """Multi-collection path with hyde=true passes query_vector to search_many; response hyde_applied=True."""
    app, client = _make_app(tmp_path)
    pipeline_mock = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(results=[], acl_filtered=False)
    )
    app.state.pipeline = pipeline_mock

    hyde_vector = [0.5, 0.6, 0.7]
    with patch(
        "archon_search.server.routes_search.resolve_hyde_vector",
        new=AsyncMock(return_value=(hyde_vector, True)),
    ):
        response = client.post("/search", json={"collections": ["a", "b"], "query": "q", "hyde": True})

    assert response.status_code == 200
    assert response.json()["hyde_applied"] is True
    call_kwargs = pipeline_mock.search_many.call_args
    # query_vector should be passed as positional or keyword
    assert hyde_vector in call_kwargs.args or call_kwargs.kwargs.get("query_vector") == hyde_vector


# ---------------------------------------------------------------------------
# C5 Task 3.1: SearchRequest.rag_fusion + SearchResponse RAG Fusion fields
# ---------------------------------------------------------------------------


def test_search_request_rag_fusion_default_false() -> None:
    """SearchRequest without rag_fusion field should default to False."""
    req = SearchRequest(query="q", collection="c")
    assert req.rag_fusion is False


def test_search_request_accepts_rag_fusion_true() -> None:
    """SearchRequest with rag_fusion=True should validate without error."""
    req = SearchRequest(query="q", collection="c", rag_fusion=True)
    assert req.rag_fusion is True


def test_search_response_has_rag_fusion_fields() -> None:
    """SearchResponse should have rag_fusion_applied=False, rag_fusion_queries_used=0, rag_fusion_attempted=False by default."""
    from archon_search.server.routes_search import SearchResponse

    resp = SearchResponse(results=[], acl_filtered=False)
    assert resp.rag_fusion_applied is False
    assert resp.rag_fusion_queries_used == 0
    assert resp.rag_fusion_attempted is False


# ---------------------------------------------------------------------------
# C5 Task 4.2: routes_search.py handler wiring for RAG Fusion
# ---------------------------------------------------------------------------


def test_search_rag_fusion_true_skips_hyde(tmp_path: Path) -> None:
    """rag_fusion=True (and enabled in config) must suppress HyDE: resolve_hyde_vector NOT called; response hyde_applied=False.

    Mutual exclusion only holds when RAG Fusion can actually run (config kill-switch on).
    When config.rag_fusion.enabled=False, HyDE still applies — see the parallel
    test_explain_rag_fusion_requested_but_disabled_hyde_still_applies in test_routes_explain.py.
    """
    app, client = _make_app(tmp_path)
    app.state.config.rag_fusion.enabled = True  # enable so mutual exclusion fires
    results = [_make_search_result(1)]
    pipeline_mock = _make_pipeline_mock(results=results)
    pipeline_mock.search = AsyncMock(
        return_value=SearchPipelineResult(
            results=results, acl_filtered=False, rag_fusion_applied=True, rag_fusion_queries_used=2
        )
    )
    app.state.pipeline = pipeline_mock

    with patch(
        "archon_search.server.routes_search.resolve_hyde_vector",
        new=AsyncMock(return_value=([0.1, 0.2], True)),
    ) as mock_hyde:
        response = client.post("/search", json={"collection": "col", "query": "q", "rag_fusion": True, "hyde": True})

    assert response.status_code == 200
    data = response.json()
    assert data["hyde_applied"] is False
    mock_hyde.assert_not_called()


def test_search_rag_fusion_requested_but_disabled_hyde_still_applies(tmp_path: Path) -> None:
    """rag_fusion=True with kill-switch off (config.rag_fusion.enabled=False): HyDE still runs.

    The mutual exclusion in routes_search.py requires BOTH body.rag_fusion AND
    config.rag_fusion.enabled.  When the kill-switch is off, RAG Fusion cannot run,
    so HyDE proceeds regardless of the body flag.

    Mirrors test_explain_rag_fusion_requested_but_disabled_hyde_still_applies in
    test_routes_explain.py, which covers the same kill-switch logic on POST /explain.
    """
    app, client = _make_app(tmp_path)
    # Kill-switch is OFF (default) — RAG Fusion cannot run, HyDE must fire.
    assert app.state.config.rag_fusion.enabled is False
    results = [_make_search_result(1)]
    pipeline_mock = _make_pipeline_mock(results=results)
    pipeline_mock.search = AsyncMock(
        return_value=SearchPipelineResult(
            results=results, acl_filtered=False, rag_fusion_applied=False, rag_fusion_queries_used=0
        )
    )
    app.state.pipeline = pipeline_mock

    with patch(
        "archon_search.server.routes_search.resolve_hyde_vector",
        new=AsyncMock(return_value=([0.1, 0.2], True)),
    ) as mock_hyde:
        response = client.post(
            "/search",
            json={"collection": "col", "query": "q", "rag_fusion": True, "hyde": True},
        )

    assert response.status_code == 200
    data = response.json()
    # With kill-switch off, HyDE must have run and been applied
    assert data["hyde_applied"] is True, (
        "HyDE should apply when rag_fusion kill-switch is disabled, even if body.rag_fusion=True"
    )
    mock_hyde.assert_called_once()


def test_search_rag_fusion_true_passes_to_pipeline(tmp_path: Path) -> None:
    """rag_fusion=True: pipeline called with rag_fusion params; response carries rag_fusion_applied and rag_fusion_queries_used."""
    app, client = _make_app(tmp_path)
    results = [_make_search_result(1)]
    pipeline_mock = _make_pipeline_mock(results=results)
    pipeline_mock.search = AsyncMock(
        return_value=SearchPipelineResult(
            results=results, acl_filtered=False, rag_fusion_applied=True, rag_fusion_queries_used=2, rag_fusion_attempted=True
        )
    )
    app.state.pipeline = pipeline_mock

    with patch("archon_search.server.routes_search.resolve_hyde_vector", new=AsyncMock(return_value=(None, False))):
        response = client.post("/search", json={"collection": "col", "query": "q", "rag_fusion": True})

    assert response.status_code == 200
    data = response.json()
    assert data["rag_fusion_applied"] is True
    assert data["rag_fusion_queries_used"] == 2
    assert data["rag_fusion_attempted"] is True
    # Verify pipeline was called with rag_fusion=True
    call_kwargs = pipeline_mock.search.call_args.kwargs
    assert call_kwargs.get("rag_fusion") is True
    assert call_kwargs.get("rag_fusion_generator") is not None
    assert call_kwargs.get("rag_fusion_config") is not None


def test_search_rag_fusion_false_hyde_still_works(tmp_path: Path) -> None:
    """rag_fusion=False + hyde=True: resolve_hyde_vector IS called normally."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock()

    hyde_vector = [0.5, 0.6, 0.7]
    with patch(
        "archon_search.server.routes_search.resolve_hyde_vector",
        new=AsyncMock(return_value=(hyde_vector, True)),
    ) as mock_hyde:
        response = client.post("/search", json={"collection": "col", "query": "q", "rag_fusion": False, "hyde": True})

    assert response.status_code == 200
    mock_hyde.assert_called_once()
    assert response.json()["hyde_applied"] is True


def test_search_rag_fusion_package_not_installed_returns_422(tmp_path: Path) -> None:
    """pipeline.search() raises RAGFusionDependencyError → 422 response."""
    from archon_search.rag_fusion import RAGFusionDependencyError

    app, client = _make_app(tmp_path)
    pipeline_mock = _make_pipeline_mock()
    pipeline_mock.search = AsyncMock(side_effect=RAGFusionDependencyError("Install archon-search[rag_fusion]"))
    app.state.pipeline = pipeline_mock

    with patch("archon_search.server.routes_search.resolve_hyde_vector", new=AsyncMock(return_value=(None, False))):
        response = client.post("/search", json={"collection": "col", "query": "q", "rag_fusion": True})

    assert response.status_code == 422
    assert "rag_fusion" in response.json()["detail"].lower() or "install" in response.json()["detail"].lower()


def test_search_many_rag_fusion_true(tmp_path: Path) -> None:
    """Multi-collection path with rag_fusion=True passes rag_fusion params to search_many."""
    app, client = _make_app(tmp_path)
    pipeline_mock = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(
            results=[], acl_filtered=False, rag_fusion_applied=True, rag_fusion_queries_used=2
        )
    )
    app.state.pipeline = pipeline_mock

    with patch("archon_search.server.routes_search.resolve_hyde_vector", new=AsyncMock(return_value=(None, False))):
        response = client.post("/search", json={"collections": ["a", "b"], "query": "q", "rag_fusion": True})

    assert response.status_code == 200
    data = response.json()
    assert data["rag_fusion_applied"] is True
    assert data["rag_fusion_queries_used"] == 2
    call_kwargs = pipeline_mock.search_many.call_args.kwargs
    assert call_kwargs.get("rag_fusion") is True
    assert call_kwargs.get("rag_fusion_generator") is not None
    assert call_kwargs.get("rag_fusion_config") is not None
    # HyDE must be suppressed: query_vector=None when rag_fusion=True
    assert call_kwargs.get("query_vector") is None


# ---------------------------------------------------------------------------
# BE-3: expansion_used and expansion_warning fields in SearchResponse
# ---------------------------------------------------------------------------


def test_search_response_expansion_used_true_on_hyde_success(tmp_path: Path) -> None:
    """When HyDE succeeds (returns a vector), expansion_used=True and expansion_warning=null."""
    import numpy as np

    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(
        results=[],
        acl_filtered=False,
    )
    # HyDE returns a valid vector — hyde_applied=True
    fake_vector = np.ones(384, dtype=np.float32)
    with patch(
        "archon_search.server.routes_search.resolve_hyde_vector",
        new=AsyncMock(return_value=(fake_vector, True)),
    ):
        response = client.post("/search", json={"collection": "col", "query": "q", "hyde": True})

    assert response.status_code == 200
    data = response.json()
    assert data["expansion_used"] is True
    assert data["expansion_warning"] is None


def test_search_response_expansion_warning_on_hyde_failure(tmp_path: Path) -> None:
    """When HyDE was requested but resolve_hyde_vector returns (None, False), expansion_warning='HyDE expansion failed'."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=[], acl_filtered=False)
    # HyDE fails: returns (None, False) with hyde=True requested
    with patch(
        "archon_search.server.routes_search.resolve_hyde_vector",
        new=AsyncMock(return_value=(None, False)),
    ):
        response = client.post("/search", json={"collection": "col", "query": "q", "hyde": True})

    assert response.status_code == 200
    data = response.json()
    assert data["expansion_used"] is False
    assert data["expansion_warning"] == "HyDE expansion failed"


def test_search_response_expansion_used_or_logic(tmp_path: Path) -> None:
    """expansion_used reflects hyde_applied OR rag_fusion_applied — tests both true and false paths."""
    import numpy as np

    app, client = _make_app(tmp_path)
    # Case 1: HyDE succeeds → expansion_used=True
    app.state.pipeline = _make_pipeline_mock(results=[], acl_filtered=False)
    fake_vector = np.ones(384, dtype=np.float32)
    with patch(
        "archon_search.server.routes_search.resolve_hyde_vector",
        new=AsyncMock(return_value=(fake_vector, True)),
    ):
        resp1 = client.post("/search", json={"collection": "col", "query": "q", "hyde": True})
    assert resp1.status_code == 200
    assert resp1.json()["expansion_used"] is True
    assert resp1.json()["expansion_warning"] is None

    # Case 2: HyDE fails → expansion_used=False
    app.state.pipeline = _make_pipeline_mock(results=[], acl_filtered=False)
    with patch(
        "archon_search.server.routes_search.resolve_hyde_vector",
        new=AsyncMock(return_value=(None, False)),
    ):
        resp2 = client.post("/search", json={"collection": "col", "query": "q", "hyde": True})
    assert resp2.status_code == 200
    assert resp2.json()["expansion_used"] is False
    assert resp2.json()["expansion_warning"] == "HyDE expansion failed"


def test_search_response_expansion_warning_on_rag_fusion_timeout(tmp_path: Path) -> None:
    """When RAG Fusion timed out, expansion_warning='RAG Fusion timed out'."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(
        results=[],
        acl_filtered=False,
    )
    # Inject rag_fusion_warning on the pipeline result
    app.state.pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(
            results=[], acl_filtered=False, rag_fusion_applied=False, rag_fusion_warning="RAG Fusion timed out"
        )
    )
    response = client.post("/search", json={"collection": "col", "query": "q", "rag_fusion": True})

    assert response.status_code == 200
    data = response.json()
    assert data["expansion_used"] is False
    assert data["expansion_warning"] == "RAG Fusion timed out"


def test_search_response_no_expansion_fields_default(tmp_path: Path) -> None:
    """Without hyde/rag_fusion, expansion_used=False and expansion_warning=null."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=[], acl_filtered=False)

    response = client.post("/search", json={"collection": "col", "query": "q"})

    assert response.status_code == 200
    data = response.json()
    assert data["expansion_used"] is False
    assert data["expansion_warning"] is None


def test_search_response_expansion_used_true_on_rag_fusion_success(tmp_path: Path) -> None:
    """When RAG Fusion succeeds (rag_fusion_applied=True), expansion_used=True and expansion_warning=None."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=[], acl_filtered=False)
    app.state.pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(
            results=[], acl_filtered=False, rag_fusion_applied=True, rag_fusion_queries_used=3, rag_fusion_warning=None
        )
    )
    response = client.post("/search", json={"collection": "col", "query": "q", "rag_fusion": True})

    assert response.status_code == 200
    data = response.json()
    assert data["expansion_used"] is True
    assert data["expansion_warning"] is None


def test_search_response_expansion_warning_on_rag_fusion_generic_error(tmp_path: Path) -> None:
    """When RAG Fusion failed with a non-timeout error, expansion_warning='RAG Fusion expansion failed'."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_pipeline_mock(results=[], acl_filtered=False)
    app.state.pipeline.search = AsyncMock(
        return_value=SearchPipelineResult(
            results=[], acl_filtered=False, rag_fusion_applied=False, rag_fusion_warning="RAG Fusion expansion failed"
        )
    )
    response = client.post("/search", json={"collection": "col", "query": "q", "rag_fusion": True})

    assert response.status_code == 200
    data = response.json()
    assert data["expansion_used"] is False
    assert data["expansion_warning"] == "RAG Fusion expansion failed"


def test_search_multi_collection_expansion_used_true_on_hyde_success(tmp_path: Path) -> None:
    """Multi-collection path: when HyDE succeeds, expansion_used=True and expansion_warning=null."""
    import numpy as np

    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(results=[], acl_filtered=False)
    )
    fake_vector = np.ones(384, dtype=np.float32)
    with patch(
        "archon_search.server.routes_search.resolve_hyde_vector",
        new=AsyncMock(return_value=(fake_vector, True)),
    ):
        response = client.post("/search", json={"collections": ["a", "b"], "query": "q", "hyde": True})

    assert response.status_code == 200
    data = response.json()
    assert data["expansion_used"] is True
    assert data["expansion_warning"] is None


def test_search_multi_collection_expansion_warning_on_hyde_failure(tmp_path: Path) -> None:
    """Multi-collection path: when HyDE was requested but fails, expansion_warning='HyDE expansion failed'."""
    app, client = _make_app(tmp_path)
    app.state.pipeline = _make_multi_pipeline_mock(
        search_many_return=SearchPipelineResult(results=[], acl_filtered=False)
    )
    with patch(
        "archon_search.server.routes_search.resolve_hyde_vector",
        new=AsyncMock(return_value=(None, False)),
    ):
        response = client.post("/search", json={"collections": ["a", "b"], "query": "q", "hyde": True})

    assert response.status_code == 200
    data = response.json()
    assert data["expansion_used"] is False
    assert data["expansion_warning"] == "HyDE expansion failed"

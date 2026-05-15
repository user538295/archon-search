"""Tests for POST /search endpoint (Task 2.1)."""
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
from archon_search.server.app import create_app


def _make_app(tmp_path: Path) -> tuple:
    """Create app and return (app, client) with embed_one mocked on app.state.embedder."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return app, client


def _make_search_result(n: int = 1) -> SearchResult:
    return SearchResult(
        doc_id="a" * 64,
        chunk_id="a" * 64 + f"-{n:06d}",
        text=f"result text {n}",
        score=0.9 - n * 0.1,
        source_path=f"/path/to/doc{n}.md",
    )


# ---------------------------------------------------------------------------
# 1. Valid request returns list of results
# ---------------------------------------------------------------------------


def test_search_returns_results(tmp_path: Path) -> None:
    results = [_make_search_result(1), _make_search_result(2)]
    app, client = _make_app(tmp_path)
    app.state.embedder.embed_one = AsyncMock(return_value=[0.1] * 128)

    store_mock = MagicMock()
    store_mock.connect = AsyncMock()
    store_mock.disconnect = AsyncMock()
    store_mock.hybrid_search = AsyncMock(return_value=results)
    reranker_mock = MagicMock()
    reranker_mock.rerank = AsyncMock(return_value=results)

    with (
        patch("archon_search.server.routes_search.SearchStore", return_value=store_mock),
        patch("archon_search.server.routes_search.Reranker", return_value=reranker_mock),
    ):
        response = client.post("/search", json={"collection": "my-col", "query": "test query"})

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["doc_id"] == results[0].doc_id
    assert data[0]["chunk_id"] == results[0].chunk_id
    assert data[0]["text"] == results[0].text
    assert data[0]["score"] == pytest.approx(results[0].score)
    assert data[0]["source_path"] == results[0].source_path


# ---------------------------------------------------------------------------
# 2. Collection not found → 200 + []
# ---------------------------------------------------------------------------


def test_search_collection_not_found_returns_empty(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    app.state.embedder.embed_one = AsyncMock(return_value=[0.1] * 128)

    store_mock = MagicMock()
    store_mock.connect = AsyncMock()
    store_mock.disconnect = AsyncMock()
    store_mock.hybrid_search = AsyncMock(side_effect=ValueError("Table not found"))

    with patch("archon_search.server.routes_search.SearchStore", return_value=store_mock):
        response = client.post("/search", json={"collection": "nonexistent", "query": "test"})

    assert response.status_code == 200
    assert response.json() == []


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
# 5. Exception in store → log WARNING + return []
# ---------------------------------------------------------------------------


def test_search_store_exception_returns_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    app, client = _make_app(tmp_path)
    app.state.embedder.embed_one = AsyncMock(return_value=[0.1] * 128)

    store_mock = MagicMock()
    store_mock.connect = AsyncMock()
    store_mock.disconnect = AsyncMock()
    store_mock.hybrid_search = AsyncMock(side_effect=RuntimeError("db failure"))

    with (
        patch("archon_search.server.routes_search.SearchStore", return_value=store_mock),
        caplog.at_level(logging.WARNING, logger="archon.search"),
    ):
        response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 200
    assert response.json() == []
    assert any("search failed" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# 6. top_k is forwarded correctly (hybrid_search gets top_k*3, reranker gets top_k)
# ---------------------------------------------------------------------------


def test_search_top_k_forwarded(tmp_path: Path) -> None:
    results = [_make_search_result(i) for i in range(1, 11)]
    app, client = _make_app(tmp_path)
    app.state.embedder.embed_one = AsyncMock(return_value=[0.1] * 128)

    store_mock = MagicMock()
    store_mock.connect = AsyncMock()
    store_mock.disconnect = AsyncMock()
    store_mock.hybrid_search = AsyncMock(return_value=results)
    captured: dict = {}

    async def fake_rerank(query: str, candidates: list, top_k: int) -> list:
        captured["top_k"] = top_k
        return candidates[:top_k]

    reranker_mock = MagicMock()
    reranker_mock.rerank = fake_rerank

    with (
        patch("archon_search.server.routes_search.SearchStore", return_value=store_mock),
        patch("archon_search.server.routes_search.Reranker", return_value=reranker_mock),
    ):
        client.post("/search", json={"collection": "col", "query": "q", "top_k": 3})

    assert captured["top_k"] == 3
    store_mock.hybrid_search.assert_called_once()
    call_kwargs = store_mock.hybrid_search.call_args
    assert call_kwargs.kwargs.get("top_k") == 3 * 3  # body.top_k=3, so hybrid_search gets 9


# ---------------------------------------------------------------------------
# 7. Embedder failure → 200 + []
# ---------------------------------------------------------------------------


def test_search_embedder_failure_returns_empty(tmp_path: Path) -> None:
    app, client = _make_app(tmp_path)
    app.state.embedder.embed_one = AsyncMock(side_effect=RuntimeError("model error"))

    with patch("archon_search.server.routes_search.SearchStore"):
        response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------------------
# 8. Reranker failure → disconnect() still called, 200 + []
# ---------------------------------------------------------------------------


def test_search_reranker_failure_disconnect_called(tmp_path: Path) -> None:
    results = [_make_search_result(1)]
    app, client = _make_app(tmp_path)
    app.state.embedder.embed_one = AsyncMock(return_value=[0.1] * 128)

    store_mock = MagicMock()
    store_mock.connect = AsyncMock()
    store_mock.disconnect = AsyncMock()
    store_mock.hybrid_search = AsyncMock(return_value=results)

    reranker_mock = MagicMock()
    reranker_mock.rerank = AsyncMock(side_effect=ValueError("score count mismatch"))

    with (
        patch("archon_search.server.routes_search.SearchStore", return_value=store_mock),
        patch("archon_search.server.routes_search.Reranker", return_value=reranker_mock),
    ):
        response = client.post("/search", json={"collection": "col", "query": "test"})

    assert response.status_code == 200
    assert response.json() == []
    store_mock.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# 8. Integration: ingest a doc, search, verify result appears
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
    results = response.json()
    assert len(results) >= 1
    assert results[0]["source_path"] == "/docs/hello.md"

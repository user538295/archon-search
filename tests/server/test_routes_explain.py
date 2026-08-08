"""Tests for POST /explain endpoint (Task 3.1)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.embedder import Embedder
from archon_search.jobs.store import JobStore
from archon_search.pipeline import CollectionNotFoundError, ExplainPipelineResult, ExplainStageError, SearchPipeline
from archon_search.reranker import Reranker
from archon_search.router import MultiCollectionRouter
from archon_search.server.app import create_app
from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search._types import ChunkRecord

pytestmark = pytest.mark.xdist_group("mcp")


# ---------------------------------------------------------------------------
# Mock backends (copied from tests/test_pipeline_explain.py)
# ---------------------------------------------------------------------------


class MockEmbedderBackend:
    """Returns dim=4 vectors for all texts."""

    model_name: str = "mock-embedder"
    is_warm: bool = False

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class MockRerankerBackend:
    """Returns 0.5 for all pairs (used in tie-break tests)."""

    is_warm: bool = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [0.5] * len(pairs)


class DistinctTextRerankerBackend:
    """Returns a distinct, text-deterministic score per candidate text."""

    is_warm: bool = False

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        return [
            int(hashlib.sha256(t.encode()).hexdigest(), 16) % 100000 / 100000
            for _, t in pairs
        ]


# ---------------------------------------------------------------------------
# Pipeline / store helpers (copied from tests/test_pipeline_explain.py)
# ---------------------------------------------------------------------------


def _chunk(doc_id: str, idx: int, text: str, *, acl: list[str] | None = None, dim: int = 4) -> ChunkRecord:
    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[float(idx + 1)] * dim,
        source_path=f"/tmp/{doc_id[:8]}.md",
        indexed_at=datetime.now(UTC).isoformat(),
        acl=acl,
    )


async def _ingest(store, col: str, records: list[ChunkRecord]) -> None:
    """Ensure collection exists and ingest pre-built chunks, then rebuild FTS."""
    await store.ensure_collection(col, 4)
    await store.ingest_chunks(col, records)
    await store.rebuild_fts_index(col)


def _make_doc_id(n: int) -> str:
    """Return a deterministic 64-hex doc_id from an integer."""
    return hashlib.sha256(f"doc-{n:04d}".encode()).hexdigest()


def _make_records(
    n: int,
    *,
    acl: list[str] | None = None,
    id_offset: int = 0,
) -> list[ChunkRecord]:
    """Create n ChunkRecords with distinct texts; shared terms so FTS matches."""
    doc_id = _make_doc_id(id_offset)
    return [
        _chunk(
            doc_id,
            i,
            f"common alpha beta token unique{i + id_offset}",
            acl=acl,
        )
        for i in range(n)
    ]


def _make_scored_candidate(idx: int = 0) -> ScoredSearchCandidate:
    """Make a minimal ScoredSearchCandidate for mocking pipeline.explain()."""
    doc_id = _make_doc_id(idx)
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=f"text {idx}",
        source_path=f"/tmp/doc{idx}.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=idx + 1,
            vector_score=0.5,
            vector_score_kind="distance",
            fts_rank=None,
            fts_score=None,
            fts_score_kind=None,
            rrf_score=0.9 - idx * 0.05,
            reranker_score=0.8 - idx * 0.05,
        ),
        collection="docs",
    )


# ---------------------------------------------------------------------------
# App helpers
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path) -> tuple:
    """Create app and return (app, client) with pipeline mock on app.state."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    return app, client


def _make_explain_result(n_top: int = 2, n_near: int = 1) -> ExplainPipelineResult:
    """Build a minimal ExplainPipelineResult for mocking."""
    top = [_make_scored_candidate(i) for i in range(n_top)]
    near = [_make_scored_candidate(n_top + i) for i in range(n_near)]
    return ExplainPipelineResult(top_results=top, near_misses=near, acl_filtered=False)


# ---------------------------------------------------------------------------
# MULTI-COLLECTION SCHEMA TESTS (B3 Task 6.1)
# ---------------------------------------------------------------------------


def test_explain_rerank_false_multi_collections_is_422_rest() -> None:
    """rerank=False with >1 collection → ValidationError at the request schema."""
    import pydantic

    from archon_search.server.routes_explain import ExplainRequest

    with pytest.raises(pydantic.ValidationError):
        ExplainRequest(query="q", collections=["a", "b"], rerank=False)


def test_explain_rerank_false_single_collection_is_valid() -> None:
    """rerank=False is allowed for a single pinned collection and a single-item list."""
    from archon_search.server.routes_explain import ExplainRequest

    req1 = ExplainRequest(query="q", collection="x", rerank=False)
    assert req1.collection == "x"
    req2 = ExplainRequest(query="q", collections=["x"], rerank=False)
    assert req2.collections == ["x"]


def test_explain_both_collection_and_collections_is_422() -> None:
    """Supplying both collection and collections → ValidationError."""
    import pydantic

    from archon_search.server.routes_explain import ExplainRequest

    with pytest.raises(pydantic.ValidationError):
        ExplainRequest(query="q", collection="x", collections=["y"])


def test_explain_neither_collection_nor_collections_is_valid() -> None:
    """Neither set stays valid (routing mode)."""
    from archon_search.server.routes_explain import ExplainRequest

    req = ExplainRequest(query="q")
    assert req.collection is None
    assert req.collections is None


def test_explain_collections_dedup_before_rerank_guard() -> None:
    """['a','a'] dedups to ['a'] (len 1), so rerank=False stays valid."""
    from archon_search.server.routes_explain import ExplainRequest

    req = ExplainRequest(query="q", collections=["a", "a"], rerank=False)
    assert req.collections == ["a"]


def test_explain_collections_blank_item_is_422() -> None:
    """A blank collection name in the list → ValidationError."""
    import pydantic

    from archon_search.server.routes_explain import ExplainRequest

    with pytest.raises(pydantic.ValidationError):
        ExplainRequest(query="q", collections=["a", "  "])


def test_explain_result_carries_collection() -> None:
    """ExplainResult.from_candidate populates collection from the candidate."""
    from archon_search.server.routes_explain import ExplainResult

    cand = _make_scored_candidate(0)  # collection="docs"
    out = ExplainResult.from_candidate(cand)
    assert out.collection == "docs"


def test_explain_near_miss_carries_collection() -> None:
    """ExplainNearMiss.from_candidate populates collection from the candidate."""
    from archon_search.server.routes_explain import ExplainNearMiss

    cand = _make_scored_candidate(1)  # collection="docs"
    out = ExplainNearMiss.from_candidate(cand)
    assert out.collection == "docs"


# ---------------------------------------------------------------------------
# UNIT TESTS
# ---------------------------------------------------------------------------


def test_post_explain_without_auth_returns_401(tmp_path: Path) -> None:
    """No auth header → 401."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    # No auth header
    client = TestClient(app)
    response = client.post("/explain", json={"query": "hello"})
    assert response.status_code == 401


def test_post_explain_empty_query_returns_422(tmp_path: Path) -> None:
    """Empty query → 422 (Pydantic validation)."""
    _, client = _make_app(tmp_path)
    response = client.post("/explain", json={"query": ""})
    assert response.status_code == 422


def test_post_explain_top_k_above_100_returns_422(tmp_path: Path) -> None:
    """top_k > 100 → 422."""
    _, client = _make_app(tmp_path)
    response = client.post("/explain", json={"query": "hello", "top_k": 101})
    assert response.status_code == 422


def test_post_explain_top_k_below_1_returns_422(tmp_path: Path) -> None:
    """top_k < 1 → 422."""
    _, client = _make_app(tmp_path)
    response = client.post("/explain", json={"query": "hello", "top_k": 0})
    assert response.status_code == 422


def test_post_explain_pinned_collection_not_found_returns_404(tmp_path: Path) -> None:
    """Pinned collection: get_collection_meta returns None → 404."""
    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(return_value=None)
    app.state.pipeline = pipeline
    response = client.post("/explain", json={"query": "hello", "collection": "missing"})
    assert response.status_code == 404
    assert response.json()["detail"] == "collection not found"


def test_post_explain_fanout_all_missing_returns_404(tmp_path: Path) -> None:
    """Fan-out: every requested collection absent → pipeline raises CollectionNotFoundError → 404."""
    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.explain = AsyncMock(
        side_effect=CollectionNotFoundError(["ghost-a", "ghost-b"])
    )
    app.state.pipeline = pipeline
    response = client.post(
        "/explain",
        json={"query": "hello", "collections": ["ghost-a", "ghost-b"]},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "collection not found"


def test_post_explain_pinned_meta_lookup_failure_returns_503(tmp_path: Path) -> None:
    """Pinned collection: get_collection_meta raises → 503."""
    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(side_effect=RuntimeError("db boom"))
    app.state.pipeline = pipeline
    response = client.post("/explain", json={"query": "hello", "collection": "col"})
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "metadata_store_error"
    assert "metadata store" in body["detail"]


def test_post_explain_collectionless_no_collections_returns_404(tmp_path: Path) -> None:
    """Collectionless: get_all_collections_meta returns [] → 404."""
    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(return_value=[])
    app.state.pipeline = pipeline
    response = client.post("/explain", json={"query": "hello"})
    assert response.status_code == 404
    assert response.json()["detail"] == "no collections available"
    assert "code" not in response.json()


def test_post_explain_collectionless_meta_lookup_failure_returns_503(tmp_path: Path) -> None:
    """Collectionless: get_all_collections_meta raises → 503 with code=metadata_store_error."""
    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(side_effect=RuntimeError("db boom"))
    app.state.pipeline = pipeline
    response = client.post("/explain", json={"query": "hello"})
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "metadata_store_error"
    assert "metadata store" in body["detail"]


def test_post_explain_collectionless_router_failure_returns_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Collectionless: router raises → 503."""
    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.get_all_collections_meta = AsyncMock(
        return_value=[CollectionMeta(name="col", namespace="default")]
    )
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])
    app.state.pipeline = pipeline

    monkeypatch.setattr(
        MultiCollectionRouter,
        "rank_with_scores",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    response = client.post("/explain", json={"query": "hello"})
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "service_unavailable"
    assert "routing" in body["detail"]


def test_post_explain_store_failure_returns_500(tmp_path: Path) -> None:
    """pipeline.explain raises ExplainStageError(stage='store') → 500 with stage detail."""
    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    exc = ExplainStageError("store", RuntimeError("lancedb exploded"))
    pipeline.explain = AsyncMock(side_effect=exc)
    app.state.pipeline = pipeline

    response = client.post("/explain", json={"query": "hello", "collection": "col"})
    assert response.status_code == 500
    detail = response.json()["detail"]
    # Detail is sanitized to stage + exception type — the original message is NOT echoed.
    assert detail == "store error: RuntimeError"
    assert "lancedb exploded" not in detail


def test_post_explain_reranker_failure_returns_500(tmp_path: Path) -> None:
    """pipeline.explain raises ExplainStageError(stage='reranker') → 500, detail startswith 'reranker error:'."""
    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    exc = ExplainStageError("reranker", ValueError("score count mismatch"))
    pipeline.explain = AsyncMock(side_effect=exc)
    app.state.pipeline = pipeline

    response = client.post("/explain", json={"query": "hello", "collection": "col"})
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail.startswith("reranker error:"), f"Expected 'reranker error:' prefix, got: {detail!r}"
    # Original message is sanitized out of the response (only the exception type leaks).
    assert detail == "reranker error: ValueError"
    assert "score count mismatch" not in detail


def test_post_explain_telemetry_writer_failure_does_not_abort_response(tmp_path: Path) -> None:
    """Telemetry writer enqueue raising must NOT cause a non-200 response."""
    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    app.state.pipeline = pipeline

    # Install a writer that always raises on enqueue
    bad_writer = MagicMock()
    bad_writer.enqueue = MagicMock(side_effect=RuntimeError("writer down"))
    app.state.telemetry_writer = bad_writer

    response = client.post("/explain", json={"query": "hello", "collection": "col"})
    assert response.status_code == 200
    data = response.json()
    assert "results" in data


@pytest.mark.asyncio
async def test_post_explain_concurrent_collectionless_requests(tmp_path: Path) -> None:
    """Three concurrent collectionless requests all return 200 with the same shape."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    pipeline = MagicMock()
    meta_list = [CollectionMeta(name="col", centroid=[0.1, 0.2, 0.3, 0.4], active_embedding_model=config.embedding_model, namespace="default")]
    pipeline.get_all_collections_meta = AsyncMock(return_value=meta_list)
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    app.state.pipeline = pipeline

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resps = await asyncio.gather(
            ac.post("/explain", json={"query": "alpha"}),
            ac.post("/explain", json={"query": "beta"}),
            ac.post("/explain", json={"query": "gamma"}),
        )

    for resp in resps:
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        # Each collectionless response must carry a routing block and the mock's shape
        # (2 results) intact — detects cross-request state bleed or content corruption.
        assert data["routing"] is not None
        assert data["routing"]["invoked"] is True
        assert len(data["results"]) == 2
    keys_list = [set(resp.json().keys()) for resp in resps]
    assert keys_list[0] == keys_list[1] == keys_list[2]


# ---------------------------------------------------------------------------
# INTEGRATION TESTS — real offline pipeline + httpx.AsyncClient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_explain_pinned_collection_happy_path(tmp_path: Path) -> None:
    """Pinned collection → routing is None, len(results) <= top_k, 200."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "docs", _make_records(8))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )
    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    top_k = 3
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post("/explain", json={"query": "common alpha beta", "collection": "docs", "top_k": top_k})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["routing"] is None
    assert len(data["results"]) <= top_k
    assert len(data["results"]) > 0
    assert data["collection"] == "docs"
    # rerank defaults to True → every result carries a populated reranker_score.
    assert data["rerank"] is True
    for r in data["results"]:
        assert r["breakdown"]["reranker_score"] is not None
    await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_collectionless_includes_routing_block(tmp_path: Path) -> None:
    """Collectionless → routing block present, candidates non-empty, sorted score desc + alpha tie-break."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    # Set threshold low so routing proceeds
    config.routing_confidence_threshold = 0.0
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()

    # docs collection: centroid very close to query vector [0.1,0.2,0.3,0.4] → high cosine
    await _ingest(store, "docs", _make_records(5))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )
    # code collection: centroid far from query → lower cosine
    await _ingest(store, "code", _make_records(5, id_offset=10))
    await store.update_collection_meta(
        CollectionMeta(
            name="code",
            centroid=[0.4, 0.3, 0.2, 0.1],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post("/explain", json={"query": "common alpha beta", "top_k": 3})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["routing"] is not None
    candidates = data["routing"]["candidates"]
    assert len(candidates) >= 2

    # Candidates must be sorted by centroid_score desc, then name asc (alpha tie-break)
    scores = [c["centroid_score"] for c in candidates if c["centroid_score"] is not None]
    for i in range(len(scores) - 1):
        if scores[i] == scores[i + 1]:
            # tie-break by name ascending
            assert candidates[i]["collection"] <= candidates[i + 1]["collection"]
        else:
            assert scores[i] >= scores[i + 1]

    await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_routing_covers_every_collection_no_gating(tmp_path: Path) -> None:
    """rank_with_scores returns all collections; with high threshold chosen_below_threshold=True."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    # Set threshold very high so the chosen collection is below it
    # Set threshold > 1.0 so any cosine score (max 1.0) is always below threshold
    config.routing_confidence_threshold = 1.1
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()

    # Distinct centroids so the collections score differently — proves a genuinely
    # low-scoring collection is retained (not just identical-score ones).
    centroids = {
        "alpha": [0.1, 0.2, 0.3, 0.4],  # ~cosine 1.0 with query [0.1,0.2,0.3,0.4]
        "beta": [0.4, 0.3, 0.2, 0.1],   # lower cosine
        "gamma": [1.0, 0.0, 0.0, 0.0],  # low cosine
    }
    col_names = list(centroids)
    for i, col in enumerate(col_names):
        await _ingest(store, col, _make_records(3, id_offset=i * 10))
        await store.update_collection_meta(
            CollectionMeta(
                name=col,
                centroid=centroids[col],
                active_embedding_model=config.embedding_model,
                namespace="default",
            )
        )

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post("/explain", json={"query": "common alpha beta", "top_k": 2})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    routing = data["routing"]
    assert routing is not None
    # candidates set must cover all default-ns collections (rank_with_scores bypasses gate)
    candidate_names = {c["collection"] for c in routing["candidates"]}
    assert candidate_names == set(col_names)
    # cosine of identical vectors is ~1.0 < 1.1 threshold → chosen_below_threshold is True
    assert routing["chosen_below_threshold"] is True

    await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_routing_candidates_acl_filtered(tmp_path: Path) -> None:
    """1 default-ns + 2 other-ns collections; only default-ns one appears in candidates."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    config.routing_confidence_threshold = 0.0
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()

    # 1 default namespace collection
    await _ingest(store, "public-col", _make_records(5))
    await store.update_collection_meta(
        CollectionMeta(
            name="public-col",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )

    # 2 other-namespace collections (should NOT appear)
    for col_name in ["secret-col-a", "secret-col-b"]:
        await _ingest(store, col_name, _make_records(3, id_offset=50))
        await store.update_collection_meta(
            CollectionMeta(
                name=col_name,
                centroid=[0.1, 0.2, 0.3, 0.4],
                active_embedding_model=config.embedding_model,
                namespace="other-ns",
            )
        )

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post("/explain", json={"query": "common alpha beta", "top_k": 2})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    routing = data["routing"]
    assert routing is not None
    candidate_names = {c["collection"] for c in routing["candidates"]}
    assert "public-col" in candidate_names
    # Other-ns collections must NOT appear
    assert "secret-col-a" not in candidate_names
    assert "secret-col-b" not in candidate_names
    # Must not appear anywhere in the serialized response
    resp_text = resp.text
    assert "secret-col-a" not in resp_text
    assert "secret-col-b" not in resp_text

    await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_collectionless_all_collections_acl_filtered_returns_404(tmp_path: Path) -> None:
    """All collections in other-ns; default-ns request gets 404 'no collections available'."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()

    # All collections in other-ns
    for col_name in ["priv-col-x", "priv-col-y"]:
        await _ingest(store, col_name, _make_records(3))
        await store.update_collection_meta(
            CollectionMeta(
                name=col_name,
                centroid=[0.1, 0.2, 0.3, 0.4],
                active_embedding_model=config.embedding_model,
                namespace="other-ns",
            )
        )

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post("/explain", json={"query": "common alpha beta"})

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "no collections available"

    await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_search_top_k_equality_at_top_k_return(tmp_path: Path) -> None:
    """Corpus <= top_k_retrieve; explain results[:top_k] order == pipeline.search results."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    top_k_retrieve = 10
    top_k_return = 5

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "docs", _make_records(8))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=top_k_retrieve,
        top_k_return=top_k_return,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    query = "common alpha beta"
    # Call pipeline.search directly for baseline
    search_result = await pipeline.search(query, "docs", embedder=pipeline._global_embedder)
    search_ids = [(r.doc_id, r.chunk_id) for r in search_result.results]
    # Guard against a vacuous [] == [] pass: search must return a full top_k_return slice.
    assert len(search_ids) == top_k_return

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post(
            "/explain",
            json={"query": query, "collection": "docs", "top_k": top_k_return},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    explain_ids = [(r["doc_id"], r["chunk_id"]) for r in data["results"]]
    assert explain_ids == search_ids, (
        f"search returned {search_ids} but explain returned {explain_ids}"
    )

    await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_near_miss_no_text_field(tmp_path: Path) -> None:
    """Near-miss entries must NOT contain a 'text' field."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "docs", _make_records(15))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post("/explain", json={"query": "common alpha beta", "collection": "docs", "top_k": 3})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    near_misses = data["near_misses"]
    assert len(near_misses) >= 1, "Expected at least one near miss with 15 docs + top_k=3"
    for nm in near_misses:
        assert "text" not in nm, f"near_miss must not have 'text', got keys: {list(nm.keys())}"

    await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_rerank_false_orders_by_rrf(tmp_path: Path) -> None:
    """rerank=False → every result.breakdown.reranker_score is None; sorted by rrf_score desc."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "docs", _make_records(10))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post(
            "/explain",
            json={"query": "common alpha beta", "collection": "docs", "top_k": 5, "rerank": False},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["rerank"] is False

    all_items = data["results"] + data["near_misses"]
    for item in all_items:
        assert item["breakdown"]["reranker_score"] is None, (
            f"Expected reranker_score=None when rerank=False, got {item['breakdown']['reranker_score']}"
        )

    # results must be sorted by rrf_score desc
    rrf_scores = [item["breakdown"]["rrf_score"] for item in data["results"]]
    assert rrf_scores == sorted(rrf_scores, reverse=True), (
        f"results not sorted by rrf_score desc: {rrf_scores}"
    )

    await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_rerank_false_collectionless(tmp_path: Path) -> None:
    """rerank=False collectionless → routing populated AND reranker_score None for all items."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    config.routing_confidence_threshold = 0.0
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "docs", _make_records(8))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post(
            "/explain",
            json={"query": "common alpha beta", "top_k": 3, "rerank": False},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    # routing must be populated (collectionless)
    assert data["routing"] is not None
    assert data["routing"]["invoked"] is True
    # All items must have reranker_score=None
    for item in data["results"] + data["near_misses"]:
        assert item["breakdown"]["reranker_score"] is None

    await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_near_miss_pool_sizes(tmp_path: Path) -> None:
    """3 corpus sizes → near_miss count bounded by top_k_retrieve."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    job_store = JobStore(path=tmp_path / "jobs.json")

    top_k = 5
    top_k_retrieve = 10

    # (corpus_size, expected_near_miss_count) with top_k=5, top_k_retrieve=10
    # pool = min(corpus_size, top_k_retrieve); near_misses = max(0, pool - top_k)
    test_cases = [
        (3, max(0, min(3, top_k_retrieve) - top_k)),    # 3 < top_k → 0
        (8, max(0, min(8, top_k_retrieve) - top_k)),    # 8 - 5 = 3
        (30, max(0, min(30, top_k_retrieve) - top_k)),  # 10 - 5 = 5 (bounded by top_k_retrieve)
    ]

    for corpus_size, expected_near in test_cases:
        app = create_app(config, job_store)
        key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

        store = SearchStore(tmp_path / f"realdb_{corpus_size}")
        await store.connect()
        await _ingest(store, "docs", _make_records(corpus_size))
        await store.update_collection_meta(
            CollectionMeta(
                name="docs",
                centroid=[0.1, 0.2, 0.3, 0.4],
                active_embedding_model=config.embedding_model,
                namespace="default",
            )
        )

        pipeline = SearchPipeline(
            store=store,
            embedder=Embedder(MockEmbedderBackend()),
            reranker=Reranker(DistinctTextRerankerBackend()),
            chunker=MagicMock(),
            parser=MagicMock(),
            top_k_retrieve=10,
            top_k_return=5,
        )
        app.state.pipeline = pipeline
        app.state.embedder = pipeline._global_embedder

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://t",
            headers={"Authorization": f"Bearer {key}"},
        ) as ac:
            resp = await ac.post(
                "/explain",
                json={"query": "common alpha beta", "collection": "docs", "top_k": top_k},
            )

        assert resp.status_code == 200, f"corpus_size={corpus_size}: {resp.text}"
        data = resp.json()
        actual_near = len(data["near_misses"])
        assert actual_near == expected_near, (
            f"corpus_size={corpus_size}: expected near_misses={expected_near}, got {actual_near}"
        )
        await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_near_miss_at_exact_boundary(tmp_path: Path) -> None:
    """Large corpora → near_misses bounded by top_k_retrieve - top_k."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    job_store = JobStore(path=tmp_path / "jobs.json")

    top_k = 5
    top_k_retrieve = 10

    for corpus_size, col_suffix in [(top_k + 20, "a"), (top_k + 21, "b")]:
        app = create_app(config, job_store)
        key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

        store = SearchStore(tmp_path / f"bdry_{col_suffix}")
        await store.connect()
        await _ingest(store, "docs", _make_records(corpus_size))
        await store.update_collection_meta(
            CollectionMeta(
                name="docs",
                centroid=[0.1, 0.2, 0.3, 0.4],
                active_embedding_model=config.embedding_model,
                namespace="default",
            )
        )

        pipeline = SearchPipeline(
            store=store,
            embedder=Embedder(MockEmbedderBackend()),
            reranker=Reranker(DistinctTextRerankerBackend()),
            chunker=MagicMock(),
            parser=MagicMock(),
            top_k_retrieve=10,
            top_k_return=5,
        )
        app.state.pipeline = pipeline
        app.state.embedder = pipeline._global_embedder

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://t",
            headers={"Authorization": f"Bearer {key}"},
        ) as ac:
            resp = await ac.post(
                "/explain",
                json={"query": "common alpha beta", "collection": "docs", "top_k": top_k},
            )

        assert resp.status_code == 200, f"corpus={corpus_size}: {resp.text}"
        actual_near = len(resp.json()["near_misses"])
        expected_near = top_k_retrieve - top_k
        assert actual_near == expected_near, (
            f"corpus_size={corpus_size}: expected {expected_near} near_misses "
            f"(top_k_retrieve - top_k), got {actual_near}"
        )
        await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_acl_filtered_returns_empty_and_flag(tmp_path: Path) -> None:
    """All chunks have acl=['other-ns']; default-ns request → acl_filtered=True, empty results."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "docs", _make_records(5, acl=["other-ns"]))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post("/explain", json={"query": "common alpha beta", "collection": "docs", "top_k": 5})
        # AC11: /search must report the same acl_filtered + empty-results shape.
        search_resp = await ac.post("/search", json={"collection": "docs", "query": "common alpha beta"})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["acl_filtered"] is True
    assert data["results"] == []
    assert data["near_misses"] == []

    assert search_resp.status_code == 200, search_resp.text
    search_data = search_resp.json()
    assert search_data["acl_filtered"] is True
    assert search_data["results"] == []

    await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_empty_collection_returns_empty_results(tmp_path: Path) -> None:
    """Pinned collection with zero chunks → 200 empty results, acl_filtered False."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    # Create empty collection (no chunks)
    await store.ensure_collection("empty-col", 4)
    await store.update_collection_meta(
        CollectionMeta(
            name="empty-col",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post(
            "/explain",
            json={"query": "common alpha beta", "collection": "empty-col", "top_k": 5},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["results"] == []
    assert data["acl_filtered"] is False

    await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_pinned_collection_wrong_namespace_returns_404(tmp_path: Path) -> None:
    """Pinned collection in other-ns; default-ns request → 404 'collection not found'."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "priv-col", _make_records(5))
    await store.update_collection_meta(
        CollectionMeta(
            name="priv-col",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="other-ns",
        )
    )

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post(
            "/explain",
            json={"query": "common alpha beta", "collection": "priv-col"},
        )

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "collection not found"

    await store.disconnect()


@pytest.mark.asyncio
async def test_post_explain_telemetry_emits_no_query(tmp_path: Path) -> None:
    """Telemetry entry for /explain must not contain 'query' key or the raw query string."""
    from archon_search.store import SearchStore
    from archon_search.telemetry.writer import TelemetryWriter
    from archon_search.telemetry.reader import TelemetryReader

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "docs", _make_records(5))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    logs_dir = tmp_path / "telemetry-logs"
    logs_dir.mkdir()
    writer = TelemetryWriter(logs_dir)
    await writer.start()
    app.state.telemetry_writer = writer

    unique_query = "telemetry-no-query-test-xyzzy42"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post(
            "/explain",
            json={"query": unique_query, "collection": "docs", "top_k": 3},
        )

    assert resp.status_code == 200, resp.text
    result_count = len(resp.json()["results"])

    await writer.drain_and_stop()

    reader = TelemetryReader(logs_dir, retention_days=30)
    today = datetime.now(UTC).date()
    entries, skipped = reader.read_entries(today, today)
    assert skipped == 0

    explain_entries = [e for e in entries if e.endpoint == "explain"]
    assert len(explain_entries) >= 1

    # Read raw JSONL to check for query string leakage
    for jsonl_file in logs_dir.glob("*.jsonl"):
        raw_text = jsonl_file.read_text(encoding="utf-8")
        assert unique_query not in raw_text, (
            f"Raw query string found in telemetry log: {jsonl_file}"
        )
        for line in raw_text.splitlines():
            if not line.strip():
                continue
            parsed = json.loads(line)
            assert "query" not in parsed, (
                f"'query' key found in telemetry entry: {parsed}"
            )

    # Verify entry fields
    entry = explain_entries[0]
    assert entry.endpoint == "explain"
    assert entry.result_count == result_count
    assert entry.collection == "docs"

    await store.disconnect()


def test_explain_telemetry_has_correlation_id(tmp_path: Path) -> None:
    """POST /explain telemetry entry carries the correlation_id set by RequestContextMiddleware."""
    from unittest.mock import MagicMock
    from archon_search.telemetry.writer import TelemetryWriter

    app, _ = _make_app(tmp_path)
    cid = "explain-corr-id-test"

    writer = MagicMock(spec=TelemetryWriter)

    pipeline = MagicMock()
    pipeline.explain = AsyncMock(return_value=_make_explain_result())
    pipeline.get_collection_meta = AsyncMock(return_value=CollectionMeta(
        name="docs", centroid=[0.1, 0.2, 0.3, 0.4], active_embedding_model="mock-embedder", namespace="default"
    ))
    pipeline._global_embedder = MagicMock()
    pipeline._global_embedder.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])
    pipeline._global_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    with TestClient(app, headers={"Authorization": f"Bearer {os.environ.get('ARCHON_SEARCH_API_KEY', '')}"}) as c:
        # Set writer AFTER lifespan startup (lifespan sets it to None when telemetry disabled)
        app.state.telemetry_writer = writer
        resp = c.post(
            "/explain",
            json={"query": "hello", "collection": "docs"},
            headers={"X-Request-ID": cid},
        )

    assert resp.status_code == 200
    writer.enqueue.assert_called_once()
    from archon_search.telemetry.entry import TelemetryEntry
    entry: TelemetryEntry = writer.enqueue.call_args[0][0]
    assert entry.correlation_id == cid


def test_post_explain_openapi_includes_route(tmp_path: Path) -> None:
    """GET /openapi.json must show /explain with 'post'; security includes HTTPBearer."""
    _, client = _make_app(tmp_path)
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    paths = schema.get("paths", {})
    assert "/explain" in paths, f"/explain not in OpenAPI paths: {list(paths.keys())}"
    explain_path = paths["/explain"]
    assert "post" in explain_path, f"'post' not in /explain path item: {list(explain_path.keys())}"
    # Security scheme must include BearerAuth
    post_op = explain_path["post"]
    security = post_op.get("security", [])
    bearer_in_security = any("BearerAuth" in sec for sec in security)
    assert bearer_in_security, f"BearerAuth not in /explain post security: {security}"


# ---------------------------------------------------------------------------
# Task 4.3 — stage_timings_ms on ExplainResponse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explain_stage_timings_keys_pinned_collection_with_rerank(tmp_path: Path) -> None:
    """Pinned collection + rerank=True → stage_timings_ms contains expected keys including 'total'."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    config.observability.stage_timings_enabled = True
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "docs", _make_records(8))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )
    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post(
            "/explain",
            json={"query": "common alpha beta", "collection": "docs", "top_k": 3, "rerank": True},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "stage_timings_ms" in data, "stage_timings_ms key must be present when enabled"
    timings = data["stage_timings_ms"]
    assert isinstance(timings, dict)
    # Must contain at minimum embed, vector, fuse, rerank, total
    # fts may or may not be present depending on FTS index
    assert "embed" in timings, f"'embed' missing from stage_timings_ms: {set(timings)}"
    assert "vector" in timings, f"'vector' missing from stage_timings_ms: {set(timings)}"
    assert "fuse" in timings, f"'fuse' missing from stage_timings_ms: {set(timings)}"
    assert "rerank" in timings, f"'rerank' missing from stage_timings_ms: {set(timings)}"
    assert "total" in timings, f"'total' missing from stage_timings_ms: {set(timings)}"

    await store.disconnect()


@pytest.mark.asyncio
async def test_explain_stage_timings_keys_collectionless(tmp_path: Path) -> None:
    """Collectionless request → stage_timings_ms contains 'route' key."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    config.routing_confidence_threshold = 0.0
    config.observability.stage_timings_enabled = True
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "docs", _make_records(5))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )
    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post("/explain", json={"query": "common alpha beta", "top_k": 3})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "stage_timings_ms" in data, "stage_timings_ms key must be present when enabled"
    timings = data["stage_timings_ms"]
    assert "route" in timings, f"'route' key must be present for collectionless explain: {set(timings)}"
    assert "total" in timings

    await store.disconnect()


@pytest.mark.asyncio
async def test_explain_stage_timings_no_rerank(tmp_path: Path) -> None:
    """rerank=False → 'rerank' stage absent from stage_timings_ms."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    config.observability.stage_timings_enabled = True
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "docs", _make_records(8))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )
    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post(
            "/explain",
            json={"query": "common alpha beta", "collection": "docs", "top_k": 3, "rerank": False},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "stage_timings_ms" in data
    timings = data["stage_timings_ms"]
    assert "rerank" not in timings, f"'rerank' must be absent when rerank=False, got: {set(timings)}"
    assert "total" in timings

    await store.disconnect()


@pytest.mark.asyncio
async def test_explain_stage_timings_values_non_negative(tmp_path: Path) -> None:
    """All float values in stage_timings_ms must be >= 0."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    config.observability.stage_timings_enabled = True
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "docs", _make_records(8))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )
    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post(
            "/explain",
            json={"query": "common alpha beta", "collection": "docs", "top_k": 3},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "stage_timings_ms" in data
    timings = data["stage_timings_ms"]
    for stage, value in timings.items():
        assert isinstance(value, float | int), f"stage {stage!r}: expected float, got {type(value)}"
        assert value >= 0.0, f"stage {stage!r}: timing must be >= 0, got {value}"

    await store.disconnect()


@pytest.mark.asyncio
async def test_explain_stage_timings_disabled(tmp_path: Path) -> None:
    """stage_timings_enabled=False → 'stage_timings_ms' key absent from response JSON entirely."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    config.observability.stage_timings_enabled = False
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "docs", _make_records(8))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )
    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post(
            "/explain",
            json={"query": "common alpha beta", "collection": "docs", "top_k": 3},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "stage_timings_ms" not in data, (
        "stage_timings_ms must be absent from response when stage_timings_enabled=False"
    )

    await store.disconnect()


@pytest.mark.asyncio
async def test_mcp_explain_emits_stage_timings_log_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """MCP explain with valid request emits a log record with event_type=='stage_timings'
    containing 'total' in stage_timings_ms."""
    import sys
    import types
    from unittest.mock import patch

    # Stub fastmcp if not present
    if "fastmcp" not in sys.modules:
        _fastmcp = types.ModuleType("fastmcp")
        _fastmcp.FastMCP = type("FastMCP", (), {})
        _fastmcp.Context = type("Context", (), {})
        sys.modules["fastmcp"] = _fastmcp

    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    config.observability.stage_timings_enabled = True

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await store.ensure_collection("docs", 4)
    await store.ingest_chunks("docs", _make_records(8))
    await store.rebuild_fts_index("docs")
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )
    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )

    class _FakeApp:
        def __init__(self, name: str) -> None:
            self.tools: dict = {}

        def tool(self):
            def decorator(func):
                self.tools[func.__name__] = func
                return func
            return decorator

        def custom_route(self, path, methods=None):
            def decorator(func):
                return func
            return decorator

    class _FakeFastMCP:
        def __new__(cls, name, **kwargs):
            return _FakeApp(name)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        mcp_app = mcp_module.create_app(pipeline, "default", writer=None, config=config)

    import logging
    with caplog.at_level(logging.INFO, logger="archon_search"):
        result = await mcp_app.tools["explain"](query="common alpha beta", collection="docs", top_k=3)

    assert "error" not in result, f"MCP explain returned error: {result}"
    assert "results" in result

    # Find a log record with event_type == "stage_timings"
    timing_records = [
        r for r in caplog.records
        if getattr(r, "event_type", None) == "stage_timings"
    ]
    assert len(timing_records) >= 1, (
        f"Expected at least 1 stage_timings log record, got {len(timing_records)}. "
        f"All records: {[(r.getMessage(), r.__dict__.get('event_type')) for r in caplog.records]}"
    )
    rec = timing_records[0]
    timings = getattr(rec, "stage_timings_ms", None)
    assert timings is not None, "stage_timings_ms must be present on the log record"
    assert "total" in timings, f"'total' must be in stage_timings_ms, got: {set(timings)}"

    await store.disconnect()


@pytest.mark.asyncio
async def test_explain_stage_timings_fts_absent_degradation(tmp_path: Path) -> None:
    """Corpus without FTS index → 'fts' absent, 'vector' and 'fuse' present in stage_timings_ms."""
    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    config.observability.stage_timings_enabled = True
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    # Ingest WITHOUT calling rebuild_fts_index to simulate corpus without FTS
    await store.ensure_collection("docs", 4)
    await store.ingest_chunks("docs", _make_records(8))
    # Deliberately skip: await store.rebuild_fts_index("docs")
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )
    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    app.state.pipeline = pipeline
    app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        resp = await ac.post(
            "/explain",
            json={"query": "common alpha beta", "collection": "docs", "top_k": 3},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "stage_timings_ms" in data
    timings = data["stage_timings_ms"]
    assert "fts" not in timings, f"'fts' must be absent when no FTS index, got: {set(timings)}"
    assert "vector" in timings, f"'vector' must be present, got: {set(timings)}"
    assert "fuse" in timings, f"'fuse' must be present, got: {set(timings)}"

    await store.disconnect()


@pytest.mark.asyncio
async def test_rest_mcp_explain_key_parity(tmp_path: Path) -> None:
    """For identical inputs, set(REST stage_timings_ms keys) == set(MCP stage_timings_ms keys)."""
    import sys
    import types
    from unittest.mock import patch

    if "fastmcp" not in sys.modules:
        _fastmcp = types.ModuleType("fastmcp")
        _fastmcp.FastMCP = type("FastMCP", (), {})
        _fastmcp.Context = type("Context", (), {})
        sys.modules["fastmcp"] = _fastmcp

    from archon_search.store import SearchStore

    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.embedding_model = "mock-embedder"
    config.observability.stage_timings_enabled = True
    job_store = JobStore(path=tmp_path / "jobs.json")
    rest_app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")

    store = SearchStore(tmp_path / "realdb")
    await store.connect()
    await _ingest(store, "docs", _make_records(8))
    await store.update_collection_meta(
        CollectionMeta(
            name="docs",
            centroid=[0.1, 0.2, 0.3, 0.4],
            active_embedding_model=config.embedding_model,
            namespace="default",
        )
    )
    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(MockEmbedderBackend()),
        reranker=Reranker(DistinctTextRerankerBackend()),
        chunker=MagicMock(),
        parser=MagicMock(),
        top_k_retrieve=10,
        top_k_return=5,
    )
    rest_app.state.pipeline = pipeline
    rest_app.state.embedder = pipeline._global_embedder

    transport = httpx.ASGITransport(app=rest_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://t",
        headers={"Authorization": f"Bearer {key}"},
    ) as ac:
        rest_resp = await ac.post(
            "/explain",
            json={"query": "common alpha beta", "collection": "docs", "top_k": 3, "rerank": True},
        )

    assert rest_resp.status_code == 200, rest_resp.text
    rest_data = rest_resp.json()
    assert "stage_timings_ms" in rest_data, "REST response must contain stage_timings_ms"

    class _FakeApp:
        def __init__(self, name: str) -> None:
            self.tools: dict = {}

        def tool(self):
            def decorator(func):
                self.tools[func.__name__] = func
                return func
            return decorator

        def custom_route(self, path, methods=None):
            def decorator(func):
                return func
            return decorator

    class _FakeFastMCP:
        def __new__(cls, name, **kwargs):
            return _FakeApp(name)

    with patch("archon_search.server.mcp.FastMCP", new=_FakeFastMCP):
        from archon_search.server import mcp as mcp_module
        mcp_app = mcp_module.create_app(pipeline, "default", writer=None, config=config)

    mcp_result = await mcp_app.tools["explain"](
        query="common alpha beta", collection="docs", top_k=3, rerank=True
    )
    assert "error" not in mcp_result, f"MCP explain returned error: {mcp_result}"
    assert "stage_timings_ms" in mcp_result, "MCP response must contain stage_timings_ms"

    rest_keys = set(rest_data["stage_timings_ms"].keys())
    mcp_keys = set(mcp_result["stage_timings_ms"].keys())
    assert rest_keys == mcp_keys, (
        f"REST and MCP stage_timings_ms key sets differ: REST={rest_keys}, MCP={mcp_keys}"
    )

    await store.disconnect()


# ---------------------------------------------------------------------------
# B3 Task 6.1 — multi-collection /explain (REST integration)
# ---------------------------------------------------------------------------


def _scored_with_collection(idx: int, collection: str) -> ScoredSearchCandidate:
    doc_id = _make_doc_id(idx)
    return ScoredSearchCandidate(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=f"text {idx}",
        source_path=f"/tmp/doc{idx}.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=idx + 1,
            vector_score=0.5,
            vector_score_kind="distance",
            fts_rank=None,
            fts_score=None,
            fts_score_kind=None,
            rrf_score=0.9 - idx * 0.05,
            reranker_score=0.8 - idx * 0.05,
        ),
        collection=collection,
    )


def test_post_explain_multi_collection_returns_200_with_provenance(tmp_path: Path) -> None:
    """POST /explain with collections returns 200; each result carries its source collection."""
    from archon_search._types import ExcludedCollection

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.explain = AsyncMock(
        return_value=ExplainPipelineResult(
            top_results=[_scored_with_collection(0, "A"), _scored_with_collection(1, "B")],
            near_misses=[],
            acl_filtered=False,
            excluded_collections=[ExcludedCollection(name="C", reason="embedding_model_mismatch")],
        )
    )
    app.state.pipeline = pipeline

    response = client.post("/explain", json={"query": "hello", "collections": ["A", "B"]})

    assert response.status_code == 200
    data = response.json()
    cols = {r["collection"] for r in data["results"]}
    assert cols == {"A", "B"}
    assert {"name": "C", "reason": "embedding_model_mismatch"} in data["excluded_collections"]
    # search_many's routing is bypassed: pipeline.explain called with collections kwarg.
    assert pipeline.explain.await_args.kwargs["collections"] == ["A", "B"]


def test_post_explain_rerank_false_multi_collections_http_422(tmp_path: Path) -> None:
    """rerank=false + multiple collections is rejected at the HTTP layer with 422."""
    _app, client = _make_app(tmp_path)
    response = client.post(
        "/explain", json={"query": "hello", "collections": ["A", "B"], "rerank": False}
    )
    assert response.status_code == 422


def test_post_explain_multi_collection_metadata_lookup_failure_returns_503(tmp_path: Path) -> None:
    """Multi-collection explain: MetadataLookupError → 503 with code=metadata_store_error."""
    from archon_search.pipeline import MetadataLookupError

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.explain = AsyncMock(side_effect=MetadataLookupError(RuntimeError("db boom")))
    app.state.pipeline = pipeline

    response = client.post("/explain", json={"query": "hello", "collections": ["A", "B"]})

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "metadata_store_error"
    assert "metadata store" in body["detail"]


def test_post_explain_multi_collection_fanout_timeout_returns_504(tmp_path: Path) -> None:
    """Multi-collection explain: an injected FanoutTimeoutError maps to 504.

    Handler-level coverage only — the exception is injected into a mock
    pipeline, so this passes regardless of whether the configured
    ``fanout_timeout_seconds`` actually reaches the pipeline.  The S435
    end-to-end reproduction (real pipeline, real TOML timeout) is
    ``tests/integration/test_e1b_be6_routes_search_integration.py::
    test_post_explain_fanout_honours_configured_timeout_504``.
    """
    from archon_search.pipeline import FanoutTimeoutError

    app, client = _make_app(tmp_path)
    pipeline = MagicMock()
    pipeline.explain = AsyncMock(side_effect=FanoutTimeoutError())
    app.state.pipeline = pipeline

    response = client.post("/explain", json={"query": "hello", "collections": ["A", "B"]})

    assert response.status_code == 504
    assert response.json()["detail"] == "Search timed out"


# ---------------------------------------------------------------------------
# C2 Task 2.3 — three-state language field in explain responses
# ---------------------------------------------------------------------------


def test_explain_result_language_field_three_state(tmp_path: Path) -> None:
    """ExplainResult.language carries 'fr' for tagged, '' for untagged (not None)."""
    from archon_search.server.routes_explain import ExplainResult, ExplainNearMiss

    # Tagged candidate → language is "fr"
    tagged_cand = ScoredSearchCandidate(
        doc_id=_make_doc_id(0),
        chunk_id=f"{_make_doc_id(0)}-000000",
        text="bonjour",
        source_path="/tmp/fr.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=1, vector_score=0.5, vector_score_kind="distance",
            fts_rank=None, fts_score=None, fts_score_kind=None,
            rrf_score=0.9, reranker_score=None,
        ),
        collection="docs",
        language="fr",
    )
    result = ExplainResult.from_candidate(tagged_cand)
    assert result.language == "fr"

    near_miss = ExplainNearMiss.from_candidate(tagged_cand)
    assert near_miss.language == "fr"

    # Untagged candidate → language is "" (not None)
    untagged_cand = ScoredSearchCandidate(
        doc_id=_make_doc_id(1),
        chunk_id=f"{_make_doc_id(1)}-000000",
        text="hello",
        source_path="/tmp/en.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=1, vector_score=0.5, vector_score_kind="distance",
            fts_rank=None, fts_score=None, fts_score_kind=None,
            rrf_score=0.8, reranker_score=None,
        ),
        collection="docs",
    )
    result2 = ExplainResult.from_candidate(untagged_cand)
    assert result2.language == "", f"Expected '' for untagged, got {result2.language!r}"

    near_miss2 = ExplainNearMiss.from_candidate(untagged_cand)
    assert near_miss2.language == "", f"Expected '' for untagged near miss, got {near_miss2.language!r}"


def test_explain_http_language_empty_not_null(tmp_path: Path) -> None:
    """POST /explain with a legacy untagged chunk must return language='' (not null) in JSON."""
    app, client = _make_app(tmp_path)

    # Build pipeline mock that returns an untagged candidate (language="")
    untagged_cand = ScoredSearchCandidate(
        doc_id=_make_doc_id(0),
        chunk_id=f"{_make_doc_id(0)}-000000",
        text="hello world",
        source_path="/tmp/legacy.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=1, vector_score=0.5, vector_score_kind="distance",
            fts_rank=None, fts_score=None, fts_score_kind=None,
            rrf_score=0.9, reranker_score=None,
        ),
        collection="col",
        language="",
    )
    near_miss_cand = ScoredSearchCandidate(
        doc_id=_make_doc_id(1),
        chunk_id=f"{_make_doc_id(1)}-000000",
        text="near miss text",
        source_path="/tmp/legacy2.md",
        score_breakdown=SearchScoreBreakdown(
            vector_rank=2, vector_score=0.6, vector_score_kind="distance",
            fts_rank=None, fts_score=None, fts_score_kind=None,
            rrf_score=0.7, reranker_score=None,
        ),
        collection="col",
        language="",
    )

    pipeline = MagicMock()
    pipeline.get_collection_meta = AsyncMock(
        return_value=CollectionMeta(name="col", namespace="default")
    )
    pipeline.explain = AsyncMock(
        return_value=ExplainPipelineResult(
            top_results=[untagged_cand],
            near_misses=[near_miss_cand],
            acl_filtered=False,
        )
    )
    app.state.pipeline = pipeline

    response = client.post("/explain", json={"query": "hello", "collection": "col"})

    assert response.status_code == 200
    data = response.json()
    # language must serialize as "" (empty string), not null
    assert data["results"][0]["language"] == "", (
        f"Expected '' for untagged result, got {data['results'][0]['language']!r}"
    )
    assert data["near_misses"][0]["language"] == "", (
        f"Expected '' for untagged near_miss, got {data['near_misses'][0]['language']!r}"
    )

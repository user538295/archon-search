"""Task 1.3 — Multi-collection search and routing HTTP integration.

Exercises the full HTTP layer for multi-collection search fan-out, missing
collection 404, explain rerank validation, and POST /route with hybrid
routing strategy.

Run with:
    uv run pytest tests/integration/test_http_multi_collection.py -v
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

import archon_search.server.routes_route as _routes_route
from archon_search.router import MultiCollectionRouter
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Test 1 — E2E multi-collection search returns results from both collections
# ---------------------------------------------------------------------------

def test_e2e_multi_collection_search_full_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest into two real collections; POST /search with collections=['col_a','col_b'].

    Asserts:
    - 200 response
    - Every result has a non-empty ``collection`` field
    - Results from BOTH collections are present (fan-out worked)
    """
    doc_a = tmp_path / "corpus_a.md"
    doc_a.write_text(
        "# Corpus Alpha\n\nThis document belongs to collection alpha.\n" * 6
    )
    doc_b = tmp_path / "corpus_b.md"
    doc_b.write_text(
        "# Corpus Beta\n\nThis document belongs to collection beta.\n" * 6
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col_a = "mc-alpha"
        col_b = "mc-beta"

        ingest_file_via_path(client, col_a, str(doc_a), api_key=api_key)
        ingest_file_via_path(client, col_b, str(doc_b), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [col_a, col_b],
                "query": "corpus document collection",
                "top_k": 10,
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        items = data["results"]
        assert items, "expected non-empty results from multi-collection search"

        # Every result must carry a non-empty collection field
        for item in items:
            assert item["collection"], (
                f"result missing non-empty collection field: {item}"
            )

        # Results from BOTH collections must appear in the response
        seen_collections = {item["collection"] for item in items}
        assert col_a in seen_collections, (
            f"collection '{col_a}' absent from results; seen: {seen_collections}"
        )
        assert col_b in seen_collections, (
            f"collection '{col_b}' absent from results; seen: {seen_collections}"
        )


# ---------------------------------------------------------------------------
# Test 2 — Missing collection in fan-out returns 404
# ---------------------------------------------------------------------------

def test_e2e_multi_collection_missing_collection_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search with one existing and one nonexistent collection returns 404."""
    doc = tmp_path / "existing.md"
    doc.write_text("# Existing document\n\nReal content here.\n" * 4)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        existing_col = "mc-existing"
        ingest_file_via_path(client, existing_col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [existing_col, "ghost-collection-does-not-exist"],
                "query": "document content",
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 404, (
            f"expected 404 for missing collection, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Test 3 — /explain with rerank=false and two collections returns 422
# ---------------------------------------------------------------------------

def test_explain_request_rerank_false_multi_collections_is_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain with collections=['a','b'] and rerank=false returns 422.

    The ExplainRequest model_validator enforces that rerank cannot be disabled
    for multi-collection explain in v1. This test exercises the validation at
    the HTTP layer.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        resp = client.post(
            "/explain",
            json={
                "query": "test query for explain",
                "collections": ["col-a", "col-b"],
                "rerank": False,
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 422, (
            f"expected 422 (rerank=false is forbidden for multi-collection explain), "
            f"got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Test 4 — POST /route with hybrid strategy returns 200 and routable collections
# ---------------------------------------------------------------------------

def test_post_route_hybrid_strategy_returns_200_and_uses_blended_ranking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config routing_strategy='hybrid'. Ingest two corpora. POST /route returns 200.

    Verifies the hybrid routing config is wired end-to-end through the HTTP layer:
    - The route handler reads routing_strategy from config and passes it to _build_router.
    - The returned RouteResponse has the correct shape.
    - Both ingested collections appear in routable_names (Tier 1: ≤3 routable, all returned).
    - The hybrid _score_collections path is exercised directly via router.rank() to confirm
      the blending code runs without error when centroid data is present.

    Note: Tier 3 blended ranking (Tier 3 requires n_routable > shortlist_size) is not
    triggered here with only 2 collections. The hybrid scoring path is verified by calling
    router.rank() directly with real metadata.

    MultiCollectionRouter.fetch_metadata() makes an HTTP call to the MCP endpoint which
    is unreachable in TestClient ASGI transport. We pre-seed the router with
    initial_metadata from the real store to avoid the network hop.
    """
    doc_a = tmp_path / "route_corpus_a.md"
    doc_a.write_text(
        "# Machine Learning Document\n\nThis document is about neural networks and deep learning.\n" * 6
    )
    doc_b = tmp_path / "route_corpus_b.md"
    doc_b.write_text(
        "# Database Engineering Document\n\nThis document is about SQL queries and indexing.\n" * 6
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        cfg.routing_strategy = "hybrid"

        col_a = "route-ml"
        col_b = "route-db"

        ingest_file_via_path(client, col_a, str(doc_a), api_key=api_key)
        ingest_file_via_path(client, col_b, str(doc_b), api_key=api_key)

        # Fetch real metadata from store so we can pre-seed the router with initial_metadata,
        # bypassing the MCP HTTP call that fetch_metadata() would otherwise make.
        store = client.app.state.search_store

        async def _get_meta():
            return await store.get_all_collections_meta()

        all_meta = asyncio.run(_get_meta())
        embedder = client.app.state.embedder

        def _patched_build_router(config, shortlist_size, embedder=None):
            """Build a hybrid router pre-seeded with real metadata to bypass HTTP fetch."""
            embedder_to_use = embedder or client.app.state.embedder
            return MultiCollectionRouter(
                search_url=f"http://{config.host}:{config.port}",
                embedder=embedder_to_use,
                shortlist_size=shortlist_size,
                confidence_threshold=config.routing_confidence_threshold,
                embedding_model=config.embedding_model,
                initial_metadata=all_meta,  # pre-seeded; no HTTP call
                strategy="hybrid",
                description_weight=config.routing_description_weight,
            )

        with patch.object(_routes_route, "_build_router", side_effect=_patched_build_router):
            resp = client.post(
                "/route",
                json={"query": "machine learning neural networks"},
                headers=_auth(api_key),
            )

        assert resp.status_code == 200, (
            f"expected 200 from /route with hybrid strategy, "
            f"got {resp.status_code}: {resp.text}"
        )
        data = resp.json()

        # Response must have the RouteResponse shape
        assert "routable_names" in data, f"routable_names missing from /route response: {data}"
        assert "pinned_names" in data, f"pinned_names missing from /route response: {data}"
        assert "decomposer_invoked" in data, f"decomposer_invoked missing: {data}"

        # Both collections were ingested; with ≤3 routable (Tier 1) all must appear
        routable = set(data["routable_names"])
        assert col_a in routable and col_b in routable, (
            f"expected both ingested collections in routable_names, got: {routable}"
        )

        # Directly verify the hybrid _score_collections path runs without error by
        # calling router.rank() with a real embedding vector and real metadata.
        # This exercises the blending branch even though Tier 1 bypasses it on /route.
        hybrid_router = MultiCollectionRouter(
            search_url=f"http://{cfg.host}:{cfg.port}",
            embedder=embedder,
            shortlist_size=cfg.routing_shortlist_size,
            confidence_threshold=0.0,  # no confidence gate — return all scored collections
            embedding_model=cfg.embedding_model,
            initial_metadata=all_meta,
            strategy="hybrid",
            description_weight=cfg.routing_description_weight,
        )
        async def _embed_and_rank():
            query_vector = await embedder.embed_one("machine learning neural networks")
            return hybrid_router.rank(query_vector, all_meta)

        ranked = asyncio.run(_embed_and_rank())
        # rank() returns a list of CollectionMeta (possibly empty if confidence gate trims all)
        # The call itself must not raise — that is the primary assertion
        assert isinstance(ranked, list), (
            f"hybrid router.rank() must return a list, got: {type(ranked)}"
        )

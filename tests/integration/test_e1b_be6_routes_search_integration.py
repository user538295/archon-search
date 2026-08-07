"""Integration tests for BE-6: routes_search.py graph_mode route extension.

Covers:
- POST /search graph_mode=global → 200 when communities exist (S2)
- POST /search graph_mode=global → 422 graph_communities_not_built when no communities (S11)
- POST /search graph_mode=local → 422 graph_communities_not_built when no communities built and entities match (S60)
- POST /search graph_mode=local → 200, graph_expansion_applied=false when no entity match (S10)
- POST /search graph_mode=global: ACL-restricted chunks filtered from response (S15)
- POST /search graph_mode=* → 422 when graph.enabled=False
- POST /search multi-collection fan-out → 504 when the TOML-configured
  [search] fanout_timeout_seconds is exceeded (S443)
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers: spaCy stub (needed for graph_enabled=True)
# ---------------------------------------------------------------------------


def _install_spacy_stub_no_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy that returns NO named entities for any text.

    Must be called BEFORE make_real_app (create_app imports spacy synchronously
    via _check_graph_deps).
    """

    class _FakeDoc:
        def __init__(self) -> None:
            self.ents: list = []

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            return _FakeDoc()

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]
    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]
    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Helper: write communities to the graph store from sync test
# ---------------------------------------------------------------------------


async def _write_communities_to_store(
    db_path: str,
    col: str,
    chunk_ids: list[str],
    *,
    community_id: str = "test-comm-1",
    entity_ids: list[str] | None = None,
) -> None:
    """Write a single community to the GraphStore at db_path.

    Creates a new connection so the call works from asyncio.run() in a sync test.
    The app's existing connection sees the data because they share the same LanceDB files.
    """
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import Community

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_communities_table(col, ns="default")
        community = Community(
            community_id=community_id,
            entity_ids=entity_ids or ["entity-1"],
            representative_chunk_ids=chunk_ids,
            built_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            summary_text=None,
        )
        await gs.write_communities(col, [community], ns="default")
    finally:
        await gs.disconnect()


# ---------------------------------------------------------------------------
# test_post_search_global_mode_200
# ---------------------------------------------------------------------------


def test_post_search_global_mode_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search graph_mode=global → 200 + results + graph_expansion_applied=true (S2).

    Requires: graph.enabled=True, collection exists, communities built with valid chunk IDs.
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "e1b-be6-global-200"
    doc = tmp_path / "test_doc.txt"
    doc.write_text(
        "SearchPipeline provides vector and FTS retrieval. "
        "Community detection clusters related entities.\n" * 5,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        # Ingest the document so the collection and chunks exist.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Get a chunk_id from the collection.
        resp = client.get(
            f"/collections/{col}/documents",
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"list documents failed: {resp.text}"
        # Use the collection's first doc_id to derive a chunk_id pattern.
        # Instead, fetch chunk IDs via the graph_store directly.
        db_path = cfg.db_path

        # Get chunk IDs from the store using list_chunks_raw via the app's pipeline store.
        async def _get_chunk_ids() -> list[str]:
            from archon_search.constants import DEFAULT_NAMESPACE
            from archon_search.store import SearchStore

            s = SearchStore(db_path)
            await s.connect()
            try:
                rows = [r async for r in s.list_chunks_raw(col, DEFAULT_NAMESPACE)]
                return [r["chunk_id"] for r in rows]
            finally:
                await s.disconnect()

        chunk_ids = asyncio.run(_get_chunk_ids())
        assert chunk_ids, "Ingest must have created at least one chunk"

        # Write a community pointing to the first chunk.
        asyncio.run(_write_communities_to_store(db_path, col, chunk_ids[:1]))

        # POST /search with graph_mode=global should return 200.
        resp = client.post(
            "/search",
            json={"collection": col, "query": "community retrieval", "graph_mode": "global"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"Expected 200 for graph_mode=global with communities; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "results" in body, f"Expected 'results' key: {list(body.keys())}"
        assert body.get("graph_expansion_applied") is True, (
            f"Expected graph_expansion_applied=True; got {body.get('graph_expansion_applied')!r}. "
            f"Full response: {body}"
        )


# ---------------------------------------------------------------------------
# test_post_search_global_no_communities_422
# ---------------------------------------------------------------------------


def test_post_search_global_no_communities_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search graph_mode=global → 422 graph_communities_not_built when no communities (S11).

    Communities are NOT built; the route must catch GraphCommunitiesNotBuiltError
    and return 422 with {"detail": {"code": "graph_communities_not_built"}}.
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "e1b-be6-global-no-communities"
    doc = tmp_path / "test_doc.txt"
    doc.write_text("Some content for testing.\n" * 5, encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Do NOT build communities.
        resp = client.post(
            "/search",
            json={"collection": col, "query": "test query", "graph_mode": "global"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422, (
            f"Expected 422 when no communities built; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "detail" in body, f"Expected 'detail' key in 422 body: {body!r}"
        detail = body["detail"]
        # detail must be an object with code="graph_communities_not_built"
        assert isinstance(detail, dict), (
            f"Expected detail to be a dict; got {type(detail).__name__!r}: {detail!r}"
        )
        assert detail.get("code") == "graph_communities_not_built", (
            f"Expected code='graph_communities_not_built'; got: {detail!r}"
        )
        assert "message" in detail, (
            f"Expected 'message' key in error detail per TypeSpec contract; got: {detail!r}"
        )
        assert detail["message"], f"'message' must be a non-empty string; got: {detail!r}"


# ---------------------------------------------------------------------------
# test_post_search_local_no_communities_422
# ---------------------------------------------------------------------------


def test_post_search_local_no_communities_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search graph_mode=local → 422 graph_communities_not_built when communities
    never built and the query matches graph entities (S60).

    Local mode must guard the not-built case exactly like global search and the explain
    path, matching the documented contract in Documentation/UserManual/65_graph_search.md.
    It must NOT silently degrade to hybrid results. The no-entity path (S10) is covered
    separately by test_post_search_local_no_entity_graph_expansion_false.
    """
    from unittest.mock import AsyncMock, MagicMock

    _install_spacy_stub_no_entities(monkeypatch)

    col = "e1b-be6-local-no-communities"
    doc = tmp_path / "test_doc.txt"
    doc.write_text("Some content for testing.\n" * 5, encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Entity matching succeeds, but communities were never built.
        from archon_search.graph_types import EntityType, GraphNode
        matched_node = GraphNode(
            id="entity-1",
            entity_name="AuthService",
            entity_type=EntityType.system,
            source_doc_id="any",
            collection_name=col,
        )
        pipeline = client.app.state.pipeline
        mock_gs = MagicMock()
        mock_gs.find_nodes_by_name = AsyncMock(return_value=[matched_node])
        mock_gs.communities_table_exists = AsyncMock(return_value=False)
        pipeline._graph_store = mock_gs

        resp = client.post(
            "/search",
            json={"collection": col, "query": "AuthService", "graph_mode": "local"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422, (
            f"Expected 422 when no communities built (local mode); "
            f"got {resp.status_code}: {resp.text}"
        )
        detail = resp.json()["detail"]
        assert detail.get("code") == "graph_communities_not_built", (
            f"Expected code='graph_communities_not_built'; got: {detail!r}"
        )


# ---------------------------------------------------------------------------
# test_post_search_local_no_entity_graph_expansion_false
# ---------------------------------------------------------------------------


def test_post_search_local_no_entity_graph_expansion_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search graph_mode=local with query matching no graph entities → 200;
    graph_expansion_applied=false (S10 HTTP-level check).

    Communities are built but the query has no entity matches;
    local mode falls back to hybrid search with graph_expansion_applied=False.
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "e1b-be6-local-no-entities"
    doc = tmp_path / "test_doc.txt"
    doc.write_text("The quick brown fox jumps over the lazy dog.\n" * 5, encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Build communities (so the table exists) but no entity names match the query.
        db_path = cfg.db_path

        async def _get_chunk_ids() -> list[str]:
            from archon_search.constants import DEFAULT_NAMESPACE
            from archon_search.store import SearchStore

            s = SearchStore(db_path)
            await s.connect()
            try:
                rows = [r async for r in s.list_chunks_raw(col, DEFAULT_NAMESPACE)]
                return [r["chunk_id"] for r in rows]
            finally:
                await s.disconnect()

        chunk_ids = asyncio.run(_get_chunk_ids())
        asyncio.run(_write_communities_to_store(db_path, col, chunk_ids[:1]))

        # Query: "xyzzy" — a string that won't match any entity name.
        resp = client.post(
            "/search",
            json={"collection": col, "query": "xyzzy frobnicate", "graph_mode": "local"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"Expected 200 for local mode with no entity match; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "graph_expansion_applied" in body, (
            f"'graph_expansion_applied' key missing from response: {list(body.keys())}"
        )
        assert body["graph_expansion_applied"] is False, (
            f"Expected graph_expansion_applied=False when no entity match (S10); "
            f"got {body['graph_expansion_applied']!r}"
        )


# ---------------------------------------------------------------------------
# test_global_mode_acl_filters_community_chunks_integration
# ---------------------------------------------------------------------------


def test_global_mode_acl_filters_community_chunks_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search graph_mode=global: community chunks restricted to ns-b
    are excluded from the default-namespace response (S15 global-mode path).

    Setup:
    - Ingest chunks with ACL=["ns-b"] (namespace B restricted)
    - Build community pointing to those restricted chunk IDs
    - Search from default namespace (no access to ns-b)
    - Expect: 200 response; restricted chunks do NOT appear in results
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "e1b-be6-acl-global"
    # Ingest a document WITHOUT ACL so a regular search can find it.
    doc_open = tmp_path / "open_doc.txt"
    doc_open.write_text(
        "This document is accessible to all namespaces.\n" * 5, encoding="utf-8"
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        # Ingest open-access document.
        ingest_file_via_path(client, col, str(doc_open), api_key=api_key)
        db_path = cfg.db_path

        # Ingest a restricted chunk directly via the store (ACL=["ns-b"]).
        # IDs must match the store's format: doc_id=[a-f0-9]{64}, chunk_id={doc_id}-{6digits}
        from archon_search._types import ChunkRecord

        restricted_doc_id = "bb" * 32  # 64 hex chars
        restricted_chunk_id = restricted_doc_id + "-000001"

        async def _seed_restricted_chunk() -> None:
            from archon_search.store import SearchStore

            s = SearchStore(db_path)
            await s.connect()
            try:
                # Vector must match the store dimension (384 for the stub embedder).
                await s.ingest_chunks(col, [ChunkRecord(
                    doc_id=restricted_doc_id,
                    chunk_id=restricted_chunk_id,
                    text="This content is restricted to namespace B only.",
                    vector=[0.0] * 384,
                    source_path="/restricted/doc.txt",
                    indexed_at="2024-01-01T00:00:00Z",
                    acl=["ns-b"],
                )])
            finally:
                await s.disconnect()

        asyncio.run(_seed_restricted_chunk())

        # Build community pointing to the restricted chunk.
        asyncio.run(_write_communities_to_store(
            db_path, col, [restricted_chunk_id],
            community_id="restricted-comm-1",
            entity_ids=["restricted-entity-1"],
        ))

        # Search from default namespace.
        resp = client.post(
            "/search",
            json={"collection": col, "query": "restricted content", "graph_mode": "global"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"Expected 200 even when all community chunks are ACL-filtered; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        results = body.get("results", [])

        # The community had only ACL-restricted chunks; ACL filter empties candidates → fallback.
        # graph_expansion_applied=False confirms the community path was attempted and fell back.
        assert "graph_expansion_applied" in body, (
            f"'graph_expansion_applied' key missing from response: {list(body.keys())}"
        )
        # The restricted chunk must NOT appear in the results.
        result_source_paths = [r.get("source_path", "") for r in results]
        assert "/restricted/doc.txt" not in result_source_paths, (
            f"Restricted chunk appeared in results for default namespace; "
            f"source_paths: {result_source_paths!r}"
        )


# ---------------------------------------------------------------------------
# test_post_search_graph_mode_disabled_422
# ---------------------------------------------------------------------------


def test_post_search_fanout_global_no_communities_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search with collections=[col] graph_mode=global → 422 when no communities (S11 fanout).

    Tests the search_many() route branch (collections plural, not collection singular).
    The route's GraphCommunitiesNotBuiltError catch at the search_many path must fire.
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "e1b-be6-fanout-no-communities"
    doc = tmp_path / "test_doc.txt"
    doc.write_text("Some content for fanout testing.\n" * 5, encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Do NOT build communities; use collections (plural) to trigger search_many().
        resp = client.post(
            "/search",
            json={"collections": [col], "query": "test query", "graph_mode": "global"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422, (
            f"Expected 422 from search_many when no communities built; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "detail" in body, f"Expected 'detail' key in 422 body: {body!r}"
        detail = body["detail"]
        assert isinstance(detail, dict), (
            f"Expected detail to be a dict; got {type(detail).__name__!r}: {detail!r}"
        )
        assert detail.get("code") == "graph_communities_not_built", (
            f"Expected code='graph_communities_not_built'; got: {detail!r}"
        )
        assert "message" in detail, (
            f"Expected 'message' key in error detail per TypeSpec contract; got: {detail!r}"
        )
        assert detail["message"], f"'message' must be a non-empty string; got: {detail!r}"


# ---------------------------------------------------------------------------
# test_post_search_graph_mode_disabled_422
# ---------------------------------------------------------------------------


def test_post_search_graph_mode_disabled_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph.enabled=False; any graph_mode (naive, local, global) → 422.

    No spaCy stub needed since graph is disabled (no _check_graph_deps call).
    """
    col = "e1b-be6-graph-disabled"

    with make_real_app(tmp_path, monkeypatch, graph_enabled=False) as (client, cfg, api_key):
        assert cfg.graph.enabled is False

        for mode in ("naive", "local", "global"):
            resp = client.post(
                "/search",
                json={"collection": col, "query": "test query", "graph_mode": mode},
                headers=_auth(api_key),
            )
            assert resp.status_code == 422, (
                f"Expected 422 for graph_mode={mode!r} when graph disabled; "
                f"got {resp.status_code}: {resp.text}"
            )
            body = resp.json()
            assert "detail" in body, f"Expected 'detail' in 422 body: {body!r}"
            detail_text = str(body["detail"]).lower()
            assert "graph" in detail_text, (
                f"Expected 'graph' in 422 detail for mode={mode!r}; got: {body['detail']!r}"
            )


# ---------------------------------------------------------------------------
# S443: a TOML-configured fan-out timeout must actually fire on POST /search
# (the create_app→SearchPipeline wiring assertion lives in
#  tests/server/test_app.py::TestCreateAppPipelineWiring)
# ---------------------------------------------------------------------------


def test_post_search_fanout_honours_configured_timeout_504(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S443 behavioral regression: a configured fan-out timeout must produce 504.

    With ``fanout_timeout_seconds = 0.001`` and every fan-out leg sleeping 0.5 s,
    ``_fanout_merge_acl``'s ``asyncio.timeout`` must fire and the route must map
    ``FanoutTimeoutError`` to HTTP 504.  Before the fix the pipeline held the
    30.0 s default, so the 0.5 s legs completed and the route returned 200.
    """
    col_a = "s443-fanout-timeout-a"
    col_b = "s443-fanout-timeout-b"
    doc = tmp_path / "s443_doc.txt"
    doc.write_text("Fan-out timeout regression corpus content.\n" * 5, encoding="utf-8")

    toml_content = "[search]\nfanout_timeout_seconds = 0.001\n"

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        ingest_file_via_path(client, col_a, str(doc), api_key=api_key)
        ingest_file_via_path(client, col_b, str(doc), api_key=api_key)

        pipeline = client.app.state.pipeline
        original = pipeline.store.hybrid_search_with_trace

        async def _slow_hybrid_search_with_trace(*args, **kwargs):
            await asyncio.sleep(0.5)
            return await original(*args, **kwargs)

        monkeypatch.setattr(
            pipeline.store, "hybrid_search_with_trace", _slow_hybrid_search_with_trace
        )

        resp = client.post(
            "/search",
            json={"collections": [col_a, col_b], "query": "regression corpus"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 504, (
            "Expected 504 because each fan-out leg (0.5 s) exceeds the configured "
            f"fanout_timeout_seconds={cfg.fanout_timeout_seconds}; "
            f"got {resp.status_code}: {resp.text[:300]}"
        )


def test_post_search_fanout_generous_timeout_still_returns_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control for the 504 test above: a generous timeout still yields 200.

    Without this, an implementation that raised ``FanoutTimeoutError``
    unconditionally on every multi-collection request would keep
    ``test_post_search_fanout_honours_configured_timeout_504`` green.  Same
    corpus, same slow legs — only ``fanout_timeout_seconds`` differs — so the
    pair pins the timeout as the actual cause of the 504.
    """
    col_a = "s443-generous-timeout-a"
    col_b = "s443-generous-timeout-b"
    doc = tmp_path / "s443_generous_doc.txt"
    doc.write_text("Fan-out timeout regression corpus content.\n" * 5, encoding="utf-8")

    toml_content = "[search]\nfanout_timeout_seconds = 30.0\n"

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        ingest_file_via_path(client, col_a, str(doc), api_key=api_key)
        ingest_file_via_path(client, col_b, str(doc), api_key=api_key)

        pipeline = client.app.state.pipeline
        original = pipeline.store.hybrid_search_with_trace

        async def _slow_hybrid_search_with_trace(*args, **kwargs):
            await asyncio.sleep(0.5)
            return await original(*args, **kwargs)

        monkeypatch.setattr(
            pipeline.store, "hybrid_search_with_trace", _slow_hybrid_search_with_trace
        )

        resp = client.post(
            "/search",
            json={"collections": [col_a, col_b], "query": "regression corpus"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            "Expected 200: the 0.5 s legs sit well inside the configured "
            f"fanout_timeout_seconds={cfg.fanout_timeout_seconds}; "
            f"got {resp.status_code}: {resp.text[:300]}"
        )


def test_post_explain_fanout_honours_configured_timeout_504(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S435 behavioral regression: /explain must 504 on a configured fan-out timeout.

    The S435 report is a live 3-collection ``POST /explain`` with
    ``fanout_timeout_seconds = 0.001`` that ran for seconds and returned 200,
    because ``create_app`` never wired the timeout into the pipeline.

    ``tests/server/test_routes_explain.py`` covers the
    ``FanoutTimeoutError`` → 504 handler by injecting the exception into a mock
    pipeline; that handler is pre-existing and its test passes with or without
    the S443 wiring fix.  This test drives the real path —
    ``pipeline.explain(collections=...)`` → ``_fanout_merge_acl`` →
    ``asyncio.timeout(self._fanout_timeout_seconds)`` — so it fails if the
    configured timeout stops reaching the pipeline.
    """
    col_a = "s435-explain-timeout-a"
    col_b = "s435-explain-timeout-b"
    doc = tmp_path / "s435_doc.txt"
    doc.write_text("Explain fan-out timeout regression corpus content.\n" * 5, encoding="utf-8")

    toml_content = "[search]\nfanout_timeout_seconds = 0.001\n"

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        ingest_file_via_path(client, col_a, str(doc), api_key=api_key)
        ingest_file_via_path(client, col_b, str(doc), api_key=api_key)

        pipeline = client.app.state.pipeline
        original = pipeline.store.hybrid_search_with_trace

        async def _slow_hybrid_search_with_trace(*args, **kwargs):
            await asyncio.sleep(0.5)
            return await original(*args, **kwargs)

        monkeypatch.setattr(
            pipeline.store, "hybrid_search_with_trace", _slow_hybrid_search_with_trace
        )

        resp = client.post(
            "/explain",
            json={"collections": [col_a, col_b], "query": "regression corpus"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 504, (
            "Expected 504 because each fan-out leg (0.5 s) exceeds the configured "
            f"fanout_timeout_seconds={cfg.fanout_timeout_seconds}; "
            f"got {resp.status_code}: {resp.text[:300]}"
        )
        assert resp.json()["detail"] == "Search timed out"

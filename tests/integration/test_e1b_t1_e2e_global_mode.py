"""E1b / T-1 — e2e tests for build-communities CLI and graph_mode=global search.

Covers:
- (a) build-communities CLI via CliRunner with real fixture graph data; communities
      written to store; exit 0; summary output includes community count  (S1)
- (b) POST /search graph_mode=global returns 200 + non-empty results +
      graph_expansion_applied=true when communities are built  (S2)
- (c) POST /search graph_mode=global returns 422 graph_communities_not_built
      when communities have NOT been built  (S11)

Test (a) requires leidenalg and is skipped gracefully when not installed.
Tests (b) and (c) use make_real_app + direct community seeding; no leidenalg needed.
"""
from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# spaCy stub — needed for make_real_app(graph_enabled=True)
# ---------------------------------------------------------------------------


def _install_spacy_stub_no_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy that returns NO named entities.

    Must be called BEFORE make_real_app because create_app calls _check_graph_deps
    which imports spacy synchronously.
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
# Helper: write communities directly to the GraphStore from sync test
# ---------------------------------------------------------------------------


async def _write_communities_to_store(
    db_path: str,
    col: str,
    chunk_ids: list[str],
    *,
    community_id: str = "test-comm-1",
    entity_ids: list[str] | None = None,
) -> None:
    """Write a single community to the GraphStore at db_path."""
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
# (a) test_e2e_build_communities_cli
# ---------------------------------------------------------------------------

try:
    import leidenalg as _leidenalg_check  # noqa: F401

    _LEIDENALG_AVAILABLE = True
except ImportError:
    _LEIDENALG_AVAILABLE = False


@pytest.mark.skipif(
    not _LEIDENALG_AVAILABLE,
    reason="leidenalg not installed; skipping T-1 (a) build-communities e2e test",
)
def test_e2e_build_communities_cli(tmp_path: Path) -> None:
    """CliRunner + real store; ingest fixture graph data; build-communities exits 0;
    store has >= 1 community and output contains community count  (S1).
    """
    from click.testing import CliRunner

    from archon_search._types import ChunkRecord
    from archon_search.cli.graph_cmd import graph_cmd
    from archon_search.config import GraphConfig
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphEdge,
        GraphNode,
        RelationshipType,
        make_stable_edge_id,
        make_stable_entity_id,
    )
    from archon_search.store import SearchStore

    col = "t1-build-communities"
    doc_id_a = "docaaaaaaaaaaaaaaaaaaaaa"  # 24 chars
    doc_id_b = "docbbbbbbbbbbbbbbbbbbbbb"  # 24 chars

    node_a = GraphNode(
        id=make_stable_entity_id("concept", "AuthService"),
        entity_name="AuthService",
        entity_type=EntityType.concept,
        source_doc_id=doc_id_a,
        collection_name=col,
    )
    node_b = GraphNode(
        id=make_stable_entity_id("concept", "TokenValidator"),
        entity_name="TokenValidator",
        entity_type=EntityType.concept,
        source_doc_id=doc_id_b,
        collection_name=col,
    )
    edge = GraphEdge(
        id=make_stable_edge_id(node_a.id, node_b.id, "related_to"),
        source_node_id=node_a.id,
        target_node_id=node_b.id,
        relationship_type=RelationshipType.related_to,
        source_doc_id=node_a.source_doc_id,
    )

    # Production CLI: GraphStore(cfg.db_path) and SearchStore(cfg.db_path) share the same
    # LanceDB directory. Graph tables use _archon_graph_ prefix; no collision with search tables.
    db_path = str(tmp_path / "db")

    async def _seed() -> None:
        graph_store = GraphStore(db_path)
        await graph_store.connect()
        await graph_store.ensure_graph_tables(col, ns="default")
        await graph_store.write_graph(col, [node_a, node_b], [edge], ns="default")
        await graph_store.disconnect()

        search_store = SearchStore(db_path)
        await search_store.connect()
        await search_store.ingest_chunks(col, [
            ChunkRecord(
                doc_id=doc_id_a,
                chunk_id=f"{doc_id_a}-000000",
                text="AuthService handles authentication tokens.",
                vector=[1.0, 0.0, 0.0],
                source_path="/docs/auth.txt",
                indexed_at="2026-01-01T00:00:00Z",
            ),
            ChunkRecord(
                doc_id=doc_id_b,
                chunk_id=f"{doc_id_b}-000000",
                text="TokenValidator checks JWT expiry and signature.",
                vector=[0.0, 1.0, 0.0],
                source_path="/docs/token.txt",
                indexed_at="2026-01-01T00:00:00Z",
            ),
        ])
        await search_store.disconnect()

    asyncio.run(_seed())

    mock_cfg = MagicMock()
    mock_cfg.db_path = db_path
    mock_cfg.graph = GraphConfig(enabled=True, community_summary_chunks=1)

    runner = CliRunner()
    with patch("archon_search.cli.graph_cmd.load_config", return_value=mock_cfg):
        # No GraphStore.__init__ patch needed — CLI constructs GraphStore(cfg.db_path)
        # which points to the same db_path where graph data was seeded above.
        result = runner.invoke(graph_cmd, ["build-communities", col])

    assert result.exit_code == 0, (
        f"Expected exit 0 from build-communities; "
        f"got exit_code={result.exit_code}:\n{result.output}"
    )
    # Output must contain a community count line (e.g. "Built 1 communities for collection...")
    import re as _re
    assert _re.search(r"built \d+ communit", result.output.lower()), (
        f"Expected 'Built N communities' in output; got: {result.output!r}"
    )

    # Verify communities were persisted to the GraphStore at the shared db_path
    async def _verify() -> None:
        graph_store = GraphStore(db_path)
        await graph_store.connect()
        count, last_built_at = await graph_store.get_community_stats(col, ns="default")
        await graph_store.disconnect()
        assert count >= 1, f"Expected at least 1 community in store; got {count}"
        assert last_built_at is not None, "Expected last_built_at to be set after build"

    asyncio.run(_verify())


# ---------------------------------------------------------------------------
# (b) test_e2e_global_mode_returns_results
# ---------------------------------------------------------------------------


def test_e2e_global_mode_returns_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After build-communities; POST /search graph_mode=global → 200, non-empty results,
    graph_expansion_applied=true  (S2).

    Communities are seeded directly into the store (no leidenalg needed).
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "t1-global-mode-results"
    doc = tmp_path / "test_doc.txt"
    doc.write_text(
        "SearchPipeline provides vector and FTS retrieval. "
        "Community detection clusters related entities.\n" * 5,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        # Ingest the document so the collection and chunks exist.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Retrieve a chunk_id from the store.
        async def _get_chunk_ids() -> list[str]:
            from archon_search.constants import DEFAULT_NAMESPACE
            from archon_search.store import SearchStore

            s = SearchStore(cfg.db_path)
            await s.connect()
            try:
                rows = [r async for r in s.list_chunks_raw(col, DEFAULT_NAMESPACE)]
                return [r["chunk_id"] for r in rows]
            finally:
                await s.disconnect()

        chunk_ids = asyncio.run(_get_chunk_ids())
        assert chunk_ids, "Ingest must have created at least one chunk"

        # Seed a community pointing to the first chunk.
        asyncio.run(
            _write_communities_to_store(cfg.db_path, col, chunk_ids[:1])
        )

        # POST /search with graph_mode=global should return 200 with results.
        resp = client.post(
            "/search",
            json={"collection": col, "query": "community retrieval", "graph_mode": "global"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"Expected 200 for graph_mode=global with communities built; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "results" in body, f"Expected 'results' key in response: {list(body.keys())}"
        assert body.get("graph_expansion_applied") is True, (
            f"Expected graph_expansion_applied=True; "
            f"got {body.get('graph_expansion_applied')!r}. Response: {body}"
        )
        assert len(body["results"]) > 0, (
            f"Expected non-empty results from global mode search; got empty list. "
            f"Response: {body}"
        )
        # Verify results originate from the community representative: the seeded community
        # points to chunk_ids[:1]. The source_path of results must include the ingested doc.
        result_sources = [r.get("source_path", "") for r in body["results"]]
        assert any(str(doc) in src for src in result_sources), (
            f"Expected at least one result from the ingested document {str(doc)!r}; "
            f"got source_paths: {result_sources!r}"
        )


# ---------------------------------------------------------------------------
# (c) test_e2e_global_mode_no_communities_422
# ---------------------------------------------------------------------------


def test_e2e_global_mode_no_communities_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No build-communities; POST /search graph_mode=global → 422 graph_communities_not_built  (S11).

    Verifies that the route catches GraphCommunitiesNotBuiltError and returns a 422
    with {"detail": {"code": "graph_communities_not_built"}} body.
    """
    _install_spacy_stub_no_entities(monkeypatch)

    col = "t1-global-no-communities"
    doc = tmp_path / "test_doc.txt"
    doc.write_text("Some content for testing graph_mode=global.\n" * 5, encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        # Ingest a document so the collection exists — but do NOT build communities.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

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
        assert isinstance(detail, dict), (
            f"Expected detail to be a dict; got {type(detail).__name__!r}: {detail!r}"
        )
        assert detail.get("code") == "graph_communities_not_built", (
            f"Expected code='graph_communities_not_built'; got: {detail!r}"
        )
        # The 422 body must contain a message (per TypeSpec contract)
        assert "message" in detail, (
            f"Expected 'message' key in error detail; got: {detail!r}"
        )
        assert detail["message"], (
            f"'message' must be a non-empty string; got: {detail!r}"
        )

"""E1b / T-2 — e2e tests for graph_mode=local search.

Covers:
- (a) POST /search graph_mode=local with a query matching a community entity →
      200 + non-empty results + graph_expansion_applied=true  (S3)
- (b) POST /search graph_mode=local with a query containing no recognisable
      entities → 200 + standard results + graph_expansion_applied=false  (S10)
- (c) POST /search graph_mode=local with a query matching a graph entity that
      has no community membership (isolated node) → 200 + results +
      graph_expansion_applied=true (naive fallback)  (S9)
- (d) POST /search with collections=[...] graph_mode=local across two collections
      where one has communities and one does not → both legs return results  (mixed-match)

Communities are seeded directly into the GraphStore (no leidenalg required).
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
# spaCy stub — needed for make_real_app(graph_enabled=True)
# ---------------------------------------------------------------------------


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
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
# Helpers: seed graph nodes + communities directly (no leidenalg needed)
# ---------------------------------------------------------------------------


async def _get_first_chunk_row(db_path: str, col: str) -> dict:
    """Return the full row dict of the first chunk in *col*."""
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.store import SearchStore

    s = SearchStore(db_path)
    await s.connect()
    try:
        rows = [r async for r in s.list_chunks_raw(col, DEFAULT_NAMESPACE)]
        assert rows, f"Expected at least one chunk in collection {col!r}"
        return rows[0]
    finally:
        await s.disconnect()


async def _seed_graph_and_community(
    db_path: str,
    col: str,
    *,
    node_name: str,
    chunk_id: str,
    doc_id: str,
) -> str:
    """Seed one graph node plus a community pointing to *chunk_id*.

    Returns the node's stable entity ID.
    """
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        Community,
        EntityType,
        GraphNode,
        make_stable_entity_id,
    )

    node = GraphNode(
        id=make_stable_entity_id("concept", node_name),
        entity_name=node_name,
        entity_type=EntityType.concept,
        source_doc_id=doc_id,
        collection_name=col,
    )

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_graph_tables(col)
        await gs.write_graph(col, [node], [])

        await gs.ensure_communities_table(col)
        community = Community(
            community_id="test-comm-local-1",
            entity_ids=[node.id],
            representative_chunk_ids=[chunk_id],
            built_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            summary_text=None,
        )
        await gs.write_communities(col, [community])
    finally:
        await gs.disconnect()

    return node.id


async def _seed_isolated_node(db_path: str, col: str, node_name: str, doc_id: str) -> None:
    """Seed a single graph node with no community membership."""
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphNode,
        make_stable_entity_id,
    )

    isolated_node = GraphNode(
        id=make_stable_entity_id("concept", node_name),
        entity_name=node_name,
        entity_type=EntityType.concept,
        source_doc_id=doc_id,
        collection_name=col,
    )
    gs = GraphStore(db_path)
    await gs.connect()
    try:
        # Table was already created by _seed_graph_and_community above.
        await gs.write_graph(col, [isolated_node], [])
    finally:
        await gs.disconnect()


# ---------------------------------------------------------------------------
# (a) test_e2e_local_mode_with_entity_match  (S3)
# ---------------------------------------------------------------------------


def test_e2e_local_mode_with_entity_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search graph_mode=local + query containing a known entity name →
    200 + non-empty results + graph_expansion_applied=true  (S3).

    Setup:
    - Ingest a document about "AuthService".
    - Manually seed a graph node "Authservice" + a community pointing to the doc chunk.
    - Query with "authservice" — the n-gram matching should find the node, which
      belongs to the seeded community, so community chunks are returned.
    """
    _install_spacy_stub(monkeypatch)

    col = "t2-local-entity-match"
    # Entity name must be a single token so the 1-gram matches exactly.
    entity_name = "Authservice"

    doc = tmp_path / "authservice.txt"
    doc.write_text(
        "The Authservice system handles authentication and session management "
        "for all downstream services.\n" * 6,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        row = asyncio.run(_get_first_chunk_row(cfg.db_path, col))
        chunk_id = row["chunk_id"]
        doc_id = row["doc_id"]

        asyncio.run(
            _seed_graph_and_community(
                cfg.db_path,
                col,
                node_name=entity_name,
                chunk_id=chunk_id,
                doc_id=doc_id,
            )
        )

        resp = client.post(
            "/search",
            json={
                "collection": col,
                "query": "authservice authentication system",
                "graph_mode": "local",
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"Expected 200 for graph_mode=local with entity match; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "results" in body, f"Expected 'results' in response: {list(body.keys())}"
        assert len(body["results"]) > 0, (
            f"Expected non-empty results from local mode search; got empty list. body={body}"
        )
        assert body.get("graph_expansion_applied") is True, (
            f"Expected graph_expansion_applied=True; "
            f"got {body.get('graph_expansion_applied')!r}. body={body}"
        )
        # Verify the community representative chunk appears in results
        result_chunk_ids = [r.get("chunk_id") for r in body["results"]]
        assert chunk_id in result_chunk_ids, (
            f"Expected community representative chunk_id={chunk_id!r} in results; "
            f"got chunk_ids: {result_chunk_ids!r}. body={body}"
        )


# ---------------------------------------------------------------------------
# (b) test_e2e_local_mode_no_entities_fallback  (S10)
# ---------------------------------------------------------------------------


def test_e2e_local_mode_no_entities_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search graph_mode=local + query with no recognisable entities →
    200 + standard results + graph_expansion_applied=false  (S10).

    The graph has nodes for "AuthService" but the query "quantum gravitational
    wave interference" contains no tokens that match those names.
    The pipeline falls back to standard hybrid search and sets
    graph_expansion_applied=False.
    """
    _install_spacy_stub(monkeypatch)

    col = "t2-local-no-entity"
    entity_name = "Authservice"

    doc = tmp_path / "authservice_docs.txt"
    doc.write_text(
        "The Authservice module manages OAuth2 token issuance and validation.\n" * 6,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        row = asyncio.run(_get_first_chunk_row(cfg.db_path, col))
        chunk_id = row["chunk_id"]
        doc_id = row["doc_id"]

        asyncio.run(
            _seed_graph_and_community(
                cfg.db_path,
                col,
                node_name=entity_name,
                chunk_id=chunk_id,
                doc_id=doc_id,
            )
        )

        # Query with terms that do NOT appear as graph entity names.
        resp = client.post(
            "/search",
            json={
                "collection": col,
                "query": "quantum gravitational wave interference",
                "graph_mode": "local",
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"Expected 200 for local mode with no entity match; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "results" in body, f"Expected 'results' in response: {list(body.keys())}"
        assert len(body["results"]) > 0, (
            "Expected at least some standard hybrid results even when graph_expansion_applied=False"
        )
        assert body.get("graph_expansion_applied") is False, (
            f"Expected graph_expansion_applied=False when no entities matched; "
            f"got {body.get('graph_expansion_applied')!r}. body={body}"
        )


# ---------------------------------------------------------------------------
# (c) test_e2e_local_mode_isolated_node_fallback  (S9)
# ---------------------------------------------------------------------------


def test_e2e_local_mode_isolated_node_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search graph_mode=local + query matching an entity that is NOT in
    any community (isolated node) → 200 + results + graph_expansion_applied=true
    (naive fallback per S9).

    Setup:
    - Seed two graph nodes: "Authservice" (in a community) and "Tokenvalidator"
      (isolated — not in any community).
    - Query with "tokenvalidator" — the node is found but has no community, so
      the pipeline falls back to naive expansion.
    - graph_expansion_applied must be True (S9 spec) and status must be 200
      (not a 4xx error).
    """
    _install_spacy_stub(monkeypatch)

    col = "t2-local-isolated-node"

    doc = tmp_path / "tokens.txt"
    doc.write_text(
        "Tokenvalidator checks JWT signatures and expiry for Authservice.\n" * 6,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        row = asyncio.run(_get_first_chunk_row(cfg.db_path, col))
        chunk_id = row["chunk_id"]
        doc_id = row["doc_id"]

        # Seed Authservice node WITH community membership.
        asyncio.run(
            _seed_graph_and_community(
                cfg.db_path,
                col,
                node_name="Authservice",
                chunk_id=chunk_id,
                doc_id=doc_id,
            )
        )

        # Seed Tokenvalidator node WITHOUT any community membership.
        asyncio.run(_seed_isolated_node(cfg.db_path, col, "Tokenvalidator", doc_id))

        # Query with "tokenvalidator" — this matches the isolated node.
        resp = client.post(
            "/search",
            json={
                "collection": col,
                "query": "tokenvalidator jwt signature validation",
                "graph_mode": "local",
            },
            headers=_auth(api_key),
        )
        # Must not be a 4xx — isolated node fallback should return 200.
        assert resp.status_code == 200, (
            f"Expected 200 for isolated node fallback; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "results" in body, f"Expected 'results' in response: {list(body.keys())}"
        # Per S9: graph_expansion_applied=True even on naive fallback.
        assert body.get("graph_expansion_applied") is True, (
            f"Expected graph_expansion_applied=True for isolated-node S9 fallback; "
            f"got {body.get('graph_expansion_applied')!r}. body={body}"
        )


# ---------------------------------------------------------------------------
# (d) test_e2e_local_mode_multi_collection  (mixed-match fanout)
# ---------------------------------------------------------------------------


def test_e2e_local_mode_multi_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-collection fanout with graph_mode=local:
    - col_a: has a community → community chunks returned for that leg.
    - col_b: has NO community table → falls back to standard hybrid search for that leg.
    Both legs must return results; the fanout must not raise or return 4xx.
    """
    _install_spacy_stub(monkeypatch)

    col_a = "t2-local-fanout-col-a"
    col_b = "t2-local-fanout-col-b"

    doc_a = tmp_path / "col_a_doc.txt"
    doc_a.write_text(
        "The Authservice module authenticates all API requests.\n" * 6,
        encoding="utf-8",
    )
    doc_b = tmp_path / "col_b_doc.txt"
    doc_b.write_text(
        "The Cacheservice module provides distributed caching for API responses.\n" * 6,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        # Ingest into both collections.
        ingest_file_via_path(client, col_a, str(doc_a), api_key=api_key)
        ingest_file_via_path(client, col_b, str(doc_b), api_key=api_key)

        # Seed graph + community for col_a (entity: "Authservice").
        row_a = asyncio.run(_get_first_chunk_row(cfg.db_path, col_a))
        chunk_id_a = row_a["chunk_id"]
        doc_id_a = row_a["doc_id"]

        asyncio.run(
            _seed_graph_and_community(
                cfg.db_path,
                col_a,
                node_name="Authservice",
                chunk_id=chunk_id_a,
                doc_id=doc_id_a,
            )
        )
        # col_b: no graph nodes and no communities seeded.

        # Query with entity name "authservice" using the multi-collection (collections plural) path.
        resp = client.post(
            "/search",
            json={
                "collections": [col_a, col_b],
                "query": "authservice api authentication",
                "graph_mode": "local",
                "top_k": 10,
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"Expected 200 for multi-collection local mode fanout; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "results" in body, f"Expected 'results' in response: {list(body.keys())}"
        assert len(body["results"]) > 0, (
            f"Expected non-empty results from multi-collection local mode; "
            f"got empty list. body={body}"
        )
        # col_a had a community match → fanout sets graph_expansion_applied=True
        assert body.get("graph_expansion_applied") is True, (
            f"Expected graph_expansion_applied=True for fanout with at least one community leg; "
            f"got {body.get('graph_expansion_applied')!r}. body={body}"
        )

        # Both collections must contribute results.
        seen_collections = {r.get("collection") for r in body["results"]}
        assert col_a in seen_collections, (
            f"col_a ({col_a!r}) absent from multi-collection results; "
            f"seen: {seen_collections}"
        )
        assert col_b in seen_collections, (
            f"col_b ({col_b!r}) absent from multi-collection results; "
            f"seen: {seen_collections}"
        )

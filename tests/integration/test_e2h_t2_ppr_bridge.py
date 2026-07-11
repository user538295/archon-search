"""Integration tests for T-2: PPR walk retrieves bridge docs.

Covers:
- PPR walk bridges two documents via a shared graph entity;
  the entity-linked (bridge) doc appears in PPR results, ppr_entities_matched > 0.
- Empty graph tables → 200 with hybrid fallback, ppr_entities_matched=0.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from archon_search.graph_types import (
    EntityType,
    GraphMention,
    GraphNode,
    make_stable_entity_id,
)
from tests.integration.conftest import (
    ingest_file_via_path,
    install_spacy_stub,
    make_real_app,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("ppr_bridge")]

# C1-B-3: constant for the distinctive marker in doc_b
# Must be semantically unrelated to "Archon" so vector similarity does not pull
# doc_b into plain hybrid results for the "Archon" query.
_DOC_B_MARKER = "photosynthesis"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _node(name: str, col: str, entity_type: EntityType = EntityType.concept) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(entity_type.value, name),
        entity_name=name,
        entity_type=entity_type,
        source_doc_id="seed-doc",
        collection_name=col,
    )


def _mention(node: GraphNode, chunk_id: str, doc_id: str = "seed-doc") -> GraphMention:
    return GraphMention(entity_id=node.id, chunk_id=chunk_id, doc_id=doc_id)


async def _seed_graph(
    db_path: str,
    collection: str,
    ns: str,
    nodes: list[GraphNode],
    mentions: list[GraphMention],
) -> None:
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_graph_tables(collection, ns=ns)
        await gs.write_graph(collection, nodes, [], ns=ns)
        await gs.write_mentions(collection, mentions, ns=ns)
    finally:
        await gs.disconnect()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_e2h_t2_pprMode_bridgeQuery_entityChunkInResults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PPR walk bridges two docs via shared entity.

    Setup:
    - Doc A: contains keyword 'Archon' → hybrid search ranks this first.
    - Doc B: contains NO keyword 'Archon' → hybrid alone ranks it lower.
    - Graph: 'Archon' entity (EntityType.concept, no auto-extraction) →
      mentions pointing to Doc B's chunk.

    Assertion:
    - ppr_entities_matched > 0: the PPR walk found entity-linked chunks (one node,
      one mention pointing to doc_b).
    - Doc B's chunk appears in PPR results (PPR graph walk pulled it in).
    - Doc B's chunk does NOT appear in plain hybrid results (verifies PPR changed output).
    """
    install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        # Doc A: contains query keyword → hybrid would rank this first
        doc_a = tmp_path / "doc_a.txt"
        doc_a.write_text("Archon is a powerful distributed system for data retrieval.")

        # Doc B: does NOT contain the keyword; hybrid alone ranks it lower or not at all
        doc_b = tmp_path / "doc_b.txt"
        doc_b.write_text(
            f"This document covers plant biology including {_DOC_B_MARKER} and cellular respiration."
        )

        # Filler docs: all contain "Archon" → FTS ranks them above doc_b for the
        # "Archon" query.  With top_k_return=5 and 5 filler docs + doc_a competing,
        # doc_b (no "Archon" FTS match, identical vector score) falls out of top-5
        # hybrid results, making the baseline assertion deterministic.
        for i in range(5):
            filler = tmp_path / f"filler_{i}.txt"
            filler.write_text(f"Archon filler document {i} about distributed computing.")
            ingest_file_via_path(client, "col", str(filler), api_key=api_key)

        ingest_file_via_path(client, "col", str(doc_a), api_key=api_key)
        ingest_file_via_path(client, "col", str(doc_b), api_key=api_key)

        db_path = cfg.db_path
        col = "col"
        ns = "default"

        # Retrieve Doc B's chunk ID by querying its distinctive text
        resp = client.post(
            "/search",
            json={"collection": col, "query": f"plant biology {_DOC_B_MARKER} cellular respiration"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results, "Expected at least one result for doc_b content query"
        doc_b_chunks = [r for r in results if _DOC_B_MARKER in r.get("text", "")]
        # C1-B-1: hard assert instead of pytest.skip — corpus is deterministic
        assert doc_b_chunks, (
            "doc_b chunk must be locatable for bridge test — "
            "ingest or chunking regression prevents the test from running"
        )
        doc_b_chunk_id = doc_b_chunks[0]["chunk_id"]

        # Build graph: 'Archon' entity → mention → Doc B's chunk.
        # No auto-extraction occurs (install_spacy_stub recognizes only Alice/Bob/Google);
        # entity_type can be arbitrary since find_nodes_by_name matches by name only.
        node_archon = _node("Archon", col)

        # 'Archon' mentions point to Doc B's chunk (the bridge)
        mentions = [_mention(node_archon, doc_b_chunk_id)]

        asyncio.run(
            _seed_graph(
                db_path,
                col,
                ns,
                [node_archon],
                mentions,
            )
        )

        # C1-B-4: Baseline — plain hybrid search must NOT include doc_b.
        # Five filler docs + doc_a all contain "Archon" and fill the top-5 via FTS;
        # doc_b (no "Archon" match, identical stub vector) is pushed out of top-5.
        baseline_resp = client.post(
            "/search",
            json={"collection": col, "query": "Archon"},
            headers=_auth(api_key),
        )
        assert baseline_resp.status_code == 200, baseline_resp.text
        baseline_chunk_ids = [r["chunk_id"] for r in baseline_resp.json()["results"]]
        assert baseline_chunk_ids, "Expected non-empty baseline results"
        assert doc_b_chunk_id not in baseline_chunk_ids, (
            f"Bridge doc chunk {doc_b_chunk_id!r} should NOT appear in plain hybrid results for 'Archon' query. "
            "If it does, the test cannot verify PPR changed the output."
        )

        # PPR search: query 'Archon' → walker seeds via find_nodes_by_name("archon") → finds mention → Doc B
        resp = client.post(
            "/search",
            json={"collection": col, "query": "Archon", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["ppr_entities_matched"] > 0, (
            f"Expected ppr_entities_matched > 0 (PPR should match 'Archon' entity), "
            f"got {body['ppr_entities_matched']}"
        )
        assert body["results"], "Expected non-empty results from PPR search"

        ppr_chunk_ids = [r["chunk_id"] for r in body["results"]]
        assert doc_b_chunk_id in ppr_chunk_ids, (
            f"Bridge doc chunk {doc_b_chunk_id!r} not in PPR results: {ppr_chunk_ids}. "
            "PPR must include entity-linked chunks via graph walk; doc_b is absent from plain hybrid."
        )


def test_e2h_t2_pprMode_emptyGraph_hybrid_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PPR mode with empty graph tables falls back to hybrid, ppr_entities_matched=0.

    Graph tables exist (created by ensure_graph_tables) but contain no nodes or
    mentions. PPRWalker finds no entity matches → ppr_entities_matched=0.
    Hybrid fallback must return non-empty results.
    """
    install_spacy_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc = tmp_path / "doc.txt"
        doc.write_text("Archon is a powerful system for hybrid information retrieval.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        # Ensure graph tables exist but leave them empty (no nodes/mentions seeded)
        asyncio.run(
            _seed_graph(cfg.db_path, "col", "default", nodes=[], mentions=[])
        )

        resp = client.post(
            "/search",
            json={"collection": "col", "query": "Archon", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["ppr_entities_matched"] == 0, (
            f"Expected ppr_entities_matched=0 for empty graph, got {body['ppr_entities_matched']}"
        )
        assert body["results"], "Expected non-empty results from hybrid fallback"

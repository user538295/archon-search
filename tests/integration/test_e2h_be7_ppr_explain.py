"""Integration tests for BE-7: PPR provenance in /explain endpoint.

Covers:
- POST /explain with graph_mode="ppr" returns graph_mode_applied="ppr" and ppr_entities_matched >= 0
- PPR explain with entities seeded returns ppr_entities_matched > 0 and graph_provenance on results
- PPR explain with empty graph falls back to hybrid with ppr_entities_matched=0
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
    make_real_app,
)
from tests._graph_engine_stub import install_graph_engine_stub

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("ppr_explain")]


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
        source_doc_id="doc1",
        collection_name=col,
    )


def _mention(node: GraphNode, chunk_id: str) -> GraphMention:
    return GraphMention(entity_id=node.id, chunk_id=chunk_id, doc_id="doc1")


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


def test_explainEndpoint_pprMode_returnsGraphModeApplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain with graph_mode='ppr' returns graph_mode_applied='ppr' and ppr_entities_matched >= 0."""
    install_graph_engine_stub(monkeypatch, empty=True)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc = tmp_path / "doc.txt"
        doc.write_text("kubernetes is a container orchestration system for deploying workloads.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        resp = client.post(
            "/explain",
            json={"collection": "col", "query": "kubernetes", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["graph_mode_applied"] == "ppr", (
            f"Expected graph_mode_applied='ppr', got {body.get('graph_mode_applied')!r}"
        )
        assert body["ppr_entities_matched"] is not None, (
            "Expected ppr_entities_matched to be present (int), got None"
        )
        assert body["ppr_entities_matched"] >= 0, (
            f"Expected ppr_entities_matched >= 0, got {body['ppr_entities_matched']}"
        )


def test_explainEndpoint_pprMode_withSeededGraph_entitiesMatchedAndProvenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PPR explain with seeded graph: ppr_entities_matched > 0, results have graph_provenance."""
    install_graph_engine_stub(monkeypatch, entities=[("kubernetes", "system")], content_aware=True)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc = tmp_path / "k8s.txt"
        doc.write_text("kubernetes is a container orchestration system for deploying workloads.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        # Get a real chunk ID
        resp = client.post(
            "/search",
            json={"collection": "col", "query": "kubernetes"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results, "Expected at least one chunk after ingest"
        chunk_id = results[0]["chunk_id"]

        # Seed graph
        node_k8s = _node("kubernetes", "col")
        mentions = [_mention(node_k8s, chunk_id)]
        asyncio.run(_seed_graph(cfg.db_path, "col", "default", [node_k8s], mentions))

        resp = client.post(
            "/explain",
            json={"collection": "col", "query": "kubernetes", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["graph_mode_applied"] == "ppr"
        assert body["ppr_entities_matched"] > 0, (
            f"Expected ppr_entities_matched > 0 with seeded graph, got {body['ppr_entities_matched']}"
        )
        assert body["results"], "Expected non-empty results"
        # At least one result should have graph_provenance with PPR steps
        results_with_provenance = [
            r for r in body["results"] if r.get("graph_provenance") is not None
        ]
        assert results_with_provenance, (
            "Expected at least one result with graph_provenance from PPR walk"
        )
        # Verify the provenance step has relationship="ppr"
        first_prov = results_with_provenance[0]["graph_provenance"]
        assert first_prov.get("steps"), "Expected provenance steps"
        ppr_steps = [s for s in first_prov["steps"] if s.get("relationship") == "ppr"]
        assert ppr_steps, f"Expected steps with relationship='ppr', got: {first_prov['steps']}"


def test_explainEndpoint_pprMode_emptyGraph_fallsBackToHybrid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PPR explain with empty graph: falls back to hybrid, ppr_entities_matched=0, results non-empty."""
    install_graph_engine_stub(monkeypatch, empty=True)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        doc = tmp_path / "doc.txt"
        doc.write_text("kubernetes is a container orchestration system.")
        ingest_file_via_path(client, "col", str(doc), api_key=api_key)

        resp = client.post(
            "/explain",
            json={"collection": "col", "query": "kubernetes", "graph_mode": "ppr"},
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["graph_mode_applied"] == "ppr"
        assert body["ppr_entities_matched"] == 0, (
            f"Expected ppr_entities_matched=0 for empty graph, got {body['ppr_entities_matched']}"
        )
        # Hybrid fallback should return results
        assert body["results"], "Expected results from hybrid fallback"

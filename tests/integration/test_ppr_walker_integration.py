"""Integration test for PPRWalker — E2h BE-5.

Uses real GraphStore with LanceDB in tmp_path. Writes nodes, edges, and
mentions, then runs PPRWalker.walk to verify end-to-end retrieval.
"""
from __future__ import annotations

import asyncio

import pytest

from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphMention,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("benchmark")]

_COL = "ppr-integ-col"
_NS = "default"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(name: str, entity_type: EntityType = EntityType.concept) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(entity_type.value, name),
        entity_name=name,
        entity_type=entity_type,
        source_doc_id="doc-integ",
        collection_name=_COL,
    )


def _edge(src: GraphNode, tgt: GraphNode) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(src.id, tgt.id, RelationshipType.related_to.value),
        source_node_id=src.id,
        target_node_id=tgt.id,
        relationship_type=RelationshipType.related_to,
        source_doc_id="doc-integ",
    )


def _mention(node: GraphNode, chunk_id: str, doc_id: str = "doc-integ") -> GraphMention:
    return GraphMention(entity_id=node.id, chunk_id=chunk_id, doc_id=doc_id)


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_pprWalker_realGraph_returnsTopKEntityChunks(tmp_path) -> None:
    """Real GraphStore + PPRWalker.walk returns non-empty chunk IDs and matches entities."""
    from archon_search.graph_store import GraphStore
    from archon_search.ppr_walker import PPRWalker

    node_k8s = _node("kubernetes")
    node_deploy = _node("deployment")
    node_hub = _node("orchestration")

    edge_k8s_hub = _edge(node_k8s, node_hub)
    edge_deploy_hub = _edge(node_deploy, node_hub)

    mentions = [
        _mention(node_k8s, "chunk-k8s-001", "doc-integ"),
        _mention(node_k8s, "chunk-k8s-002", "doc-integ"),
        _mention(node_deploy, "chunk-deploy-001", "doc-integ"),
    ]

    async def _run() -> None:
        gs = GraphStore(str(tmp_path / "db"))
        await gs.connect()
        try:
            await gs.ensure_graph_tables(_COL, ns=_NS)
            await gs.write_graph(
                _COL,
                [node_k8s, node_deploy, node_hub],
                [edge_k8s_hub, edge_deploy_hub],
                ns=_NS,
            )
            await gs.write_mentions(_COL, mentions, ns=_NS)

            walker = PPRWalker(gs)
            result = await walker.walk(
                "kubernetes deployment",
                _COL,
                damping=0.85,
                top_entities=20,
                ns=_NS,
            )

            assert result.entities_matched > 0, (
                f"Expected at least 1 entity matched, got {result.entities_matched}"
            )
            assert result.chunk_ids, f"Expected non-empty chunk_ids, got {result.chunk_ids}"
            # Both kubernetes chunks should be present (higher mention count → included)
            assert "chunk-k8s-001" in result.chunk_ids or "chunk-k8s-002" in result.chunk_ids
        finally:
            await gs.disconnect()

    asyncio.run(_run())

"""E2e test for T-1 (S34, S35): the served graph viewer page distinguishes a typed
edge from its ``related_to`` twin over the same node pair.

No browser harness exists for this viewer — per the task note, the viewer is
asserted as text. This test combines two real HTTP calls against a real app
(TestClient, real GraphStore, real ``graph_viewer.html`` file — only the graph-engine stubbed):

  1. GET /graph/{collection}      — real BE-1-ranked edges for a node pair that
     carries BOTH a typed edge (``uses``) and a ``related_to`` edge.
  2. GET /graph/{collection}/view — the real served HTML, from which the
     FE-1 ``RELATIONSHIP_COLORS`` / ``UNDIRECTED_RELATIONSHIP_TYPES`` maps and the
     ``buildVisEdge`` function body are extracted by text parsing.

The two are then cross-referenced to prove the served viewer would render the
typed edge with a different colour AND an arrowhead, while its related_to twin
gets DEFAULT_EDGE_COLOR and no arrowhead — and that BE-1's tie-break ordering
(equal weight, typed-before-untyped) actually places the typed edge first.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from tests.integration.conftest import install_graph_stub, make_real_app
from tests.server.test_e2j_fe1_graph_viewer_html import (
    _extract_function_body,
    _extract_json_like_block,
    _extract_undirected_relationship_types,
)

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def _seed_typed_and_related_to_twin(db_path: str, collection: str, ns: str = "default") -> None:
    """Write two nodes plus a typed edge AND a related_to edge over the SAME
    node pair (entity-a <-> entity-b), with shared mentions so both edges get
    a non-zero co-occurrence weight and survive into the inspection response."""
    from archon_search.collection_meta import CollectionMeta
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import EntityType, GraphEdge, GraphMention, GraphNode, RelationshipType
    from archon_search.store import SearchStore

    store = SearchStore(db_path)
    await store.connect()
    try:
        await store.ensure_collection(collection, 384)
        meta = CollectionMeta(
            name=collection,
            active_embedding_model="stub-model",
            doc_count=0,
            chunk_count=0,
            namespace=ns,
        )
        await store.update_collection_meta(meta)
    finally:
        await store.disconnect()

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        await gs.ensure_graph_tables(collection, ns=ns)
        nodes = [
            GraphNode(
                id="entity-a", entity_name="A", entity_type=EntityType.concept,
                source_doc_id="doc-1", collection_name=collection,
            ),
            GraphNode(
                id="entity-b", entity_name="B", entity_type=EntityType.concept,
                source_doc_id="doc-1", collection_name=collection,
            ),
        ]
        edges = [
            GraphEdge(
                id="edge-typed",
                source_node_id="entity-a",
                target_node_id="entity-b",
                relationship_type=RelationshipType.uses,
                source_doc_id="doc-1",
            ),
            GraphEdge(
                id="edge-related-to",
                source_node_id="entity-a",
                target_node_id="entity-b",
                relationship_type=RelationshipType.related_to,
                source_doc_id="doc-1",
            ),
        ]
        await gs.write_graph(collection, nodes, edges, ns=ns)
        mentions = [
            GraphMention(entity_id="entity-a", chunk_id="chunk-1", doc_id="doc-1"),
            GraphMention(entity_id="entity-b", chunk_id="chunk-1", doc_id="doc-1"),
        ]
        await gs.write_mentions(collection, mentions, ns=ns)
    finally:
        await gs.disconnect()


def _relationship_color(relationship_colors_block: str, rel_type: str, default_color: str) -> str:
    """Mirror buildVisEdge's hasOwnProperty-guarded RELATIONSHIP_COLORS lookup in Python."""
    m = re.search(rf'\b{re.escape(rel_type)}\s*:\s*"(#[0-9A-Fa-f]+)"', relationship_colors_block)
    return m.group(1) if m else default_color


def test_e2e_viewer_differentiates_typed_and_untyped_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S34/S35: over the same node pair, the served viewer colours the typed edge
    differently from its related_to twin and arrows only the typed edge."""
    install_graph_stub(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, cfg, api_key):
        asyncio.run(_seed_typed_and_related_to_twin(cfg.db_path, "testcol"))

        graph_resp = client.get("/graph/testcol", headers=_auth(api_key))
        view_resp = client.get("/graph/testcol/view", headers=_auth(api_key))

    assert graph_resp.status_code == 200, graph_resp.text
    assert view_resp.status_code == 200, view_resp.text

    edges = graph_resp.json()["edges"]
    edge_ids = [e["edge_id"] for e in edges]
    typed_edge = next(e for e in edges if e["edge_id"] == "edge-typed")
    related_edge = next(e for e in edges if e["edge_id"] == "edge-related-to")

    assert typed_edge["relationship_type"] == "uses"
    assert related_edge["relationship_type"] == "related_to"
    assert typed_edge["source_entity_id"] == related_edge["source_entity_id"] == "entity-a"
    assert typed_edge["target_entity_id"] == related_edge["target_entity_id"] == "entity-b"
    # Both edges co-occur in the same single shared mention chunk — equal weight is S35's premise.
    assert typed_edge["weight"] == related_edge["weight"]

    # BE-1's actual guarantee: with weights tied, _edge_sort_key breaks the tie toward the
    # typed edge — this is the assertion that would fail if the tie-break were reverted.
    assert edge_ids.index("edge-typed") < edge_ids.index("edge-related-to"), (
        f"Expected typed edge before related_to edge in BE-1-sorted response, got {edge_ids!r}"
    )

    html = view_resp.text
    m = re.search(r'const DEFAULT_EDGE_COLOR\s*=\s*"(#[0-9A-Fa-f]+)"', html)
    assert m is not None, "Expected 'const DEFAULT_EDGE_COLOR = \"#...\"' in HTML"
    default_edge_color = m.group(1)
    relationship_colors_block = _extract_json_like_block(html, "const RELATIONSHIP_COLORS")
    undirected_types = _extract_undirected_relationship_types(html)

    typed_color = _relationship_color(
        relationship_colors_block, typed_edge["relationship_type"], default_edge_color
    )
    related_color = _relationship_color(
        relationship_colors_block, related_edge["relationship_type"], default_edge_color
    )

    # Distinct colours: the typed edge gets its own colour, the untyped twin falls back.
    assert typed_color != related_color, (
        f"Typed edge colour {typed_color!r} must differ from related_to colour {related_color!r}"
    )
    assert related_color == default_edge_color, "related_to must fall back to DEFAULT_EDGE_COLOR"

    # Arrowhead: only the typed edge is directional.
    assert typed_edge["relationship_type"] not in undirected_types, "uses must be directional (arrowed)"
    assert related_edge["relationship_type"] in undirected_types, "related_to must be undirected (no arrowhead)"

    # buildVisEdge must actually consult RELATIONSHIP_COLORS and set arrows conditionally —
    # otherwise the constants above could be dead code that the viewer never reads.
    build_vis_edge_body = _extract_function_body(html, "buildVisEdge")
    assert "RELATIONSHIP_COLORS" in build_vis_edge_body, (
        "buildVisEdge must reference RELATIONSHIP_COLORS to colour edges"
    )
    assert re.search(
        r'if\s*\(\s*isDirectional\s*\)\s*\{[^}]*\.arrows\s*=\s*"to"', build_vis_edge_body,
    ), (
        "buildVisEdge must set edge.arrows = \"to\" only inside an 'if (isDirectional) { ... }' guard"
    )

    # C2-1: prove the served viewer actually wires these constants to the /graph/{collection}
    # endpoint's edges — otherwise everything above could be dead code the page never runs.
    assert 'fetch("/graph/" + collection' in html, (
        "Expected the viewer to fetch edges from '/graph/' + collection"
    )
    assert "edges.map(buildVisEdge)" in html, (
        "Expected the viewer to build vis-network edges via edges.map(buildVisEdge)"
    )

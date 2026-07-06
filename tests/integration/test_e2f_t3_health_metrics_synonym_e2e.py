"""E2f T-3 e2e test: after synonym enrichment, GET /status shows synonym_edge_count > 0
and GET /graph/{collection} shows relationship_type: "synonym_of" on synonym edges.

Scenarios covered:
- S6: GraphCollectionStats.synonym_edge_count > 0 after enrichment
- S7: GET /graph/{collection} edge responses include relationship_type="synonym_of"

Strategy:
- Install a content-dependent spaCy stub: returns "K8s" (ORG label → system type)
  only when "K8s" appears in text; returns "Kubernetes" (ORG label → system type) only
  when "Kubernetes" appears in text — so K8s-doc and Kubernetes-doc get different nodes.
  graph_extractor._LABEL_TO_ENTITY_TYPE maps "ORG" → EntityType.system.
- Start app via make_real_app(graph_enabled=True).
- Ingest K8s-doc and Kubernetes-doc to produce two distinct graph nodes.
- Backfill name_embedding on both nodes with near-identical vectors (cosine ≈ 0.9997, well above the 0.85 default threshold).
- Run SynonymDetector.detect() → writes synonym_of edge with extraction_method="embedding".
- GET /status → assert GraphCollectionStats.synonym_edge_count > 0.
- GET /graph/{collection} → assert at least one edge has relationship_type="synonym_of".
"""
from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Embedder stub (not used for synonym detection — stored embeddings take precedence)
# ---------------------------------------------------------------------------


def _make_stub_embedder():
    from archon_search.embedder import Embedder

    class _StubEmbedderBackend:
        model_name: str = "stub-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    return Embedder(_StubEmbedderBackend())


# ---------------------------------------------------------------------------
# Async helpers — fresh GraphStore connections independent from the app
# ---------------------------------------------------------------------------


async def _read_and_seed_embeddings(db_path: str, collection: str, ns: str) -> None:
    """Backfill name_embedding on K8s and Kubernetes nodes with near-identical vectors.

    K8s gets [0.9, 0.1, 0.0, 0.0]; Kubernetes gets [0.88, 0.12, 0.0, 0.0].
    Cosine similarity ≈ 0.9997 (well above the 0.85 default threshold).
    """
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        nodes = await gs.get_all_nodes(collection, ns=ns)
        assert nodes, (
            f"Expected nodes after ingest; got none (collection={collection!r}, ns={ns!r})"
        )
        updated_nodes = []
        for node in nodes:
            if node.entity_name.lower() == "k8s":
                updated_nodes.append(dataclasses.replace(node, name_embedding=[0.9, 0.1, 0.0, 0.0]))
            elif node.entity_name.lower() == "kubernetes":
                updated_nodes.append(dataclasses.replace(node, name_embedding=[0.88, 0.12, 0.0, 0.0]))
            else:
                updated_nodes.append(node)
        await gs.write_graph(collection, updated_nodes, [], ns=ns)
    finally:
        await gs.disconnect()


async def _run_synonym_detection(db_path: str, collection: str, ns: str, cfg) -> list:
    """Run SynonymDetector.detect() and write synonym_of edges. Returns edges written."""
    from archon_search.graph_store import GraphStore
    from archon_search.synonym_detector import SynonymDetector

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        embedder = _make_stub_embedder()
        detector = SynonymDetector(graph_store=gs, embedder=embedder, config=cfg)
        synonym_edges = await detector.detect(collection, ns=ns)
        if synonym_edges:
            await gs.write_graph(collection, [], synonym_edges, ns=ns)
        return synonym_edges
    finally:
        await gs.disconnect()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_e2e_health_metrics_reflect_synonym_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest → enrich → GET /status shows synonym_edge_count > 0; GET /graph shows relationship_type.

    S6: GraphCollectionStats.synonym_edge_count > 0 after real synonym enrichment.
    S7: GET /graph/{collection} edge responses include relationship_type="synonym_of".

    Steps:
    1. Install content-dependent spaCy stub.
    2. Start app with graph_enabled=True.
    3. Ingest K8s-doc (contains "K8s") → K8s system node written.
    4. Ingest Kubernetes-doc (contains "Kubernetes") → Kubernetes system node.
    5. Backfill name_embedding with near-identical vectors (cosine ≈ 0.9997, well above the 0.85 default threshold).
    6. Run SynonymDetector → writes synonym_of edge.
    7. GET /status → assert synonym_edge_count > 0 in the collection's GraphCollectionStats.
    8. GET /graph/{collection} → assert at least one edge has relationship_type="synonym_of".
    """
    from tests.integration.conftest import ingest_file_via_path, install_k8s_synonym_spacy_stub, make_real_app

    # Step 1: install content-dependent spaCy stub BEFORE make_real_app
    install_k8s_synonym_spacy_stub(monkeypatch)

    col = "t3-health-metrics"
    ns = "default"

    k8s_doc = tmp_path / "k8s-doc.md"
    k8s_doc.write_text(
        "# K8s Overview\n\nK8s is a container orchestration platform. "
        "K8s manages containerized applications.\n",
        encoding="utf-8",
    )
    kubernetes_doc = tmp_path / "kubernetes-doc.md"
    kubernetes_doc.write_text(
        "# Kubernetes Architecture\n\nKubernetes provides scheduling and management. "
        "Kubernetes clusters have control planes and worker nodes.\n",
        encoding="utf-8",
    )

    # Step 2: start app with graph_enabled=True
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True, maintenance_enabled=False) as (
        client,
        cfg,
        api_key,
    ):
        headers = {"Authorization": f"Bearer {api_key}"}
        db_path = cfg.db_path

        # Steps 3–4: ingest both documents
        ingest_file_via_path(client, col, str(k8s_doc), api_key=api_key)
        ingest_file_via_path(client, col, str(kubernetes_doc), api_key=api_key)

        # Step 5: backfill name_embedding (cosine ≈ 0.9997, well above the 0.85 default threshold)
        asyncio.run(_read_and_seed_embeddings(db_path, col, ns))

        # Step 6: run synonym detection — writes synonym_of edge
        synonym_edges = asyncio.run(_run_synonym_detection(db_path, col, ns, cfg))
        assert synonym_edges, (
            "SynonymDetector returned no edges — check that both K8s and Kubernetes nodes "
            "exist and have near-identical name_embedding vectors."
        )

        # Step 7: GET /status — assert synonym_edge_count > 0 (S6)
        resp = client.get("/status", headers=headers)
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "graph" in data and data["graph"] is not None, (
            "Expected 'graph' key in /status response when graph is enabled"
        )
        graph_status = data["graph"]
        assert "collections" in graph_status, (
            f"Expected 'collections' in graph status; got: {list(graph_status.keys())}"
        )

        col_entries = [c for c in graph_status["collections"] if c["collection"] == col]
        assert len(col_entries) == 1, (
            f"Expected 1 entry for collection {col!r} in /status; "
            f"got: {[c['collection'] for c in graph_status['collections']]}"
        )
        stats = col_entries[0]

        # S6: synonym_edge_count must be positive after enrichment
        assert "synonym_edge_count" in stats, (
            f"Expected 'synonym_edge_count' field in GraphCollectionStats; got fields: {list(stats.keys())}"
        )
        assert stats["synonym_edge_count"] == 1, (
            f"Expected synonym_edge_count == 1 after enrichment (fixture produces exactly 1 synonym pair); "
            f"got: {stats['synonym_edge_count']}. Full stats: {stats}"
        )

        # Also verify the other health metric fields are present (S6 completeness)
        assert "singleton_node_pct" in stats, (
            f"Expected 'singleton_node_pct' in GraphCollectionStats; got: {list(stats.keys())}"
        )
        # Both K8s and Kubernetes nodes are endpoints of the synonym edge, so no isolated nodes exist.
        assert stats["singleton_node_pct"] == pytest.approx(0.0), (
            "Both nodes are connected via the synonym edge; no singletons expected"
        )
        assert "synonym_link_rate" in stats, (
            f"Expected 'synonym_link_rate' in GraphCollectionStats; got: {list(stats.keys())}"
        )
        # Wiring check: synonym_link_rate is populated and non-zero.
        # Arithmetic correctness (formula = synonym_edge_count / edge_count) is verified by
        # test_be7_graph_health_and_relationship_type.py with a mixed-edge fixture (rate=0.5).
        # This fixture is intentionally degenerate (1 synonym / 1 total = 1.0) — sufficient
        # to confirm the field is wired and populated, not to stress the division.
        assert stats["synonym_link_rate"] == pytest.approx(1.0), (
            f"Expected synonym_link_rate == 1.0 (1 synonym edge / 1 total edge); "
            f"got: {stats['synonym_link_rate']}"
        )
        assert stats["node_count"] == 2, "Fixture produces exactly 2 entity nodes (K8s and Kubernetes)"
        assert stats["edge_count"] == 1, "Fixture produces exactly 1 edge (the synonym edge; no co-occurrence edges)"

        # Step 8: GET /graph/{collection} — assert relationship_type="synonym_of" present (S7)
        resp = client.get(f"/graph/{col}", headers=headers)
        assert resp.status_code == 200, f"GET /graph/{col} failed: {resp.status_code} {resp.text}"
        graph_data = resp.json()

        assert "edges" in graph_data, (
            f"Expected 'edges' key in GET /graph/{col} response; got: {list(graph_data.keys())}"
        )
        edges = graph_data["edges"]
        assert len(edges) > 0, (
            f"Expected at least one edge in GET /graph/{col}; got empty list"
        )

        # All edges must have relationship_type field
        for edge in edges:
            assert "relationship_type" in edge, (
                f"Edge {edge.get('edge_id')!r} is missing 'relationship_type' field; "
                f"edge keys: {list(edge.keys())}"
            )

        # S7: at least one edge must be a synonym_of edge
        synonym_edges_in_response = [
            e for e in edges if e["relationship_type"] == "synonym_of"
        ]
        assert len(synonym_edges_in_response) > 0, (
            f"Expected at least one edge with relationship_type='synonym_of' in GET /graph/{col}; "
            f"got relationship types: {[e['relationship_type'] for e in edges]}"
        )

        non_synonym_edges = [e for e in edges if e["relationship_type"] != "synonym_of"]
        # In this fixture, all edges are synonym edges (no co-occurrence edges produced)
        assert len(non_synonym_edges) == 0, (
            f"Expected no non-synonym edges in this fixture, got {len(non_synonym_edges)}"
        )

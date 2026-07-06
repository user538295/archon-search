"""E2f T-1 e2e test: ingest two synonym documents, run synonym enrichment, assert
search for one entity name returns content from the other's document.

Strategy:
- Install a content-dependent spaCy stub: returns "K8s" (ORG label → system type) only
  when "K8s" appears in text; returns "Kubernetes" (ORG label → system type) only when
  "Kubernetes" appears in text — so K8s-doc and Kubernetes-doc get different graph nodes.
  Note: graph_extractor.py _LABEL_TO_ENTITY_TYPE maps "ORG" → EntityType.system; "CONCEPT"
  is not in that map and would be silently discarded.
- Start app via make_real_app(graph_enabled=True).
- Ingest K8s-doc (text mentions "K8s" only) → K8s system node written.
- Ingest Kubernetes-doc (text mentions "Kubernetes" only) → Kubernetes system node.
- After ingest, read nodes via fresh GraphStore, backfill name_embedding with
  pre-seeded values that have cosine similarity > 0.85, re-write via write_graph.
- Run SynonymDetector.detect() → write synonym_of edge.
- Assert at least one synonym_of edge exists with extraction_method="embedding".
- POST /search?graph_mode=naive for query "K8s" → assert Kubernetes-doc content in results.
"""
from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Mock embedder (not used for synonym detection — stored embeddings take precedence)
# ---------------------------------------------------------------------------

def _make_embedder():
    from archon_search.embedder import Embedder

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts):
            return [[0.1] * 4 for _ in texts]

    return Embedder(_MockEmbedderBackend())


# ---------------------------------------------------------------------------
# Async helpers — run in a fresh GraphStore connection (never reuse server's store)
# ---------------------------------------------------------------------------

async def _read_and_seed_embeddings(db_path: str, collection: str, ns: str) -> None:
    """Read existing graph nodes and write back pre-seeded name_embeddings.

    K8s node gets [0.9, 0.1, 0.0, 0.0]; Kubernetes node gets [0.88, 0.12, 0.0, 0.0].
    Cosine similarity of these two vectors > 0.85, triggering synonym detection.
    """
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        nodes = await gs.get_all_nodes(collection, ns=ns)
        assert nodes, f"Expected nodes after ingest; got none (collection={collection!r}, ns={ns!r})"

        # Assign embeddings based on entity name
        updated_nodes = []
        for node in nodes:
            if node.entity_name.lower() == "k8s":
                updated_nodes.append(dataclasses.replace(node, name_embedding=[0.9, 0.1, 0.0, 0.0]))
            elif node.entity_name.lower() == "kubernetes":
                updated_nodes.append(dataclasses.replace(node, name_embedding=[0.88, 0.12, 0.0, 0.0]))
            else:
                updated_nodes.append(node)

        # Write back with embeddings set
        await gs.write_graph(collection, updated_nodes, [], ns=ns)
    finally:
        await gs.disconnect()


async def _run_synonym_detection(
    db_path: str,
    collection: str,
    ns: str,
    cfg,
) -> list:
    """Run SynonymDetector.detect() and write synonym_of edges. Returns edges written."""
    from archon_search.graph_store import GraphStore
    from archon_search.synonym_detector import SynonymDetector

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        embedder = _make_embedder()
        detector = SynonymDetector(graph_store=gs, embedder=embedder, config=cfg)
        synonym_edges = await detector.detect(collection, ns=ns)
        if synonym_edges:
            await gs.write_graph(collection, [], synonym_edges, ns=ns)
        return synonym_edges
    finally:
        await gs.disconnect()


async def _get_all_edges(db_path: str, collection: str, ns: str) -> list:
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        return await gs.get_all_edges(collection, ns=ns)
    finally:
        await gs.disconnect()


async def _get_all_nodes(db_path: str, collection: str, ns: str) -> list:
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        return await gs.get_all_nodes(collection, ns=ns)
    finally:
        await gs.disconnect()


async def _expand_query(db_path: str, collection: str, query: str, ns: str):
    """Run GraphExpander.expand() against the real graph store and return ExpandedQuery.

    This is the mechanism-based proof for S2: expansion_applied=True and
    neighbour_names_added containing "Kubernetes" proves the synonym_of edge was
    traversed, independent of search ranking under the zero-vector stub harness.
    """
    from archon_search.graph_expander import GraphExpander
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        expander = GraphExpander(graph_store=gs)
        return await expander.expand(query, collection=collection, ns=ns)
    finally:
        await gs.disconnect()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_e2e_search_traverses_synonym_edges(tmp_path: Path, monkeypatch) -> None:
    """Ingest K8s-doc and Kubernetes-doc; enrich synonyms; search K8s → returns Kubernetes content.

    Steps:
    1. Install content-dependent spaCy stub.
    2. Start app with graph_enabled=True.
    3. Ingest K8s-doc (contains "K8s" but not "Kubernetes").
    4. Ingest Kubernetes-doc (contains "Kubernetes" but not "K8s").
    5. Backfill name_embedding on both nodes with near-identical vectors (cosine > 0.85).
    6. Run SynonymDetector → writes synonym_of edge.
    7. Assert at least one synonym_of edge with extraction_method="embedding" exists.
    8. POST /search with graph_mode="naive" and query "K8s".
    9. Assert search results contain text from the Kubernetes document.
    """
    from tests.integration.conftest import ingest_file_via_path, install_k8s_synonym_spacy_stub, make_real_app

    # Step 1: install content-dependent spaCy stub BEFORE make_real_app
    install_k8s_synonym_spacy_stub(monkeypatch)

    col = "synonymdocs"
    ns = "default"

    # Write document files
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

        # Step 3: ingest K8s-doc
        ingest_file_via_path(client, col, str(k8s_doc), api_key=api_key)

        # Step 4: ingest Kubernetes-doc
        ingest_file_via_path(client, col, str(kubernetes_doc), api_key=api_key)

        db_path = cfg.db_path

        # Step 5: backfill name_embedding on graph nodes
        asyncio.run(_read_and_seed_embeddings(db_path, col, ns))

        # Verify both nodes exist before synonym detection
        all_nodes = asyncio.run(_get_all_nodes(db_path, col, ns))
        assert len(all_nodes) >= 2, (
            f"Expected at least 2 graph nodes (K8s, Kubernetes); got {len(all_nodes)}. "
            f"Node names: {[n.entity_name for n in all_nodes]}"
        )
        k8s_nodes = [n for n in all_nodes if n.entity_name.lower() == "k8s"]
        kubernetes_nodes = [n for n in all_nodes if n.entity_name.lower() == "kubernetes"]
        assert k8s_nodes, f"Expected K8s node; found nodes: {[n.entity_name for n in all_nodes]}"
        assert kubernetes_nodes, f"Expected Kubernetes node; found nodes: {[n.entity_name for n in all_nodes]}"

        # Step 6: run synonym detection and write synonym_of edges
        synonym_edges = asyncio.run(_run_synonym_detection(db_path, col, ns, cfg))

        # Step 7: assert at least one synonym_of edge with extraction_method="embedding"
        all_edges = asyncio.run(_get_all_edges(db_path, col, ns))
        from archon_search.graph_types import RelationshipType

        synonym_of_edges = [
            e
            for e in all_edges
            if e.relationship_type == RelationshipType.synonym_of
        ]
        assert len(synonym_of_edges) >= 1, (
            f"Expected at least one synonym_of edge after enrichment; "
            f"got {len(all_edges)} total edges. synonym_edges returned: {synonym_edges}"
        )
        embedding_method_edges = [
            e for e in synonym_of_edges if e.extraction_method == "embedding"
        ]
        assert len(embedding_method_edges) >= 1, (
            f"Expected synonym_of edge with extraction_method='embedding'; "
            f"edges found: {synonym_of_edges}"
        )

        # Step 8: prove naive expansion traverses the synonym edge (S2).
        # Call GraphExpander.expand() directly — the REST response exposes no expansion
        # signal, and the zero-vector stub embedder returns all chunks for any query
        # (content-presence in /search results is vacuous under this harness).
        # ExpandedQuery.expansion_applied=True and "Kubernetes" in neighbour_names_added
        # proves the synonym_of edge was actually traversed, independent of search ranking.
        expansion_result = asyncio.run(
            _expand_query(db_path, col, "K8s", ns)
        )
        assert expansion_result.expansion_applied, (
            "Expected GraphExpander to apply expansion for query 'K8s'; "
            f"expansion_applied={expansion_result.expansion_applied}, "
            f"expanded_text={expansion_result.expanded_text!r}"
        )
        assert any(
            "kubernetes" in name.lower()
            for name in expansion_result.neighbour_names_added
        ), (
            "Expected 'Kubernetes' in neighbour_names_added after synonym traversal; "
            f"got neighbour_names_added={expansion_result.neighbour_names_added!r}. "
            "This proves the synonym_of edge from K8s → Kubernetes was traversed."
        )

        # Step 9: also verify the end-to-end HTTP search returns 200 (smoke check).
        resp = client.post(
            "/search",
            json={"collection": col, "query": "K8s", "graph_mode": "naive"},
            headers=headers,
        )
        assert resp.status_code == 200, f"Search failed: {resp.status_code} {resp.text}"
        assert resp.json()["results"], "Expected search results; got none"

"""E2f T-2 e2e test: configure alias file with one synonym pair; trigger enrichment;
verify the manual edge exists and ``extraction_method="manual"``.

S5 scenario:
- Given an alias file configured with ``"K8s" = "Kubernetes"``
- When synonym enrichment runs (AliasLoader.load → write_graph)
- Then a ``synonym_of`` edge with ``extraction_method="manual"`` links the two nodes

Strategy:
- Install a content-dependent spaCy stub: returns "K8s" (ORG label → system type) only
  when "K8s" appears in text; returns "Kubernetes" (ORG label → system type) only when
  "Kubernetes" appears in text — so K8s-doc and Kubernetes-doc get different graph nodes.
  Note: graph_extractor.py _LABEL_TO_ENTITY_TYPE maps "ORG" → EntityType.system.
- Start app via make_real_app(graph_enabled=True) with alias_file in the TOML config.
- Ingest K8s-doc and Kubernetes-doc so both entities exist in the graph store.
- Run AliasLoader.load() + write_graph() directly (same pattern as T-1 uses for
  SynonymDetector.detect() directly) to trigger the alias-based enrichment.
- Query the graph store for all edges.
- Assert a synonym_of edge with extraction_method="manual" exists between the two nodes.

S13 is NOT tested here (non-existent alias file → WARNING only); that case is unit-tested
in the AliasLoader unit tests.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Async helpers — run in a fresh GraphStore connection
# ---------------------------------------------------------------------------


async def _run_alias_enrichment(
    db_path: str,
    collection: str,
    ns: str,
    alias_file_path: str,
) -> list:
    """Run AliasLoader.load() and write the resulting manual synonym edges.

    Returns the list of alias edges written.  Mirrors the T-1 pattern of
    calling the production component directly rather than through REST.
    """
    from archon_search.alias_loader import AliasLoader
    from archon_search.config import GraphConfig
    from archon_search.graph_store import GraphStore

    gs = GraphStore(db_path)
    await gs.connect()
    try:
        graph_cfg = GraphConfig()
        graph_cfg.alias_file = alias_file_path

        loader = AliasLoader(config=graph_cfg, graph_store=gs)
        alias_edges, _skip_pairs = await loader.load(collection, ns)

        if alias_edges:
            await gs.write_graph(collection, [], alias_edges, ns=ns)

        return alias_edges
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


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_e2e_alias_file_creates_manual_synonym_edge(
    tmp_path: Path, monkeypatch
) -> None:
    """S5: alias file with "K8s" = "Kubernetes" → synonym_of edge with extraction_method="manual".

    Steps:
    1. Write a TOML alias file containing ``"K8s" = "Kubernetes"``.
    2. Install content-dependent spaCy stub.
    3. Start app with graph_enabled=True.
    4. Ingest K8s-doc (contains "K8s") → K8s system node written.
    5. Ingest Kubernetes-doc (contains "Kubernetes") → Kubernetes system node written.
    6. Verify both nodes exist in the graph store.
    7. Run AliasLoader.load() + write_graph() → alias edge written.
    8. Assert a synonym_of edge with extraction_method="manual" exists.
    """
    from archon_search.graph_types import RelationshipType
    from tests.integration.conftest import ingest_file_via_path, install_k8s_synonym_spacy_stub, make_real_app

    # Step 1: write alias TOML file
    alias_toml = tmp_path / "aliases.toml"
    alias_toml.write_text('"K8s" = "Kubernetes"\n', encoding="utf-8")

    # Step 2: install content-dependent spaCy stub BEFORE make_real_app
    install_k8s_synonym_spacy_stub(monkeypatch)

    col = "aliasdocs"
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

    # Step 3: start app with graph_enabled=True
    with make_real_app(tmp_path, monkeypatch, graph_enabled=True, maintenance_enabled=False) as (
        client,
        cfg,
        api_key,
    ):
        # Step 4: ingest K8s-doc
        ingest_file_via_path(client, col, str(k8s_doc), api_key=api_key)

        # Step 5: ingest Kubernetes-doc
        ingest_file_via_path(client, col, str(kubernetes_doc), api_key=api_key)

        db_path = cfg.db_path

        # Step 6: verify both nodes exist before alias enrichment
        all_nodes = asyncio.run(_get_all_nodes(db_path, col, ns))
        assert all_nodes, (
            f"Expected graph nodes after ingest; got none (collection={col!r}, ns={ns!r})"
        )
        k8s_nodes = [n for n in all_nodes if n.entity_name.lower() == "k8s"]
        kubernetes_nodes = [n for n in all_nodes if n.entity_name.lower() == "kubernetes"]
        assert k8s_nodes, (
            f"Expected K8s node; found nodes: {[n.entity_name for n in all_nodes]}"
        )
        assert kubernetes_nodes, (
            f"Expected Kubernetes node; found nodes: {[n.entity_name for n in all_nodes]}"
        )
        k8s_node_id = k8s_nodes[0].id
        kubernetes_node_id = kubernetes_nodes[0].id

        # Step 7: run alias enrichment — load alias file and write manual synonym edge
        alias_edges = asyncio.run(
            _run_alias_enrichment(db_path, col, ns, str(alias_toml))
        )
        assert alias_edges, (
            "AliasLoader.load() returned no edges — check that both K8s and Kubernetes "
            "nodes exist and share the same entity_type. "
            f"Nodes found: {[n.entity_name for n in all_nodes]}"
        )

        # Step 8: assert the manual synonym_of edge exists
        all_edges = asyncio.run(_get_all_edges(db_path, col, ns))
        assert all_edges, (
            f"Expected edges after alias enrichment; got none (collection={col!r})"
        )

        synonym_of_edges = [
            e for e in all_edges if e.relationship_type == RelationshipType.synonym_of
        ]
        assert len(synonym_of_edges) == 1, (
            f"Expected exactly one synonym_of edge; "
            f"got {len(synonym_of_edges)} with types: "
            f"{[e.relationship_type for e in all_edges]}"
        )

        manual_edges = [
            e for e in synonym_of_edges if e.extraction_method == "manual"
        ]
        assert len(manual_edges) == 1, (
            f"Expected exactly one synonym_of edge with extraction_method='manual'; "
            f"found edges: {[(e.relationship_type, e.extraction_method) for e in all_edges]}"
        )

        # Verify the edge connects K8s ↔ Kubernetes (not some other pair)
        manual_edge = manual_edges[0]
        assert {manual_edge.source_node_id, manual_edge.target_node_id} == {k8s_node_id, kubernetes_node_id}, (
            f"Expected manual synonym_of edge to connect K8s ({k8s_node_id!r}) ↔ Kubernetes ({kubernetes_node_id!r}); "
            f"got source={manual_edge.source_node_id!r}, target={manual_edge.target_node_id!r}"
        )

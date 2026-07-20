"""Pytest fixtures and CLI options for the eval slice.

This module:

- Registers the ``--thresholds-path`` pytest CLI option used by +
  gated smoke tests.
- Provides module-scoped fixtures for the eval corpus and a temporary
  LanceDB root.
- Activates the deterministic eval backends from
  :mod:`archon_search.eval.backends` for every eval test so the suite never
  needs to download real embedding or reranker models.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from archon_search.eval.backends import EvalEmbedderBackend, EvalRerankerBackend
from archon_search.eval.fixtures import EvalCorpus, load_eval_corpus


CORPUS_ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# --thresholds-path CLI option
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--thresholds-path",
        action="store",
        default=None,
        help=(
            "Path to thresholds.toml for gated eval smoke tests. "
            "Must be passed explicitly by CI; not auto-discovered."
        ),
    )


@pytest.fixture(scope="session")
def thresholds_path(pytestconfig: pytest.Config) -> Path:
    """Return the path passed via ``--thresholds-path``.

    - In CI (``CI`` env var set): ``pytest.fail`` so misconfiguration is loud.
    - Locally: ``pytest.skip`` with guidance.
    """
    raw = pytestconfig.getoption("--thresholds-path")
    if raw is None:
        if os.environ.get("CI"):
            pytest.fail(
                "thresholds-path not provided in CI — pass --thresholds-path explicitly"
            )
        pytest.skip(
            "thresholds-path not provided; use -k 'not gated' for report-only mode "
            "or pass --thresholds-path for gated mode."
        )
    return Path(raw)


# ---------------------------------------------------------------------------
# Corpus + LanceDB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def eval_corpus() -> EvalCorpus:
    """Load the committed eval corpus once per module."""
    return load_eval_corpus(CORPUS_ROOT)


@pytest.fixture(scope="module")
def eval_tmp_lancedb_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Module-scoped temporary directory for a fresh LanceDB store."""
    return tmp_path_factory.mktemp("eval_lancedb")


# ---------------------------------------------------------------------------
# Deterministic backend activation (autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _activate_deterministic_eval_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activate deterministic eval backends for every eval test.

    Exposes the backend instances via env-var sentinels so callers that build
    pipelines can pick them up without importing test code, and also installs
    them on a shared registry attribute used by the harness when present.
    """
    embedder = EvalEmbedderBackend()
    reranker = EvalRerankerBackend()

    monkeypatch.setenv("ARCHON_SEARCH_EVAL_BACKENDS", "1")

    # Best-effort: if the eval backends module exposes a registry hook, set it.
    import archon_search.eval.backends as backends_mod

    monkeypatch.setattr(
        backends_mod, "_ACTIVE_EMBEDDER", embedder, raising=False
    )
    monkeypatch.setattr(
        backends_mod, "_ACTIVE_RERANKER", reranker, raising=False
    )


# ---------------------------------------------------------------------------
# Community builder fixture for multi-hop eval collections (BE-6)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def build_communities_for_eval(eval_tmp_lancedb_root: Path):
    """Build communities for multi-hop eval collections and synonym edges for synonym-bridge.

    Skips if leidenalg is not installed (graph extras absent).

    Pre-builds graph data for:
    1. multihop-musique and multihop-2wiki: entity nodes + related_to edges for Leiden
       community detection (deferred to run_eval_suite after corpus ingest).
    2. synonym-bridge: explicit synonym_of edges connecting K8s↔Kubernetes and
       ML↔machine learning. These edges are written before run_eval_suite ingests the
       corpus so that RealGraphExpander can expand queries across synonym pairs.

    Module-scoped so it runs once per test module before any test that imports it.

    Yields: (lancedb_root, dict[collection_name, GraphStore])
    """
    import asyncio
    import hashlib

    pytest.importorskip("leidenalg")  # Skip if graph extras absent

    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import (
        EntityType,
        GraphEdge,
        GraphNode,
        RelationshipType,
        make_stable_edge_id,
        make_stable_entity_id,
    )

    async def _build_communities():
        # Create GraphStore for the eval temp directory
        graph_store = GraphStore(db_path=str(eval_tmp_lancedb_root))
        await graph_store.connect()

        try:
            ns = "default"
            graph_stores = {}  # collection_name -> GraphStore

            # Process each multihop collection
            for collection_name in ["multihop-musique", "multihop-2wiki"]:
                corpus_dir = CORPUS_ROOT / "corpus" / collection_name
                if not corpus_dir.exists():
                    continue

                # Ensure graph tables exist
                await graph_store.ensure_graph_tables(collection_name, ns=ns)

                # Ingest documents from corpus
                all_nodes = {}  # entity_id -> node (dedup across documents)
                all_edges = []
                for txt_file in sorted(corpus_dir.glob("*.txt")):
                    text = txt_file.read_text(encoding="utf-8")
                    # Compute source_doc_id using the same SHA-256 hash that
                    # pipeline.ingest_file uses (line 377 of pipeline.py), so
                    # CommunityBuilder.get_chunks_for_doc finds the right chunks.
                    source_doc_id = hashlib.sha256(
                        str(txt_file.resolve()).encode()
                    ).hexdigest()
                    # Extract entity names from text (basic approach: capitalized words)
                    words = text.split()
                    entity_names = [
                        w.rstrip(":.,'\"") for w in words if w and w[0].isupper()
                    ]

                    # Create graph nodes for entities found in the text
                    doc_nodes = []
                    for entity_name in entity_names[:5]:  # limit to 5 per document for speed
                        entity_id = make_stable_entity_id("concept", entity_name)
                        if entity_id not in all_nodes:
                            node = GraphNode(
                                id=entity_id,
                                entity_name=entity_name,
                                entity_type=EntityType.concept,
                                source_doc_id=source_doc_id,
                                collection_name=collection_name,
                            )
                            all_nodes[entity_id] = node
                        doc_nodes.append(all_nodes[entity_id])

                    # Create some edges between consecutive nodes
                    for i in range(len(doc_nodes) - 1):
                        edge = GraphEdge(
                            id=make_stable_edge_id(
                                doc_nodes[i].id, doc_nodes[i + 1].id, "related_to"
                            ),
                            source_node_id=doc_nodes[i].id,
                            target_node_id=doc_nodes[i + 1].id,
                            relationship_type=RelationshipType.related_to,
                            source_doc_id=source_doc_id,
                        )
                        all_edges.append(edge)

                # Write all collected nodes and edges to graph store
                if all_nodes:
                    await graph_store.write_graph(
                        collection_name, list(all_nodes.values()), all_edges, ns=ns
                    )

                # NOTE: Communities are NOT built here. Building requires chunks in the
                # search_store (for MMR representative selection), but chunks are only
                # available after _ingest_corpus runs inside run_eval_suite. Community
                # building is deferred to run_eval_suite, after _ingest_corpus completes.

                graph_stores[collection_name] = graph_store

            # ------------------------------------------------------------------
            # synonym-bridge: write synonym_of edges so RealGraphExpander can
            # bridge queries that use one term to documents that use the other.
            #
            # Corpus design (BE-8) — 12 docs total (4 synonym pairs + 8 distractors):
            #
            #   Doc ID                          File                          Content term
            #   synonym-bridge-kubernetes       kubernetes-overview.txt       "Kubernetes" only
            #   synonym-bridge-k8s              k8s-cluster-setup.txt         "K8s" only
            #   synonym-bridge-machine-learning ml-neural-networks.txt        "machine learning" only
            #   synonym-bridge-ml-abbrev        ml-abbreviation-guide.txt     "ML" only
            #
            #   Distractor docs (numeric IDs, orthogonal topics):
            #   synonym-bridge-001              sql-database-indexing.txt
            #   synonym-bridge-002              python-packaging.txt
            #   synonym-bridge-003              docker-containers.txt
            #   synonym-bridge-004              react-state-management.txt
            #   synonym-bridge-005              git-branching-strategies.txt
            #   synonym-bridge-006              api-rate-limiting.txt
            #   synonym-bridge-007              rust-ownership-model.txt
            #   synonym-bridge-008              observability-tracing.txt
            #
            # Edges written here:
            #   Kubernetes --synonym_of--> K8s   (bidirectional via two edges)
            #   K8s --synonym_of--> Kubernetes
            #   machine learning --synonym_of--> ML
            #   ML --synonym_of--> machine learning
            #
            # RealGraphExpander.expand("Kubernetes container orchestration", collection):
            #   1. tokenize_and_generate_ngrams → ["Kubernetes", "container", ...]
            #   2. find_nodes_by_name → matches node "Kubernetes"
            #   3. get_neighbours(kubernetes_node) → returns node "K8s"
            #   4. expands query with "K8s" → K8s doc scores higher via lexical match
            # ------------------------------------------------------------------
            syn_collection = "synonym-bridge"
            await graph_store.ensure_graph_tables(syn_collection, ns=ns)

            # Use a stable dummy source_doc_id for the synonym nodes (they are
            # not tied to a real ingested document — they represent vocabulary entries).
            _SYN_SOURCE_DOC = "synonym-bridge-vocab-placeholder"

            def _synonym_pair(
                name_a: str,
                name_b: str,
                collection: str,
                source_doc_id: str,
            ) -> tuple[list[GraphNode], list[GraphEdge]]:
                """Construct nodes and bidirectional synonym_of edges for one synonym pair."""
                id_a = make_stable_entity_id("concept", name_a)
                id_b = make_stable_entity_id("concept", name_b)
                node_a = GraphNode(
                    id=id_a,
                    entity_name=name_a,
                    entity_type=EntityType.concept,
                    source_doc_id=source_doc_id,
                    collection_name=collection,
                )
                node_b = GraphNode(
                    id=id_b,
                    entity_name=name_b,
                    entity_type=EntityType.concept,
                    source_doc_id=source_doc_id,
                    collection_name=collection,
                )
                edge_a_b = GraphEdge(
                    id=make_stable_edge_id(id_a, id_b, "synonym_of"),
                    source_node_id=id_a,
                    target_node_id=id_b,
                    relationship_type=RelationshipType.synonym_of,
                    source_doc_id=source_doc_id,
                )
                edge_b_a = GraphEdge(
                    id=make_stable_edge_id(id_b, id_a, "synonym_of"),
                    source_node_id=id_b,
                    target_node_id=id_a,
                    relationship_type=RelationshipType.synonym_of,
                    source_doc_id=source_doc_id,
                )
                return [node_a, node_b], [edge_a_b, edge_b_a]

            k8s_nodes, k8s_edges = _synonym_pair(
                "K8s", "Kubernetes", syn_collection, _SYN_SOURCE_DOC
            )
            ml_nodes, ml_edges = _synonym_pair(
                "ML", "machine learning", syn_collection, _SYN_SOURCE_DOC
            )

            syn_nodes = k8s_nodes + ml_nodes
            syn_edges = k8s_edges + ml_edges
            await graph_store.write_graph(syn_collection, syn_nodes, syn_edges, ns=ns)
            graph_stores[syn_collection] = graph_store

            return eval_tmp_lancedb_root, graph_stores, graph_store

        except Exception:
            await graph_store.disconnect()
            raise

    # Run async code via asyncio.run (synchronous wrapper)
    lancedb_root, graph_stores, graph_store = asyncio.run(_build_communities())

    yield lancedb_root, graph_stores

    # Cleanup
    try:
        asyncio.run(graph_store.disconnect())
    except Exception:
        pass

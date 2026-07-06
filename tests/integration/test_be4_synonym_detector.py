"""Integration test for E2f BE-4: SynonymDetector writes real synonym_of edges to LanceDB.

Tests:
- test_synonym_detector_writes_edges_to_graph_store: detector writes real synonym_of edges
  into a real LanceDB nodes + edges table using a content-dependent embedder stub.

SynonymDetector uses all-pairs Python cosine over the embeddings returned by the Embedder —
no vector_search_nodes call — so no monkeypatching is needed.  The test exercises the full
get_all_nodes → Python cosine → write_graph round-trip against a real GraphStore.
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from archon_search.config import GraphConfig, SearchConfig
from archon_search.embedder import Embedder
from archon_search.graph_store import GraphStore
from archon_search.graph_types import (
    EntityType,
    GraphNode,
    RelationshipType,
    make_stable_entity_id,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

_EMBED_DIM = 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256_doc_id(name: str) -> str:
    """Return a valid SHA-256-hex-based chunk doc_id for test use."""
    return hashlib.sha256(name.encode()).hexdigest()


def _make_node(
    name: str,
    entity_type: EntityType = EntityType.concept,
    embedding: list[float] | None = None,
) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(entity_type.value, name),
        entity_name=name,
        entity_type=entity_type,
        source_doc_id=_sha256_doc_id(name) + "-000000",
        collection_name="test-col",
        name_embedding=embedding,
    )


class _ContentDependentEmbedderBackend:
    """Embedder backend that returns distinct, content-dependent vectors.

    For 'kubernetes' and 'k8s' → same direction (near-identical cosine).
    For 'unrelated' → orthogonal direction (cosine = 0).
    """

    model_name: str = "content-dependent-stub"
    is_warm: bool = True

    def encode(self, texts: list[str]) -> list[list[float]]:
        result = []
        for text in texts:
            t = text.lower()
            if t in ("kubernetes", "k8s"):
                result.append([1.0, 0.0, 0.0, 0.0])
            elif t == "unrelated":
                result.append([0.0, 1.0, 0.0, 0.0])
            else:
                result.append([0.5, 0.5, 0.0, 0.0])
        return result


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_synonym_detector_writes_edges_to_graph_store(tmp_path) -> None:
    """SynonymDetector writes real synonym_of edges into a real LanceDB store.

    Setup:
    - Three concept nodes: 'kubernetes' and 'k8s' (near-identical embeddings),
      'unrelated' (orthogonal embedding).
    - threshold = 0.85 → 'kubernetes' and 'k8s' should be linked; 'unrelated' should not.

    Steps:
    1. Create real GraphStore in tmp_path.
    2. Write nodes with embeddings directly.
    3. Run SynonymDetector.detect() — uses all-pairs Python cosine over the embedder's
       output (get_all_nodes for node list; no vector_search_nodes call).
    4. Caller writes resulting edges via graph_store.write_graph().
    5. Assert edges table contains exactly one synonym_of edge between the correct nodes.
    """
    from archon_search.synonym_detector import SynonymDetector

    node_k8s = _make_node("kubernetes", EntityType.concept, [1.0, 0.0, 0.0, 0.0])
    node_k8s_alias = _make_node("k8s", EntityType.concept, [1.0, 0.0, 0.0, 0.0])
    node_unrelated = _make_node("unrelated", EntityType.concept, [0.0, 1.0, 0.0, 0.0])

    embedder = Embedder(_ContentDependentEmbedderBackend())
    cfg = SearchConfig()
    cfg.graph = GraphConfig(synonym_threshold=0.85)

    async def _run() -> None:
        gs = GraphStore(str(tmp_path))
        await gs.connect()
        try:
            await gs.ensure_graph_tables("test-col", ns="default")
            # Write nodes with embeddings so get_all_nodes can read them back.
            await gs.write_graph(
                "test-col",
                [node_k8s, node_k8s_alias, node_unrelated],
                [],
                ns="default",
            )

            detector = SynonymDetector(graph_store=gs, embedder=embedder, config=cfg)
            synonym_edges = await detector.detect("test-col", ns="default")

            # Caller writes edges — callers' responsibility per spec
            await gs.write_graph("test-col", [], synonym_edges, ns="default")

            all_edges = await gs.get_all_edges("test-col", ns="default")
        finally:
            await gs.disconnect()

        synonym_only = [
            e for e in all_edges if e.relationship_type == RelationshipType.synonym_of
        ]

        assert len(synonym_only) == 1, (
            f"Expected exactly 1 synonym_of edge, got {len(synonym_only)}: "
            f"{[(e.source_node_id, e.target_node_id) for e in synonym_only]}"
        )

        edge = synonym_only[0]
        assert {edge.source_node_id, edge.target_node_id} == {node_k8s.id, node_k8s_alias.id}, (
            f"Synonym edge endpoints wrong: expected {{{node_k8s.id!r}, {node_k8s_alias.id!r}}}, "
            f"got {{{edge.source_node_id!r}, {edge.target_node_id!r}}}"
        )
        assert edge.extraction_method == "embedding"

        # 'unrelated' must not be linked to anything as a synonym
        unrelated_edges = [
            e for e in synonym_only
            if node_unrelated.id in (e.source_node_id, e.target_node_id)
        ]
        assert unrelated_edges == [], (
            f"'unrelated' node must not be in synonym edges, got {unrelated_edges}"
        )

    asyncio.run(_run())

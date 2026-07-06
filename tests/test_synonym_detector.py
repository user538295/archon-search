"""Unit tests for SynonymDetector — E2f BE-4.

Tests:
- test_synonym_detector_pairs_above_threshold: cosine ≥ threshold → synonym_of edge
- test_synonym_detector_cross_type_excluded: different entity types → no edge
- test_synonym_detector_below_threshold_and_no_error: high threshold → empty list, no exception
- test_synonym_detector_self_pairs_excluded: same entity ID not linked to itself
- test_synonym_detector_skips_alias_pairs: skip_pairs excludes those pairs regardless of similarity
- test_synonym_detector_canonical_ordering: (a,b) and (b,a) produce the same edge ID
- test_synonym_detector_skips_nodes_with_null_embedding: nodes with None name_embedding are skipped
- test_cosine_similarity_mismatched_lengths_returns_zero: length-mismatch guard returns 0.0
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.config import GraphConfig, SearchConfig
from archon_search.graph_types import (
    EntityType,
    GraphEdge,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIM = 4  # small embedding dimension for tests


def _make_config(synonym_threshold: float = 0.85) -> SearchConfig:
    cfg = SearchConfig()
    cfg.graph = GraphConfig(synonym_threshold=synonym_threshold)
    return cfg


def _make_node(
    name: str,
    entity_type: EntityType = EntityType.concept,
    embedding: list[float] | None = None,
) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(entity_type.value, name),
        entity_name=name,
        entity_type=entity_type,
        source_doc_id="doc-test",
        collection_name="test-col",
        name_embedding=embedding,
    )


def _make_embedder_mock(
    name_to_embedding: dict[str, list[float]],
) -> MagicMock:
    """Return an Embedder mock whose .embed(texts) returns vectors keyed by name."""
    embedder = MagicMock()
    embedder.embedding_dim = _DIM

    async def _embed(texts: list[str]) -> list[list[float]]:
        result = []
        for t in texts:
            result.append(name_to_embedding.get(t, [0.0] * _DIM))
        return result

    embedder.embed = _embed
    return embedder


def _make_graph_store_mock(
    nodes: list[GraphNode],
    vector_search_result: list[GraphNode] | None = None,
) -> MagicMock:
    """Return a GraphStoreProtocol mock with configurable get_all_nodes and vector_search_nodes."""
    store = MagicMock()
    store.get_all_nodes = AsyncMock(return_value=nodes)
    if vector_search_result is None:
        # By default, vector_search_nodes returns all nodes (simulates a complete ANN result)
        store.vector_search_nodes = AsyncMock(return_value=nodes)
    else:
        store.vector_search_nodes = AsyncMock(return_value=vector_search_result)
    store.write_graph = AsyncMock(return_value=None)
    store.find_nodes_by_name = AsyncMock(return_value=[])
    return store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_synonym_detector_pairs_above_threshold() -> None:
    """Cosine similarity >= threshold between two same-type nodes → synonym_of edge."""
    from archon_search.synonym_detector import SynonymDetector

    # Two concept nodes with identical embeddings → cosine = 1.0
    node_a = _make_node("kubernetes", EntityType.concept, [1.0, 0.0, 0.0, 0.0])
    node_b = _make_node("k8s", EntityType.concept, [1.0, 0.0, 0.0, 0.0])

    embeddings = {"kubernetes": [1.0, 0.0, 0.0, 0.0], "k8s": [1.0, 0.0, 0.0, 0.0]}
    embedder = _make_embedder_mock(embeddings)

    # vector_search_nodes returns both nodes for any query
    store = _make_graph_store_mock([node_a, node_b], vector_search_result=[node_a, node_b])

    cfg = _make_config(synonym_threshold=0.85)
    detector = SynonymDetector(graph_store=store, embedder=embedder, config=cfg)

    edges = asyncio.run(detector.detect("test-col", ns="default"))

    assert len(edges) == 1
    edge = edges[0]
    assert edge.relationship_type == RelationshipType.synonym_of
    assert edge.extraction_method == "embedding"
    # Both node IDs must be endpoint of this edge
    assert {edge.source_node_id, edge.target_node_id} == {node_a.id, node_b.id}


def test_synonym_detector_cross_type_excluded() -> None:
    """Nodes of different entity_types are never linked as synonyms.

    With all-pairs cosine, nodes are grouped by type before iteration — so cross-type
    pairs structurally cannot form.  This test verifies the invariant: 2 concept nodes
    + 1 person node, all with identical embeddings and threshold=0.5 → only the 2
    concept nodes are linked; the person node is not linked to anything.
    """
    from archon_search.synonym_detector import SynonymDetector

    node_concept_a = _make_node("mercury", EntityType.concept, [1.0, 0.0, 0.0, 0.0])
    node_concept_b = _make_node("venus", EntityType.concept, [1.0, 0.0, 0.0, 0.0])
    node_person = _make_node("mercury", EntityType.person, [1.0, 0.0, 0.0, 0.0])

    embeddings = {
        "mercury": [1.0, 0.0, 0.0, 0.0],
        "venus": [1.0, 0.0, 0.0, 0.0],
    }
    embedder = _make_embedder_mock(embeddings)

    store = _make_graph_store_mock([node_concept_a, node_concept_b, node_person])

    cfg = _make_config(synonym_threshold=0.5)
    detector = SynonymDetector(graph_store=store, embedder=embedder, config=cfg)

    edges = asyncio.run(detector.detect("test-col", ns="default"))

    # The two concept nodes must be linked (cosine = 1.0 >= 0.5)
    concept_edges = [
        e for e in edges
        if {e.source_node_id, e.target_node_id} == {node_concept_a.id, node_concept_b.id}
    ]
    assert len(concept_edges) == 1, f"Expected 1 concept-concept edge, got {concept_edges}"

    # The person node must not appear in any edge
    person_edges = [
        e for e in edges
        if node_person.id in (e.source_node_id, e.target_node_id)
    ]
    assert person_edges == [], f"Expected no edges involving person node, got {person_edges}"


def test_synonym_detector_below_threshold_and_no_error() -> None:
    """High synonym_threshold with pairs far below it → empty list, no exception (S11)."""
    from archon_search.synonym_detector import SynonymDetector

    # Orthogonal embeddings → cosine = 0.0
    node_a = _make_node("alpha", EntityType.concept, [1.0, 0.0, 0.0, 0.0])
    node_b = _make_node("beta", EntityType.concept, [0.0, 1.0, 0.0, 0.0])

    embeddings = {"alpha": [1.0, 0.0, 0.0, 0.0], "beta": [0.0, 1.0, 0.0, 0.0]}
    embedder = _make_embedder_mock(embeddings)

    store = _make_graph_store_mock([node_a, node_b], vector_search_result=[node_a, node_b])

    cfg = _make_config(synonym_threshold=0.99)
    detector = SynonymDetector(graph_store=store, embedder=embedder, config=cfg)

    # Must not raise; must return empty list
    edges = asyncio.run(detector.detect("test-col", ns="default"))
    assert edges == []


def test_synonym_detector_self_pairs_excluded() -> None:
    """A node is never linked to itself as a synonym.

    With index-based all-pairs iteration (i+1 slice), self-pairs structurally cannot
    form.  This test verifies the invariant: 2 nodes in the same type group with
    threshold=0.0 → exactly 1 edge between them, and no self-edges exist.
    """
    from archon_search.synonym_detector import SynonymDetector

    node_a = _make_node("kubernetes", EntityType.concept, [1.0, 0.0, 0.0, 0.0])
    node_b = _make_node("k8s", EntityType.concept, [1.0, 0.0, 0.0, 0.0])

    embeddings = {"kubernetes": [1.0, 0.0, 0.0, 0.0], "k8s": [1.0, 0.0, 0.0, 0.0]}
    embedder = _make_embedder_mock(embeddings)

    store = _make_graph_store_mock([node_a, node_b])

    cfg = _make_config(synonym_threshold=0.0)
    detector = SynonymDetector(graph_store=store, embedder=embedder, config=cfg)

    edges = asyncio.run(detector.detect("test-col", ns="default"))

    # The two nodes must be linked (cosine = 1.0 >= 0.0)
    assert len(edges) == 1, f"Expected exactly 1 edge between the two nodes, got {edges}"

    # No self-edges must exist in any scenario
    self_edges = [e for e in edges if e.source_node_id == e.target_node_id]
    assert self_edges == [], f"Self-pairs should be excluded, got {self_edges}"


def test_synonym_detector_skips_alias_pairs() -> None:
    """Pairs in skip_pairs (in either order) are excluded regardless of similarity."""
    from archon_search.synonym_detector import SynonymDetector

    node_a = _make_node("kubernetes", EntityType.concept, [1.0, 0.0, 0.0, 0.0])
    node_b = _make_node("k8s", EntityType.concept, [1.0, 0.0, 0.0, 0.0])

    embeddings = {"kubernetes": [1.0, 0.0, 0.0, 0.0], "k8s": [1.0, 0.0, 0.0, 0.0]}
    embedder = _make_embedder_mock(embeddings)

    store = _make_graph_store_mock([node_a, node_b], vector_search_result=[node_a, node_b])

    cfg = _make_config(synonym_threshold=0.5)
    detector = SynonymDetector(graph_store=store, embedder=embedder, config=cfg)

    # Skip the pair in canonical order
    a_id, b_id = node_a.id, node_b.id
    canonical = (min(a_id, b_id), max(a_id, b_id))
    skip_pairs = {canonical}

    edges = asyncio.run(detector.detect("test-col", ns="default", skip_pairs=skip_pairs))
    assert edges == [], f"Skipped alias pair should not appear in edges, got {edges}"

    # Also test skip in reverse order — both orderings must be excluded
    skip_pairs_reversed = {(canonical[1], canonical[0])}
    edges_reversed = asyncio.run(
        detector.detect("test-col", ns="default", skip_pairs=skip_pairs_reversed)
    )
    assert edges_reversed == [], f"Reversed skip pair should also be excluded, got {edges_reversed}"


def test_synonym_detector_zero_magnitude_embedding_no_error() -> None:
    """One node with a zero-vector embedding → no exception and no edge produced.

    A zero-vector embedding has zero magnitude; _cosine_similarity returns 0.0,
    which is below any positive threshold, so the pair must not be linked.
    """
    from archon_search.synonym_detector import SynonymDetector

    node_a = _make_node("kubernetes", EntityType.concept, [0.0, 0.0, 0.0, 0.0])
    node_b = _make_node("k8s", EntityType.concept, [1.0, 0.0, 0.0, 0.0])

    embedder = _make_embedder_mock({})

    store = _make_graph_store_mock([node_a, node_b], vector_search_result=[node_a, node_b])

    cfg = _make_config(synonym_threshold=0.85)
    detector = SynonymDetector(graph_store=store, embedder=embedder, config=cfg)

    # Must not raise; cosine(zero_vec, any) == 0.0 < 0.85 → no edge
    edges = asyncio.run(detector.detect("test-col", ns="default"))
    assert edges == [], f"Zero-magnitude embedding should not produce a synonym edge, got {edges}"


def test_synonym_detector_canonical_ordering() -> None:
    """Detecting (a,b) and then (b,a) must produce the same edge ID (idempotency)."""
    from archon_search.synonym_detector import SynonymDetector

    node_a = _make_node("kubernetes", EntityType.concept, [1.0, 0.0, 0.0, 0.0])
    node_b = _make_node("k8s", EntityType.concept, [1.0, 0.0, 0.0, 0.0])

    embeddings = {"kubernetes": [1.0, 0.0, 0.0, 0.0], "k8s": [1.0, 0.0, 0.0, 0.0]}

    embedder_ab = _make_embedder_mock(embeddings)
    # Run 1: vector search from node_a's perspective returns [node_a, node_b]
    store_ab = _make_graph_store_mock([node_a, node_b], vector_search_result=[node_a, node_b])
    cfg = _make_config(synonym_threshold=0.5)
    detector_ab = SynonymDetector(graph_store=store_ab, embedder=embedder_ab, config=cfg)
    edges_ab = asyncio.run(detector_ab.detect("test-col", ns="default"))

    embedder_ba = _make_embedder_mock(embeddings)
    # Run 2: nodes in reversed order in the store
    store_ba = _make_graph_store_mock([node_b, node_a], vector_search_result=[node_b, node_a])
    detector_ba = SynonymDetector(graph_store=store_ba, embedder=embedder_ba, config=cfg)
    edges_ba = asyncio.run(detector_ba.detect("test-col", ns="default"))

    assert len(edges_ab) == 1
    assert len(edges_ba) == 1

    # Both runs must produce the same stable edge ID
    assert edges_ab[0].id == edges_ba[0].id, (
        f"Edge IDs differ: {edges_ab[0].id!r} != {edges_ba[0].id!r}"
    )


def test_synonym_detector_skips_nodes_with_null_embedding() -> None:
    """Nodes with name_embedding=None are skipped; only nodes with embeddings form edges.

    3 concept nodes: node_a and node_b have embeddings, node_null has None.
    With threshold=0.5 and identical embeddings on a/b → exactly 1 edge (a-b).
    node_null must not appear in any edge.
    """
    from archon_search.synonym_detector import SynonymDetector

    node_a = _make_node("kubernetes", EntityType.concept, [1.0, 0.0, 0.0, 0.0])
    node_b = _make_node("k8s", EntityType.concept, [1.0, 0.0, 0.0, 0.0])
    node_null = _make_node("container-orchestrator", EntityType.concept, embedding=None)

    embedder = _make_embedder_mock(
        {"kubernetes": [1.0, 0.0, 0.0, 0.0], "k8s": [1.0, 0.0, 0.0, 0.0]}
    )
    store = _make_graph_store_mock([node_a, node_b, node_null])

    cfg = _make_config(synonym_threshold=0.5)
    detector = SynonymDetector(graph_store=store, embedder=embedder, config=cfg)

    edges = asyncio.run(detector.detect("test-col", ns="default"))

    # Exactly 1 edge — between node_a and node_b
    assert len(edges) == 1, f"Expected exactly 1 edge, got {edges}"
    assert {edges[0].source_node_id, edges[0].target_node_id} == {node_a.id, node_b.id}

    # node_null must not appear in any edge
    null_edges = [
        e for e in edges if node_null.id in (e.source_node_id, e.target_node_id)
    ]
    assert null_edges == [], f"node_null should not appear in any edge, got {null_edges}"


def test_cosine_similarity_mismatched_lengths_returns_zero() -> None:
    """_cosine_similarity returns 0.0 when vectors have different lengths.

    Guards against partial embeddings produced by stubs or mismatched model configs.
    """
    from archon_search.synonym_detector import _cosine_similarity

    # Mismatched lengths: 2 vs 3
    assert _cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0

    # Empty vs non-empty
    assert _cosine_similarity([], [1.0]) == 0.0

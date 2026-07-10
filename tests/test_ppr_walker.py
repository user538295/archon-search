"""Unit tests for PPRWalker — E2h BE-5.

All tests use AsyncMock for GraphStore async methods. The networkx PPR
computation runs synchronously in unit tests via asyncio.run(); actual CPU-bound
dispatch via asyncio.to_thread is tested separately.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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
from archon_search.ppr_walker import PPRWalkResult, PPRWalker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(name: str, entity_type: EntityType = EntityType.concept) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(entity_type.value, name),
        entity_name=name,
        entity_type=entity_type,
        source_doc_id="doc-test",
        collection_name="test-col",
    )


def _edge(src: GraphNode, tgt: GraphNode) -> GraphEdge:
    return GraphEdge(
        id=make_stable_edge_id(src.id, tgt.id, RelationshipType.related_to.value),
        source_node_id=src.id,
        target_node_id=tgt.id,
        relationship_type=RelationshipType.related_to,
        source_doc_id="doc-test",
    )


def _mention(node: GraphNode, chunk_id: str, doc_id: str = "doc-test") -> GraphMention:
    return GraphMention(entity_id=node.id, chunk_id=chunk_id, doc_id=doc_id)


def _make_store(
    *,
    all_nodes: list[GraphNode],
    all_edges: list[GraphEdge],
    named_nodes: list[GraphNode],
    mentions: list[GraphMention],
) -> MagicMock:
    """Build a mock GraphStore with the given responses."""
    store = MagicMock()
    store.get_all_nodes = AsyncMock(return_value=all_nodes)
    store.get_all_edges = AsyncMock(return_value=all_edges)
    store.find_nodes_by_name = AsyncMock(return_value=named_nodes)
    store.get_mentions_for_entity_ids = AsyncMock(return_value=mentions)
    return store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_pprWalker_seedsFromQueryNgrams_matchedEntityReturned() -> None:
    """1-gram and 2-gram seeding both produce matches."""
    node_k8s = _node("kubernetes")
    node_ml = _node("machine learning")
    hub = _node("hub")
    edge_k8s_hub = _edge(node_k8s, hub)
    edge_ml_hub = _edge(node_ml, hub)

    # Test 1: 1-gram "kubernetes" found from query "kubernetes deployment"
    store_1gram = _make_store(
        all_nodes=[node_k8s, hub],
        all_edges=[edge_k8s_hub],
        named_nodes=[node_k8s],
        mentions=[_mention(node_k8s, "chunk-k8s-1"), _mention(node_k8s, "chunk-k8s-2")],
    )
    walker = PPRWalker(store_1gram)
    result = asyncio.run(walker.walk("kubernetes deployment", "col", 0.85, 5, "default"))
    assert result.entities_matched == 1
    assert result.chunk_ids  # at least one chunk returned

    # Test 2: 2-gram "machine learning" found from query "machine learning inference"
    store_2gram = _make_store(
        all_nodes=[node_ml, hub],
        all_edges=[edge_ml_hub],
        named_nodes=[node_ml],
        mentions=[_mention(node_ml, "chunk-ml-1")],
    )
    walker2 = PPRWalker(store_2gram)
    result2 = asyncio.run(walker2.walk("machine learning inference", "col", 0.85, 5, "default"))
    assert result2.entities_matched == 1
    assert "chunk-ml-1" in result2.chunk_ids


def test_pprWalker_substringQuery_doesNotMatchExactEntity() -> None:
    """Query "kubernetesish" does NOT match entity "kubernetes" (exact match only)."""
    node_k8s = _node("kubernetes")
    store = _make_store(
        all_nodes=[node_k8s],
        all_edges=[],
        named_nodes=[],  # no match for "kubernetesish"
        mentions=[],
    )
    walker = PPRWalker(store)
    result = asyncio.run(walker.walk("kubernetesish", "col", 0.85, 5, "default"))
    assert result.entities_matched == 0
    assert result.chunk_ids == []
    assert result.entity_ids == []


def test_pprWalker_ngramDedup_duplicateTokensLookedUpOnce() -> None:
    """Repeated words in query produce deduplicated n-gram lookups."""
    node = _node("go")
    hub = _node("hub")
    store = _make_store(
        all_nodes=[node, hub],
        all_edges=[_edge(node, hub)],
        named_nodes=[node],
        mentions=[_mention(node, "chunk-1")],
    )
    walker = PPRWalker(store)

    asyncio.run(walker.walk("go go lang", "col", 0.85, 5, "default"))

    # find_nodes_by_name should be called once, with deduplicated n-grams
    store.find_nodes_by_name.assert_called_once()
    call_args = store.find_nodes_by_name.call_args
    ngrams_passed = call_args[0][1]  # positional: (collection, names, ns=...)
    # "go" should appear only once even though the query has it twice
    assert ngrams_passed.count("go") == 1


def test_pprWalker_personalizationWeightedByRawMentionRowCount() -> None:
    """Entity A with 3 mention rows gets weight 3, entity B with 1 gets weight 1."""
    node_a = _node("entity-alpha")
    node_b = _node("entity-beta")
    hub = _node("hub")
    edges = [_edge(node_a, hub), _edge(node_b, hub)]
    mentions = [
        _mention(node_a, "chunk-a-1"),
        _mention(node_a, "chunk-a-2"),
        _mention(node_a, "chunk-a-3"),
        _mention(node_b, "chunk-b-1"),
    ]

    captured_personalization: dict = {}

    def fake_pagerank(G, personalization=None, alpha=0.85):
        captured_personalization.update(personalization or {})
        # Return scores proportional to personalization
        return {k: v for k, v in (personalization or {}).items()}

    store = _make_store(
        all_nodes=[node_a, node_b, hub],
        all_edges=edges,
        named_nodes=[node_a, node_b],
        mentions=mentions,
    )
    walker = PPRWalker(store)

    with patch("networkx.pagerank", side_effect=fake_pagerank):
        asyncio.run(walker.walk("entity-alpha entity-beta", "col", 0.85, 5, "default"))

    # node_a has 3 mention rows, node_b has 1 → raw weights 3:1 before normalization
    weight_a = captured_personalization.get(node_a.id, 0)
    weight_b = captured_personalization.get(node_b.id, 0)
    # After normalization: weight_a / weight_b == 3
    assert weight_b > 0
    assert abs(weight_a / weight_b - 3.0) < 1e-9


def test_pprWalker_mentionCountFlipsEntityOrdering() -> None:
    """Symmetric graph topology: mention count alone determines chunk ordering."""
    import networkx as nx  # verify real PPR

    node_a = _node("alpha-entity")
    node_b = _node("beta-entity")
    hub = _node("hub-entity")

    # Symmetric topology: both A and B connect to hub with equal-weight edges
    edge_a_hub = _edge(node_a, hub)
    edge_b_hub = _edge(node_b, hub)
    all_nodes = [node_a, node_b, hub]
    all_edges = [edge_a_hub, edge_b_hub]

    def _run_case(a_mention_count: int, b_mention_count: int) -> list[str]:
        mentions = (
            [_mention(node_a, "chunk-a") for _ in range(a_mention_count)]
            + [_mention(node_b, "chunk-b") for _ in range(b_mention_count)]
        )
        store = _make_store(
            all_nodes=all_nodes,
            all_edges=all_edges,
            named_nodes=[node_a, node_b],
            mentions=mentions,
        )
        walker = PPRWalker(store)
        return asyncio.run(walker.walk(
            "alpha-entity beta-entity", "col", 0.85, 20, "default"
        )).chunk_ids

    # A has 3 mentions → chunk-a should come first
    chunks_a_first = _run_case(3, 1)
    assert len(chunks_a_first) >= 2
    assert chunks_a_first.index("chunk-a") < chunks_a_first.index("chunk-b")

    # Flip: B has 3 mentions → chunk-b should come first
    chunks_b_first = _run_case(1, 3)
    assert len(chunks_b_first) >= 2
    assert chunks_b_first.index("chunk-b") < chunks_b_first.index("chunk-a")


def test_pprWalker_noEntityMatch_returnsEmptyResult() -> None:
    """Query matching no node names → empty PPRWalkResult."""
    node = _node("some-other-entity")
    store = _make_store(
        all_nodes=[node],
        all_edges=[],
        named_nodes=[],  # no match
        mentions=[],
    )
    walker = PPRWalker(store)
    result = asyncio.run(walker.walk("completely different query", "col", 0.85, 5, "default"))
    assert result == PPRWalkResult(entity_ids=[], chunk_ids=[], entities_matched=0)


def test_pprWalker_topKRespectsPprTopEntities() -> None:
    """ppr_top_entities=3 limits PPR to top 3 entities even with 10 in graph.

    Topology: isolated nodes (no edges) so PPR degenerates to the personalization
    vector itself, making the top-3 exactly the 3 most-mentioned entities.
    Each entity is both a matched seed and has a unique mention → chunk.
    """
    nodes = [_node(f"entity-{i}") for i in range(10)]
    # No edges — isolated nodes; PPR rank == personalization weight (mention count).
    # Each entity has 1 mention to a distinct chunk.
    all_mentions = [_mention(n, f"chunk-{i}") for i, n in enumerate(nodes)]

    # Fix C1-I-20: use side_effect to differentiate the two get_mentions_for_entity_ids calls:
    # first call (seeding) may receive any subset, second call (chunk resolution) receives top-3 ids.
    def mentions_side_effect(collection, entity_ids, *, ns):
        return [m for m in all_mentions if m.entity_id in entity_ids]

    store = MagicMock()
    store.get_all_nodes = AsyncMock(return_value=nodes)
    store.get_all_edges = AsyncMock(return_value=[])
    store.find_nodes_by_name = AsyncMock(return_value=nodes)
    store.get_mentions_for_entity_ids = AsyncMock(side_effect=mentions_side_effect)

    walker = PPRWalker(store)
    result = asyncio.run(walker.walk(
        " ".join(f"entity-{i}" for i in range(10)),
        "col",
        0.85,
        3,  # ppr_top_entities=3
        "default",
    ))
    # Exactly 3 entities contribute chunks (each contributes 1 unique chunk here)
    assert len(result.chunk_ids) == 3, f"Expected 3 chunk_ids, got {len(result.chunk_ids)}: {result.chunk_ids}"
    assert result.entities_matched > 0


def test_pprWalker_networkxRunsInToThread() -> None:
    """asyncio.to_thread is invoked once for the networkx PPR computation."""
    node = _node("alpha")
    hub = _node("hub")
    store = _make_store(
        all_nodes=[node, hub],
        all_edges=[_edge(node, hub)],
        named_nodes=[node],
        mentions=[_mention(node, "chunk-1")],
    )
    walker = PPRWalker(store)

    async def _run() -> PPRWalkResult:
        with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            # Return a plausible result from the sync helper
            mock_thread.return_value = [node.id]
            result = await walker.walk("alpha", "col", 0.85, 5, "default")
            mock_thread.assert_called_once()
            return result

    asyncio.run(_run())


def test_pprWalker_personalizationVectorSumsToOne() -> None:
    """The personalization vector passed to networkx.pagerank sums to 1.0."""
    node_a = _node("node-aaa")
    node_b = _node("node-bbb")
    hub = _node("hub-node")
    edges = [_edge(node_a, hub), _edge(node_b, hub)]
    mentions = [
        _mention(node_a, "chunk-a"),
        _mention(node_a, "chunk-a2"),
        _mention(node_b, "chunk-b"),
    ]

    captured: dict[str, object] = {}

    def fake_pagerank(G, personalization=None, alpha=0.85):
        captured["personalization"] = dict(personalization or {})
        return {k: v for k, v in (personalization or {}).items()}

    store = _make_store(
        all_nodes=[node_a, node_b, hub],
        all_edges=edges,
        named_nodes=[node_a, node_b],
        mentions=mentions,
    )
    walker = PPRWalker(store)

    with patch("networkx.pagerank", side_effect=fake_pagerank):
        asyncio.run(walker.walk("node-aaa node-bbb", "col", 0.85, 5, "default"))

    pers = captured.get("personalization", {})
    assert pers, "personalization vector must be non-empty"
    total = sum(pers.values())  # type: ignore[union-attr]
    assert abs(total - 1.0) < 1e-9, f"Expected sum=1.0, got {total}"


def test_pprWalker_zeroMentionEntities_fallsBackGracefully() -> None:
    """Entity matched in graph but zero mention rows → empty PPRWalkResult."""
    node = _node("some-entity")
    hub = _node("hub")
    store = _make_store(
        all_nodes=[node, hub],
        all_edges=[_edge(node, hub)],
        named_nodes=[node],  # entity found by name lookup
        mentions=[],  # but zero mention rows
    )
    walker = PPRWalker(store)
    result = asyncio.run(walker.walk("some-entity", "col", 0.85, 5, "default"))
    assert result == PPRWalkResult(entity_ids=[], chunk_ids=[], entities_matched=0)


# ---------------------------------------------------------------------------
# Fix C1-B-2: direct unit tests for _run_ppr_sync
# ---------------------------------------------------------------------------


def test_runPprSync_danglingEdgesExcluded_nodesOnlyInGraph() -> None:
    """_run_ppr_sync: dangling edges (referencing unknown node IDs) are excluded from the graph."""
    from archon_search.ppr_walker import _run_ppr_sync

    node_a = GraphNode(
        id=make_stable_entity_id("concept", "aaa"),
        entity_name="aaa",
        entity_type=EntityType.concept,
        source_doc_id="d",
        collection_name="c",
    )
    node_b = GraphNode(
        id=make_stable_entity_id("concept", "bbb"),
        entity_name="bbb",
        entity_type=EntityType.concept,
        source_doc_id="d",
        collection_name="c",
    )

    # Create a dangling edge that references a non-existent node ID
    dangling_edge = GraphEdge(
        id=make_stable_edge_id(node_a.id, "nonexistent-id-xyz", "related_to"),
        source_node_id=node_a.id,
        target_node_id="nonexistent-id-xyz",
        relationship_type=RelationshipType.related_to,
        source_doc_id="d",
    )

    personalization = {node_a.id: 1.0}
    result = _run_ppr_sync([node_a, node_b], [dangling_edge], personalization, 0.85, 10)

    # Both nodes should be returned (both are in the graph)
    assert node_a.id in result
    assert node_b.id in result
    # The dangling edge target must NOT be in the result
    assert "nonexistent-id-xyz" not in result


def test_runPprSync_convergenceFailure_returnsEmpty() -> None:
    """_run_ppr_sync: nx.PageRank convergence failure returns empty list gracefully."""
    import networkx as nx

    from archon_search.ppr_walker import _run_ppr_sync

    node = GraphNode(
        id=make_stable_entity_id("concept", "x"),
        entity_name="x",
        entity_type=EntityType.concept,
        source_doc_id="d",
        collection_name="c",
    )

    with patch("networkx.pagerank", side_effect=nx.PowerIterationFailedConvergence(100)):
        result = _run_ppr_sync([node], [], {node.id: 1.0}, 0.85, 5)

    assert result == []

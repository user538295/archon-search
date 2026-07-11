"""Unit tests for BE-8 — naive expansion cap in GraphExpander.

Tests verify:
- Cap is enforced before build_expanded_text (not after dedup)
- Fewer neighbours than cap: all are passed through
- Constructor stores naive_max_expansion_terms
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from archon_search.graph_types import (
    EntityType,
    GraphNode,
    make_stable_entity_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(name: str, etype: EntityType = EntityType.concept) -> GraphNode:
    return GraphNode(
        id=make_stable_entity_id(etype.value, name),
        entity_name=name,
        entity_type=etype,
        source_doc_id="doc-1",
        collection_name="test-col",
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_naiveCap_50Neighbours_cappedAtLimit() -> None:
    """50 distinct neighbour names with cap=20 → exactly 20 names in expanded text.

    All 50 names are unique and NOT present in the query ("x"), so dedup
    inside build_expanded_text cannot reduce the count.  The cap — not dedup
    — is the binding constraint; result must contain exactly 20 names.
    """
    from archon_search.graph_expander import GraphExpander
    from archon_search.graph_types import EntityType

    seed_node = _node("SeedEntity", EntityType.system)
    # 50 unique neighbour names that do not appear in the query "x"
    neighbour_nodes = [_node(f"Neighbour{i:03d}", EntityType.concept) for i in range(50)]

    mock_store = AsyncMock()
    mock_store.find_nodes_by_name = AsyncMock(return_value=[seed_node])
    mock_store.get_neighbours = AsyncMock(return_value=neighbour_nodes)

    expander = GraphExpander(graph_store=mock_store, naive_max_expansion_terms=20)
    result = await expander.expand("x", "col", ns="default")

    assert result.expansion_applied is True
    assert len(result.neighbour_names_added) == 20, (
        f"Expected exactly 20 neighbours added (cap), got {len(result.neighbour_names_added)}"
    )


@pytest.mark.asyncio
async def test_naiveCap_fewNeighbours_allReturned() -> None:
    """5 neighbours with cap=20 → all 5 are returned (cap is not the binding constraint)."""
    from archon_search.graph_expander import GraphExpander

    seed_node = _node("SeedEntity", EntityType.system)
    neighbour_nodes = [_node(f"Neighbour{i:03d}", EntityType.concept) for i in range(5)]

    mock_store = AsyncMock()
    mock_store.find_nodes_by_name = AsyncMock(return_value=[seed_node])
    mock_store.get_neighbours = AsyncMock(return_value=neighbour_nodes)

    expander = GraphExpander(graph_store=mock_store, naive_max_expansion_terms=20)
    result = await expander.expand("x", "col", ns="default")

    assert result.expansion_applied is True
    assert len(result.neighbour_names_added) == 5, (
        f"Expected all 5 neighbours added, got {len(result.neighbour_names_added)}"
    )


def test_naiveCap_graphExpander_acceptsConfig_inConstructor() -> None:
    """GraphExpander(graph_store, naive_max_expansion_terms=5) stores the limit."""
    from archon_search.graph_expander import GraphExpander

    mock_store = AsyncMock()
    expander = GraphExpander(graph_store=mock_store, naive_max_expansion_terms=5)
    assert expander._naive_max_expansion_terms == 5

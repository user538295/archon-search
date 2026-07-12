"""Unit tests for BE-1: entity_type field propagation through the graph inspection pipeline.

Tests (TDD — written before implementation):
  - test_node_inspection_includes_entity_type: GraphNodeInspection carries entity_type
  - test_view_to_response_maps_entity_type: _view_to_response propagates entity_type onto GraphNodeResponse
"""
from __future__ import annotations

import pytest

from archon_search.graph_inspector import GraphNodeInspection


# ---------------------------------------------------------------------------
# Unit: GraphNodeInspection carries entity_type
# ---------------------------------------------------------------------------


def test_node_inspection_includes_entity_type() -> None:
    """GraphNodeInspection carries entity_type from the underlying GraphNode.entity_type.value."""
    node = GraphNodeInspection(
        entity_id="abc123",
        entity_name="Alice",
        chunk_count=3,
        salience=0.5,
        entity_type="person",
    )
    assert node.entity_type == "person"


def test_node_inspection_entity_type_is_required() -> None:
    """GraphNodeInspection.entity_type is a required field (no default)."""
    import dataclasses
    fields = {f.name: f for f in dataclasses.fields(GraphNodeInspection)}
    assert "entity_type" in fields
    # Required field: default must be MISSING (dataclasses.MISSING)
    assert fields["entity_type"].default is dataclasses.MISSING
    assert fields["entity_type"].default_factory is dataclasses.MISSING  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Unit: _view_to_response propagates entity_type onto GraphNodeResponse
# ---------------------------------------------------------------------------


def test_view_to_response_maps_entity_type() -> None:
    """_view_to_response in routes_graph.py propagates entity_type onto GraphNodeResponse."""
    from archon_search.graph_inspector import CollectionGraphView
    from archon_search.server.routes_graph import _view_to_response

    view = CollectionGraphView(
        nodes=[
            GraphNodeInspection(
                entity_id="n1",
                entity_name="Kubernetes",
                chunk_count=5,
                salience=0.8,
                entity_type="system",
            ),
            GraphNodeInspection(
                entity_id="n2",
                entity_name="Alice",
                chunk_count=2,
                salience=0.3,
                entity_type="person",
                pagerank_score=0.5,
            ),
        ],
        edges=[],
        truncated=False,
        node_count=2,
        edge_count=0,
        salience_mode="frequency",
    )

    response = _view_to_response(view)

    assert len(response.nodes) == 2
    assert response.nodes[0].entity_type == "system"
    assert response.nodes[1].entity_type == "person"


def test_cross_collection_view_to_response_maps_entity_type() -> None:
    """_cross_collection_view_to_response populates entity_type on every merged node."""
    from archon_search.graph_inspector import CrossCollectionGraphView
    from archon_search.server.routes_graph import _cross_collection_view_to_response

    view = CrossCollectionGraphView(
        collections=["col1", "col2"],
        nodes=[
            GraphNodeInspection(
                entity_id="n1",
                entity_name="Kubernetes",
                chunk_count=5,
                salience=0.8,
                entity_type="system",
            ),
            GraphNodeInspection(
                entity_id="n2",
                entity_name="Bob",
                chunk_count=1,
                salience=0.1,
                entity_type="person",
            ),
        ],
        edges=[],
        truncated=False,
        node_count=2,
        edge_count=0,
        salience_mode="frequency",
    )

    response = _cross_collection_view_to_response(view)

    assert len(response.nodes) == 2
    assert response.nodes[0].entity_type == "system"
    assert response.nodes[1].entity_type == "person"


def test_graph_node_response_has_entity_type_field() -> None:
    """GraphNodeResponse Pydantic model has an entity_type field."""
    from archon_search.server.schemas import GraphNodeResponse

    fields = GraphNodeResponse.model_fields
    assert "entity_type" in fields


# ---------------------------------------------------------------------------
# Unit: _apply_tfidf preserves entity_type through the transformation
# ---------------------------------------------------------------------------


def test_apply_tfidf_preserves_entity_type() -> None:
    """_apply_tfidf passes entity_type through unchanged on each output node."""
    from archon_search.graph_inspector import _apply_tfidf

    nodes = [
        GraphNodeInspection(
            entity_id="e1",
            entity_name="Alice",
            chunk_count=4,
            salience=0.4,
            entity_type="person",
        ),
        GraphNodeInspection(
            entity_id="e2",
            entity_name="MyFunction",
            chunk_count=2,
            salience=0.2,
            entity_type="code_symbol",
        ),
    ]
    entity_presence = {"e1": 1, "e2": 2}
    result = _apply_tfidf(nodes, entity_presence, num_collections=3)

    assert len(result) == 2
    assert result[0].entity_type == "person"
    assert result[1].entity_type == "code_symbol"

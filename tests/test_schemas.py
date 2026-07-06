"""Tests for Pydantic schema structure (Tasks 6.x)."""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Task 6.4 — SearchResponse.embedding_model field
# ---------------------------------------------------------------------------


def test_search_response_has_embedding_model_field() -> None:
    """SearchResponse schema declares an embedding_model: str field."""
    from archon_search.server.routes_search import SearchResponse

    fields = SearchResponse.model_fields
    assert "embedding_model" in fields, "SearchResponse must have an embedding_model field"
    # Verify it accepts a string value (not optional)
    sr = SearchResponse(results=[], acl_filtered=False, embedding_model="BAAI/bge-small-en-v1.5")
    assert sr.embedding_model == "BAAI/bge-small-en-v1.5"


# ---------------------------------------------------------------------------
# BE-7 — GraphCollectionStats health fields and GraphEdgeResponse.relationship_type
# ---------------------------------------------------------------------------


def test_graph_collection_stats_has_health_metric_fields() -> None:
    """GraphCollectionStats has the three health fields with correct defaults."""
    from archon_search.server.schemas import GraphCollectionStats

    stats = GraphCollectionStats(collection="test-col", node_count=5, edge_count=3)

    assert stats.synonym_edge_count == 0
    assert stats.singleton_node_pct == 0.0
    assert stats.synonym_link_rate == 0.0

    # All three fields must exist
    fields = GraphCollectionStats.model_fields
    assert "synonym_edge_count" in fields
    assert "singleton_node_pct" in fields
    assert "synonym_link_rate" in fields
    assert "connected_component_count" not in fields


def test_graph_edge_response_has_relationship_type_field() -> None:
    """GraphEdgeResponse has a relationship_type field defaulting to 'related_to'."""
    from archon_search.server.schemas import GraphEdgeResponse

    edge = GraphEdgeResponse(
        edge_id="e1",
        source_entity_id="src",
        target_entity_id="tgt",
        weight=1,
        source_chunk_ids=[],
    )

    assert edge.relationship_type == "related_to"

    fields = GraphEdgeResponse.model_fields
    assert "relationship_type" in fields

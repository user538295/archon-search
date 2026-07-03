"""Unit tests for graph_types.py — BE-2 of E1a (GraphRAG entity extraction).

These tests cover:
- `make_stable_entity_id`: deterministic SHA-256 ID; type-prefixed collision avoidance
- `make_stable_edge_id`: deterministic SHA-256 ID; distinct for different types/directions
- `GraphNode`, `GraphEdge`, `GraphExtractionResult`, `ChunkInput` dataclass field presence
- `GraphExtractionResult` defaults: `warnings=[]`, `llm_fallback_used=False`
- `RelationshipType` and `EntityType` enum completeness
"""

from __future__ import annotations

import hashlib

from archon_search.graph_types import (
    ChunkInput,
    EntityType,
    GraphEdge,
    GraphExtractionResult,
    GraphMention,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
    make_stable_entity_id,
)


# ---------------------------------------------------------------------------
# make_stable_entity_id
# ---------------------------------------------------------------------------


def test_graph_node_stable_id_deterministic() -> None:
    """Same entity_type + entity_name produces identical IDs across calls."""
    id1 = make_stable_entity_id("person", "Alice")
    id2 = make_stable_entity_id("person", "Alice")
    assert id1 == id2


def test_graph_node_stable_id_case_insensitive_name() -> None:
    """Entity name is lowercased and stripped before hashing: 'Alice' == 'alice'."""
    id_upper = make_stable_entity_id("person", "Alice")
    id_lower = make_stable_entity_id("person", "alice")
    assert id_upper == id_lower


def test_graph_node_stable_id_strips_whitespace() -> None:
    """Leading/trailing whitespace in entity_name is stripped before hashing."""
    id_plain = make_stable_entity_id("concept", "mercury")
    id_padded = make_stable_entity_id("concept", "  mercury  ")
    assert id_plain == id_padded


def test_graph_node_stable_id_type_prefix_prevents_collision() -> None:
    """Different entity_type with same entity_name must produce different IDs.

    E.g. 'mercury' as 'concept' vs 'person'.
    """
    id_concept = make_stable_entity_id("concept", "mercury")
    id_person = make_stable_entity_id("person", "mercury")
    assert id_concept != id_person


def test_graph_node_stable_id_matches_sha256_formula() -> None:
    """make_stable_entity_id output equals hashlib.sha256 applied to the canonical formula."""
    entity_type = "system"
    entity_name = "AuthService"
    expected = hashlib.sha256(
        f"{entity_type.strip().lower()}:{entity_name.strip().lower()}".encode()
    ).hexdigest()
    assert make_stable_entity_id(entity_type, entity_name) == expected


def test_graph_node_stable_id_returns_64_hex_chars() -> None:
    """SHA-256 hexdigest is always 64 characters."""
    result = make_stable_entity_id("event", "Deployment")
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


# ---------------------------------------------------------------------------
# make_stable_edge_id
# ---------------------------------------------------------------------------


def test_graph_edge_stable_id_deterministic() -> None:
    """Same (source, target, relationship_type) produces identical IDs across calls."""
    id1 = make_stable_edge_id("src_id", "tgt_id", RelationshipType.related_to.value)
    id2 = make_stable_edge_id("src_id", "tgt_id", RelationshipType.related_to.value)
    assert id1 == id2


def test_graph_edge_stable_id_different_type_differs() -> None:
    """Different relationship_type for same (source, target) pair produces different IDs."""
    id_related = make_stable_edge_id("src_id", "tgt_id", RelationshipType.related_to.value)
    id_uses = make_stable_edge_id("src_id", "tgt_id", RelationshipType.uses.value)
    assert id_related != id_uses


def test_graph_edge_stable_id_direction_matters() -> None:
    """Reversed (source, target) order produces a different ID (directed edge)."""
    id_forward = make_stable_edge_id("src_id", "tgt_id", RelationshipType.related_to.value)
    id_reverse = make_stable_edge_id("tgt_id", "src_id", RelationshipType.related_to.value)
    assert id_forward != id_reverse


def test_graph_edge_stable_id_matches_sha256_formula() -> None:
    """make_stable_edge_id output equals hashlib.sha256 applied to the canonical formula."""
    source_id = "abc123"
    target_id = "def456"
    relationship_type = RelationshipType.related_to.value
    expected = hashlib.sha256(
        f"{source_id}:{target_id}:{relationship_type.strip().lower()}".encode()
    ).hexdigest()
    assert make_stable_edge_id(source_id, target_id, relationship_type) == expected


def test_graph_edge_stable_id_returns_64_hex_chars() -> None:
    """SHA-256 hexdigest is always 64 characters."""
    result = make_stable_edge_id("a", "b", RelationshipType.related_to.value)
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


def test_graph_node_stable_id_entity_type_normalized() -> None:
    """entity_type is strip+lowercased: 'Person' == 'person' == ' person '."""
    id_lower = make_stable_entity_id("person", "Alice")
    id_upper = make_stable_entity_id("Person", "Alice")
    id_padded = make_stable_entity_id(" person ", "Alice")
    assert id_lower == id_upper == id_padded


def test_graph_edge_stable_id_uses_real_enum_value() -> None:
    """make_stable_edge_id with RelationshipType.related_to.value matches direct string."""
    id_enum = make_stable_edge_id("src", "tgt", RelationshipType.related_to.value)
    id_str = make_stable_edge_id("src", "tgt", "related_to")
    assert id_enum == id_str


def test_graph_edge_stable_id_relationship_type_normalized() -> None:
    """relationship_type is strip+lowercased: 'RELATED_TO' == 'related_to' == ' related_to '."""
    id_lower = make_stable_edge_id("src", "tgt", "related_to")
    id_upper = make_stable_edge_id("src", "tgt", "RELATED_TO")
    id_padded = make_stable_edge_id("src", "tgt", " related_to ")
    assert id_lower == id_upper == id_padded


# ---------------------------------------------------------------------------
# EntityType enum
# ---------------------------------------------------------------------------


def test_entity_type_enum_values() -> None:
    """EntityType must contain all five expected members."""
    assert EntityType.person.value == "person"
    assert EntityType.concept.value == "concept"
    assert EntityType.system.value == "system"
    assert EntityType.event.value == "event"
    assert EntityType.code_symbol.value == "code_symbol"


def test_entity_type_enum_complete() -> None:
    """EntityType has exactly five members — no accidental extras."""
    assert len(EntityType) == 5


# ---------------------------------------------------------------------------
# RelationshipType enum
# ---------------------------------------------------------------------------


def test_relationship_type_enum_values() -> None:
    """RelationshipType must contain all four expected members."""
    assert RelationshipType.uses.value == "uses"
    assert RelationshipType.implements.value == "implements"
    assert RelationshipType.depends_on.value == "depends_on"
    assert RelationshipType.related_to.value == "related_to"


def test_relationship_type_enum_complete() -> None:
    """RelationshipType has exactly four members — no accidental extras."""
    assert len(RelationshipType) == 4


# ---------------------------------------------------------------------------
# ChunkInput dataclass
# ---------------------------------------------------------------------------


def test_chunk_input_required_fields() -> None:
    """ChunkInput can be constructed with all required fields."""
    chunk = ChunkInput(
        chunk_id="chunk-001",
        text="Alice works at Acme Corp.",
        symbol_type=None,
        symbol_subtype=None,
    )
    assert chunk.chunk_id == "chunk-001"
    assert chunk.text == "Alice works at Acme Corp."
    assert chunk.symbol_type is None
    assert chunk.symbol_subtype is None


def test_chunk_input_with_symbol_fields() -> None:
    """ChunkInput carries symbol_type and symbol_subtype for C3-enriched code chunks."""
    chunk = ChunkInput(
        chunk_id="chunk-002",
        text="def process(): pass",
        symbol_type="function",
        symbol_subtype="method",
    )
    assert chunk.symbol_type == "function"
    assert chunk.symbol_subtype == "method"


# ---------------------------------------------------------------------------
# GraphNode dataclass
# ---------------------------------------------------------------------------


def test_graph_node_required_fields() -> None:
    """GraphNode can be constructed with all required fields."""
    node_id = make_stable_entity_id("person", "Alice")
    node = GraphNode(
        id=node_id,
        entity_name="Alice",
        entity_type=EntityType.person,
        source_doc_id="doc-001",
        collection_name="test_collection",
    )
    assert node.id == node_id
    assert node.entity_name == "Alice"
    assert node.entity_type == EntityType.person
    assert node.source_doc_id == "doc-001"
    assert node.collection_name == "test_collection"


def test_graph_node_entity_subtype_defaults_to_none() -> None:
    """GraphNode.entity_subtype is None by default (optional field)."""
    node = GraphNode(
        id="some-id",
        entity_name="process",
        entity_type=EntityType.code_symbol,
        source_doc_id="doc-001",
        collection_name="col",
    )
    assert node.entity_subtype is None


def test_graph_node_entity_subtype_set_explicitly() -> None:
    """GraphNode.entity_subtype can be set to a subtype string."""
    node = GraphNode(
        id="some-id",
        entity_name="process",
        entity_type=EntityType.code_symbol,
        source_doc_id="doc-001",
        collection_name="col",
        entity_subtype="method",
    )
    assert node.entity_subtype == "method"


# ---------------------------------------------------------------------------
# GraphEdge dataclass
# ---------------------------------------------------------------------------


def test_graph_edge_fields() -> None:
    """GraphEdge can be constructed with all required fields; RelationshipType validates."""
    source_id = make_stable_entity_id("person", "Alice")
    target_id = make_stable_entity_id("system", "AcmeCorp")
    edge_id = make_stable_edge_id(source_id, target_id, RelationshipType.related_to.value)
    edge = GraphEdge(
        id=edge_id,
        source_node_id=source_id,
        target_node_id=target_id,
        relationship_type=RelationshipType.related_to,
        source_doc_id="doc-001",
    )
    assert edge.id == edge_id
    assert edge.source_node_id == source_id
    assert edge.target_node_id == target_id
    assert edge.relationship_type == RelationshipType.related_to
    assert edge.source_doc_id == "doc-001"


def test_graph_edge_relationship_type_is_enum() -> None:
    """GraphEdge.relationship_type must be a RelationshipType enum member."""
    edge = GraphEdge(
        id="edge-id",
        source_node_id="src",
        target_node_id="tgt",
        relationship_type=RelationshipType.uses,
        source_doc_id="doc-001",
    )
    assert isinstance(edge.relationship_type, RelationshipType)


# ---------------------------------------------------------------------------
# GraphMention dataclass
# ---------------------------------------------------------------------------


def test_graph_mention_dataclass_fields() -> None:
    """GraphMention carries entity_id, chunk_id, doc_id fields."""
    mention = GraphMention(
        entity_id="entity-abc123",
        chunk_id="chunk-001",
        doc_id="doc-001",
    )
    assert mention.entity_id == "entity-abc123"
    assert mention.chunk_id == "chunk-001"
    assert mention.doc_id == "doc-001"


def test_graph_mention_dataclass_equality() -> None:
    """Two GraphMention instances with identical fields are equal."""
    mention1 = GraphMention(
        entity_id="entity-abc123",
        chunk_id="chunk-001",
        doc_id="doc-001",
    )
    mention2 = GraphMention(
        entity_id="entity-abc123",
        chunk_id="chunk-001",
        doc_id="doc-001",
    )
    assert mention1 == mention2


def test_graph_mention_dataclass_inequality_entity_id() -> None:
    """Two GraphMention instances with different entity_id are not equal."""
    mention1 = GraphMention(
        entity_id="entity-abc123",
        chunk_id="chunk-001",
        doc_id="doc-001",
    )
    mention2 = GraphMention(
        entity_id="entity-different",
        chunk_id="chunk-001",
        doc_id="doc-001",
    )
    assert mention1 != mention2


def test_graph_mention_dataclass_inequality_chunk_id() -> None:
    """Two GraphMention instances with different chunk_id are not equal."""
    mention1 = GraphMention(
        entity_id="entity-abc123",
        chunk_id="chunk-001",
        doc_id="doc-001",
    )
    mention2 = GraphMention(
        entity_id="entity-abc123",
        chunk_id="chunk-002",
        doc_id="doc-001",
    )
    assert mention1 != mention2


def test_graph_mention_dataclass_inequality_doc_id() -> None:
    """Two GraphMention instances with different doc_id are not equal."""
    mention1 = GraphMention(
        entity_id="entity-abc123",
        chunk_id="chunk-001",
        doc_id="doc-001",
    )
    mention2 = GraphMention(
        entity_id="entity-abc123",
        chunk_id="chunk-001",
        doc_id="doc-002",
    )
    assert mention1 != mention2


# ---------------------------------------------------------------------------
# GraphExtractionResult dataclass
# ---------------------------------------------------------------------------


def test_graph_extraction_result_defaults() -> None:
    """`warnings=[]`, `mentions=[]`, and `llm_fallback_used=False` by default; nodes and edges can be empty."""
    result = GraphExtractionResult(nodes=[], edges=[])
    assert result.mentions == []
    assert result.warnings == []
    assert result.llm_fallback_used is False


def test_graph_extraction_result_with_values() -> None:
    """GraphExtractionResult carries nodes, edges, llm_fallback_used, and warnings."""
    node = GraphNode(
        id=make_stable_entity_id("concept", "AI"),
        entity_name="AI",
        entity_type=EntityType.concept,
        source_doc_id="doc-001",
        collection_name="col",
    )
    result = GraphExtractionResult(
        nodes=[node],
        edges=[],
        llm_fallback_used=True,
        warnings=["LLM call failed; fell back to spaCy-only extraction."],
    )
    assert len(result.nodes) == 1
    assert result.llm_fallback_used is True
    assert len(result.warnings) == 1


def test_graph_extraction_result_warnings_are_independent_instances() -> None:
    """Two separate GraphExtractionResult instances have independent warning lists.

    Ensures `field(default_factory=list)` is used — not a mutable default argument.
    """
    r1 = GraphExtractionResult(nodes=[], edges=[])
    r2 = GraphExtractionResult(nodes=[], edges=[])
    r1.warnings.append("warning")
    assert r2.warnings == []


def test_extraction_result_mentions_defaults_to_empty() -> None:
    """GraphExtractionResult.mentions defaults to empty list."""
    result = GraphExtractionResult(nodes=[], edges=[])
    assert result.mentions == []


def test_extraction_result_mentions_are_independent_instances() -> None:
    """Two separate GraphExtractionResult instances have independent mention lists.

    Ensures `field(default_factory=list)` is used — not a mutable default argument.
    """
    r1 = GraphExtractionResult(nodes=[], edges=[])
    r2 = GraphExtractionResult(nodes=[], edges=[])
    mention = GraphMention(entity_id="e1", chunk_id="c1", doc_id="d1")
    r1.mentions.append(mention)
    assert r2.mentions == []


def test_extraction_result_mentions_can_be_set() -> None:
    """GraphExtractionResult can be constructed with mentions list."""
    mention1 = GraphMention(entity_id="e1", chunk_id="c1", doc_id="d1")
    mention2 = GraphMention(entity_id="e2", chunk_id="c2", doc_id="d2")
    result = GraphExtractionResult(
        nodes=[],
        edges=[],
        mentions=[mention1, mention2],
    )
    assert len(result.mentions) == 2
    assert result.mentions[0] == mention1
    assert result.mentions[1] == mention2

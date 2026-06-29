"""Entities layer for GraphRAG — E1a.

Defines the core graph domain types used across GraphExtractor, GraphStore,
and GraphExpander. All SHA-256 ID helpers live here as the single source of
truth; no other module may inline the hash formula.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum


class EntityType(str, Enum):
    """Supported entity categories for graph nodes."""

    person = "person"
    concept = "concept"
    system = "system"
    event = "event"
    code_symbol = "code_symbol"


class RelationshipType(str, Enum):
    """Supported edge relationship types."""

    uses = "uses"
    implements = "implements"
    depends_on = "depends_on"
    related_to = "related_to"


def make_stable_entity_id(entity_type: str, entity_name: str) -> str:
    """Return the stable SHA-256 hex ID for an entity.

    Formula: ``hashlib.sha256(f"{entity_type.strip().lower()}:{entity_name.strip().lower()}".encode()).hexdigest()``

    The type-prefix prevents collisions between entities with the same name but
    different types (e.g. "mercury" as ``concept`` vs ``person``).

    This function is the **single source of truth** for entity ID derivation.
    ``GraphExtractor`` and ``GraphStore`` MUST call this function — never
    inline the formula.
    """
    canonical = f"{entity_type.strip().lower()}:{entity_name.strip().lower()}"
    return hashlib.sha256(canonical.encode()).hexdigest()


def make_stable_edge_id(
    source_id: str, target_id: str, relationship_type: str
) -> str:
    """Return the stable SHA-256 hex ID for an edge.

    Formula: ``hashlib.sha256(f"{source_id}:{target_id}:{relationship_type.strip().lower()}".encode()).hexdigest()``

    ``relationship_type`` is normalized (strip + lower) for consistency with
    ``make_stable_entity_id``. Direction matters: ``make_stable_edge_id(a, b, t) !=
    make_stable_edge_id(b, a, t)``. ``source_id`` and ``target_id`` are SHA-256 hex
    digests and need no normalization. ``GraphStore`` uses this to deduplicate edges on upsert.

    This function is the **single source of truth** for edge ID derivation.
    ``GraphExtractor`` MUST call this function — never inline the formula.
    """
    canonical = f"{source_id}:{target_id}:{relationship_type.strip().lower()}"
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class ChunkInput:
    """Input record passed from the pipeline to ``GraphExtractor.extract()``.

    Carries the minimal data needed for entity extraction: the chunk text
    plus optional C3 enrichment fields that enable the code-symbol path.
    """

    chunk_id: str
    """Identifier of the originating chunk (e.g. ``{doc_id}-{idx:06d}``)."""
    text: str
    """Chunked text body to extract entities from."""
    symbol_type: str | None
    """C3-enriched code symbol type (e.g. ``"function"``, ``"class"``).
    When non-None, the code-symbol extraction path is used instead of spaCy NER.
    """
    symbol_subtype: str | None
    """Optional C3 sub-label (e.g. ``"method"``); maps to ``GraphNode.entity_subtype``."""
    containing_function: str | None = None
    """C3 ``_containing_function`` value — used as entity NAME for function-level code chunks."""
    containing_class: str | None = None
    """C3 ``_containing_class`` value — used as entity NAME for class-level code chunks when
    ``containing_function`` is absent."""
    source_path: str | None = None
    """Source file path — ``basename`` used as entity NAME fallback when both
    ``containing_function`` and ``containing_class`` are absent."""


@dataclass
class GraphNode:
    """A named entity vertex in the graph.

    The ``id`` field MUST be produced by ``make_stable_entity_id``; callers
    must never inline the hash formula.
    """

    id: str
    """SHA-256 hex of ``"{entity_type.strip().lower()}:{entity_name.strip().lower()}"`` (byte-encoded); see ``make_stable_entity_id``."""
    entity_name: str
    """Human-readable entity name as extracted (before lowercasing)."""
    entity_type: EntityType
    """Semantic category of this entity."""
    source_doc_id: str
    """Doc ID of the document that produced this node (last-writer-wins on upsert)."""
    collection_name: str
    """Name of the collection this node belongs to."""
    entity_subtype: str | None = None
    """Optional sub-label for ``code_symbol`` entities (from ``_symbol_subtype``).
    ``None`` for all other entity types.
    """


@dataclass
class GraphEdge:
    """A directed relationship edge between two graph nodes.

    The ``id`` field MUST be produced by ``make_stable_edge_id``; callers
    must never inline the hash formula.
    """

    id: str
    """SHA-256 hex of ``"{source_node_id}:{target_node_id}:{relationship_type}"``."""
    source_node_id: str
    """ID of the source ``GraphNode``."""
    target_node_id: str
    """ID of the target ``GraphNode``."""
    relationship_type: RelationshipType
    """Semantic relationship type between the two nodes."""
    source_doc_id: str
    """Doc ID of the document that produced this edge (last-writer-wins on upsert)."""


@dataclass
class GraphExtractionResult:
    """Output of ``GraphExtractor.extract()``.

    Forwarded by the pipeline into ``GraphStore.writeGraph()`` and warnings
    are appended to ``IngestResult.warnings``.
    """

    nodes: list[GraphNode]
    """Extracted entity nodes."""
    edges: list[GraphEdge]
    """Extracted relationship edges."""
    llm_fallback_used: bool = False
    """True when ``extraction_model`` is configured but LLM extraction is deferred to
    post-E1a (it is not implemented yet); spaCy-only result is used instead.
    This is a stub indicator, NOT a runtime failure flag.
    """
    warnings: list[str] = field(default_factory=list)
    """Human-readable warning messages forwarded to ``IngestResult.warnings``."""
    fatal_error: str | None = None
    """When non-None, extraction completely failed (e.g. spaCy absent or model load failed).
    The pipeline should set ``IngestResult.status = "error"`` when this field is non-None.
    The value is an actionable human-readable error message.
    """

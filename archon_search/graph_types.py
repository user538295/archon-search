"""Entities layer for GraphRAG — E1a / E1b.

Defines the core graph domain types used across GraphExtractor, GraphStore,
GraphExpander, and CommunityBuilder. All SHA-256 ID helpers live here as the
single source of truth; no other module may inline the hash formula.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


@dataclass
class GcPassResult:
    """Result of a single garbage-collection pass on graph tables — E2d.

    Returned by ``GraphStore.delete_orphan_nodes_and_edges``.
    ``communities_invalidated`` is derived automatically: it is ``True`` whenever
    at least one orphan node was removed (because community membership is anchored
    to node IDs; stale communities must be rebuilt after a GC pass that removes nodes).
    """

    orphan_nodes_removed: int
    """Number of graph nodes deleted because they had zero remaining mention rows."""
    orphan_edges_removed: int
    """Number of graph edges deleted because at least one endpoint was an orphan node."""
    communities_invalidated: bool = field(init=False)
    """``True`` when ``orphan_nodes_removed > 0``; computed by ``__post_init__``.
    When ``True`` the caller should trigger a ``build-communities`` pass to
    rebuild community data for the collection.
    """

    def __post_init__(self) -> None:
        self.communities_invalidated = self.orphan_nodes_removed > 0


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
    synonym_of = "synonym_of"
    calls = "calls"
    imports = "imports"
    defines = "defines"
    inherits = "inherits"


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


def make_code_symbol_qualified_name(name: str, source_path: str | None) -> str:
    """Return the file-qualified string fed into ``make_stable_entity_id`` for ``code_symbol`` nodes.

    Qualifies *name* with *source_path* so same-named symbols in different
    files hash to distinct node IDs. Falls back to the bare *name* when
    *source_path* is falsy (preserves pre-file-qualification IDs).

    This is the **single source of truth** for the code-symbol qualification
    formula — ``GraphExtractor`` and ``DefRefExtractor`` MUST call this
    function — never inline the ``f"{name}::{source_path}"`` formula.
    """
    return f"{name}::{source_path}" if source_path else name


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
    name_embedding: list[float] | None = None
    """Dense embedding of ``entity_name`` for ANN similarity search — BE-2.
    ``None`` for nodes whose embedding has not yet been computed (pre-E2f nodes
    or nodes written without an embedding).
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
    extraction_method: str | None = None
    """How the edge was extracted, when recorded. Currently-produced values:
    ``"embedding"`` (E2f ``SynonymDetector`` cosine-similarity matches) and
    ``"manual"`` (E2f ``alias_loader`` TOML-configured synonym pairs).
    Reserved for E2g (not yet produced by any extractor): ``"extracted"``
    (same-file code def/ref edges found by static parsing) and ``"inferred"``
    (cross-file name-based best-guess matches). This is a plain string field,
    not an enum — no validation is enforced on the value. ``None`` means the
    extraction method was not recorded — this includes every edge produced by
    the spaCy named-entity co-occurrence path (``graph_extractor.py``), which
    never sets this field, as well as any pre-E2f edge.
    """


@dataclass
class GraphMention:
    """An incidence record linking an entity to a chunk where it was mentioned — E2b.

    Stored in ``_archon_graph_{ns}__{col}_mentions`` per collection. Each mention
    records that a specific entity was extracted from a specific chunk within
    a specific document, enabling derivation of chunk frequency (salience) and
    co-occurrence metrics at inspection time.
    """

    entity_id: str
    """ID of the ``GraphNode`` mentioned (from ``make_stable_entity_id``)."""
    chunk_id: str
    """Chunk ID where this entity mention occurred (e.g., ``{doc_id}-{idx:06d}``)."""
    doc_id: str
    """Document ID containing this chunk; used for idempotent delete-then-add
    on re-ingest of the same document.
    """


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
    mentions: list[GraphMention] = field(default_factory=list)
    """Per-chunk entity incidence records for salience and co-occurrence derivation (E2b).
    Each mention records that a specific entity was mentioned in a specific chunk within
    a specific document. Empty by default; populated by ``GraphExtractor.extract()`` when
    graph extraction is enabled.
    """
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


@dataclass
class Community:
    """A Leiden-detected community of related graph entities — E1b.

    Stored in ``_archon_graph_{ns}__{col}_communities`` per collection. Each community
    groups entities that are strongly connected in the entity graph; its
    ``representative_chunk_ids`` are the MMR-selected diverse chunk IDs used
    at search time for local and global graph-mode retrieval.
    """

    community_id: str
    """Stable identifier for this community (UUID or deterministic hash — assigned by
    ``CommunityBuilder``)."""
    entity_ids: list[str]
    """IDs of ``GraphNode`` members belonging to this community."""
    representative_chunk_ids: list[str]
    """Chunk IDs selected as diverse representatives (MMR over member chunks).
    Used directly by ``SearchPipeline._search_graph_mode()`` at query time.
    """
    built_at: datetime
    """UTC datetime when this community was last built by ``build-communities``.
    UTC is expected by convention; not enforced at construction (consistent with
    entity-layer policy).
    """
    summary_text: str | None = None
    """Optional LLM-generated abstractive summary of this community.
    ``None`` when ``[graph].extraction_model`` is not set or LLM summarisation
    failed (falls back to MMR-only representatives).
    """

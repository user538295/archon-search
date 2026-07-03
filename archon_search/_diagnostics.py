"""Private production-observable diagnostic dataclasses.

These types are intentionally kept in the production package and must NOT
import from ``archon_search.eval`` — they must be importable without loading
the eval sub-package.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from archon_search._types import IngestedBy


@dataclass
class TraversalStep:
    """A single hop in a graph traversal chain.

    Represents one step that the graph retrieval algorithm took to reach a
    chunk.  At least one of ``relationship``, ``community_id``, or
    ``chunk_id`` must be set — degenerate steps (all three null) are
    semantically meaningless.  The constraint is enforced at the Pydantic
    response-model layer (``TraversalStepResponse`` in
    ``archon_search.server.routes_explain``), not here, so the dataclass
    itself is constraint-free.

    Attributes:
        entity: Human-readable entity name matched in the query.
        entity_id: Stable, deterministic graph-node identifier (from
            ``make_stable_entity_id``).
        relationship: Edge label connecting this entity to its neighbour
            (e.g. ``"CALLS"``, ``"DEPENDS_ON"``).  ``None`` for community
            or terminal chunk steps.
        community_id: Community the entity belongs to (E1b community-mode
            steps).  ``None`` for naive-mode steps.
        chunk_id: Chunk reached at this traversal step.  Non-null only for
            terminal (leaf) steps.
    """

    entity: str
    entity_id: str
    relationship: str | None = None
    community_id: str | None = None
    chunk_id: str | None = None


@dataclass
class GraphProvenance:
    """Full graph traversal chain for a single graph-retrieved chunk.

    Attributes:
        steps: Ordered list of traversal hops from the query entity to the
            retrieved chunk.  An empty list signals a graph-layer bug (the
            chunk was attributed to graph retrieval but no path was
            recorded) — it is surfaced as-is rather than masked as null.
    """

    steps: list[TraversalStep] = field(default_factory=list)


@dataclass
class SearchScoreBreakdown:
    """Per-candidate score breakdown for the hybrid RRF retrieval pipeline.

    Attributes:
        vector_rank: Rank of this candidate in the vector search results.
            ``None`` when the candidate did not appear in vector results.
        vector_score: Raw vector score (distance or similarity).
            ``None`` when the candidate did not appear in vector results.
        vector_score_kind: Semantic of ``vector_score``, e.g. ``"distance"``
            or ``"similarity"``.  ``None`` when ``vector_score`` is ``None``.
        fts_rank: Rank in full-text / BM25 results.
            ``None`` when the candidate did not appear in FTS results.
        fts_score: Raw FTS score (e.g. BM25).
            ``None`` when the candidate did not appear in FTS results.
        fts_score_kind: Semantic of ``fts_score``, e.g. ``"bm25"``.
            ``None`` when ``fts_score`` is ``None``.
        rrf_score: Fused RRF score (always present).
        reranker_score: Score assigned by a cross-encoder reranker.
            ``None`` when no reranker was applied.
    """

    vector_rank: int | None
    vector_score: float | None
    vector_score_kind: str | None
    fts_rank: int | None
    fts_score: float | None
    fts_score_kind: str | None
    rrf_score: float
    reranker_score: float | None


@dataclass
class ScoredSearchCandidate:
    """A scored search candidate as it exists inside the retrieval pipeline.

    This is a production-side type used for internal observability and
    diagnostics.  It is *not* part of the public ``SearchResult`` API.

    Attributes:
        doc_id: Runtime / store ID (path-derived).
        chunk_id: Chunk identifier within the document.
        text: Chunk text content.
        source_path: File path of the source document.
        score_breakdown: Full score provenance for this candidate.
        collection: Collection this candidate was retrieved from.
        acl: ACL namespace tokens from the stored row; None when absent.
        file_type: Source file extension (lowercased at ingest, no leading dot; empty when absent).
        indexed_at: ISO 8601 UTC ingest timestamp (empty when absent).
        updated_at: File mtime ISO 8601 UTC; falls back to indexed_at when absent.
        ingested_by: Call-site identity for the ingest write (cli/http/watcher/reindex).
        language: Detected language code; '' when untagged.
        metadata: Parsed key/value metadata dict (empty when absent).
        graph_provenance: Graph traversal chain that produced this candidate.
            ``None`` for standard hybrid-search results (non-graph path).
    """

    doc_id: str
    chunk_id: str
    text: str
    source_path: str
    score_breakdown: SearchScoreBreakdown
    collection: str
    acl: list[str] | None = None
    file_type: str = ""
    indexed_at: str = ""
    updated_at: str = ""
    ingested_by: IngestedBy = "cli"
    language: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    graph_provenance: GraphProvenance | None = None
    scopes: list[str] | None = None

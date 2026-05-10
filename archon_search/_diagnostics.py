"""Private production-observable diagnostic dataclasses.

These types are intentionally kept in the production package and must NOT
import from ``archon_search.eval`` — they must be importable without loading
the eval sub-package.
"""
from __future__ import annotations

from dataclasses import dataclass


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
    """

    doc_id: str
    chunk_id: str
    text: str
    source_path: str
    score_breakdown: SearchScoreBreakdown
    collection: str

"""POST /explain endpoint schemas and handler (A4).

This module is the only seam between ``archon_search._diagnostics`` and the
public wire schema. Adding fields here is a public-contract change.

All schemas use ``extra="forbid"``: ``/explain`` is a new endpoint with no
legacy clients, so rejecting unknown fields makes contract violations loud
rather than silent — desirable for a debug endpoint. This intentionally
diverges from ``schemas.py`` / ``routes_search.py``, which omit it.

The route handler is added in Task 3.1; for now ``router`` is an empty
``APIRouter`` so importing the schemas does not register a half-built route.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator

from archon_search._types import IngestedBy

if TYPE_CHECKING:
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.pipeline import ExplainPipelineResult

router = APIRouter()


def _final_score(b: SearchScoreBreakdown) -> float:
    """reranker_score when a reranker ran, else the fused RRF score."""
    return b.reranker_score if b.reranker_score is not None else b.rrf_score


class ExplainScoreBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vector_rank: int | None
    vector_score: float | None
    vector_score_kind: str | None  # "distance" for LanceDB cosine (lower is closer); surfaced verbatim
    fts_rank: int | None
    fts_score: float | None
    fts_score_kind: str | None  # "bm25" when raw score present; null when LanceDB omits _score
    rrf_score: float
    reranker_score: float | None  # null when rerank=false

    @classmethod
    def from_breakdown(cls, b: SearchScoreBreakdown) -> ExplainScoreBreakdown:
        return cls(
            vector_rank=b.vector_rank,
            vector_score=b.vector_score,
            vector_score_kind=b.vector_score_kind,
            fts_rank=b.fts_rank,
            fts_score=b.fts_score,
            fts_score_kind=b.fts_score_kind,
            rrf_score=b.rrf_score,
            reranker_score=b.reranker_score,
        )


class ExplainResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    chunk_id: str
    source_path: str
    text: str
    score: float
    breakdown: ExplainScoreBreakdown
    file_type: str = ""
    indexed_at: str = ""
    updated_at: str = ""
    ingested_by: IngestedBy = "cli"
    language: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    acl: list[str] | None = None

    @classmethod
    def from_candidate(cls, c: ScoredSearchCandidate) -> ExplainResult:
        return cls(
            doc_id=c.doc_id,
            chunk_id=c.chunk_id,
            source_path=c.source_path,
            text=c.text,
            score=_final_score(c.score_breakdown),
            breakdown=ExplainScoreBreakdown.from_breakdown(c.score_breakdown),
            file_type=c.file_type,
            indexed_at=c.indexed_at,
            updated_at=c.updated_at,
            ingested_by=c.ingested_by,
            language=c.language,
            metadata=c.metadata,
            acl=c.acl,
        )


class ExplainNearMiss(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    chunk_id: str
    source_path: str
    score: float
    breakdown: ExplainScoreBreakdown
    file_type: str = ""
    indexed_at: str = ""
    updated_at: str = ""
    ingested_by: IngestedBy = "cli"
    language: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    acl: list[str] | None = None
    # NOTE: no `text` field. Absence is structural.

    @classmethod
    def from_candidate(cls, c: ScoredSearchCandidate) -> ExplainNearMiss:
        return cls(
            doc_id=c.doc_id,
            chunk_id=c.chunk_id,
            source_path=c.source_path,
            score=_final_score(c.score_breakdown),
            breakdown=ExplainScoreBreakdown.from_breakdown(c.score_breakdown),
            file_type=c.file_type,
            indexed_at=c.indexed_at,
            updated_at=c.updated_at,
            ingested_by=c.ingested_by,
            language=c.language,
            metadata=c.metadata,
            acl=c.acl,
        )


class RoutingCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: str
    centroid_score: float | None  # null for mismatched-model / no-centroid collections


class RoutingExplain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoked: bool
    chosen_collection: str
    confidence_threshold: float
    chosen_below_threshold: bool
    candidates: list[RoutingCandidate]


class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    collection: str | None = None
    top_k: int = Field(default=5, ge=1, le=100)
    rerank: bool = True

    @field_validator("query")
    @classmethod
    def _query_nonempty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("query must not be empty")
        return stripped

    @field_validator("collection")
    @classmethod
    def _collection_nonempty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("collection must not be empty")
        return stripped


class ExplainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rerank: bool
    routing: RoutingExplain | None
    collection: str
    acl_filtered: bool
    results: list[ExplainResult]
    near_misses: list[ExplainNearMiss]

    @classmethod
    def from_pipeline_result(
        cls,
        *,
        rerank: bool,
        collection: str,
        routing: RoutingExplain | None,
        result: ExplainPipelineResult,
    ) -> ExplainResponse:
        return cls(
            rerank=rerank,
            routing=routing,
            collection=collection,
            acl_filtered=result.acl_filtered,
            results=[ExplainResult.from_candidate(c) for c in result.top_results],
            near_misses=[ExplainNearMiss.from_candidate(c) for c in result.near_misses],
        )

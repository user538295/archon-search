"""POST /explain endpoint — public schemas and route handler.

This module is the only seam between `archon_search._diagnostics` and the
public wire schema. Adding fields here is a public-contract change.
"""
from __future__ import annotations

import logging
from time import monotonic
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.pipeline import ExplainPipelineResult

logger = logging.getLogger("archon.search")

router = APIRouter()


# ---------------------------------------------------------------------------
# Score breakdown
# ---------------------------------------------------------------------------


class ExplainScoreBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vector_rank: int | None
    vector_score: float | None
    vector_score_kind: str | None
    fts_rank: int | None
    fts_score: float | None
    fts_score_kind: str | None
    rrf_score: float
    reranker_score: float | None

    @classmethod
    def from_breakdown(cls, b: "SearchScoreBreakdown") -> "ExplainScoreBreakdown":
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


# ---------------------------------------------------------------------------
# Explain result / near-miss
# ---------------------------------------------------------------------------


class ExplainResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    chunk_id: str
    source_path: str
    text: str
    score: float
    breakdown: ExplainScoreBreakdown
    # A1/A2 metadata fields
    file_type: str = ""
    indexed_at: str = ""
    updated_at: str = ""
    ingested_by: str = "cli"
    language: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_candidate(cls, c: "ScoredSearchCandidate") -> "ExplainResult":
        bd = c.score_breakdown
        score = (
            bd.reranker_score if bd.reranker_score is not None else bd.rrf_score
        )
        return cls(
            doc_id=c.doc_id,
            chunk_id=c.chunk_id,
            source_path=c.source_path,
            text=c.text,
            score=score,
            breakdown=ExplainScoreBreakdown.from_breakdown(bd),
            file_type=c.file_type,
            indexed_at=c.indexed_at,
            updated_at=c.updated_at,
            ingested_by=c.ingested_by,
            language=c.language,
            metadata=c.metadata,
        )


class ExplainNearMiss(BaseModel):
    """Same as ExplainResult but without the text field."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    chunk_id: str
    source_path: str
    score: float
    breakdown: ExplainScoreBreakdown
    # A1/A2 metadata fields
    file_type: str = ""
    indexed_at: str = ""
    updated_at: str = ""
    ingested_by: str = "cli"
    language: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_candidate(cls, c: "ScoredSearchCandidate") -> "ExplainNearMiss":
        bd = c.score_breakdown
        score = (
            bd.reranker_score if bd.reranker_score is not None else bd.rrf_score
        )
        return cls(
            doc_id=c.doc_id,
            chunk_id=c.chunk_id,
            source_path=c.source_path,
            score=score,
            breakdown=ExplainScoreBreakdown.from_breakdown(bd),
            file_type=c.file_type,
            indexed_at=c.indexed_at,
            updated_at=c.updated_at,
            ingested_by=c.ingested_by,
            language=c.language,
            metadata=c.metadata,
        )


# ---------------------------------------------------------------------------
# Routing explain
# ---------------------------------------------------------------------------


class RoutingCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: str
    centroid_score: float | None


class RoutingExplain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoked: bool
    chosen_collection: str
    confidence_threshold: float
    chosen_below_threshold: bool
    candidates: list[RoutingCandidate]


# ---------------------------------------------------------------------------
# Request / Response
# ---------------------------------------------------------------------------


class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    collection: str | None = None
    top_k: int = Field(default=5, ge=1, le=100)
    rerank: bool = True

    @field_validator("query")
    @classmethod
    def query_nonempty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("query must not be empty or whitespace")
        return stripped

    @field_validator("collection")
    @classmethod
    def collection_nonempty(cls, v: str | None) -> str | None:
        if v is not None:
            stripped = v.strip()
            if not stripped:
                raise ValueError("collection must not be empty")
            return stripped
        return v


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
        pipeline_result: "ExplainPipelineResult",
        collection: str,
        rerank: bool,
        routing: RoutingExplain | None,
    ) -> "ExplainResponse":
        return cls(
            rerank=rerank,
            routing=routing,
            collection=collection,
            acl_filtered=pipeline_result.acl_filtered,
            results=[ExplainResult.from_candidate(c) for c in pipeline_result.top_results],
            near_misses=[ExplainNearMiss.from_candidate(c) for c in pipeline_result.near_misses],
        )


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.post("/explain", response_model=ExplainResponse)
async def explain(
    body: ExplainRequest, request: Request
) -> ExplainResponse | JSONResponse:
    from archon_search.collection_meta import CollectionMeta  # noqa: PLC0415
    from archon_search.router import MultiCollectionRouter  # noqa: PLC0415
    from archon_search.telemetry.entry import ErrorKind, TelemetryEntry  # noqa: PLC0415

    start = monotonic()
    pipeline = request.app.state.pipeline
    ns = request.state.namespace
    config = request.app.state.config
    writer = getattr(request.app.state, "telemetry_writer", None)

    if body.collection is not None:
        # ----------------------------------------------------------------
        # Pinned collection path
        # ----------------------------------------------------------------
        try:
            meta = await pipeline.get_collection_meta(body.collection, namespace=ns)
        except Exception as exc:
            logger.error(
                "explain: meta lookup failed for collection %r: %s",
                body.collection,
                exc,
                exc_info=True,
            )
            if writer is not None:
                try:
                    writer.enqueue(TelemetryEntry.from_error(endpoint="explain", status="internal_error", error_kind=ErrorKind.other, latency_ms=(monotonic() - start) * 1000.0))
                except Exception:
                    logger.warning("telemetry: explain error entry enqueue failed", exc_info=True)
            return JSONResponse({"detail": "service unavailable"}, status_code=503)

        if meta is None:
            if writer is not None:
                try:
                    writer.enqueue(TelemetryEntry.from_error(endpoint="explain", status="validation_error", error_kind=ErrorKind.validation_error, latency_ms=(monotonic() - start) * 1000.0))
                except Exception:
                    logger.warning("telemetry: explain error entry enqueue failed", exc_info=True)
            return JSONResponse({"detail": "collection not found"}, status_code=404)

        try:
            pipeline_result = await pipeline.explain(
                body.query,
                body.collection,
                top_k=body.top_k,
                rerank=body.rerank,
                namespace=ns,
            )
        except Exception as exc:
            logger.error(
                "explain: pipeline failed for collection %r: %s",
                body.collection,
                exc,
                exc_info=True,
            )
            if writer is not None:
                try:
                    writer.enqueue(TelemetryEntry.from_error(endpoint="explain", status="internal_error", error_kind=ErrorKind.other, latency_ms=(monotonic() - start) * 1000.0))
                except Exception:
                    logger.warning("telemetry: explain error entry enqueue failed", exc_info=True)
            return JSONResponse({"detail": "internal server error"}, status_code=500)

        routing: RoutingExplain | None = None
        chosen_collection = body.collection

    else:
        # ----------------------------------------------------------------
        # Collectionless path — build routing explain
        # ----------------------------------------------------------------
        try:
            all_meta: list[CollectionMeta] = await pipeline.get_all_collections_meta(namespace=ns)
        except Exception as exc:
            logger.error("explain: meta lookup failed: %s", exc, exc_info=True)
            if writer is not None:
                try:
                    writer.enqueue(TelemetryEntry.from_error(endpoint="explain", status="internal_error", error_kind=ErrorKind.other, latency_ms=(monotonic() - start) * 1000.0))
                except Exception:
                    logger.warning("telemetry: explain error entry enqueue failed", exc_info=True)
            return JSONResponse({"detail": "service unavailable"}, status_code=503)

        if not all_meta:
            if writer is not None:
                try:
                    writer.enqueue(TelemetryEntry.from_error(endpoint="explain", status="validation_error", error_kind=ErrorKind.validation_error, latency_ms=(monotonic() - start) * 1000.0))
                except Exception:
                    logger.warning("telemetry: explain error entry enqueue failed", exc_info=True)
            return JSONResponse({"detail": "no collections available"}, status_code=404)

        # Embed query once for both routing and search
        try:
            query_vector = await pipeline._embedder.embed_one(body.query)
        except Exception as exc:
            logger.error("explain: embedding failed: %s", exc, exc_info=True)
            if writer is not None:
                try:
                    writer.enqueue(TelemetryEntry.from_error(endpoint="explain", status="internal_error", error_kind=ErrorKind.other, latency_ms=(monotonic() - start) * 1000.0))
                except Exception:
                    logger.warning("telemetry: explain error entry enqueue failed", exc_info=True)
            return JSONResponse({"detail": "internal server error"}, status_code=500)

        # Build router inline
        inline_router = MultiCollectionRouter(
            search_url="",
            embedder=pipeline._embedder,
            shortlist_size=config.routing_shortlist_size,
            confidence_threshold=config.routing_confidence_threshold,
            embedding_model=pipeline._embedder.model_name,
        )
        scored_pairs = inline_router.rank_with_scores(query_vector, all_meta)

        # ACL filter: only keep collections the caller's namespace is allowed to access
        scored_pairs = [(m, s) for m, s in scored_pairs if m.namespace == ns]

        if not scored_pairs:
            if writer is not None:
                try:
                    writer.enqueue(TelemetryEntry.from_error(endpoint="explain", status="validation_error", error_kind=ErrorKind.validation_error, latency_ms=(monotonic() - start) * 1000.0))
                except Exception:
                    logger.warning("telemetry: explain error entry enqueue failed", exc_info=True)
            return JSONResponse({"detail": "no collections available"}, status_code=404)

        routing_candidates = [
            RoutingCandidate(collection=m.name, centroid_score=score)
            for m, score in scored_pairs
        ]

        # Determine chosen collection (first after building scored list)
        chosen_meta, chosen_score = scored_pairs[0]
        chosen_collection = chosen_meta.name

        # Confidence gate
        confidence_threshold = config.routing_confidence_threshold
        chosen_below_threshold = bool(
            chosen_score is not None and chosen_score < confidence_threshold
        )

        routing = RoutingExplain(
            invoked=True,
            chosen_collection=chosen_collection,
            confidence_threshold=confidence_threshold,
            chosen_below_threshold=chosen_below_threshold,
            candidates=routing_candidates,
        )

        try:
            pipeline_result = await pipeline.explain(
                body.query,
                chosen_collection,
                top_k=body.top_k,
                rerank=body.rerank,
                namespace=ns,
                query_vector=query_vector,
            )
        except Exception as exc:
            logger.error(
                "explain: pipeline failed for collection %r: %s",
                chosen_collection,
                exc,
                exc_info=True,
            )
            if writer is not None:
                try:
                    writer.enqueue(TelemetryEntry.from_error(endpoint="explain", status="internal_error", error_kind=ErrorKind.other, latency_ms=(monotonic() - start) * 1000.0))
                except Exception:
                    logger.warning("telemetry: explain error entry enqueue failed", exc_info=True)
            return JSONResponse({"detail": "internal server error"}, status_code=500)

    response = ExplainResponse.from_pipeline_result(
        pipeline_result=pipeline_result,
        collection=chosen_collection,
        rerank=body.rerank,
        routing=routing,
    )

    # Telemetry (non-blocking)
    if writer is not None:
        try:
            writer.enqueue(
                TelemetryEntry.from_explain_result(
                    collection=chosen_collection,
                    result_count=len(response.results),
                    latency_ms=(monotonic() - start) * 1000.0,
                )
            )
        except Exception:
            logger.warning("telemetry: explain entry enqueue failed", exc_info=True)

    return response

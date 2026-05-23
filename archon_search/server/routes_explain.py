"""POST /explain endpoint — public schemas and route handler.

This module is the only seam between `archon_search._diagnostics` and the
public wire schema. Adding fields here is a public-contract change.
"""
from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.config import SearchConfig
    from archon_search.pipeline import ExplainPipelineResult, SearchPipeline
    from archon_search.telemetry.entry import TelemetryEntry
    from archon_search.telemetry.writer import TelemetryWriter

logger = logging.getLogger("archon.search")

router = APIRouter()

# 30-second hard timeout for pipeline.explain(); surface as HTTP 504.
_EXPLAIN_TIMEOUT_SECONDS = 30.0


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
# Telemetry helpers (Fix 3 + Fix 4)
# ---------------------------------------------------------------------------


def _enqueue_explain_error(
    writer: "TelemetryWriter | None",
    start: float,
    status: str,
    kind: Any,
) -> None:
    """Enqueue an error telemetry entry for the explain endpoint.

    Swallows all exceptions so telemetry failures never abort the response.
    """
    if writer is not None:
        from archon_search.telemetry.entry import TelemetryEntry  # noqa: PLC0415

        try:
            writer.enqueue(
                TelemetryEntry.from_error(
                    endpoint="explain",
                    status=status,
                    error_kind=kind,
                    latency_ms=(monotonic() - start) * 1000.0,
                )
            )
        except Exception as tel_exc:
            logger.warning("telemetry enqueue failed: %s", type(tel_exc).__name__)


def _enqueue_explain_success(
    writer: "TelemetryWriter | None",
    start: float,
    entry: "TelemetryEntry",
) -> None:
    """Enqueue a success telemetry entry for the explain endpoint.

    Swallows all exceptions so telemetry failures never abort the response.
    """
    if writer is not None:
        try:
            writer.enqueue(entry)
        except Exception as tel_exc:
            logger.warning("telemetry enqueue failed: %s", type(tel_exc).__name__)


# ---------------------------------------------------------------------------
# Shared collectionless routing helper (Fix 5)
# ---------------------------------------------------------------------------


async def _build_routing_explain(
    pipeline: "SearchPipeline",
    query: str,
    ns: str,
    config: "SearchConfig",
    writer: "TelemetryWriter | None",
    start: float,
) -> "tuple[RoutingExplain | None, list[float], JSONResponse | None]":
    """Build a RoutingExplain by embedding the query and ranking collections.

    Returns:
        (routing, query_vector, error_response) where:
        - On success: (RoutingExplain, query_vector, None)
        - On error: (None, [], JSONResponse to return to caller)
    """
    from archon_search.collection_meta import CollectionMeta  # noqa: PLC0415
    from archon_search.router import MultiCollectionRouter  # noqa: PLC0415
    from archon_search.telemetry.entry import ErrorKind  # noqa: PLC0415

    # 1. Load all collection metadata
    try:
        all_meta: list[CollectionMeta] = await pipeline.get_all_collections_meta(namespace=ns)
    except Exception as exc:
        logger.error("explain: meta lookup failed: %s", exc, exc_info=True)
        _enqueue_explain_error(writer, start, "internal_error", ErrorKind.other)
        return None, [], JSONResponse({"detail": "service unavailable"}, status_code=503)

    if not all_meta:
        _enqueue_explain_error(writer, start, "validation_error", ErrorKind.validation_error)
        return None, [], JSONResponse({"detail": "no collections available"}, status_code=404)

    # 2. Embed query
    try:
        query_vector = await pipeline._embedder.embed_one(query)
    except ValueError as exc:
        logger.error("explain: embedding validation failed: %s", exc, exc_info=True)
        _enqueue_explain_error(writer, start, "validation_error", ErrorKind.validation_error)
        return None, [], JSONResponse({"detail": "invalid query for embedding"}, status_code=422)
    except Exception as exc:
        logger.error("explain: embedding failed: %s", exc, exc_info=True)
        _enqueue_explain_error(writer, start, "internal_error", ErrorKind.other)
        return None, [], JSONResponse({"detail": "internal server error"}, status_code=500)

    # 3. Build inline router and rank
    inline_router = MultiCollectionRouter(
        search_url="",
        embedder=pipeline._embedder,
        shortlist_size=config.routing_shortlist_size,
        confidence_threshold=config.routing_confidence_threshold,
        embedding_model=pipeline._embedder.model_name,
    )
    scored_pairs = inline_router.rank_with_scores(query_vector, all_meta)

    # 4. ACL filter: only keep collections the caller's namespace is allowed to access
    scored_pairs = [(m, s) for m, s in scored_pairs if m.namespace == ns]

    if not scored_pairs:
        _enqueue_explain_error(writer, start, "validation_error", ErrorKind.validation_error)
        return None, [], JSONResponse({"detail": "no collections available"}, status_code=404)

    # 5. Build routing explain
    routing_candidates = [
        RoutingCandidate(collection=m.name, centroid_score=score)
        for m, score in scored_pairs
    ]

    chosen_meta, chosen_score = scored_pairs[0]

    confidence_threshold = config.routing_confidence_threshold
    chosen_below_threshold = bool(
        chosen_score is not None and chosen_score < confidence_threshold
    )

    routing = RoutingExplain(
        invoked=True,
        chosen_collection=chosen_meta.name,
        confidence_threshold=confidence_threshold,
        chosen_below_threshold=chosen_below_threshold,
        candidates=routing_candidates,
    )

    return routing, query_vector, None


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.post("/explain", response_model=ExplainResponse)
async def explain(
    body: ExplainRequest, request: Request
) -> ExplainResponse | JSONResponse:
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
            _enqueue_explain_error(writer, start, "internal_error", ErrorKind.other)
            return JSONResponse({"detail": "service unavailable"}, status_code=503)

        if meta is None:
            _enqueue_explain_error(writer, start, "validation_error", ErrorKind.validation_error)
            return JSONResponse({"detail": "collection not found"}, status_code=404)

        try:
            pipeline_result = await asyncio.wait_for(
                pipeline.explain(
                    body.query,
                    body.collection,
                    top_k=body.top_k,
                    rerank=body.rerank,
                    namespace=ns,
                ),
                timeout=_EXPLAIN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            _enqueue_explain_error(writer, start, "timeout", ErrorKind.timeout)
            logger.error(
                "explain timed out after %.1fs for collection %r",
                _EXPLAIN_TIMEOUT_SECONDS,
                body.collection,
                extra={"event_type": "explain_timeout"},
            )
            return JSONResponse({"detail": "explain timed out"}, status_code=504)
        except Exception as exc:
            logger.error(
                "explain: pipeline failed for collection %r: %s",
                body.collection,
                exc,
                exc_info=True,
            )
            _enqueue_explain_error(writer, start, "internal_error", ErrorKind.other)
            return JSONResponse({"detail": "internal server error"}, status_code=500)

        routing: RoutingExplain | None = None
        chosen_collection = body.collection

    else:
        # ----------------------------------------------------------------
        # Collectionless path — build routing explain
        # ----------------------------------------------------------------
        routing, query_vector, error_response = await _build_routing_explain(
            pipeline=pipeline,
            query=body.query,
            ns=ns,
            config=config,
            writer=writer,
            start=start,
        )
        if error_response is not None:
            return error_response

        chosen_collection = routing.chosen_collection  # type: ignore[union-attr]

        try:
            pipeline_result = await asyncio.wait_for(
                pipeline.explain(
                    body.query,
                    chosen_collection,
                    top_k=body.top_k,
                    rerank=body.rerank,
                    namespace=ns,
                    query_vector=query_vector,
                ),
                timeout=_EXPLAIN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            _enqueue_explain_error(writer, start, "timeout", ErrorKind.timeout)
            logger.error(
                "explain timed out after %.1fs for collection %r",
                _EXPLAIN_TIMEOUT_SECONDS,
                chosen_collection,
                extra={"event_type": "explain_timeout"},
            )
            return JSONResponse({"detail": "explain timed out"}, status_code=504)
        except Exception as exc:
            logger.error(
                "explain: pipeline failed for collection %r: %s",
                chosen_collection,
                exc,
                exc_info=True,
            )
            _enqueue_explain_error(writer, start, "internal_error", ErrorKind.other)
            return JSONResponse({"detail": "internal server error"}, status_code=500)

    response = ExplainResponse.from_pipeline_result(
        pipeline_result=pipeline_result,
        collection=chosen_collection,
        rerank=body.rerank,
        routing=routing,
    )

    # Telemetry (non-blocking)
    _enqueue_explain_success(
        writer,
        start,
        TelemetryEntry.from_explain_result(
            collection=chosen_collection,
            result_count=len(response.results),
            latency_ms=(monotonic() - start) * 1000.0,
        ),
    )

    return response

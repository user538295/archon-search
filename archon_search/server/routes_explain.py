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

import logging
import time
from contextlib import ExitStack
from time import monotonic
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from archon_search._types import IngestedBy
from archon_search.observability import bind_stage_recorder, correlation_id as _correlation_id
from archon_search.pipeline import ExplainStageError
from archon_search.router import MultiCollectionRouter
from archon_search.server.schemas import ExcludedCollectionSchema  # noqa: F401  (used by later tasks)
from archon_search.telemetry.entry import TelemetryEntry

if TYPE_CHECKING:
    from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
    from archon_search.pipeline import ExplainPipelineResult

logger = logging.getLogger("archon.search")

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
    stage_timings_ms: dict[str, float] | None = None

    @classmethod
    def from_pipeline_result(
        cls,
        *,
        rerank: bool,
        collection: str,
        routing: RoutingExplain | None,
        result: ExplainPipelineResult,
        stage_timings_ms: dict[str, float] | None = None,
    ) -> ExplainResponse:
        return cls(
            rerank=rerank,
            routing=routing,
            collection=collection,
            acl_filtered=result.acl_filtered,
            results=[ExplainResult.from_candidate(c) for c in result.top_results],
            near_misses=[ExplainNearMiss.from_candidate(c) for c in result.near_misses],
            stage_timings_ms=stage_timings_ms,
        )


@router.post("/explain", response_model=ExplainResponse)
async def explain_endpoint(body: ExplainRequest, request: Request) -> ExplainResponse | JSONResponse:
    """Return the per-stage retrieval/reranking trace for a query, plus the
    routing decision when no collection is pinned.

    503 is reserved for meta-lookup / router failures (mirrors A3's /search
    taxonomy); pipeline-stage failures (store / reranker) surface as 500 with a
    stage-specific detail. The query is never echoed in the response or telemetry.
    """
    start = monotonic()
    pipeline = request.app.state.pipeline
    config = request.app.state.config
    writer = getattr(request.app.state, "telemetry_writer", None)
    ns: str = request.state.namespace
    timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)

    def _emit_ok(collection: str, result_count: int) -> None:
        if writer is None:
            return
        try:
            writer.enqueue(
                TelemetryEntry.from_explain_result(
                    collection=collection,
                    result_count=result_count,
                    latency_ms=(monotonic() - start) * 1000.0,
                    correlation_id=_correlation_id.get(),
                )
            )
        except Exception as tel_exc:
            logger.warning("telemetry enqueue failed: %s", type(tel_exc).__name__)

    def _emit_err() -> None:
        if writer is None:
            return
        try:
            writer.enqueue(
                TelemetryEntry.from_error(
                    endpoint="explain",
                    status="internal_error",
                    error_kind="other",
                    latency_ms=(monotonic() - start) * 1000.0,
                    correlation_id=_correlation_id.get(),
                )
            )
        except Exception as tel_exc:
            logger.warning("telemetry enqueue failed: %s", type(tel_exc).__name__)

    routing: RoutingExplain | None = None
    query_vector: list[float] | None = None

    with ExitStack() as stack:
        recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
        t0 = time.perf_counter()

        if body.collection is not None:
            try:
                meta = await pipeline.get_collection_meta(body.collection, namespace=ns)
            except Exception as exc:
                logger.error("explain: meta lookup failed for %r: %s", body.collection, type(exc).__name__)
                _emit_err()
                return JSONResponse({"detail": "service unavailable"}, status_code=503)
            if meta is None:
                return JSONResponse({"detail": "collection not found"}, status_code=404)
            chosen = body.collection
        else:
            try:
                all_meta = await pipeline.get_all_collections_meta(namespace=ns)
            except Exception as exc:
                logger.error("explain: meta lookup failed: %s", type(exc).__name__)
                _emit_err()
                return JSONResponse({"detail": "service unavailable"}, status_code=503)
            if not all_meta:
                return JSONResponse({"detail": "no collections available"}, status_code=404)
            # all_meta is already namespace-filtered, which IS the collection-level ACL
            # boundary in this codebase: a caller only ever sees collections in its own
            # namespace, so disallowed collections can never leak into routing.candidates.
            try:
                query_vector = await pipeline._embedder.embed_one(body.query)
                col_router = MultiCollectionRouter(
                    search_url=f"http://{config.host}:{config.port}",
                    embedder=pipeline._embedder,
                    shortlist_size=config.routing_shortlist_size,
                    confidence_threshold=config.routing_confidence_threshold,
                    embedding_model=config.embedding_model,
                )
                ranked = col_router.rank_with_scores(query_vector, all_meta)
            except Exception as exc:
                logger.error("explain: routing failed: %s", type(exc).__name__)
                _emit_err()
                return JSONResponse({"detail": "service unavailable"}, status_code=503)
            # rank_with_scores returns every supplied collection, so ranked is non-empty.
            chosen_meta, chosen_score = ranked[0]
            chosen = chosen_meta.name
            threshold = config.routing_confidence_threshold
            routing = RoutingExplain(
                invoked=True,
                chosen_collection=chosen,
                confidence_threshold=threshold,
                chosen_below_threshold=(chosen_score is None or chosen_score < threshold),
                candidates=[RoutingCandidate(collection=m.name, centroid_score=s) for m, s in ranked],
            )

        try:
            result = await pipeline.explain(
                body.query,
                chosen,
                top_k=body.top_k,
                rerank=body.rerank,
                namespace=ns,
                query_vector=query_vector,
            )
        except ExplainStageError as exc:
            # Full original is logged server-side; the response detail is sanitized to
            # stage + exception type only — the original message could echo the query
            # (e.g. an FTS error), and the query must never leave the process.
            logger.warning("explain stage %s failed: %s", exc.stage, exc.original, exc_info=exc.original)
            _emit_err()
            return JSONResponse(
                {"detail": f"{exc.stage} error: {type(exc.original).__name__}"}, status_code=500
            )
        except Exception as exc:
            logger.error("explain failed for %r: %s", chosen, exc, exc_info=True)
            _emit_err()
            return JSONResponse({"detail": "explain failed"}, status_code=500)

        if recorder is not None:
            recorder.record("total", (time.perf_counter() - t0) * 1000.0)
            logger.info(
                "stage timings",
                extra={
                    "event_type": "stage_timings",
                    "correlation_id": _correlation_id.get(),
                    "endpoint": "explain",
                    "collection": chosen,
                    "stage_timings_ms": recorder.stage_timings_ms,
                },
            )

    stage_timings = recorder.stage_timings_ms if recorder is not None else None
    response = ExplainResponse.from_pipeline_result(
        rerank=body.rerank, collection=chosen, routing=routing, result=result,
        stage_timings_ms=stage_timings,
    )
    _emit_ok(chosen, len(response.results))
    result_dict = response.model_dump(mode="json")
    if stage_timings is None:
        result_dict.pop("stage_timings_ms", None)
    return JSONResponse(content=result_dict, status_code=200)

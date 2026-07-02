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
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from archon_search._types import IngestedBy
from archon_search.hyde import resolve_hyde_vector
from archon_search.observability import bind_stage_recorder, correlation_id as _correlation_id
from archon_search.pipeline import (
    CollectionNotFoundError,
    ExplainStageError,
    FanoutTimeoutError,
    MetadataLookupError,
)
from archon_search.router import MultiCollectionRouter
from archon_search.server.schemas import ExcludedCollectionSchema
from archon_search.telemetry.entry import TelemetryEntry

if TYPE_CHECKING:
    from archon_search._diagnostics import (
        GraphProvenance,
        ScoredSearchCandidate,
        SearchScoreBreakdown,
    )
    from archon_search.pipeline import ExplainPipelineResult

logger = logging.getLogger(__name__)

router = APIRouter()


def _final_score(b: SearchScoreBreakdown) -> float:
    """reranker_score when a reranker ran, else the fused RRF score."""
    return b.reranker_score if b.reranker_score is not None else b.rrf_score


class TraversalStepResponse(BaseModel):
    """Pydantic response model for a single graph traversal hop.

    Enforces the invariant that at least one of ``relationship``,
    ``community_id``, or ``chunk_id`` is set — degenerate steps (all three
    null) are meaningless and are rejected at the wire boundary.

    Populated from ``archon_search._diagnostics.TraversalStep`` dataclass
    instances by ``GraphProvenanceResponse.from_provenance()``.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    entity: str
    entity_id: str
    relationship: str | None = None
    community_id: str | None = None
    chunk_id: str | None = None

    @model_validator(mode="after")
    def _at_least_one_optional_set(self) -> "TraversalStepResponse":
        if self.relationship is None and self.community_id is None and self.chunk_id is None:
            raise ValueError(
                "TraversalStep must have at least one of relationship, community_id, or chunk_id set"
            )
        return self


class GraphProvenanceResponse(BaseModel):
    """Pydantic response model for the full graph traversal chain of a chunk.

    An empty ``steps`` list is valid and signals a graph-layer bug (a chunk
    was attributed to graph retrieval but no traversal path was recorded).
    It is returned as-is rather than masked as null — surfacing it to the
    operator is the correct behaviour (S11).
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    steps: list[TraversalStepResponse]

    @classmethod
    def from_provenance(cls, prov: "GraphProvenance") -> "GraphProvenanceResponse":
        """Build from a ``GraphProvenance`` dataclass instance.

        Uses ``TraversalStepResponse.model_validate`` (enabled by ``from_attributes=True``)
        to map each ``TraversalStep`` dataclass to its Pydantic response model.  The
        ``_at_least_one_optional_set`` validator runs during this coercion, so
        degenerate steps (all three optional fields null) raise ``ValidationError``.
        """
        return cls(
            steps=[TraversalStepResponse.model_validate(s) for s in prov.steps]
        )


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
    language: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)
    acl: list[str] | None = None
    collection: str = ""
    graph_provenance: GraphProvenanceResponse | None = None

    @classmethod
    def from_candidate(cls, c: ScoredSearchCandidate) -> ExplainResult:
        graph_prov: GraphProvenanceResponse | None = None
        if c.graph_provenance is not None:
            graph_prov = GraphProvenanceResponse.from_provenance(c.graph_provenance)
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
            collection=c.collection,
            graph_provenance=graph_prov,
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
    language: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)
    acl: list[str] | None = None
    collection: str = ""
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
            collection=c.collection,
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


class RagFusionSubQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant_index: int
    result_count: int
    top_doc_ids: list[str]


class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    collection: str | None = None
    collections: list[str] | None = None
    top_k: int = Field(default=5, ge=1)
    rerank: bool = True
    hyde: bool = False
    rag_fusion: bool = False
    graph_mode: Literal["naive", "local", "global"] | None = None

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

    @field_validator("collections")
    @classmethod
    def _collections_clean(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if len(v) == 0:
            raise ValueError("collections must not be empty")
        deduped: list[str] = []
        for name in v:
            stripped = name.strip()
            if not stripped:
                raise ValueError("collection names must not be empty or whitespace")
            if stripped not in deduped:
                deduped.append(stripped)
        return deduped

    @model_validator(mode="after")
    def _validate_collection_selection(self) -> "ExplainRequest":
        if self.collection is not None and self.collections is not None:
            raise ValueError("supply either collection or collections, not both")
        # Neither set stays valid: explain falls back to centroid routing.
        if self.collections is not None and self.rerank is False and len(self.collections) > 1:
            raise ValueError(
                "reranking cannot be disabled for multi-collection search in v1"
            )
        return self


class ExplainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rerank: bool
    routing: RoutingExplain | None
    collection: str = ""
    acl_filtered: bool
    results: list[ExplainResult]
    near_misses: list[ExplainNearMiss]
    excluded_collections: list[ExcludedCollectionSchema] = Field(default_factory=list)
    embedding_model: str = ""
    hyde_applied: bool = False
    stage_timings_ms: dict[str, float] | None = None
    rag_fusion_applied: bool = False
    rag_fusion_queries_used: int = 0
    rag_fusion_attempted: bool = False
    rag_fusion_failure_reason: str | None = None
    rag_fusion_sub_queries: list[RagFusionSubQueryResult] | None = None
    graph_mode_applied: Literal["naive", "local", "global"] | None = None

    @classmethod
    def from_pipeline_result(
        cls,
        *,
        rerank: bool,
        collection: str,
        routing: RoutingExplain | None,
        result: ExplainPipelineResult,
        embedding_model: str = "",
        hyde_applied: bool = False,
        stage_timings_ms: dict[str, float] | None = None,
        rag_fusion_applied: bool = False,
        rag_fusion_queries_used: int = 0,
        rag_fusion_attempted: bool = False,
        rag_fusion_failure_reason: str | None = None,
        rag_fusion_sub_query_results: list | None = None,
        graph_mode_applied: Literal["naive", "local", "global"] | None = None,
    ) -> ExplainResponse:
        sub_queries: list[RagFusionSubQueryResult] | None = None
        if rag_fusion_sub_query_results is not None:
            sub_queries = [
                RagFusionSubQueryResult(
                    variant_index=r.variant_index,
                    result_count=r.result_count,
                    top_doc_ids=r.top_doc_ids,
                )
                for r in rag_fusion_sub_query_results
            ]
        return cls(
            rerank=rerank,
            routing=routing,
            collection=collection,
            acl_filtered=result.acl_filtered,
            results=[ExplainResult.from_candidate(c) for c in result.top_results],
            near_misses=[ExplainNearMiss.from_candidate(c) for c in result.near_misses],
            excluded_collections=[
                ExcludedCollectionSchema(name=e.name, reason=e.reason)
                for e in result.excluded_collections
            ],
            embedding_model=embedding_model,
            hyde_applied=hyde_applied,
            stage_timings_ms=stage_timings_ms,
            rag_fusion_applied=rag_fusion_applied,
            rag_fusion_queries_used=rag_fusion_queries_used,
            rag_fusion_attempted=rag_fusion_attempted,
            rag_fusion_failure_reason=rag_fusion_failure_reason,
            rag_fusion_sub_queries=sub_queries,
            graph_mode_applied=graph_mode_applied,
        )


@router.post("/explain", response_model=ExplainResponse)
async def explain_endpoint(body: ExplainRequest, request: Request) -> ExplainResponse | JSONResponse:
    """Return the per-stage retrieval/reranking trace for a query, plus the
    routing decision when no collection is pinned.

    503 is reserved for meta-lookup / router failures (mirrors A3's /search
    taxonomy); pipeline-stage failures (store / reranker) surface as 500 with a
    stage-specific detail. The query is never echoed in the response or telemetry.
    """
    # Late-bound import: archon_search.rag_fusion may be reloaded in tests,
    # so we import lazily to always get the current class from sys.modules.
    from archon_search.rag_fusion import RAGFusionDependencyError  # noqa: PLC0415

    start = monotonic()
    pipeline = request.app.state.pipeline
    config = request.app.state.config
    writer = getattr(request.app.state, "telemetry_writer", None)
    ns: str = request.state.namespace
    timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)

    def _emit_ok(
        collection: str,
        result_count: int,
        rag_fusion_applied: bool | None = None,
        rag_fusion_queries_used: int | None = None,
    ) -> None:
        if writer is None:
            return
        try:
            writer.enqueue(
                TelemetryEntry.from_explain_result(
                    collection=collection,
                    result_count=result_count,
                    latency_ms=(monotonic() - start) * 1000.0,
                    correlation_id=_correlation_id.get(),
                    rag_fusion_applied=rag_fusion_applied,
                    rag_fusion_queries_used=rag_fusion_queries_used,
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

    # Config-wired fanout and top_k validation (moved from Pydantic layer).
    if body.collections is not None and len(body.collections) > config.max_fanout:
        return JSONResponse(
            {"detail": f"collections length exceeds maximum of {config.max_fanout}"},
            status_code=422,
        )
    if body.top_k > config.top_k_max:
        return JSONResponse(
            {"detail": f"top_k {body.top_k} exceeds operator-configured maximum of {config.top_k_max}"},
            status_code=422,
        )

    routing: RoutingExplain | None = None
    rag_fusion_gen = getattr(request.app.state, "rag_fusion_generator", None)

    # Mutual exclusion: rag_fusion=True suppresses HyDE entirely.
    if body.rag_fusion:
        hyde_vector, hyde_applied = None, False
    else:
        generator = getattr(request.app.state, "hyde_generator", None)
        try:
            hyde_vector, hyde_applied = await resolve_hyde_vector(
                body.query, body.hyde, generator, config.hyde
            )
        except RuntimeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)

    # graph_mode and HyDE are mutually exclusive — graph_mode wins.
    # Null the vector so HyDE does not drive retrieval even in the stub path.
    # Computed once here so both the single-collection and multi-collection
    # response paths inherit the correct value without repeating the check.
    if body.graph_mode is not None:
        hyde_applied = False
        hyde_vector = None

    with ExitStack() as stack:
        recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
        t0 = time.perf_counter()

        if body.collections is not None:
            # Multi-collection fan-out path (B3). Routing is bypassed; the pipeline
            # resolves scope/exclusions and merges legs into a single reranked pool.
            try:
                result = await pipeline.explain(
                    body.query,
                    collections=body.collections,
                    top_k=body.top_k,
                    rerank=body.rerank,
                    namespace=ns,
                    query_vector=hyde_vector,
                    embedder=None,
                    rag_fusion=body.rag_fusion,
                    rag_fusion_generator=rag_fusion_gen,
                    rag_fusion_config=config.rag_fusion,
                )
            except RAGFusionDependencyError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=422)
            except CollectionNotFoundError:
                return JSONResponse({"detail": "collection not found"}, status_code=404)
            except MetadataLookupError:
                _emit_err()
                return JSONResponse({"detail": "service unavailable"}, status_code=503)
            except FanoutTimeoutError:
                _emit_err()
                return JSONResponse({"detail": "Search timed out"}, status_code=504)
            except ExplainStageError as exc:
                logger.warning("explain stage %s failed: %s", exc.stage, exc.original, exc_info=exc.original)
                _emit_err()
                return JSONResponse(
                    {"detail": f"{exc.stage} error: {type(exc.original).__name__}"}, status_code=500
                )
            except Exception as exc:
                logger.error("multi-collection explain failed: %s", exc, exc_info=True)
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
                        "collection": "",
                        "stage_timings_ms": recorder.stage_timings_ms,
                    },
                )

            stage_timings = recorder.stage_timings_ms if recorder is not None else None
            response = ExplainResponse.from_pipeline_result(
                rerank=body.rerank, collection="", routing=None, result=result,
                embedding_model=config.embedding_model,
                hyde_applied=hyde_applied,
                stage_timings_ms=stage_timings,
                rag_fusion_applied=result.rag_fusion_applied,
                rag_fusion_queries_used=result.rag_fusion_queries_used,
                rag_fusion_attempted=result.rag_fusion_attempted,
                rag_fusion_failure_reason=result.rag_fusion_failure_reason,
                rag_fusion_sub_query_results=result.rag_fusion_sub_query_results,
                graph_mode_applied=result.graph_mode_applied,
            )
            _emit_ok(
                "",
                len(response.results),
                rag_fusion_applied=result.rag_fusion_applied,
                rag_fusion_queries_used=result.rag_fusion_queries_used,
            )
            result_dict = response.model_dump(mode="json")
            if stage_timings is None:
                result_dict.pop("stage_timings_ms", None)
            return JSONResponse(content=result_dict, status_code=200)

        active_model: str = config.embedding_model
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
            active_model = meta.active_embedding_model or config.embedding_model
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
                routing_vector = await pipeline._global_embedder.embed_one(body.query)
                col_router = MultiCollectionRouter(
                    search_url=f"http://{config.host}:{config.port}",
                    embedder=pipeline._global_embedder,
                    shortlist_size=config.routing_shortlist_size,
                    confidence_threshold=config.routing_confidence_threshold,
                    embedding_model=config.embedding_model,
                )
                ranked = col_router.rank_with_scores(routing_vector, all_meta)
            except Exception as exc:
                logger.error("explain: routing failed: %s", type(exc).__name__)
                _emit_err()
                return JSONResponse({"detail": "service unavailable"}, status_code=503)
            # rank_with_scores returns every supplied collection, so ranked is non-empty.
            chosen_meta, chosen_score = ranked[0]
            chosen = chosen_meta.name
            active_model = chosen_meta.active_embedding_model or config.embedding_model
            threshold = config.routing_confidence_threshold
            routing = RoutingExplain(
                invoked=True,
                chosen_collection=chosen,
                confidence_threshold=threshold,
                chosen_below_threshold=(chosen_score is None or chosen_score < threshold),
                candidates=[RoutingCandidate(collection=m.name, centroid_score=s) for m, s in ranked],
            )

        embedder_cache = getattr(request.app.state, "embedder_cache", None)
        if embedder_cache is not None:
            _embedder = await embedder_cache.get_or_load(active_model)
        else:
            logger.warning("explain: embedder_cache absent from app.state — falling back to global embedder")
            _embedder = pipeline._global_embedder

        try:
            result = await pipeline.explain(
                body.query,
                chosen,
                top_k=body.top_k,
                rerank=body.rerank,
                namespace=ns,
                query_vector=hyde_vector,
                embedder=_embedder,
                rag_fusion=body.rag_fusion,
                rag_fusion_generator=rag_fusion_gen,
                rag_fusion_config=config.rag_fusion,
                graph_mode=body.graph_mode,
            )
        except RAGFusionDependencyError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
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
        embedding_model=active_model,
        hyde_applied=hyde_applied,
        stage_timings_ms=stage_timings,
        rag_fusion_applied=result.rag_fusion_applied,
        rag_fusion_queries_used=result.rag_fusion_queries_used,
        rag_fusion_attempted=result.rag_fusion_attempted,
        rag_fusion_failure_reason=result.rag_fusion_failure_reason,
        rag_fusion_sub_query_results=result.rag_fusion_sub_query_results,
        graph_mode_applied=result.graph_mode_applied,
    )
    _emit_ok(
        chosen,
        len(response.results),
        rag_fusion_applied=result.rag_fusion_applied,
        rag_fusion_queries_used=result.rag_fusion_queries_used,
    )
    result_dict = response.model_dump(mode="json")
    if stage_timings is None:
        result_dict.pop("stage_timings_ms", None)
    return JSONResponse(content=result_dict, status_code=200)

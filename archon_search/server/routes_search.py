"""POST /search endpoint — delegates to SearchPipeline (Task 3.4)."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import ExitStack
from time import monotonic
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from archon_search._types import SearchResult
from archon_search.filters import SearchFilters
from archon_search.hyde import resolve_hyde_vector
from archon_search.pipeline import (
    CollectionNotFoundError,
    FanoutTimeoutError,
    GraphCommunitiesNotBuiltError,
    MetadataLookupError,
)
from archon_search.rag_fusion import RAGFusionDependencyError
from archon_search.server.schemas import ExcludedCollectionSchema
from archon_search.observability import bind_stage_recorder, correlation_id as _correlation_id
from archon_search.telemetry.entry import FilterFlags, TelemetryEntry

# TODO: make configurable via config.py (see /route for parity)
_SEARCH_TIMEOUT_SECONDS = 30.0

_HYDE_EXPANSION_FAILED_WARNING = "HyDE expansion failed"

logger = logging.getLogger(__name__)

router = APIRouter()


def _check_scope_filter(scope_filter: str | None) -> str | None:
    """Return an error message string if ``scope_filter`` is syntactically invalid, else ``None``.

    Valid values: no wildcard (exact match) or exactly one trailing ``*`` with a non-empty prefix.
    Invalid: bare ``*``, leading ``*``, mid-string ``*``, multiple ``*``.
    """
    if scope_filter is None:
        return None
    if not scope_filter:
        return "scope_filter must not be empty"
    if "*" not in scope_filter:
        return None  # exact match — always valid
    star_count = scope_filter.count("*")
    if star_count > 1:
        return "scope_filter contains multiple '*' characters; only a single trailing '*' is permitted"
    # Exactly one '*' — must be at the end with a non-empty prefix
    if not scope_filter.endswith("*"):
        return "scope_filter wildcard '*' must appear only at the end of the string"
    prefix = scope_filter[:-1]
    if not prefix:
        return "bare '*' is not a valid scope_filter; use a prefix followed by '*' for wildcard matching"
    return None  # valid trailing wildcard e.g. 'user:*' or 'user:alice*'


class SearchRequest(BaseModel):
    collection: str | None = None
    collections: list[str] | None = None
    query: str
    top_k: int = Field(default=5, ge=1)
    filters: SearchFilters | None = None
    hyde: bool = False
    rag_fusion: bool = False
    graph_mode: Literal["naive", "local", "global"] | None = None
    scope_filter: str | None = None

    @field_validator("collection")
    @classmethod
    def collection_nonempty(cls, v: str | None) -> str | None:
        if v is None:
            return v
        stripped = v.strip()
        if not stripped:
            raise ValueError("collection must not be empty")
        return stripped

    @field_validator("query")
    @classmethod
    def query_nonempty(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("query must not be empty")
        return stripped

    @model_validator(mode="after")
    def _validate_collection_selection(self) -> "SearchRequest":
        if self.collection is not None and self.collections is not None:
            raise ValueError("supply either collection or collections, not both")
        if self.collection is None and self.collections is None:
            raise ValueError("supply either collection or collections")
        if self.collections is not None:
            if len(self.collections) == 0:
                raise ValueError("collections must not be empty")
            stripped: list[str] = []
            for name in self.collections:
                s = name.strip()
                if not s:
                    raise ValueError("collection names must not be empty or whitespace")
                stripped.append(s)
            deduped: list[str] = []
            for s in stripped:
                if s not in deduped:
                    deduped.append(s)
            self.collections = deduped
        return self


class SearchResultSchema(BaseModel):
    doc_id: str
    chunk_id: str
    text: str
    score: float
    source_path: str
    file_type: str = ""
    language: str = ""
    indexed_at: str = ""
    updated_at: str = ""
    ingested_by: str = "cli"
    metadata: dict[str, str] = Field(default_factory=dict)
    acl: list[str] | None = None
    collection: str = ""

    @classmethod
    def from_result(cls, r: SearchResult) -> "SearchResultSchema":
        return cls(
            doc_id=r.doc_id,
            chunk_id=r.chunk_id,
            text=r.text,
            score=r.score,
            source_path=r.source_path,
            file_type=r.file_type,
            language=r.language,
            indexed_at=r.indexed_at,
            updated_at=r.updated_at,
            ingested_by=r.ingested_by,
            metadata=r.metadata,
            acl=r.acl,
            collection=r.collection,
        )


class SearchResponse(BaseModel):
    results: list[SearchResultSchema]
    acl_filtered: bool
    excluded_collections: list[ExcludedCollectionSchema] = Field(default_factory=list)
    embedding_model: str = ""
    hyde_applied: bool = False
    rag_fusion_applied: bool = False
    rag_fusion_queries_used: int = 0
    rag_fusion_attempted: bool = False
    graph_expansion_applied: bool = False
    expansion_used: bool = False
    expansion_warning: str | None = None
    applied_filters: SearchFilters | None = None


@router.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, request: Request) -> SearchResponse | JSONResponse:
    pipeline = request.app.state.pipeline
    ns = request.state.namespace
    writer = getattr(request.app.state, "telemetry_writer", None)
    config = request.app.state.config
    start = monotonic()
    timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)

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

    # scope_filter syntax guard (400, not 422 — invalid input, not server-state conflict)
    scope_filter_err = _check_scope_filter(body.scope_filter)
    if scope_filter_err is not None:
        return JSONResponse(
            {"detail": {"code": "invalid_scope_filter", "message": scope_filter_err}},
            status_code=400,
        )

    # scope_filter + graph_mode are mutually exclusive (graph paths bypass scope predicates)
    if body.scope_filter is not None and body.graph_mode is not None:
        return JSONResponse(
            {"detail": "scope_filter is not supported with graph_mode"},
            status_code=422,
        )

    # graph_mode guard: require [graph] enabled=true
    if body.graph_mode is not None and not config.graph.enabled:
        return JSONResponse(
            {"detail": "graph_mode requires [graph] enabled=true in server config"},
            status_code=422,
        )

    # Resolve HyDE vector and RAG Fusion generator — mutual exclusion: rag_fusion wins
    rag_fusion_gen = getattr(request.app.state, "rag_fusion_generator", None)
    if body.rag_fusion:
        hyde_vector, hyde_applied = None, False
        hyde_expansion_warning: str | None = None
    else:
        generator = getattr(request.app.state, "hyde_generator", None)
        try:
            hyde_vector, hyde_applied = await resolve_hyde_vector(body.query, body.hyde, generator, config.hyde)
        except RuntimeError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        # HyDE failure: requested but returned no vector
        hyde_expansion_warning = _HYDE_EXPANSION_FAILED_WARNING if (body.hyde and not hyde_applied) else None

    if body.collections is not None:
        try:
            result = await pipeline.search_many(
                body.query,
                body.collections,
                namespace=ns,
                query_vector=hyde_vector,
                rag_fusion=body.rag_fusion,
                rag_fusion_generator=rag_fusion_gen,
                rag_fusion_config=config.rag_fusion,
                filters=body.filters,
                graph_mode=body.graph_mode,
                scope_filter=body.scope_filter,
            )
        except RAGFusionDependencyError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=422)
        except GraphCommunitiesNotBuiltError as exc:
            return JSONResponse(
                {"detail": {"code": "graph_communities_not_built", "message": str(exc)}}, status_code=422
            )
        except CollectionNotFoundError:
            return JSONResponse({"detail": "collection not found"}, status_code=404)
        except MetadataLookupError:
            return JSONResponse({"detail": "service unavailable"}, status_code=503)
        except FanoutTimeoutError:
            raise HTTPException(status_code=504, detail="Search timed out")
        schemas = [SearchResultSchema.from_result(r) for r in result.results]
        if writer is not None:
            try:
                excluded_count = len(result.excluded_collections)
                writer.enqueue(
                    TelemetryEntry.from_search_multi_result(
                        collections=body.collections,
                        fanout_count=len(body.collections) - excluded_count,
                        result_count=len(result.results),
                        latency_ms=(monotonic() - start) * 1000.0,
                        excluded_count=excluded_count,
                        correlation_id=_correlation_id.get(),
                        rag_fusion_applied=result.rag_fusion_applied,
                        rag_fusion_queries_used=result.rag_fusion_queries_used,
                    )
                )
            except Exception:
                logger.warning("telemetry: search_multi entry enqueue failed", exc_info=True)
        _multi_expansion_warning = hyde_expansion_warning or result.rag_fusion_warning
        return SearchResponse(
            results=schemas,
            acl_filtered=result.acl_filtered,
            excluded_collections=[
                ExcludedCollectionSchema(name=e.name, reason=e.reason)
                for e in result.excluded_collections
            ],
            embedding_model=config.embedding_model,
            hyde_applied=hyde_applied,
            rag_fusion_applied=result.rag_fusion_applied,
            rag_fusion_queries_used=result.rag_fusion_queries_used,
            rag_fusion_attempted=result.rag_fusion_attempted,
            graph_expansion_applied=result.graph_expansion_applied,
            expansion_used=hyde_applied or result.rag_fusion_applied or result.graph_expansion_applied,
            expansion_warning=_multi_expansion_warning,
            applied_filters=body.filters,
        )

    try:
        meta = await pipeline.get_collection_meta(body.collection, namespace=ns)
    except Exception as exc:
        logger.error("search: meta lookup failed for collection %r: %s", body.collection, exc, exc_info=True)
        return JSONResponse({"detail": "service unavailable"}, status_code=503)

    if meta is None:
        return JSONResponse({"detail": "collection not found"}, status_code=404)

    with ExitStack() as stack:
        recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
        t0 = time.perf_counter()

        def _emit_timings() -> None:
            if recorder is not None:
                recorder.record("total", (time.perf_counter() - t0) * 1000.0)
                logger.info(
                    "stage timings",
                    extra={
                        "event_type": "stage_timings",
                        "correlation_id": _correlation_id.get(),
                        "endpoint": "search",
                        "collection": body.collection,
                        "stage_timings_ms": recorder.stage_timings_ms,
                    },
                )

        try:
            embedder_cache = getattr(request.app.state, "embedder_cache", None)
            active_model = meta.active_embedding_model or config.embedding_model
            if embedder_cache is not None:
                embedder = await embedder_cache.get_or_load(active_model)
            else:
                logger.warning("search: embedder_cache absent from app.state — falling back to global embedder")
                embedder = pipeline._global_embedder
                active_model = config.embedding_model
            result = await asyncio.wait_for(
                pipeline.search(
                    body.query,
                    body.collection,
                    namespace=ns,
                    embedder=embedder,
                    filters=body.filters,
                    query_vector=hyde_vector,
                    rag_fusion=body.rag_fusion,
                    rag_fusion_generator=rag_fusion_gen,
                    rag_fusion_config=config.rag_fusion,
                    graph_mode=body.graph_mode,
                    scope_filter=body.scope_filter,
                ),
                timeout=_SEARCH_TIMEOUT_SECONDS,
            )
            include_metadata = body.filters is not None and body.filters.include_metadata
            schemas = [SearchResultSchema.from_result(r) for r in result.results]
            if not include_metadata:
                for schema in schemas:
                    schema.metadata = {}
            if writer is not None:
                try:
                    flags = FilterFlags.from_search_filters(body.filters) if body.filters is not None else FilterFlags()
                    writer.enqueue(
                        TelemetryEntry.from_search_tool_result(
                            endpoint="search",
                            collection=body.collection,
                            result_doc_ids=[r.doc_id for r in result.results],
                            latency_ms=(monotonic() - start) * 1000.0,
                            filter_flags=flags,
                            correlation_id=_correlation_id.get(),
                            rag_fusion_applied=result.rag_fusion_applied,
                            rag_fusion_queries_used=result.rag_fusion_queries_used,
                            doc_id_hasher=getattr(request.app.state, "doc_id_hasher", None),
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search entry enqueue failed", exc_info=True)
            _emit_timings()
            _expansion_warning = hyde_expansion_warning or result.rag_fusion_warning
            return SearchResponse(
                results=schemas,
                acl_filtered=result.acl_filtered,
                excluded_collections=[
                    ExcludedCollectionSchema(name=e.name, reason=e.reason)
                    for e in result.excluded_collections
                ],
                embedding_model=active_model,
                hyde_applied=hyde_applied,
                rag_fusion_applied=result.rag_fusion_applied,
                rag_fusion_queries_used=result.rag_fusion_queries_used,
                rag_fusion_attempted=result.rag_fusion_attempted,
                graph_expansion_applied=result.graph_expansion_applied,
                expansion_used=hyde_applied or result.rag_fusion_applied or result.graph_expansion_applied,
                expansion_warning=_expansion_warning,
                applied_filters=body.filters,
            )
        except RAGFusionDependencyError as exc:
            _emit_timings()
            return JSONResponse({"detail": str(exc)}, status_code=422)
        except GraphCommunitiesNotBuiltError as exc:
            _emit_timings()
            return JSONResponse(
                {"detail": {"code": "graph_communities_not_built", "message": str(exc)}}, status_code=422
            )
        except asyncio.TimeoutError:
            _emit_timings()
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_error(
                            endpoint="search",
                            status="timeout",
                            error_kind="timeout",
                            latency_ms=(monotonic() - start) * 1000.0,
                            correlation_id=_correlation_id.get(),
                        )
                    )
                except Exception as tel_exc:
                    logger.warning("telemetry enqueue failed: %s", type(tel_exc).__name__)
            logger.error(
                "search pipeline timed out",
                extra={"event_type": "search_timeout"},
            )
            raise HTTPException(status_code=504, detail="Search timed out")
        except Exception as exc:
            _emit_timings()
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_error(
                            endpoint="search",
                            status="internal_error",
                            error_kind="other",
                            latency_ms=(monotonic() - start) * 1000.0,
                            correlation_id=_correlation_id.get(),
                        )
                    )
                except Exception as tel_exc:
                    logger.warning("telemetry enqueue failed: %s", type(tel_exc).__name__)
            logger.error(
                "search pipeline failed: %s",
                type(exc).__name__,
                extra={"event_type": "search_pipeline_failure"},
                exc_info=True,
            )
            raise

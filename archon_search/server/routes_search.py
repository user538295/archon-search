"""POST /search endpoint — delegates to SearchPipeline (Task 3.4)."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import ExitStack
from time import monotonic

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from archon_search._types import SearchResult
from archon_search.filters import SearchFilters
from archon_search.pipeline import (
    CollectionNotFoundError,
    FanoutTimeoutError,
    MetadataLookupError,
)
from archon_search.server.schemas import ExcludedCollectionSchema
from archon_search.observability import bind_stage_recorder, correlation_id as _correlation_id
from archon_search.telemetry.entry import FilterFlags, TelemetryEntry

# TODO: make configurable via config.py (see /route for parity)
_SEARCH_TIMEOUT_SECONDS = 30.0

_FANOUT_VALIDATION_LIMIT = 8  # Pydantic-layer cap; must match SearchConfig.max_fanout default. See B3 known limitations.

logger = logging.getLogger(__name__)

router = APIRouter()


class SearchRequest(BaseModel):
    collection: str | None = None
    collections: list[str] | None = None
    query: str
    top_k: int = Field(default=5, ge=1, le=100)
    filters: SearchFilters | None = None

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
            if len(self.collections) > _FANOUT_VALIDATION_LIMIT:
                raise ValueError(
                    f"collections length exceeds maximum of {_FANOUT_VALIDATION_LIMIT}"
                )
            if self.filters is not None:
                raise ValueError("filters are not supported for multi-collection search in v1")
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


@router.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, request: Request) -> SearchResponse | JSONResponse:
    pipeline = request.app.state.pipeline
    ns = request.state.namespace
    writer = getattr(request.app.state, "telemetry_writer", None)
    config = request.app.state.config
    start = monotonic()
    timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)

    if body.collections is not None:
        try:
            result = await pipeline.search_many(body.query, body.collections, namespace=ns)
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
                    )
                )
            except Exception:
                logger.warning("telemetry: search_multi entry enqueue failed", exc_info=True)
        return SearchResponse(
            results=schemas,
            acl_filtered=result.acl_filtered,
            excluded_collections=[
                ExcludedCollectionSchema(name=e.name, reason=e.reason)
                for e in result.excluded_collections
            ],
            embedding_model=config.embedding_model,
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
                pipeline.search(body.query, body.collection, namespace=ns, embedder=embedder, filters=body.filters),
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
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search entry enqueue failed", exc_info=True)
            _emit_timings()
            return SearchResponse(
                results=schemas,
                acl_filtered=result.acl_filtered,
                excluded_collections=[
                    ExcludedCollectionSchema(name=e.name, reason=e.reason)
                    for e in result.excluded_collections
                ],
                embedding_model=active_model,
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

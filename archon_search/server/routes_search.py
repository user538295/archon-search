"""POST /search endpoint — delegates to SearchPipeline (Task 3.4)."""
from __future__ import annotations

import logging
from time import monotonic

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from archon_search._types import SearchResult
from archon_search.filters import SearchFilters
from archon_search.telemetry.entry import ErrorKind, FilterFlags, TelemetryEntry

logger = logging.getLogger("archon.search")

router = APIRouter()


class SearchRequest(BaseModel):
    collection: str
    query: str
    top_k: int = Field(default=5, ge=1, le=100)
    filters: SearchFilters | None = None

    @field_validator("collection")
    @classmethod
    def collection_nonempty(cls, v: str) -> str:
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


class SearchResultSchema(BaseModel):
    doc_id: str
    chunk_id: str
    text: str
    score: float
    source_path: str
    file_type: str = ""
    indexed_at: str = ""
    updated_at: str = ""
    ingested_by: str = "cli"
    metadata: dict[str, str] = Field(default_factory=dict)
    language: str | None = None
    acl: list[str] | None = None

    @classmethod
    def from_result(cls, r: SearchResult, *, include_metadata: bool = True) -> "SearchResultSchema":
        return cls(
            doc_id=r.doc_id,
            chunk_id=r.chunk_id,
            text=r.text,
            score=r.score,
            source_path=r.source_path,
            file_type=r.file_type,
            indexed_at=r.indexed_at,
            updated_at=r.updated_at,
            ingested_by=r.ingested_by,
            metadata=r.metadata if include_metadata else {},
            language=r.language,
            acl=r.acl,
        )


class SearchResponse(BaseModel):
    results: list[SearchResultSchema]
    acl_filtered: bool


@router.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, request: Request) -> SearchResponse | JSONResponse:
    pipeline = request.app.state.pipeline
    ns = request.state.namespace
    include_metadata = bool(body.filters and body.filters.include_metadata)
    writer = getattr(request.app.state, "telemetry_writer", None)
    start = monotonic()

    try:
        meta = await pipeline.get_collection_meta(body.collection, namespace=ns)
    except Exception as exc:
        logger.error("search: meta lookup failed for collection %r: %s", body.collection, exc, exc_info=True)
        return JSONResponse({"detail": "service unavailable"}, status_code=503)

    if meta is None:
        return JSONResponse({"detail": "collection not found"}, status_code=404)

    try:
        result = await pipeline.search(body.query, body.collection, namespace=ns, filters=body.filters)
        if writer is not None:
            try:
                _ff = FilterFlags(
                    file_type=bool(body.filters and body.filters.file_type is not None),
                    source_path_prefix=bool(body.filters and body.filters.source_path_prefix is not None),
                    source_path_glob=bool(body.filters and body.filters.source_path_glob is not None),
                    indexed_after=bool(body.filters and body.filters.indexed_after is not None),
                    indexed_before=bool(body.filters and body.filters.indexed_before is not None),
                    include_metadata=bool(body.filters and body.filters.include_metadata),
                )
                writer.enqueue(
                    TelemetryEntry.from_search_tool_result(
                        endpoint="search",
                        collection=body.collection,
                        result_doc_ids=[r.doc_id for r in result.results],
                        latency_ms=(monotonic() - start) * 1000.0,
                        filter_flags=_ff,
                    )
                )
            except Exception:
                logger.warning("telemetry: search entry enqueue failed", exc_info=True)
        return SearchResponse(
            results=[
                SearchResultSchema.from_result(r, include_metadata=include_metadata)
                for r in result.results
            ],
            acl_filtered=result.acl_filtered,
        )
    except Exception as exc:
        logger.warning("search failed for collection %r: %s", body.collection, exc, exc_info=True)
        if writer is not None:
            try:
                writer.enqueue(
                    TelemetryEntry.from_error(
                        endpoint="search",
                        status="internal_error",
                        error_kind=ErrorKind.other,
                        latency_ms=(monotonic() - start) * 1000.0,
                    )
                )
            except Exception:
                logger.warning("telemetry: search error entry enqueue failed", exc_info=True)
        return SearchResponse(results=[], acl_filtered=False)

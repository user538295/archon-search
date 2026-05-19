"""POST /search endpoint — delegates to SearchPipeline (Task 3.4)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from archon_search._types import SearchResult

logger = logging.getLogger("archon.search")

router = APIRouter()


class SearchRequest(BaseModel):
    collection: str
    query: str
    top_k: int = Field(default=5, ge=1, le=100)

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

    @classmethod
    def from_result(cls, r: SearchResult) -> "SearchResultSchema":
        return cls(
            doc_id=r.doc_id,
            chunk_id=r.chunk_id,
            text=r.text,
            score=r.score,
            source_path=r.source_path,
        )


class SearchResponse(BaseModel):
    results: list[SearchResultSchema]
    acl_filtered: bool


@router.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, request: Request) -> SearchResponse | JSONResponse:
    pipeline = request.app.state.pipeline
    ns = request.state.namespace

    try:
        meta = await pipeline.get_collection_meta(body.collection, namespace=ns)
    except Exception as exc:
        logger.error("search: meta lookup failed for collection %r: %s", body.collection, exc, exc_info=True)
        return JSONResponse({"detail": "service unavailable"}, status_code=503)

    if meta is None:
        return JSONResponse({"detail": "collection not found"}, status_code=404)

    try:
        result = await pipeline.search(body.query, body.collection, namespace=ns)
        return SearchResponse(
            results=[SearchResultSchema.from_result(r) for r in result.results],
            acl_filtered=result.acl_filtered,
        )
    except Exception as exc:
        logger.warning("search failed for collection %r: %s", body.collection, exc, exc_info=True)
        return SearchResponse(results=[], acl_filtered=False)

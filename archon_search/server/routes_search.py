"""POST /search endpoint — vector + FTS hybrid search (Task 2.1)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from archon_search._types import SearchResult
from archon_search.reranker import ModelReranker, Reranker
from archon_search.store import SearchStore

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


@router.post("/search", response_model=list[SearchResultSchema])
async def search(body: SearchRequest, request: Request) -> list[SearchResultSchema]:
    config = request.app.state.config
    embedder = request.app.state.embedder
    try:
        vector = await embedder.embed_one(body.query)
        store = SearchStore(config.db_path)
        await store.connect()
        try:
            candidates = await store.hybrid_search(
                body.collection, vector, body.query, top_k=body.top_k * 3
            )
            backend = ModelReranker(config.reranker_model, providers=config.providers or None)
            reranker = Reranker(backend)
            reranked = await reranker.rerank(body.query, candidates, top_k=body.top_k)
            return [SearchResultSchema.from_result(r) for r in reranked]
        finally:
            await store.disconnect()
    except Exception as exc:
        logger.warning("search failed for collection %r: %s", body.collection, exc, exc_info=True)
        return []

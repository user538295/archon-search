"""POST /route endpoint — collection routing pre-context for decomposer (Task 5.5)."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from archon_search.config import SearchConfig
from archon_search.embedder import Embedder, ModelEmbedder
from archon_search.router import MultiCollectionRouter
from archon_search.sync import path_to_collection_name

router = APIRouter()


class RouteRequest(BaseModel):
    query: str
    slots: int | None = None


class RouteResponse(BaseModel):
    pre_context: str | None
    pinned_names: list[str]
    routable_names: list[str]
    decomposer_invoked: bool


def _build_router(
    config: SearchConfig,
    shortlist_size: int,
    embedder: Embedder | None = None,
) -> MultiCollectionRouter:
    """Build a MultiCollectionRouter from config. Extracted for test injection."""
    if embedder is None:
        backend = ModelEmbedder(config.embedding_model, providers=config.providers or None)
        embedder = Embedder(backend)
    search_url = f"http://{config.host}:{config.port}"
    return MultiCollectionRouter(
        search_url=search_url,
        embedder=embedder,
        shortlist_size=shortlist_size,
        confidence_threshold=config.routing_confidence_threshold,
        embedding_model=config.embedding_model,
    )


@router.post("/route", response_model=RouteResponse)
async def route(body: RouteRequest, request: Request) -> Any:
    """Route a query to the appropriate search collections.

    Returns the pre_context block to inject before route_task(), along with
    the resolved pinned and routable collection names and a flag indicating
    whether the decomposer will be invoked.
    """
    if not body.query or not body.query.strip():
        raise HTTPException(status_code=400, detail="query must not be empty")

    if body.slots is not None and body.slots < 1:
        raise HTTPException(status_code=400, detail="slots must be >= 1")

    config: SearchConfig = request.app.state.config

    shortlist_size = body.slots if body.slots is not None else config.routing_shortlist_size

    embedder: Embedder | None = getattr(request.app.state, "embedder", None)
    col_router = _build_router(config, shortlist_size, embedder=embedder)

    pinned_names = [path_to_collection_name(p) for p in config.pinned_collections]

    try:
        pre_context = await asyncio.wait_for(
            col_router.get_pre_context(
                query=body.query,
                pinned_names=pinned_names,
                available_slots=shortlist_size,
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="routing timed out")

    return RouteResponse(
        pre_context=pre_context,
        pinned_names=pinned_names,
        routable_names=col_router.last_routable_names,
        decomposer_invoked=col_router.decomposer_was_invoked,
    )

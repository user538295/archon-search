"""GET /v1/models and POST /v1/chat/completions handlers for the G9 OpenAI-compatible shim.

This module is conditionally registered in ``create_app()`` when
``config.openai_shim.enabled = true``.  When disabled, no routes or
middleware are added — the existing REST surface is untouched.

Design:
- ``GET /v1/models`` delegates entirely to ``SearchPipeline.get_all_collections_meta``
  (the Use Cases layer) and maps results to ``ModelList`` (Entities).
- ``POST /v1/chat/completions`` implements the non-streaming retrieval path.
  The ``model`` field drives collection routing: ``archon-search`` fans out
  across all namespace collections; ``archon-search/{col}`` queries one.
- ``OpenAI401Middleware`` is a thin Starlette middleware that rewrites any bodyless
  401 response on a ``/v1/*`` path to the OpenAI JSON error envelope.  It must
  be added AFTER ``APIKeyMiddleware`` in Starlette LIFO ordering so it wraps
  outgoing 401 responses before they reach the client.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from archon_search._types import SearchResult

from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, StreamingResponse

from archon_search.pipeline import CollectionNotFoundError, FanoutTimeoutError, MetadataLookupError
from archon_search.server.schemas_openai import (
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ChunkDelta,
    ModelList,
    ModelObject,
    OpenAIError,
    OpenAIErrorResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# The catch-all model ID — returned regardless of how many collections exist.
_CATCH_ALL_MODEL_ID = "archon-search"

# Mirror routes_search.py — single-collection search timeout in seconds.
_SEARCH_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _openai_error(status: int, message: str, error_type: str, *, code: str | None = None) -> JSONResponse:
    """Return a JSON response in the OpenAI error envelope shape."""
    body = OpenAIErrorResponse(error=OpenAIError(message=message, type=error_type, code=code))
    return JSONResponse(status_code=status, content=body.model_dump())


def _format_chunk(r: SearchResult, *, inject_citations: bool) -> str:
    """Return formatted text for a single SearchResult chunk."""
    if inject_citations:
        return f"\n\nContext:\n{r.text}\n[Source: {r.source_path}]"
    return r.text


async def _stream_completion(
    results: list[SearchResult],
    *,
    completion_id: str,
    model: str,
    inject_citations: bool,
) -> AsyncGenerator[str, None]:
    """Async generator yielding SSE frames for a streaming chat completion.

    Yields:
    - One ``data: {...}`` frame per result chunk (role='assistant' on first frame only).
    - One stop frame (delta={}, finish_reason='stop').
    - ``data: [DONE]\\n\\n``.

    Zero-result case: one assistant-role frame with empty content + stop frame + [DONE].
    """
    created = int(time.time())  # fixed for the lifetime of this stream

    def _chunk_frame(delta: ChunkDelta, finish_reason: str | None) -> str:
        chunk = ChatCompletionChunk(
            id=completion_id,
            created=created,
            model=model,
            choices=[ChatCompletionChunkChoice(delta=delta, finish_reason=finish_reason)],
        )
        data = chunk.model_dump()
        # Serialize delta with exclude_none so stop-frame is {} and frames 2..N omit role.
        data["choices"][0]["delta"] = delta.model_dump(exclude_none=True)
        return f"data: {json.dumps(data)}\n\n"

    if not results:
        yield _chunk_frame(ChunkDelta(role="assistant", content=""), None)
    else:
        for i, r in enumerate(results):
            text = _format_chunk(r, inject_citations=inject_citations)
            role = "assistant" if i == 0 else None
            yield _chunk_frame(ChunkDelta(role=role, content=text), None)

    yield _chunk_frame(ChunkDelta(), "stop")
    yield "data: [DONE]\n\n"


def _format_content(results: list[SearchResult], *, inject_citations: bool) -> str:
    """Combine SearchResult items into assistant content string.

    When ``inject_citations`` is True each chunk is wrapped with a citation
    block: ``Context:\\n{text}\\n[Source: {path}]``.  When False only the
    raw chunk text is included.
    """
    if not results:
        return ""
    parts: list[str] = []
    for r in results:
        parts.append(_format_chunk(r, inject_citations=inject_citations))
    return "".join(parts)


# ---------------------------------------------------------------------------
# POST /chat/completions — non-streaming retrieval path (BE-5)
# ---------------------------------------------------------------------------


@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: Request, body: ChatCompletionRequest) -> Response:
    """POST /v1/chat/completions — supports both streaming and non-streaming retrieval.

    ``model`` routing:
    - ``archon-search`` — fan-out across all namespace collections (capped at
      ``config.max_fanout``; truncation logs WARNING and degrades gracefully).
    - ``archon-search/{col}`` — direct single-collection search.
    - Any other value — 404 with OpenAI error shape.

    The last ``role="user"`` message is extracted as the search query.
    A 422 is returned when no user message is present; a 400 with
    ``code="no_user_message"`` is returned when the message is empty or whitespace-only.

    When ``stream=True``: retrieval is materialized in full first (so errors return
    JSON, not a broken SSE stream), then a ``StreamingResponse`` is returned that
    yields one SSE ``data:`` frame per chunk.
    """
    pipeline = request.app.state.pipeline
    config = request.app.state.config
    ns: str = request.state.namespace

    # --- Extract last user message as query ---
    query: str | None = None
    for msg in reversed(body.messages):
        if msg.role == "user":
            query = msg.content
            break
    if query is None:
        return _openai_error(422, "messages must contain at least one user message", "invalid_request_error")
    if query.strip() == "":
        return _openai_error(400, "No user message provided", "invalid_request_error", code="no_user_message")

    # --- Parse model field ---
    model = body.model
    parts = model.split("/", 1)
    prefix = parts[0]

    if prefix != _CATCH_ALL_MODEL_ID:
        return _openai_error(
            404,
            f"The model '{model}' does not exist.",
            "invalid_request_error",
        )

    is_fanout = len(parts) == 1  # exactly "archon-search"
    collection = parts[1] if len(parts) == 2 else None

    inject_citations: bool = config.openai_shim.inject_citations
    content: str = ""

    # --- Resolve embedder (single-collection path) ---
    async def _resolve_embedder(meta):
        embedder_cache = getattr(request.app.state, "embedder_cache", None)
        active_model = meta.active_embedding_model or config.embedding_model
        if embedder_cache is not None:
            return await embedder_cache.get_or_load(active_model)
        logger.warning("chat_completions: embedder_cache absent from app.state — falling back to global embedder")
        return pipeline._global_embedder

    # -------------------------------------------------------------------------
    # Fanout path — model = "archon-search"
    # -------------------------------------------------------------------------
    if is_fanout:
        all_meta = await pipeline.get_all_collections_meta(ns)

        col_names = [m.name for m in all_meta]

        if not col_names:
            return _openai_error(404, "No collections available.", "invalid_request_error")

        if len(col_names) > config.max_fanout:
            omitted = col_names[config.max_fanout:]
            col_names = col_names[: config.max_fanout]
            logger.warning(
                "chat_completions: fanout exceeds max_fanout=%d; omitting collections: %s",
                config.max_fanout,
                omitted,
            )

        try:
            result = await pipeline.search_many(query, col_names, namespace=ns)
        except CollectionNotFoundError as exc:
            logger.warning("chat_completions: collection not found during fanout: %s", exc)
            return _openai_error(404, "Collection not found.", "invalid_request_error")
        except FanoutTimeoutError:
            return _openai_error(504, "Request timeout.", "server_error")
        except MetadataLookupError as exc:
            logger.error("chat_completions: metadata lookup failed during search: %s", exc)
            # Intentionally uses OpenAI error envelope — not ErrorDetail/metadata_store_error (different contract).
            return _openai_error(503, "Service temporarily unavailable.", "server_error")

        for excl in result.excluded_collections:
            logger.warning(
                "chat_completions: collection '%s' excluded from fanout: %s",
                excl.name,
                excl.reason,
            )

        if not body.stream:
            content = _format_content(result.results, inject_citations=inject_citations)

    # -------------------------------------------------------------------------
    # Direct single-collection path — model = "archon-search/{col}"
    # -------------------------------------------------------------------------
    else:
        # Empty collection name (model="archon-search/") → treat as not found
        try:
            meta = await pipeline.get_collection_meta(collection, namespace=ns)
        except Exception as exc:
            logger.error("chat_completions: meta lookup failed for collection %r: %s", collection, exc, exc_info=True)
            return _openai_error(503, "Service temporarily unavailable.", "server_error")

        if meta is None:
            return _openai_error(
                404,
                f"The model '{model}' does not exist.",
                "invalid_request_error",
            )

        embedder = await _resolve_embedder(meta)

        try:
            result = await asyncio.wait_for(
                pipeline.search(query, collection, namespace=ns, embedder=embedder),
                timeout=_SEARCH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return _openai_error(504, "Request timeout.", "server_error")
        except Exception as exc:
            logger.error("chat_completions: search failed for collection %r: %s", collection, exc, exc_info=True)
            return _openai_error(503, "Service temporarily unavailable.", "server_error")

        if not body.stream:
            content = _format_content(result.results, inject_citations=inject_citations)

    # Streaming path — results are already materialized above so errors (404/504)
    # are returned as plain JSON before StreamingResponse is opened.
    if body.stream:
        return StreamingResponse(
            _stream_completion(
                result.results,
                completion_id=f"chatcmpl-{uuid4()}",
                model=model,
                inject_citations=inject_citations,
            ),
            media_type="text/event-stream",
        )

    # --- Build response ---
    response_obj = ChatCompletionResponse(
        id=f"chatcmpl-{uuid4()}",
        created=int(time.time()),
        model=model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=content),
                finish_reason="stop",
            )
        ],
    )
    return JSONResponse(content=response_obj.model_dump())


@router.get("/models", response_model=ModelList)
async def list_models(request: Request) -> ModelList:
    """Return one ``ModelObject`` per namespace-visible collection plus the catch-all.

    The response always contains at least one entry (the ``archon-search``
    catch-all), even when the namespace has no collections.
    """
    pipeline = request.app.state.pipeline
    ns: str = request.state.namespace

    all_meta = await pipeline.get_all_collections_meta(ns)

    data: list[ModelObject] = [
        ModelObject(id=_CATCH_ALL_MODEL_ID),
    ]
    for meta in all_meta:
        data.append(ModelObject(id=f"{_CATCH_ALL_MODEL_ID}/{meta.name}"))

    return ModelList(data=data)


# ---------------------------------------------------------------------------
# Middleware — 401 body rewrite for /v1/* paths
# ---------------------------------------------------------------------------


class OpenAI401Middleware(BaseHTTPMiddleware):
    """Rewrite bodyless 401 responses on ``/v1/*`` paths to OpenAI error shape.

    ``APIKeyMiddleware`` returns ``Response(status_code=401)`` with an empty
    body and a ``WWW-Authenticate: Bearer`` header.  Clients that speak the
    OpenAI protocol expect a JSON body on 401.  This middleware intercepts
    those responses and adds the expected body — leaving non-/v1/ responses
    and non-401 status codes untouched.

    Starlette processes middleware in LIFO order, so this middleware must be
    added AFTER ``APIKeyMiddleware`` in ``create_app()`` — it will then sit
    *outside* ``APIKeyMiddleware`` in the call stack and can see its outgoing
    401 responses.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        if response.status_code == 401 and request.url.path.startswith("/v1/"):
            error_body = OpenAIErrorResponse(
                error=OpenAIError(
                    message="Incorrect API key.",
                    type="authentication_error",
                )
            )
            return JSONResponse(
                status_code=401,
                content=error_body.model_dump(),
                headers={"WWW-Authenticate": "Bearer"},
            )

        return response

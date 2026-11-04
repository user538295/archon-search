"""POST /search endpoint — an adapter over ``SearchService`` for the single and the multi-collection path."""
from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from archon_search._types import SearchResult
from archon_search.constants import (
    TOP_K_MIN,
    GraphMode,
)
from archon_search.filters import SearchFilters
from archon_search.server.authz import caller_identity
from archon_search.server.schemas import (
    AclGateSchema,
    CodedErrorDetail,
    ErrorDetail,
    ExcludedCollectionSchema,
    blank_unrequested_metadata,
    route_422_response,
)
from archon_search.services.container import build_search_service_from_state
from archon_search.services.search_command import (
    SearchCommand,
    normalize_collection,
    normalize_query,
    select_collections,
)
from archon_search.services.search_service import SearchHit, SearchOutcome
from archon_search.services.service_error import (
    INVALID_SCOPE_FILTER_CODE,
    ServiceError,
    ServiceErrorKind,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# REST wire text of a search that overran its budget (the 504 body), and of Starlette's plain-text 500.
_SEARCH_TIMED_OUT_DETAIL = "Search timed out"
_PLAIN_500_BODY = "Internal Server Error"

router = APIRouter()


def _call_normaliser[**P, R](rule: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run one service normaliser rule as transport parsing: its ``ServiceError`` becomes the framework's 422 ``ValueError``."""
    try:
        return rule(*args, **kwargs)
    except ServiceError as error:
        raise ValueError(error.detail) from error


class SearchRequest(BaseModel):
    collection: str | None = None
    collections: list[str] | None = None
    query: str
    top_k: int | None = Field(default=None, ge=TOP_K_MIN)
    filters: SearchFilters | None = None
    hyde: bool = False
    rag_fusion: bool = False
    graph_mode: GraphMode | None = None
    scope_filter: str | None = None
    acl_context: bool = False
    # Response-shaping flag, accepted top-level to mirror the MCP search tool
    # (server/mcp.py) and real REST clients; OR-ed with filters.include_metadata.
    include_metadata: bool = False

    @field_validator("collection")
    @classmethod
    def collection_nonempty(cls, v: str | None) -> str | None:
        return _call_normaliser(normalize_collection, v)

    @field_validator("query")
    @classmethod
    def query_nonempty(cls, v: str) -> str:
        return _call_normaliser(normalize_query, v)

    @model_validator(mode="after")
    def _validate_collection_selection(self) -> SearchRequest:
        selected = _call_normaliser(select_collections, self.collection, self.collections)
        self.collections = None if selected is None else list(selected)
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
    reranker_score: float | None = None
    acl_gate: AclGateSchema | None = None

    @classmethod
    def from_result(cls, r: SearchResult) -> SearchResultSchema:
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
            reranker_score=r.reranker_score,
        )

    @classmethod
    def from_hit(cls, hit: SearchHit) -> SearchResultSchema:
        """Render a service hit: the ``acl_gate`` is the one the service built (present only when ``acl_context`` was set)."""
        schema = cls.from_result(hit.result)
        schema.acl_gate = AclGateSchema.from_gate(hit.acl_gate)
        return schema


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
    # BE-2 — PPR retrieval: count of seed entities matched during PPR graph walk
    ppr_entities_matched: int | None = None


def _error_response(error: ServiceError) -> JSONResponse | PlainTextResponse:
    """The one error table of the route: a :class:`ServiceError` kind to its REST body and status.

    The service has already logged the failure with its exception and recorded its telemetry; the wire body carries only
    the error's fixed, sanitized detail.  An unexpected failure is Starlette's plain ``500`` (never exception text).
    """
    kind = error.kind
    match kind:
        case (
            ServiceErrorKind.VALIDATION
            | ServiceErrorKind.INVALID_GRAPH_MODE
            | ServiceErrorKind.GRAPH_DISABLED
            | ServiceErrorKind.SCOPE_FILTER_GRAPH_MODE_INCOMPATIBLE
        ):
            return JSONResponse({"detail": error.detail}, status_code=422)
        case ServiceErrorKind.INVALID_SCOPE_FILTER:
            return JSONResponse({"detail": {"code": error.code, "message": error.detail}}, status_code=400)
        case ServiceErrorKind.GRAPH_COMMUNITIES_NOT_BUILT:
            return JSONResponse({"detail": {"code": error.code, "message": error.detail}}, status_code=422)
        case ServiceErrorKind.NOT_FOUND:
            return JSONResponse({"detail": error.detail}, status_code=404)
        case ServiceErrorKind.METADATA_STORE_ERROR | ServiceErrorKind.EMBEDDER_NOT_READY:
            retry_after = error.retry_after_seconds
            return JSONResponse(
                {"detail": error.detail, "code": error.code},
                status_code=503,
                headers=None if retry_after is None else {"Retry-After": str(retry_after)},
            )
        case ServiceErrorKind.TIMEOUT:
            return JSONResponse({"detail": _SEARCH_TIMED_OUT_DETAIL}, status_code=504)
        case ServiceErrorKind.HYDE_FAILED:
            return JSONResponse({"detail": error.detail}, status_code=500)
        case ServiceErrorKind.INTERNAL:
            return PlainTextResponse(_PLAIN_500_BODY, status_code=500)
        case _:
            assert_never(kind)


def _render_outcome(outcome: SearchOutcome) -> SearchResponse:
    """Render the service outcome as the REST response; ``metadata`` is blanked unless the outcome's ORed flag is set."""
    schemas = [SearchResultSchema.from_hit(hit) for hit in outcome.results]
    blank_unrequested_metadata(schemas, outcome.include_metadata)
    return SearchResponse(
        results=schemas,
        acl_filtered=outcome.acl_filtered,
        excluded_collections=[ExcludedCollectionSchema(name=e.name, reason=e.reason) for e in outcome.excluded_collections],
        embedding_model=outcome.embedding_model,
        hyde_applied=outcome.hyde_applied,
        rag_fusion_applied=outcome.rag_fusion_applied,
        rag_fusion_queries_used=outcome.rag_fusion_queries_used,
        rag_fusion_attempted=outcome.rag_fusion_attempted,
        graph_expansion_applied=outcome.graph_expansion_applied,
        expansion_used=outcome.expansion_used,
        expansion_warning=outcome.expansion_warning,
        applied_filters=outcome.applied_filters,
        ppr_entities_matched=outcome.ppr_entities_matched,
    )


@router.post(
    "/search",
    response_model=SearchResponse,
    responses={
        400: {
            "model": CodedErrorDetail,
            "description": f"`scope_filter` is syntactically invalid (code `{INVALID_SCOPE_FILTER_CODE}`)",
        },
        404: {"model": ErrorDetail, "description": "Collection not found"},
        422: route_422_response(),
        500: {
            "model": ErrorDetail,
            "description": "HyDE generation failure (JSON detail); a pipeline failure is a plain-text 500",
        },
        503: {"model": ErrorDetail},
        504: {"model": ErrorDetail, "description": _SEARCH_TIMED_OUT_DETAIL},
    },
)
async def search(body: SearchRequest, request: Request) -> SearchResponse | JSONResponse | PlainTextResponse:
    # A comment, not a docstring: FastAPI publishes a docstring as the operation description (OpenAPI snapshot).
    # Single or multi path is chosen by which of collection/collections the request set; the service runs it.
    caller = caller_identity(request)
    service = build_search_service_from_state(request.app.state)
    command = SearchCommand(
        query=body.query,
        collection=body.collection,
        collections=None if body.collections is None else tuple(body.collections),
        top_k=body.top_k,
        filters=body.filters,
        include_metadata=body.include_metadata,
        hyde=body.hyde,
        rag_fusion=body.rag_fusion,
        graph_mode=body.graph_mode,
        scope_filter=body.scope_filter,
        acl_context=body.acl_context,
    )
    try:
        outcome = await service.search(command, caller)
    except ServiceError as error:
        return _error_response(error)
    return _render_outcome(outcome)

"""POST /search endpoint — the single-collection branch is an adapter over ``SearchService``; the multi branch still calls the pipeline."""
from __future__ import annotations

import logging
from dataclasses import asdict
from time import monotonic
from typing import TYPE_CHECKING, Literal, assert_never

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from archon_search._types import SearchResult
from archon_search.constants import (
    SEARCH_TIMEOUT_SECONDS as _SEARCH_TIMEOUT_SECONDS,
)
from archon_search.constants import (
    VALID_ACL_SOURCES,
    GraphMode,
)
from archon_search.embedder_cache import (
    EmbedderNotReadyError,
)
from archon_search.filters import SearchFilters
from archon_search.observability import correlation_id as _correlation_id
from archon_search.pipeline import (
    METADATA_STORE_UNAVAILABLE_CODE,
    METADATA_STORE_UNAVAILABLE_DETAIL,
    CollectionNotFoundError,
    FanoutTimeoutError,
    GraphCommunitiesNotBuiltError,
    MetadataLookupError,
)
from archon_search.server.authz import caller_identity
from archon_search.server.schemas import (
    AclGateSchema,
    CodedErrorDetail,
    ErrorDetail,
    ExcludedCollectionSchema,
    route_422_response,
)
from archon_search.services.authz import AuthIdentity
from archon_search.services.container import build_search_service_from_state
from archon_search.services.request_hyde import RequestHyDE
from archon_search.services.search_budget import (
    SearchBudgetExceeded,
    run_within_budget,
    warmup_for_search,
)
from archon_search.services.search_command import (
    SearchCommand,
    normalize_collection,
    normalize_query,
    select_collections,
)
from archon_search.services.search_service import SearchHit, SearchOutcome
from archon_search.services.service_error import (
    COLLECTION_NOT_FOUND_DETAIL,
    GRAPH_COMMUNITIES_NOT_BUILT_CODE,
    INVALID_SCOPE_FILTER_CODE,
    SEARCH_FAILED_DETAIL,
    ServiceError,
    ServiceErrorKind,
    fanout_exceeded_detail,
    top_k_exceeded_detail,
)
from archon_search.services.validators import (
    validate_scope_filter as _check_scope_filter,
)
from archon_search.telemetry.entry import TelemetryEntry

if TYPE_CHECKING:
    from collections.abc import Callable

    from archon_search.telemetry.writer import TelemetryWriter

# REST wire text of a search that overran its budget (the 504 body), and of Starlette's plain-text 500.
_SEARCH_TIMED_OUT_DETAIL = "Search timed out"
_PLAIN_500_BODY = "Internal Server Error"

logger = logging.getLogger(__name__)

router = APIRouter()


def _enqueue_error(
    writer: TelemetryWriter | None,
    start: float,
    *,
    status: Literal["internal_error", "timeout"],
    error_kind: Literal["other", "timeout"],
) -> None:
    """Enqueue the error telemetry entry for a ``/search`` request begun at *start*."""
    if writer is None:
        return
    try:
        writer.enqueue(
            TelemetryEntry.from_error(
                endpoint="search",
                status=status,
                error_kind=error_kind,
                latency_ms=(monotonic() - start) * 1000.0,
                correlation_id=_correlation_id.get(),
            )
        )
    except Exception as tel_exc:  # noqa: BLE001 — telemetry must never break the request; failure is logged
        logger.warning("telemetry enqueue failed: %s", type(tel_exc).__name__)


def _report_pipeline_failure(writer: TelemetryWriter | None, start: float, exc: Exception) -> None:
    """Enqueue the ``internal_error`` telemetry entry and log ``search_pipeline_failure`` for *exc*.

    The caller re-raises *exc*, so the client gets Starlette's plain ``500`` — never the
    exception text.
    """
    _enqueue_error(writer, start, status="internal_error", error_kind="other")
    logger.error(
        "search pipeline failed: %s",
        type(exc).__name__,
        extra={"event_type": "search_pipeline_failure"},
        exc_info=exc,
    )


def _hyde_failure_response(writer: TelemetryWriter | None, start: float, exc: Exception) -> JSONResponse:
    """Answer a HyDE failure: reported like a pipeline failure, answered like ``/explain``'s HyDE
    catch-all — JSON ``SEARCH_FAILED_DETAIL``, never the exception text."""
    _report_pipeline_failure(writer, start, exc)
    return JSONResponse({"detail": SEARCH_FAILED_DETAIL}, status_code=500)


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
    top_k: int = Field(default=5, ge=1)
    filters: SearchFilters | None = None
    hyde: bool = False
    rag_fusion: bool = False
    graph_mode: GraphMode | None = None
    scope_filter: str | None = None
    acl_context: bool = False
    # Response-shaping flag, accepted top-level to mirror the MCP search tool
    # (server/mcp.py) and real REST clients; OR-ed with filters.include_metadata.
    include_metadata: bool = False

    @property
    def wants_metadata(self) -> bool:
        return self.include_metadata or (self.filters is not None and self.filters.include_metadata)

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
    def from_result(cls, r: SearchResult, *, include_acl_gate: bool = False) -> SearchResultSchema:
        gate: AclGateSchema | None = None
        if include_acl_gate:
            gate = AclGateSchema(
                allowed_principals=r.acl,
                source=r.acl_source if r.acl_source in VALID_ACL_SOURCES else None,
                sidecar_path=r.acl_sidecar_path,
                warnings=list(r.acl_warning),
            )
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
            acl_gate=gate,
        )

    @classmethod
    def from_hit(cls, hit: SearchHit) -> SearchResultSchema:
        """Render a service hit: the ``acl_gate`` is the one the service built (present only when ``acl_context`` was set)."""
        schema = cls.from_result(hit.result)
        gate = hit.acl_gate
        if gate is not None:
            schema.acl_gate = AclGateSchema(**asdict(gate))
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
    """The one error table of the single-collection branch: a :class:`ServiceError` kind to its REST body and status.

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
    if not outcome.include_metadata:
        for schema in schemas:
            schema.metadata = {}
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


async def _search_single(
    body: SearchRequest, request: Request, caller: AuthIdentity
) -> SearchResponse | JSONResponse | PlainTextResponse:
    """The single-collection branch: build the command, run the service, render its outcome or its error."""
    service = build_search_service_from_state(request.app.state)
    command = SearchCommand(
        query=body.query,
        collection=body.collection,
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
    caller = caller_identity(request)
    if body.collections is None:
        return await _search_single(body, request, caller)
    ns = caller.namespace
    pipeline = request.app.state.pipeline
    writer = getattr(request.app.state, "telemetry_writer", None)
    config = request.app.state.config
    start = monotonic()

    # Config-wired fanout and top_k validation (moved from Pydantic layer).
    if len(body.collections) > config.max_fanout:
        return JSONResponse(
            {"detail": fanout_exceeded_detail(config.max_fanout)},
            status_code=422,
        )
    if body.top_k > config.top_k_max:
        return JSONResponse(
            {"detail": top_k_exceeded_detail(body.top_k, config.top_k_max)},
            status_code=422,
        )

    # scope_filter syntax guard (400, not 422 — invalid input, not server-state conflict)
    scope_filter_err = _check_scope_filter(body.scope_filter)
    if scope_filter_err is not None:
        return JSONResponse(
            {"detail": {"code": INVALID_SCOPE_FILTER_CODE, "message": scope_filter_err}},
            status_code=400,
        )

    # scope_filter + graph_mode are mutually exclusive (graph paths bypass scope predicates)
    if body.scope_filter is not None and body.graph_mode is not None:
        return JSONResponse(
            {"detail": "scope_filter is not supported with graph_mode"},
            status_code=422,
        )

    # ppr graph_mode + multi-collection fanout is not supported (search_many has no PPR branch)
    if body.graph_mode == "ppr":
        return JSONResponse(
            {"detail": "graph_mode is not supported with multi-collection fanout; use a single collection"},
            status_code=422,
        )

    # graph_mode guard: require [graph] enabled=true
    if body.graph_mode is not None and not config.graph.enabled:
        return JSONResponse(
            {"detail": "graph_mode requires [graph] enabled=true in server config"},
            status_code=422,
        )

    rag_fusion_gen = getattr(request.app.state, "rag_fusion_generator", None)
    request_hyde = RequestHyDE.for_request(
        body.query,
        requested=body.hyde,
        rag_fusion=body.rag_fusion,
        rag_fusion_enabled=config.rag_fusion.enabled,
        graph_mode=body.graph_mode,
        generator=getattr(request.app.state, "hyde_generator", None),
        config=config.hyde,
        timeout=_SEARCH_TIMEOUT_SECONDS,
    )

    # Warm both lazy models outside the fan-out budget (S184). With no embedder
    # argument warmup_models warms the pipeline's global embedder — the one
    # search_many embeds with. Both legs are bounded so a cold model cannot stall
    # the request: rerank=False degrades to the fused RRF ranking (S288) and
    # fts_only=True to the FTS leg alone (S290).
    warmup = await warmup_for_search(pipeline)
    try:
        # search_many embeds with the global embedder and excludes every collection
        # pinned to another model (embedding_model_mismatch), so HyDE embeds with it too.
        hyde_outcome = await request_hyde.resolve(pipeline.global_embedder, embedder_warm=not warmup.fts_only)
    except EmbedderNotReadyError:
        raise  # retryable 503 (app-level handler), as for the fan-out itself
    except Exception as exc:  # noqa: BLE001 — any HyDE/embedding failure is mapped to an internal-error response
        return _hyde_failure_response(writer, start, exc)
    try:
        # search_many's own _fanout_timeout_seconds bounds the per-collection leg
        # gathers only; the meta lookup, embed_one, graph-store calls and the
        # global rerank pass sit outside it, so the whole call gets the same
        # overall budget as the single-collection path.
        result = await run_within_budget(
            pipeline.search_many(
                body.query,
                body.collections,
                namespace=ns,
                query_vector=hyde_outcome.vector,
                rag_fusion=body.rag_fusion,
                rag_fusion_generator=rag_fusion_gen,
                rag_fusion_config=config.rag_fusion,
                filters=body.filters,
                graph_mode=body.graph_mode,
                scope_filter=body.scope_filter,
                rerank=warmup.rerank,
                fts_only=warmup.fts_only,
            ),
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )
    except GraphCommunitiesNotBuiltError as exc:
        return JSONResponse(
            {"detail": {"code": GRAPH_COMMUNITIES_NOT_BUILT_CODE, "message": str(exc)}}, status_code=422
        )
    except CollectionNotFoundError:
        return JSONResponse({"detail": COLLECTION_NOT_FOUND_DETAIL}, status_code=404)
    except MetadataLookupError:
        logger.exception("multi-collection search: metadata lookup failed")
        return JSONResponse(
            {"detail": METADATA_STORE_UNAVAILABLE_DETAIL, "code": METADATA_STORE_UNAVAILABLE_CODE},
            status_code=503,
        )
    except (FanoutTimeoutError, SearchBudgetExceeded):
        # SearchBudgetExceeded, not asyncio.TimeoutError: since 3.11 the latter is
        # the builtin TimeoutError (an OSError), so catching it here would also
        # swallow a socket ETIMEDOUT from the store and mis-map it to a 504.
        _enqueue_error(writer, start, status="timeout", error_kind="timeout")
        logger.error(
            "search pipeline timed out",
            extra={
                "event_type": "search_timeout",
                "collections": list(body.collections),
            },
        )
        raise HTTPException(status_code=504, detail=_SEARCH_TIMED_OUT_DETAIL)
    except Exception as exc:
        # A search_pipeline_failure log and the telemetry entry, then a plain 500.
        _report_pipeline_failure(writer, start, exc)
        raise
    schemas = [SearchResultSchema.from_result(r, include_acl_gate=body.acl_context) for r in result.results]
    if not body.wants_metadata:
        for schema in schemas:
            schema.metadata = {}
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
    _multi_expansion_warning = hyde_outcome.expansion_warning or result.rag_fusion_warning
    return SearchResponse(
        results=schemas,
        acl_filtered=result.acl_filtered,
        excluded_collections=[
            ExcludedCollectionSchema(name=e.name, reason=e.reason)
            for e in result.excluded_collections
        ],
        embedding_model=config.embedding_model,
        hyde_applied=hyde_outcome.applied,
        rag_fusion_applied=result.rag_fusion_applied,
        rag_fusion_queries_used=result.rag_fusion_queries_used,
        rag_fusion_attempted=result.rag_fusion_attempted,
        graph_expansion_applied=result.graph_expansion_applied,
        expansion_used=hyde_outcome.applied or result.rag_fusion_applied or result.graph_expansion_applied,
        expansion_warning=_multi_expansion_warning,
        applied_filters=body.filters,
        ppr_entities_matched=result.ppr_entities_matched,
    )

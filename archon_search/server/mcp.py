"""FastMCP HTTP server for RAG search."""
from __future__ import annotations

import logging
import time
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from fastmcp import Context, FastMCP
from pydantic import Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from archon_search._path_safety import PathUnsafeError, validate_ingest_path
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.filters import SearchFilters
from archon_search.key_manager import load_or_generate_key
from archon_search.pipeline import (
    CollectionNotFoundError,
    ExplainMultiCollectionNoRerankError,
    ExplainStageError,
    FanoutTimeoutError,
    MetadataLookupError,
    SearchPipeline,
)
from archon_search.progress import IndexingState, IndexingStatus
from archon_search.router import MultiCollectionRouter
from archon_search.server.middleware_auth import APIKeyMiddleware
from archon_search.server.routes_explain import (
    ExplainRequest,
    ExplainResponse,
    RoutingCandidate,
    RoutingExplain,
)
from archon_search.server.routes_search import _FANOUT_VALIDATION_LIMIT
from archon_search.store import StoreBusyError
from archon_search.observability import bind_stage_recorder, correlation_id as _correlation_id
from archon_search.telemetry.entry import FilterFlags, TelemetryEntry
from archon_search.telemetry.writer import TelemetryWriter

from archon_search.embedder_cache import EmbedderCache
from archon_search.model_validation import ModelValidationError, validate_embedding_model
from archon_search.types import JobStatus

if TYPE_CHECKING:
    from archon_search.config import SearchConfig
    from archon_search.jobs.store import JobStore

logger = logging.getLogger(__name__)

# Language filter parameter for the ``search`` tool (single-collection path).
# Defined at module level so FastMCP can resolve the Annotated type inside closures.
_LanguageParamSearch = Annotated[
    str | None,
    Field(
        description=(
            "ISO 639-1 or ISO 639-3 language code to filter results (e.g. 'fr', 'de', 'unknown'). "
            "Single-collection queries only — multi-collection fan-out (collections parameter) "
            "rejects this filter with a validation error."
        ),
        default=None,
    ),
]

# Language filter parameter for the ``search_with_context`` tool.
# Defined at module level so FastMCP can resolve the Annotated type inside closures.
_LanguageParamSearchWithContext = Annotated[
    str | None,
    Field(
        description=(
            "ISO 639-1 or ISO 639-3 language code to filter results (e.g. 'fr', 'de', 'unknown'). "
            "Single-collection only."
        ),
        default=None,
    ),
]


class McpErrorResponse(TypedDict):
    error: str
    code: str


_PATH_UNSAFE_MESSAGES: dict[str, str] = {
    "empty": "path is unsafe: the path is empty — provide an absolute filesystem path",
    "whitespace_only": "path is unsafe: the path is only whitespace — provide an absolute filesystem path",
    "nul_byte": "path is unsafe: the path contains a NUL byte — provide a valid absolute path",
    "contains_dotdot": "path is unsafe: the path contains a '..' segment — use an absolute path without traversal",
    "not_absolute": "path is unsafe: the path is not absolute — use an absolute path (no relative or '..' segments)",
}


def _path_unsafe_message(reason: str) -> str:
    """Map a PathUnsafeError reason code to an LLM-readable rejection phrase."""
    return _PATH_UNSAFE_MESSAGES.get(reason, f"path is unsafe: {reason}")


def _chunk_to_context_dict(chunk: Any, *, include_metadata: bool = True) -> dict[str, Any]:
    """Serialize a ChunkRecord for MCP ``search_with_context`` payloads, dropping
    the ``vector`` field — raw embeddings should not leak over MCP and add no
    value to context-window consumers.  When ``include_metadata`` is False the
    ``metadata`` key is set to an empty dict (consistent with the REST surface)."""
    d = asdict(chunk)
    d.pop("vector", None)
    if not include_metadata:
        d["metadata"] = {}
    return d


async def _resolve_embedder_by_model(
    pipeline: Any,
    embedder_cache: Any,
    model: str,
) -> Any:
    """Return the Embedder for *model* from cache, or global embedder as fallback."""
    if embedder_cache is None:
        return pipeline._global_embedder
    return await embedder_cache.get_or_load(model)


async def _resolve_embedder(
    pipeline: Any,
    embedder_cache: Any,
    collection: str,
    config: Any,
) -> Any:
    """Resolve the Embedder for *collection* by looking up its active_embedding_model.

    Falls back to pipeline._global_embedder when no cache is configured or when
    the resolved model name is empty (no config and no meta record).
    """
    if embedder_cache is None:
        return pipeline._global_embedder
    active_model: str = config.embedding_model if config is not None else ""
    meta = await pipeline.get_collection_meta(collection)
    if meta is not None:
        active_model = meta.active_embedding_model or active_model
    if not active_model:
        return pipeline._global_embedder
    return await embedder_cache.get_or_load(active_model)


def create_app(
    pipeline: SearchPipeline,
    default_collection: str,
    writer: TelemetryWriter | None = None,
    config: SearchConfig | None = None,
    embedder_cache: EmbedderCache | None = None,
    job_store: JobStore | None = None,
) -> FastMCP:
    """Create a FastMCP app with 11 RAG tools registered.

    ``config`` is required only for the collectionless ``explain`` routing path;
    when omitted, ``explain`` without a collection falls back to
    ``default_collection`` (mirroring the ``search`` tool).

    ``job_store`` is required for the ``update_collection`` tool's 409 guard
    (active reindex check).
    """
    app = FastMCP("archon-search")

    @app.tool()
    async def search(
        query: str,
        collection: str | None = None,
        collections: list[str] | None = None,
        include_metadata: bool = False,
        file_type: str | None = None,
        source_path_prefix: str | None = None,
        source_path_glob: str | None = None,
        indexed_after: str | None = None,
        indexed_before: str | None = None,
        language: _LanguageParamSearch = None,
    ) -> dict[str, Any]:
        """Search for relevant document chunks using hybrid vector + FTS search."""
        timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)
        start = monotonic()

        # Multi-collection fan-out path (B3). The single-collection path below is
        # unchanged; when neither field is set it falls back to default_collection.
        if collection is not None and collections is not None:
            return McpErrorResponse(
                error="supply either collection or collections, not both",
                code="validation_error",
            )
        if collections is not None:
            if len(collections) == 0:
                return McpErrorResponse(error="collections must not be empty", code="validation_error")
            if language is not None:
                return McpErrorResponse(
                    error="language filter is not supported for multi-collection search in v1",
                    code="validation_error",
                )
            deduped: list[str] = []
            for name in collections:
                stripped = name.strip()
                if not stripped:
                    return McpErrorResponse(
                        error="collection names must not be whitespace", code="validation_error"
                    )
                if stripped not in deduped:
                    deduped.append(stripped)
            if len(deduped) > _FANOUT_VALIDATION_LIMIT:
                return McpErrorResponse(
                    error=f"collections length exceeds {_FANOUT_VALIDATION_LIMIT}",
                    code="validation_error",
                )
            try:
                result_obj = await pipeline.search_many(query, deduped)
            except CollectionNotFoundError:
                return McpErrorResponse(error="collection not found", code="not_found")
            except FanoutTimeoutError:
                return McpErrorResponse(error="search timed out", code="timeout")
            except MetadataLookupError:
                # Transient infrastructure error (store unavailable); mirror REST's
                # 503 semantics with a clean message rather than leaking the cause.
                return McpErrorResponse(error="service unavailable", code="internal_error")
            except Exception as exc:
                logger.exception("multi-collection search failed")
                return McpErrorResponse(error=str(exc), code="internal_error")
            results = []
            for r in result_obj.results:
                d = asdict(r)
                d.pop("vector", None)
                if not include_metadata:
                    d["metadata"] = {}
                results.append(d)
            if writer is not None:
                try:
                    excluded_count = len(result_obj.excluded_collections)
                    writer.enqueue(
                        TelemetryEntry.from_search_multi_result(
                            collections=deduped,
                            fanout_count=len(deduped) - excluded_count,
                            result_count=len(result_obj.results),
                            latency_ms=(monotonic() - start) * 1000.0,
                            excluded_count=excluded_count,
                            correlation_id=_correlation_id.get(),
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search_multi entry enqueue failed", exc_info=True)
            return {
                "results": results,
                "acl_filtered": result_obj.acl_filtered,
                "excluded_collections": [
                    {"name": e.name, "reason": e.reason} for e in result_obj.excluded_collections
                ],
            }

        try:
            try:
                filters = SearchFilters(
                    file_type=file_type,
                    source_path_prefix=source_path_prefix,
                    source_path_glob=source_path_glob,
                    indexed_after=indexed_after,
                    indexed_before=indexed_before,
                    language=language,
                    include_metadata=include_metadata,
                )
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code="validation_error")
            _col = collection or default_collection
            _search_embedder = await _resolve_embedder(pipeline, embedder_cache, _col, config)
            with ExitStack() as stack:
                recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
                t0 = time.perf_counter()
                result_obj = await pipeline.search(query, _col, embedder=_search_embedder, filters=filters)
                if recorder is not None:
                    recorder.record("total", (time.perf_counter() - t0) * 1000.0)
                    logger.info(
                        "stage timings",
                        extra={
                            "event_type": "stage_timings",
                            "correlation_id": _correlation_id.get(),
                            "endpoint": "search",
                            "collection": _col,
                            "stage_timings_ms": recorder.stage_timings_ms,
                        },
                    )
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_search_tool_result(
                            endpoint="search",
                            collection=_col,
                            result_doc_ids=[r.doc_id for r in result_obj.results],
                            latency_ms=(monotonic() - start) * 1000.0,
                            filter_flags=FilterFlags.from_search_filters(filters),
                            correlation_id=_correlation_id.get(),
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search entry enqueue failed", exc_info=True)
            results = []
            for r in result_obj.results:
                d = asdict(r)
                d.pop("vector", None)
                if not include_metadata:
                    # empty dict not key-absent, consistent with REST surface
                    d["metadata"] = {}
                results.append(d)
            return {
                "results": results,
                "acl_filtered": result_obj.acl_filtered,
                "excluded_collections": [
                    {"name": e.name, "reason": e.reason} for e in result_obj.excluded_collections
                ],
            }
        except Exception as exc:
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
                except Exception:
                    logger.warning("telemetry: search error entry enqueue failed", exc_info=True)
            logger.exception("search failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def search_with_context(
        query: str,
        collection: str | None = None,
        context_window: int = 1,
        include_metadata: bool = False,
        file_type: str | None = None,
        source_path_prefix: str | None = None,
        source_path_glob: str | None = None,
        indexed_after: str | None = None,
        indexed_before: str | None = None,
        language: _LanguageParamSearchWithContext = None,
    ) -> list[dict[str, Any]]:
        """Search and return surrounding chunks for richer context."""
        timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)
        start = monotonic()
        try:
            try:
                filters = SearchFilters(
                    file_type=file_type,
                    source_path_prefix=source_path_prefix,
                    source_path_glob=source_path_glob,
                    indexed_after=indexed_after,
                    indexed_before=indexed_before,
                    language=language,
                    include_metadata=include_metadata,
                )
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code="validation_error")
            _swc_col = collection or default_collection
            _swc_embedder = await _resolve_embedder(pipeline, embedder_cache, _swc_col, config)
            with ExitStack() as stack:
                recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
                t0 = time.perf_counter()
                results = await pipeline.search_with_context(
                    query, _swc_col, context_window, embedder=_swc_embedder, filters=filters
                )
                if recorder is not None:
                    recorder.record("total", (time.perf_counter() - t0) * 1000.0)
                    logger.info(
                        "stage timings",
                        extra={
                            "event_type": "stage_timings",
                            "correlation_id": _correlation_id.get(),
                            "endpoint": "search_with_context",
                            "collection": _swc_col,
                            "stage_timings_ms": recorder.stage_timings_ms,
                        },
                    )
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_search_tool_result(
                            endpoint="search_with_context",
                            collection=_swc_col,
                            result_doc_ids=[r["result"].doc_id for r in results],
                            latency_ms=(monotonic() - start) * 1000.0,
                            filter_flags=FilterFlags.from_search_filters(filters),
                            correlation_id=_correlation_id.get(),
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search_with_context entry enqueue failed", exc_info=True)
            output = []
            for r in results:
                result_dict = asdict(r["result"])
                result_dict.pop("vector", None)
                if not include_metadata:
                    # empty dict not key-absent, consistent with REST surface
                    result_dict["metadata"] = {}
                output.append(
                    {
                        "result": result_dict,
                        "context_before": [_chunk_to_context_dict(c, include_metadata=include_metadata) for c in r["context_before"]],
                        "context_after": [_chunk_to_context_dict(c, include_metadata=include_metadata) for c in r["context_after"]],
                    }
                )
            return output
        except Exception as exc:
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_error(
                            endpoint="search_with_context",
                            status="internal_error",
                            error_kind="other",
                            latency_ms=(monotonic() - start) * 1000.0,
                            correlation_id=_correlation_id.get(),
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search_with_context error entry enqueue failed", exc_info=True)
            logger.exception("search_with_context failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def explain(
        query: str,
        collection: str | None = None,
        collections: list[str] | None = None,
        top_k: int = 5,
        rerank: bool = True,
    ) -> dict[str, Any]:
        """Return the per-stage retrieval/reranking trace for a query, plus the
        routing decision when no collection is pinned. Operates in the default
        namespace only. The query is never echoed in the response or telemetry."""
        start = monotonic()

        # Multi-collection fan-out path (B3). The single/routing path below is unchanged.
        if collection is not None and collections is not None:
            return McpErrorResponse(
                error="supply either collection or collections, not both",
                code="validation_error",
            )
        if collections is not None:
            if len(collections) == 0:
                return McpErrorResponse(error="collections must not be empty", code="validation_error")
            deduped: list[str] = []
            for name in collections:
                stripped = name.strip()
                if not stripped:
                    return McpErrorResponse(
                        error="collection names must not be whitespace", code="validation_error"
                    )
                if stripped not in deduped:
                    deduped.append(stripped)
            if len(deduped) > _FANOUT_VALIDATION_LIMIT:
                return McpErrorResponse(
                    error=f"collections length exceeds {_FANOUT_VALIDATION_LIMIT}",
                    code="validation_error",
                )
            if rerank is False and len(deduped) > 1:
                return McpErrorResponse(
                    error="reranking cannot be disabled for multi-collection search in v1",
                    code="validation_error",
                )
            try:
                result = await pipeline.explain(
                    query, collections=deduped, top_k=top_k, rerank=rerank, namespace=DEFAULT_NAMESPACE
                )
            except CollectionNotFoundError:
                return McpErrorResponse(error="collection not found", code="not_found")
            except FanoutTimeoutError:
                return McpErrorResponse(error="search timed out", code="timeout")
            except MetadataLookupError:
                return McpErrorResponse(error="service unavailable", code="internal_error")
            except ExplainMultiCollectionNoRerankError as exc:
                return McpErrorResponse(error=str(exc), code="validation_error")
            except ExplainStageError as exc:
                logger.warning("explain stage %s failed: %s", exc.stage, exc.original, exc_info=exc.original)
                return McpErrorResponse(
                    error=f"{exc.stage} error: {type(exc.original).__name__}", code="internal_error"
                )
            except Exception:
                logger.exception("multi-collection explain failed")
                return McpErrorResponse(error="explain failed", code="internal_error")
            response = ExplainResponse.from_pipeline_result(
                rerank=rerank, collection="", routing=None, result=result, stage_timings_ms=None
            )
            result_dict = response.model_dump(mode="json", exclude_none=False)
            result_dict.pop("stage_timings_ms", None)
            return result_dict

        try:
            req = ExplainRequest(query=query, collection=collection, top_k=top_k, rerank=rerank)
        except ValidationError:
            # Do not echo str(exc): a query-field validation failure embeds the
            # rejected input value. The code conveys the category; the query is
            # never reflected back.
            return McpErrorResponse(error="invalid explain request", code="validation_error")

        ns = DEFAULT_NAMESPACE
        routing: RoutingExplain | None = None
        query_vector: list[float] | None = None
        timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)
        try:
            with ExitStack() as stack:
                recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
                t0 = time.perf_counter()

                _explain_active_model: str = config.embedding_model if config is not None else ""
                if req.collection is not None:
                    meta = await pipeline.get_collection_meta(req.collection, namespace=ns)
                    if meta is None:
                        return McpErrorResponse(error=f"Collection {req.collection!r} not found", code="not_found")
                    chosen = req.collection
                    _explain_active_model = meta.active_embedding_model or _explain_active_model
                elif config is None:
                    # No routing config — fall back to the default collection (like search).
                    chosen = default_collection
                else:
                    all_meta = await pipeline.get_all_collections_meta(namespace=ns)
                    if not all_meta:
                        return McpErrorResponse(error="no collections available", code="not_found")
                    query_vector = await pipeline._global_embedder.embed_one(req.query)
                    col_router = MultiCollectionRouter(
                        search_url="http://mcp",
                        embedder=pipeline._global_embedder,
                        shortlist_size=config.routing_shortlist_size,
                        confidence_threshold=config.routing_confidence_threshold,
                        embedding_model=config.embedding_model,
                    )
                    ranked = col_router.rank_with_scores(query_vector, all_meta)
                    chosen_meta, chosen_score = ranked[0]
                    chosen = chosen_meta.name
                    _explain_active_model = chosen_meta.active_embedding_model or _explain_active_model
                    threshold = config.routing_confidence_threshold
                    routing = RoutingExplain(
                        invoked=True,
                        chosen_collection=chosen,
                        confidence_threshold=threshold,
                        chosen_below_threshold=(chosen_score is None or chosen_score < threshold),
                        candidates=[RoutingCandidate(collection=m.name, centroid_score=s) for m, s in ranked],
                    )

                _explain_embedder = await _resolve_embedder_by_model(
                    pipeline, embedder_cache, _explain_active_model
                )
                # If the chosen collection uses a different model from the global
                # embedder, the pre-computed query_vector is in the wrong space.
                # Nullify it so the pipeline re-embeds with _explain_embedder.
                if _explain_embedder is not pipeline._global_embedder:
                    query_vector = None
                result = await pipeline.explain(
                    req.query, chosen, top_k=req.top_k, rerank=req.rerank, namespace=ns,
                    query_vector=query_vector, embedder=_explain_embedder,
                )
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
                rerank=req.rerank, collection=chosen, routing=routing, result=result,
                stage_timings_ms=stage_timings,
            )
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_explain_result(
                            collection=chosen,
                            result_count=len(response.results),
                            latency_ms=(monotonic() - start) * 1000.0,
                            correlation_id=_correlation_id.get(),
                        )
                    )
                except Exception:
                    logger.warning("telemetry: explain entry enqueue failed", exc_info=True)
            result_dict = response.model_dump(mode="json", exclude_none=False)
            if stage_timings is None:
                result_dict.pop("stage_timings_ms", None)
            return result_dict
        except ExplainStageError as exc:
            if writer is not None:
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
                except Exception:
                    logger.warning("telemetry: explain error entry enqueue failed", exc_info=True)
            # Sanitize: the original message could echo the query (e.g. an FTS error).
            logger.warning("explain stage %s failed: %s", exc.stage, exc.original, exc_info=exc.original)
            return McpErrorResponse(error=f"{exc.stage} error: {type(exc.original).__name__}", code="internal_error")
        except Exception:
            if writer is not None:
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
                except Exception:
                    logger.warning("telemetry: explain error entry enqueue failed", exc_info=True)
            logger.exception("explain failed")
            return McpErrorResponse(error="explain failed", code="internal_error")

    @app.tool()
    async def ingest_file(
        path: str,
        collection: str | None = None,
    ) -> dict[str, Any]:
        """Ingest a single file into the RAG store."""
        timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)
        try:
            validated = validate_ingest_path(path)
        except PathUnsafeError as e:
            return McpErrorResponse(error=_path_unsafe_message(e.reason), code="path_unsafe")
        try:
            _ingest_col = collection or default_collection
            _ingest_embedder = await _resolve_embedder(pipeline, embedder_cache, _ingest_col, config)
            with ExitStack() as stack:
                recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
                t0 = time.perf_counter()
                result = await pipeline.ingest_file(
                    validated, _ingest_col, embedder=_ingest_embedder, ingested_by="http",
                )
                if recorder is not None:
                    recorder.record("total", (time.perf_counter() - t0) * 1000.0)
                    logger.info(
                        "stage timings",
                        extra={
                            "event_type": "stage_timings",
                            "correlation_id": _correlation_id.get(),
                            "endpoint": "ingest",
                            "collection": _ingest_col,
                            "stage_timings_ms": recorder.stage_timings_ms,
                        },
                    )
            return asdict(result)
        except StoreBusyError as exc:
            return McpErrorResponse(error=str(exc), code="store_busy")
        except Exception as exc:
            logger.exception("ingest_file failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def ingest_directory(
        path: str,
        glob_pattern: str = "**/*",
        collection: str | None = None,
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        """Ingest all files in a directory into the RAG store."""
        timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)
        try:
            validated = validate_ingest_path(path)
        except PathUnsafeError as e:
            return McpErrorResponse(error=_path_unsafe_message(e.reason), code="path_unsafe")
        try:
            _dir_col = collection or default_collection
            _dir_embedder = await _resolve_embedder(pipeline, embedder_cache, _dir_col, config)

            async def progress_cb(done: int, total: int) -> None:
                if ctx is not None:
                    await ctx.report_progress(done, total)

            with ExitStack() as stack:
                recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
                t0 = time.perf_counter()
                results = await pipeline.ingest_directory(
                    validated,
                    _dir_col,
                    glob_pattern=glob_pattern,
                    progress_cb=progress_cb,
                    embedder=_dir_embedder,
                    ingested_by="http",
                )
                if recorder is not None:
                    recorder.record("total", (time.perf_counter() - t0) * 1000.0)
                    aggregated = recorder.stage_sums_ms
                    logger.info(
                        "stage timings",
                        extra={
                            "event_type": "stage_timings",
                            "correlation_id": _correlation_id.get(),
                            "endpoint": "ingest",
                            "collection": _dir_col,
                            "stage_timings_ms": aggregated,
                        },
                    )
            return [asdict(r) for r in results]
        except StoreBusyError as exc:
            return McpErrorResponse(error=str(exc), code="store_busy")
        except Exception as exc:
            logger.exception("ingest_directory failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def list_collections() -> list[dict[str, Any]]:
        """List all document collections with doc/chunk counts (centroid omitted)."""
        try:
            results = await pipeline.get_all_collections_meta()
            output = []
            for r in results:
                d = asdict(r)
                d.pop("centroid", None)
                d.pop("description_embedding", None)
                output.append(d)
            return output
        except Exception as exc:
            logger.exception("list_collections failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def get_collections_meta(
        include_description_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        """Return full CollectionMeta for all collections, including centroid vectors.

        ``description_embedding`` is stripped by default because it can significantly
        increase payload size at scale (one dense vector per collection). Pass
        ``include_description_embedding=True`` to retain the field.
        """
        try:
            results = await pipeline.get_all_collections_meta()
            output = []
            for r in results:
                d = asdict(r)
                if not include_description_embedding:
                    d.pop("description_embedding", None)
                output.append(d)
            return output
        except Exception as exc:
            logger.exception("get_collections_meta failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def get_collection_meta(name: str) -> dict[str, Any]:
        """Return full CollectionMeta for one named collection, including centroid."""
        try:
            meta = await pipeline.get_collection_meta(name)
            if meta is None:
                return McpErrorResponse(error=f"Collection {name!r} not found", code="not_found")
            return asdict(meta)
        except Exception as exc:
            logger.exception("get_collection_meta failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def list_documents(
        collection: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List documents in a collection."""
        try:
            results = await pipeline.list_documents(
                collection or default_collection, limit
            )
            return [asdict(r) for r in results]
        except Exception as exc:
            logger.exception("list_documents failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def delete_document(
        doc_id: str,
        collection: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Delete all chunks for a document from the store."""
        from archon_search.constants import DEFAULT_NAMESPACE  # noqa: PLC0415
        try:
            count = await pipeline.delete_document(
                doc_id, collection or default_collection,
                namespace=namespace or DEFAULT_NAMESPACE,
            )
            return {"deleted": count}
        except StoreBusyError as exc:
            logger.warning("delete_document store busy: %s", exc)
            return McpErrorResponse(error=str(exc), code="store_busy")
        except Exception as exc:
            logger.exception("delete_document failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def update_collection(
        collection_name: str,
        embedding_model: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Update the embedding model for a collection. Triggers reindex if needed.

        Returns the updated collection metadata dict (same shape as PATCH /collections/{name}).
        Error cases:
        - Collection not found in config or for caller's namespace → error dict (not_found).
        - Invalid embedding model → error dict (validation_error, 422-equivalent).
        - Active reindex job in progress → error dict (conflict, 409-equivalent).
        """
        try:
            ns: str = ctx.meta.get("namespace", DEFAULT_NAMESPACE)
            store = pipeline.store

            # 404 if collection not in config
            if config is not None:
                from archon_search.server.routes_collections import _all_collection_paths  # noqa: PLC0415
                path_to_name = _all_collection_paths(config)
                if collection_name not in path_to_name:
                    return McpErrorResponse(error=f"Collection {collection_name!r} not found", code="not_found")

            # 404 if meta not found for this namespace
            meta = await store.get_collection_meta(collection_name, namespace=ns)
            if meta is None:
                return McpErrorResponse(error=f"Collection {collection_name!r} not found", code="not_found")

            # Validate embedding model — 422-equivalent on ModelValidationError
            try:
                new_dim = await validate_embedding_model(embedding_model)
            except ModelValidationError as exc:
                return McpErrorResponse(error=str(exc), code="validation_error")

            # Dimension mismatch guard
            stored_dim = await store.get_stored_vector_dimension(collection_name, namespace=ns)
            if stored_dim is not None and stored_dim != new_dim:
                return McpErrorResponse(
                    error=(
                        f"model dimension mismatch: current vectors are {stored_dim}-dim, "
                        f"new model produces {new_dim}-dim; delete and recreate collection to change dimensions"
                    ),
                    code="validation_error",
                )

            # 409 guard: check if reindex job is still active (after validation, before state machine)
            stale_cleared = False
            if meta.reindex_job_id is not None and job_store is not None:
                job = job_store.get(meta.reindex_job_id)
                if job is not None and job.status in (JobStatus.RUNNING, JobStatus.PENDING):
                    return McpErrorResponse(
                        error="reindex in progress; wait for job to complete before changing embedding model",
                        code="conflict",
                    )
                # Stale (DONE/FAILED/CANCELLED) — clear it
                meta.reindex_job_id = None
                stale_cleared = True

            # State machine (mirrors patch_collection logic)
            active = meta.active_embedding_model
            pending = meta.pending_embedding_model
            requested = embedding_model

            if active == requested and pending is None:
                # (a) no-op — persist only if stale reindex_job_id was cleared
                if stale_cleared:
                    await store.update_collection_meta(meta)
            elif pending == requested:
                # (a') no-op — persist only if stale reindex_job_id was cleared
                if stale_cleared:
                    await store.update_collection_meta(meta)
            elif pending is not None and active == requested:
                # (c) revert: clear pending and any stale reindex_job_id
                meta.pending_embedding_model = None
                meta.needs_reindex = False
                meta.reindex_job_id = None
                await store.update_collection_meta(meta)
            else:
                # (b) or (d): new model requested
                chunk_count = await store.count_chunks(collection_name, namespace=ns)
                if chunk_count > 0:
                    meta.pending_embedding_model = requested
                    meta.needs_reindex = True
                else:
                    meta.active_embedding_model = requested
                    meta.pending_embedding_model = None
                    meta.needs_reindex = False
                    meta.reindex_job_id = None
                await store.update_collection_meta(meta)

            return asdict(meta)
        except Exception as exc:
            logger.exception("update_collection failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return app


def create_mcp_http_app(
    pipeline: SearchPipeline,
    default_collection: str,
    writer: TelemetryWriter | None = None,
    config: SearchConfig | None = None,
    request_id_header: str = "X-Request-ID",
    embedder_cache: EmbedderCache | None = None,
    job_store: JobStore | None = None,
) -> Starlette:
    """Return a Starlette HTTP app wrapping the FastMCP server with auth middleware.

    The underlying FastMCP app is exposed via the streamable HTTP transport
    (endpoint: /mcp).  APIKeyMiddleware is added so every request to /mcp
    requires a valid Bearer token; /health remains exempt per _EXEMPT_PATHS.
    """
    from archon_search.server.middleware_context import RequestContextMiddleware

    fastmcp_app = create_app(pipeline, default_collection, writer=writer, config=config, embedder_cache=embedder_cache, job_store=job_store)
    starlette_app: Starlette = fastmcp_app.streamable_http_app()
    api_key, _ = load_or_generate_key()
    starlette_app.add_middleware(APIKeyMiddleware, api_key=api_key, namespaces={})
    starlette_app.add_middleware(RequestContextMiddleware, header_name=request_id_header)
    return starlette_app


def _needs_install_trigger(
    existing_state: IndexingState | None,
    desired: dict[str, str],
) -> bool:
    """Return True if any desired collection needs (re-)indexing.

    Returns False immediately when desired is empty (nothing to index).

    Triggers when:
    - No state file exists (first run)
    - A desired collection is absent from state
    - A desired collection has any status other than DONE (PENDING/IN_PROGRESS/FAILED)
      — on restart, IN_PROGRESS always means the process crashed mid-index
    """
    if not desired:
        return False
    if existing_state is None:
        return True
    for name in desired:
        cp = existing_state.collections.get(name)
        if cp is None or cp.status != IndexingStatus.DONE:
            return True
    return False

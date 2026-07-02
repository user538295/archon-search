"""FastMCP HTTP server for RAG search."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import ExitStack
from pathlib import Path
from time import monotonic
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from fastmcp import Context, FastMCP
from pydantic import Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

import tarfile

from archon_search._path_safety import PathUnsafeError, validate_archive_members, validate_export_path, validate_ingest_path
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.filters import SearchFilters
from archon_search.hyde import resolve_hyde_vector
from archon_search.key_manager import KeyStore, load_or_generate_key
from archon_search.pipeline import (
    CollectionNotFoundError,
    ExplainMultiCollectionNoRerankError,
    ExplainStageError,
    FanoutTimeoutError,
    GraphCommunitiesNotBuiltError,
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
from archon_search.server.routes_search import _HYDE_EXPANSION_FAILED_WARNING
from archon_search.store import StoreBusyError
from archon_search.observability import bind_stage_recorder, correlation_id as _correlation_id
from archon_search.telemetry.entry import FilterFlags, TelemetryEntry
from archon_search.telemetry.writer import TelemetryWriter

from archon_search.embedder_cache import EmbedderCache
from archon_search.jobs.export_archive import EXPORT_SCHEMA_VERSION, ImportArchiveReader
from archon_search.jobs.model import job_to_dict
from archon_search.model_validation import ModelValidationError, validate_embedding_model
from archon_search.paths import get_data_dir
from archon_search.server.mcp_schemas import (
    CollectionDetailSchema,
    CollectionListItemSchema,
    CollectionMetaMcpSchema,
    ContextChunkSchema,
    DeleteDocumentSchema,
    DocumentInfoSchema,
    ExcludedCollectionMcpSchema,
    IngestResultSchema,
    McpSearchResponse,
    McpSearchResultSchema,
    SearchWithContextItemSchema,
    SearchWithContextResponse,
)
from archon_search.types import JobStatus

if TYPE_CHECKING:
    from archon_search.config import SearchConfig
    from archon_search.hyde import HyDEGenerator
    from archon_search.jobs.store import JobStore
    from archon_search.rag_fusion import RAGFusionGenerator

logger = logging.getLogger(__name__)

# Module-level lock serialising the MCP key rotation sequence (read current_token →
# write .search.env → rotate_default_key). Prevents concurrent MCP rotate_key calls
# from creating orphaned active key records (mirrors _rotate_lock in routes_keys.py).
# NOTE: This lock is INDEPENDENT of _rotate_lock in routes_keys.py. Concurrent calls
# to REST POST /keys/rotate and MCP rotate_key are NOT serialised against each other.
# This is an accepted limitation: cross-surface concurrent rotation is unsupported.
_mcp_rotate_lock = asyncio.Lock()

# Language filter parameter for the ``search`` tool.
# Defined at module level so FastMCP can resolve the Annotated type inside closures.
_LanguageParamSearch = Annotated[
    str | None,
    Field(
        description=(
            "ISO 639-1 or ISO 639-3 language code to filter results (e.g. 'fr', 'de', 'unknown'). "
            "Applied per-leg in multi-collection fan-out (collections parameter) "
            "and as a direct filter in single-collection search."
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


_ERR_SCHEMA = "schema_validation_error"
_VALID_GRAPH_MODES: tuple[None | str, ...] = (None, "naive", "local", "global")
# E2a — ingest TTL and scope validation limits (same constraints as routes_jobs.py IngestRequest validators)
_INT32_MAX: int = 2**31 - 1
_MAX_SCOPE_LIST_ITEMS: int = 100
_MAX_SCOPE_ITEM_LEN: int = 255


def _validate_ttl_and_scopes(
    chunk_ttl_seconds: int | None,
    chunk_scopes: list[str] | None,
) -> "McpErrorResponse | None":
    """Validate chunk_ttl_seconds and chunk_scopes for MCP ingest tools.

    Returns a McpErrorResponse dict if validation fails, None if valid.
    Mirrors the validation in routes_jobs.py IngestRequest validators.
    """
    if chunk_ttl_seconds is not None:
        if chunk_ttl_seconds < 1 or chunk_ttl_seconds > _INT32_MAX:
            return McpErrorResponse(
                error=f"chunk_ttl_seconds must be in [1, {_INT32_MAX}]; got {chunk_ttl_seconds}",
                code="invalid_parameter",
            )
    if chunk_scopes is not None:
        if len(chunk_scopes) > _MAX_SCOPE_LIST_ITEMS:
            return McpErrorResponse(
                error=f"chunk_scopes must not exceed {_MAX_SCOPE_LIST_ITEMS} items; got {len(chunk_scopes)}",
                code="invalid_parameter",
            )
        for scope in chunk_scopes:
            scope_chars = len(scope)
            if scope_chars < 1 or scope_chars > _MAX_SCOPE_ITEM_LEN:
                return McpErrorResponse(
                    error=(
                        f"each scope must be 1-{_MAX_SCOPE_ITEM_LEN} characters; "
                        f"got {scope_chars} for {scope!r}"
                    ),
                    code="invalid_parameter",
                )
    return None


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



def _get_request_namespace() -> str:
    """Return the namespace resolved by APIKeyMiddleware for the current MCP request.

    FastMCP's RequestContextMiddleware stores the current Starlette Request in the
    HTTP context. APIKeyMiddleware writes request.state.namespace before calling next.
    This helper reads that value so every tool closure can use the authenticated
    namespace instead of hardcoding DEFAULT_NAMESPACE.

    Falls back to DEFAULT_NAMESPACE when called outside an HTTP request context
    (e.g. direct invocation in tests or CLI mode).

    Per K-1 ADR: _current_http_request is set fresh on every HTTP POST (not frozen
    at session-initialize time), so this must be called on each tool invocation, not
    cached at app-creation time.

    Uses fastmcp.server.dependencies.get_http_request() (public API) which handles
    the MCP SDK request_ctx, FastMCP's HTTP context, and Docket worker snapshots.
    The import is lazy to avoid breaking test stubs that replace the fastmcp package
    with a plain module.
    """
    # ponytail: lazy import with fallback — test stubs replace fastmcp with a plain
    # module; ImportError or RuntimeError (no active request) → fall back to DEFAULT_NAMESPACE.
    try:
        from fastmcp.server.dependencies import get_http_request  # noqa: PLC0415
        req = get_http_request()
    except (ImportError, RuntimeError):
        return DEFAULT_NAMESPACE
    return getattr(req.state, "namespace", DEFAULT_NAMESPACE)


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
    namespace: str = DEFAULT_NAMESPACE,
    meta: Any = None,  # pre-fetched CollectionMeta; skips the get_collection_meta call
) -> Any:
    """Resolve the Embedder for *collection* by looking up its active_embedding_model.

    Falls back to pipeline._global_embedder when no cache is configured or when
    the resolved model name is empty (no config and no meta record).

    ``namespace`` is passed to ``pipeline.get_collection_meta()`` so the lookup
    is scoped to the authenticated namespace (asymmetry fix #2, BE-5).

    ``meta`` may be passed when the caller has already fetched the CollectionMeta,
    avoiding a redundant round-trip to the store.
    """
    if embedder_cache is None:
        return pipeline._global_embedder
    active_model: str = config.embedding_model if config is not None else ""
    if meta is None:
        meta = await pipeline.get_collection_meta(collection, namespace=namespace)
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
    hyde_generator: "HyDEGenerator | None" = None,
    rag_fusion_generator: "RAGFusionGenerator | None" = None,
    key_store: "KeyStore | None" = None,
    doc_id_hasher: Callable[[str], str] | None = None,
) -> FastMCP:
    """Create a FastMCP app with 13 RAG tools + up to 4 optional key-management tools.

    ``config`` is required only for the collectionless ``explain`` routing path;
    when omitted, ``explain`` without a collection falls back to
    ``default_collection`` (mirroring the ``search`` tool).

    ``job_store`` is required for the ``update_collection`` tool's 409 guard
    (active reindex check).

    ``key_store`` — when provided, the four key-management tools (``create_key``,
    ``list_keys``, ``revoke_key``, ``rotate_key``) are registered and can
    create/list/revoke/rotate managed API keys stored in ``keys.json``.
    """
    # Late-bound import: archon_search.rag_fusion may be reloaded in tests,
    # so we import lazily here (inside create_app) so the closures below always
    # capture the current class from sys.modules at app-creation time.
    from archon_search.rag_fusion import RAGFusionDependencyError  # noqa: PLC0415

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
        hyde: bool = False,
        rag_fusion: bool = False,
        graph_mode: str | None = None,
    ) -> dict[str, Any]:
        """Search for relevant document chunks using hybrid vector + FTS search.

        ``graph_mode`` controls graph-aware retrieval:
        - ``"naive"`` — entity n-gram expansion (E1a).
        - ``"local"`` — community-scoped retrieval for the matched community (E1b).
        - ``"global"`` — corpus-wide synthesis over all community representatives (E1b).
        Requires ``[graph] enabled = true`` in the server config.
        """
        ns = _get_request_namespace()
        timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)
        start = monotonic()

        # graph_mode validation — must be None, "naive", "local", or "global".
        if graph_mode not in _VALID_GRAPH_MODES:
            return McpErrorResponse(error=f"graph_mode must be one of {list(m for m in _VALID_GRAPH_MODES if m is not None)!r}", code="invalid_graph_mode")
        _graph_config = getattr(config, "graph", None)
        _graph_enabled = getattr(_graph_config, "enabled", False) if _graph_config is not None else False
        if graph_mode is not None and not _graph_enabled:
            return McpErrorResponse(error="graph_mode requires [graph] enabled=true", code="graph_disabled")

        # Mutual exclusion: rag_fusion=True suppresses HyDE entirely.
        _rf_config = getattr(config, "rag_fusion", None)
        if _rf_config is None:
            from archon_search.config import RAGFusionConfig  # noqa: PLC0415
            _rf_config = RAGFusionConfig()
        if rag_fusion:
            hyde_vector, hyde_applied = None, False
            _search_hyde_expansion_warning: str | None = None
        else:
            _hyde_config = getattr(config, "hyde", None)
            if _hyde_config is None:
                from archon_search.config import HyDEConfig  # noqa: PLC0415
                _hyde_config = HyDEConfig()
            try:
                hyde_vector, hyde_applied = await resolve_hyde_vector(query, hyde, hyde_generator, _hyde_config)
            except RuntimeError as exc:
                return McpErrorResponse(error=str(exc), code="validation_error")
            # HyDE failure: requested but returned no vector
            _search_hyde_expansion_warning = _HYDE_EXPANSION_FAILED_WARNING if (hyde and not hyde_applied) else None

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
            deduped: list[str] = []
            for name in collections:
                stripped = name.strip()
                if not stripped:
                    return McpErrorResponse(
                        error="collection names must not be whitespace", code="validation_error"
                    )
                if stripped not in deduped:
                    deduped.append(stripped)
            if config is not None:
                _cfg = config
            else:
                from archon_search.config import SearchConfig as _SearchConfig  # noqa: PLC0415
                _cfg = _SearchConfig()
            _max_fanout = _cfg.max_fanout
            if len(deduped) > _max_fanout:
                return McpErrorResponse(
                    error=f"collections length exceeds maximum of {_max_fanout}",
                    code="validation_error",
                )
            # Build SearchFilters for multi-collection path (mirrors single-collection path
            # below). Pass filters=None when all filter args are None to preserve the
            # None-vs-all-defaults distinction required by the search_many() contract.
            _any_filter = any(
                v is not None
                for v in (
                    file_type, source_path_prefix, source_path_glob,
                    indexed_after, indexed_before, language,
                )
            )
            if _any_filter:
                try:
                    _multi_filters: SearchFilters | None = SearchFilters(
                        file_type=file_type,
                        source_path_prefix=source_path_prefix,
                        source_path_glob=source_path_glob,
                        indexed_after=indexed_after,
                        indexed_before=indexed_before,
                        language=language,
                    )
                except ValidationError as exc:
                    return McpErrorResponse(error=str(exc), code="validation_error")
            else:
                _multi_filters = None
            try:
                result_obj = await pipeline.search_many(
                    query, deduped, namespace=ns, query_vector=hyde_vector,
                    rag_fusion=rag_fusion,
                    rag_fusion_generator=rag_fusion_generator,
                    rag_fusion_config=_rf_config,
                    filters=_multi_filters,
                    graph_mode=graph_mode,
                )
            except RAGFusionDependencyError as exc:
                return McpErrorResponse(error=str(exc), code="validation_error")
            except GraphCommunitiesNotBuiltError as exc:
                return McpErrorResponse(error=str(exc), code="graph_communities_not_built")
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
                            rag_fusion_applied=result_obj.rag_fusion_applied,
                            rag_fusion_queries_used=result_obj.rag_fusion_queries_used,
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search_multi entry enqueue failed", exc_info=True)
            try:
                result_schemas = []
                for r in result_obj.results:
                    rs = McpSearchResultSchema.from_result(r)
                    if not include_metadata:
                        rs.metadata = {}
                    result_schemas.append(rs)
                _multi_expansion_warning = _search_hyde_expansion_warning or result_obj.rag_fusion_warning
                _multi_graph_expansion_applied = result_obj.graph_expansion_applied
                response = McpSearchResponse(
                    results=result_schemas,
                    acl_filtered=result_obj.acl_filtered,
                    excluded_collections=[
                        ExcludedCollectionMcpSchema(name=e.name, reason=e.reason)
                        for e in result_obj.excluded_collections
                    ],
                    hyde_applied=hyde_applied,
                    expansion_used=hyde_applied or result_obj.rag_fusion_applied or _multi_graph_expansion_applied,
                    expansion_warning=_multi_expansion_warning,
                    graph_expansion_applied=_multi_graph_expansion_applied,
                )
                return response.model_dump(mode="json")
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)

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
            # Namespace gate: collection must exist in the caller's namespace.
            # Mirrors the REST search route's get_collection_meta check (routes_search.py)
            # which returns 404 when the collection is owned by a different namespace.
            _col_meta = await pipeline.get_collection_meta(_col, namespace=ns)
            if _col_meta is None:
                return McpErrorResponse(error=f"collection {_col!r} not found", code="not_found")
            _search_embedder = await _resolve_embedder(pipeline, embedder_cache, _col, config, namespace=ns, meta=_col_meta)
            with ExitStack() as stack:
                recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
                t0 = time.perf_counter()
                result_obj = await pipeline.search(
                    query, _col, ns, embedder=_search_embedder, filters=filters, query_vector=hyde_vector,
                    rag_fusion=rag_fusion,
                    rag_fusion_generator=rag_fusion_generator,
                    rag_fusion_config=_rf_config,
                    graph_mode=graph_mode,
                )
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
                            rag_fusion_applied=result_obj.rag_fusion_applied,
                            rag_fusion_queries_used=result_obj.rag_fusion_queries_used,
                            doc_id_hasher=doc_id_hasher,
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search entry enqueue failed", exc_info=True)
            try:
                result_schemas = []
                for r in result_obj.results:
                    rs = McpSearchResultSchema.from_result(r)
                    if not include_metadata:
                        rs.metadata = {}
                    result_schemas.append(rs)
                _single_expansion_warning = _search_hyde_expansion_warning or result_obj.rag_fusion_warning
                _single_graph_expansion_applied = result_obj.graph_expansion_applied
                response = McpSearchResponse(
                    results=result_schemas,
                    acl_filtered=result_obj.acl_filtered,
                    excluded_collections=[
                        ExcludedCollectionMcpSchema(name=e.name, reason=e.reason)
                        for e in result_obj.excluded_collections
                    ],
                    hyde_applied=hyde_applied,
                    expansion_used=hyde_applied or result_obj.rag_fusion_applied or _single_graph_expansion_applied,
                    expansion_warning=_single_expansion_warning,
                    graph_expansion_applied=_single_graph_expansion_applied,
                )
                return response.model_dump(mode="json")
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)
        except RAGFusionDependencyError as exc:
            return McpErrorResponse(error=str(exc), code="validation_error")
        except GraphCommunitiesNotBuiltError as exc:
            return McpErrorResponse(error=str(exc), code="graph_communities_not_built")
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
        hyde: bool = False,
        rag_fusion: bool = False,
        graph_mode: str | None = None,
    ) -> dict[str, Any]:
        """Search and return surrounding chunks for richer context.

        Returns ``{"results": [...], "hyde_applied": bool, "expansion_used": bool,
        "expansion_warning": str | null}``.
        """
        # graph_mode on search_with_context is not supported; use the search tool instead.
        if graph_mode is not None:
            return McpErrorResponse(
                error="graph_mode (naive, local, global) on search_with_context is not supported; use the search tool instead",
                code="graph_mode_not_supported",
            )

        _swc_ns = _get_request_namespace()
        timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)
        start = monotonic()

        # Mutual exclusion: rag_fusion=True suppresses HyDE entirely.
        _swc_rf_config = getattr(config, "rag_fusion", None)
        if _swc_rf_config is None:
            from archon_search.config import RAGFusionConfig  # noqa: PLC0415
            _swc_rf_config = RAGFusionConfig()
        if rag_fusion:
            swc_hyde_vector, swc_hyde_applied = None, False
            _swc_hyde_expansion_warning: str | None = None
        else:
            _swc_hyde_config = getattr(config, "hyde", None)
            if _swc_hyde_config is None:
                from archon_search.config import HyDEConfig  # noqa: PLC0415
                _swc_hyde_config = HyDEConfig()
            try:
                swc_hyde_vector, swc_hyde_applied = await resolve_hyde_vector(
                    query, hyde, hyde_generator, _swc_hyde_config
                )
            except RuntimeError as exc:
                return McpErrorResponse(error=str(exc), code="validation_error")
            # HyDE failure: requested but returned no vector
            _swc_hyde_expansion_warning = _HYDE_EXPANSION_FAILED_WARNING if (hyde and not swc_hyde_applied) else None

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
            # Namespace gate: collection must exist in the caller's namespace.
            _swc_meta = await pipeline.get_collection_meta(_swc_col, namespace=_swc_ns)
            if _swc_meta is None:
                return McpErrorResponse(error=f"collection {_swc_col!r} not found", code="not_found")
            _swc_embedder = await _resolve_embedder(pipeline, embedder_cache, _swc_col, config, namespace=_swc_ns, meta=_swc_meta)
            with ExitStack() as stack:
                recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
                t0 = time.perf_counter()
                swc_result = await pipeline.search_with_context(
                    query, _swc_col, context_window, namespace=_swc_ns, embedder=_swc_embedder, filters=filters,
                    query_vector=swc_hyde_vector,
                    rag_fusion=rag_fusion,
                    rag_fusion_generator=rag_fusion_generator,
                    rag_fusion_config=_swc_rf_config,
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
            _swc_pipeline_result = swc_result.pipeline_result
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_search_tool_result(
                            endpoint="search_with_context",
                            collection=_swc_col,
                            result_doc_ids=[r["result"].doc_id for r in swc_result.results],
                            latency_ms=(monotonic() - start) * 1000.0,
                            filter_flags=FilterFlags.from_search_filters(filters),
                            correlation_id=_correlation_id.get(),
                            rag_fusion_applied=_swc_pipeline_result.rag_fusion_applied,
                            rag_fusion_queries_used=_swc_pipeline_result.rag_fusion_queries_used,
                            doc_id_hasher=doc_id_hasher,
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search_with_context entry enqueue failed", exc_info=True)
            try:
                items = []
                for r in swc_result.results:
                    result_schema = McpSearchResultSchema.from_result(r["result"])
                    if not include_metadata:
                        result_schema.metadata = {}
                    context_before = []
                    for c in r["context_before"]:
                        chunk_schema = ContextChunkSchema.from_result(c)
                        if not include_metadata:
                            chunk_schema.metadata = {}
                        context_before.append(chunk_schema)
                    context_after = []
                    for c in r["context_after"]:
                        chunk_schema = ContextChunkSchema.from_result(c)
                        if not include_metadata:
                            chunk_schema.metadata = {}
                        context_after.append(chunk_schema)
                    items.append(
                        SearchWithContextItemSchema(
                            result=result_schema,
                            context_before=context_before,
                            context_after=context_after,
                        )
                    )
                _swc_expansion_warning = _swc_hyde_expansion_warning or _swc_pipeline_result.rag_fusion_warning
                _swc_graph_expansion_applied = _swc_pipeline_result.graph_expansion_applied
                response = SearchWithContextResponse(
                    results=items,
                    hyde_applied=swc_hyde_applied,
                    expansion_used=swc_hyde_applied or _swc_pipeline_result.rag_fusion_applied or _swc_graph_expansion_applied,
                    expansion_warning=_swc_expansion_warning,
                )
                return response.model_dump(mode="json")
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)
        except RAGFusionDependencyError as exc:
            return McpErrorResponse(error=str(exc), code="validation_error")
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
        hyde: bool = False,
        rag_fusion: bool = False,
        graph_mode: str | None = None,
    ) -> dict[str, Any]:
        """Return the per-stage retrieval/reranking trace for a query, plus the
        routing decision when no collection is pinned. Operates in the caller's
        authenticated namespace. The query is never echoed in the response or telemetry."""
        _explain_ns = _get_request_namespace()
        start = monotonic()

        if config is not None:
            _explain_cfg = config
        else:
            from archon_search.config import SearchConfig as _SearchConfig  # noqa: PLC0415
            _explain_cfg = _SearchConfig()
        _top_k_max = _explain_cfg.top_k_max
        if top_k > _top_k_max:
            return McpErrorResponse(
                error=f"top_k {top_k} exceeds operator-configured maximum of {_top_k_max}",
                code="validation_error",
            )
        if top_k < 1:
            return McpErrorResponse(
                error="top_k must be at least 1",
                code="validation_error",
            )

        # graph_mode validation — must be None, "naive", "local", or "global".
        if graph_mode not in _VALID_GRAPH_MODES:
            return McpErrorResponse(
                error=f"graph_mode must be one of {list(m for m in _VALID_GRAPH_MODES if m is not None)!r}",
                code="invalid_graph_mode",
            )
        _explain_graph_config = getattr(config, "graph", None)
        _explain_graph_enabled = (
            getattr(_explain_graph_config, "enabled", False) if _explain_graph_config is not None else False
        )
        if graph_mode is not None and not _explain_graph_enabled:
            return McpErrorResponse(error="graph_mode requires [graph] enabled=true", code="graph_disabled")

        # Mutual exclusion: graph_mode wins over HyDE — skip the LLM call entirely.
        # rag_fusion=True also suppresses HyDE. graph_mode check comes first so the
        # HyDE LLM call is never made when graph_mode is set (mirrors rag_fusion pattern).
        _explain_rf_config = getattr(config, "rag_fusion", None)
        if _explain_rf_config is None:
            from archon_search.config import RAGFusionConfig  # noqa: PLC0415
            _explain_rf_config = RAGFusionConfig()
        if graph_mode is not None or rag_fusion:
            explain_hyde_vector, explain_hyde_applied = None, False
        else:
            _explain_hyde_config = getattr(config, "hyde", None)
            if _explain_hyde_config is None:
                from archon_search.config import HyDEConfig  # noqa: PLC0415
                _explain_hyde_config = HyDEConfig()
            try:
                explain_hyde_vector, explain_hyde_applied = await resolve_hyde_vector(
                    query, hyde, hyde_generator, _explain_hyde_config
                )
            except RuntimeError as exc:
                return McpErrorResponse(error=str(exc), code="validation_error")

        # graph_mode with multi-collection fanout is not supported in E1c — each collection
        # has independent graph tables; cross-collection graph merge is out of scope.
        if graph_mode is not None and collections is not None:
            return McpErrorResponse(
                error="graph_mode is not supported with multi-collection fanout (collections parameter)",
                code="graph_mode_multi_collection_unsupported",
            )

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
            _explain_max_fanout = _explain_cfg.max_fanout
            if len(deduped) > _explain_max_fanout:
                return McpErrorResponse(
                    error=f"collections length exceeds maximum of {_explain_max_fanout}",
                    code="validation_error",
                )
            if rerank is False and len(deduped) > 1:
                return McpErrorResponse(
                    error="reranking cannot be disabled for multi-collection search in v1",
                    code="validation_error",
                )
            try:
                result = await pipeline.explain(
                    query, collections=deduped, top_k=top_k, rerank=rerank, namespace=_explain_ns,
                    query_vector=explain_hyde_vector,
                    rag_fusion=rag_fusion,
                    rag_fusion_generator=rag_fusion_generator,
                    rag_fusion_config=_explain_rf_config,
                )
            except RAGFusionDependencyError as exc:
                return McpErrorResponse(error=str(exc), code="validation_error")
            except GraphCommunitiesNotBuiltError as exc:
                return McpErrorResponse(error=str(exc), code="graph_communities_not_built")
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
            try:
                response = ExplainResponse.from_pipeline_result(
                    rerank=rerank, collection="", routing=None, result=result, stage_timings_ms=None,
                    hyde_applied=explain_hyde_applied,
                    rag_fusion_applied=result.rag_fusion_applied,
                    rag_fusion_queries_used=result.rag_fusion_queries_used,
                    rag_fusion_attempted=result.rag_fusion_attempted,
                    rag_fusion_failure_reason=result.rag_fusion_failure_reason,
                    rag_fusion_sub_query_results=result.rag_fusion_sub_query_results,
                    graph_mode_applied=result.graph_mode_applied,
                )
                result_dict = response.model_dump(mode="json", exclude_none=False)
                result_dict.pop("stage_timings_ms", None)
                return result_dict
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)

        try:
            req = ExplainRequest(query=query, collection=collection, top_k=top_k, rerank=rerank)
        except ValidationError:
            # Do not echo str(exc): a query-field validation failure embeds the
            # rejected input value. The code conveys the category; the query is
            # never reflected back.
            return McpErrorResponse(error="invalid explain request", code="validation_error")

        ns = _explain_ns
        routing: RoutingExplain | None = None
        query_vector: list[float] | None = explain_hyde_vector
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
                    # Use HyDE vector for routing if available, otherwise embed the query.
                    if query_vector is None:
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
                # Also update explain_hyde_applied: if we discard the HyDE vector,
                # it was not actually used for retrieval.
                if _explain_embedder is not pipeline._global_embedder:
                    query_vector = None
                    explain_hyde_applied = False
                # graph_mode: null the query_vector so routing-computed vectors do not
                # leak into graph retrieval (explain_hyde_applied is already False from
                # the early HyDE suppression block above).
                if graph_mode is not None:
                    query_vector = None
                result = await pipeline.explain(
                    req.query, chosen, top_k=req.top_k, rerank=req.rerank, namespace=ns,
                    query_vector=query_vector, embedder=_explain_embedder,
                    rag_fusion=rag_fusion,
                    rag_fusion_generator=rag_fusion_generator,
                    rag_fusion_config=_explain_rf_config,
                    graph_mode=graph_mode,
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
            try:
                response = ExplainResponse.from_pipeline_result(
                    rerank=req.rerank, collection=chosen, routing=routing, result=result,
                    stage_timings_ms=stage_timings, hyde_applied=explain_hyde_applied,
                    rag_fusion_applied=result.rag_fusion_applied,
                    rag_fusion_queries_used=result.rag_fusion_queries_used,
                    rag_fusion_attempted=result.rag_fusion_attempted,
                    rag_fusion_failure_reason=result.rag_fusion_failure_reason,
                    rag_fusion_sub_query_results=result.rag_fusion_sub_query_results,
                    graph_mode_applied=result.graph_mode_applied,
                )
                if writer is not None:
                    try:
                        writer.enqueue(
                            TelemetryEntry.from_explain_result(
                                collection=chosen,
                                result_count=len(response.results),
                                latency_ms=(monotonic() - start) * 1000.0,
                                correlation_id=_correlation_id.get(),
                                rag_fusion_applied=result.rag_fusion_applied,
                                rag_fusion_queries_used=result.rag_fusion_queries_used,
                            )
                        )
                    except Exception:
                        logger.warning("telemetry: explain entry enqueue failed", exc_info=True)
                result_dict = response.model_dump(mode="json", exclude_none=False)
                if stage_timings is None:
                    result_dict.pop("stage_timings_ms", None)
                return result_dict
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)
        except RAGFusionDependencyError as exc:
            return McpErrorResponse(error=str(exc), code="validation_error")
        except GraphCommunitiesNotBuiltError as exc:
            return McpErrorResponse(error=str(exc), code="graph_communities_not_built")
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
        chunk_ttl_seconds: int | None = None,
        chunk_scopes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Ingest a single file into the RAG store."""
        _err = _validate_ttl_and_scopes(chunk_ttl_seconds, chunk_scopes)
        if _err is not None:
            return _err
        _ingest_ns = _get_request_namespace()
        timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)
        try:
            validated = validate_ingest_path(path)
        except PathUnsafeError as e:
            return McpErrorResponse(error=_path_unsafe_message(e.reason), code="path_unsafe")
        try:
            _ingest_col = collection or default_collection
            _ingest_embedder = await _resolve_embedder(pipeline, embedder_cache, _ingest_col, config, namespace=_ingest_ns)
            with ExitStack() as stack:
                recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
                t0 = time.perf_counter()
                result = await pipeline.ingest_file(
                    validated, _ingest_col, embedder=_ingest_embedder, ingested_by="http",
                    namespace=_ingest_ns,
                    chunk_ttl_seconds=chunk_ttl_seconds,
                    chunk_scopes=chunk_scopes,
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
            try:
                schema = IngestResultSchema.from_result(result)
                return schema.model_dump(mode="json")
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)
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
        chunk_ttl_seconds: int | None = None,
        chunk_scopes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Ingest all files in a directory into the RAG store."""
        _err = _validate_ttl_and_scopes(chunk_ttl_seconds, chunk_scopes)
        if _err is not None:
            return _err
        _dir_ns = _get_request_namespace()
        timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)
        try:
            validated = validate_ingest_path(path)
        except PathUnsafeError as e:
            return McpErrorResponse(error=_path_unsafe_message(e.reason), code="path_unsafe")
        try:
            _dir_col = collection or default_collection
            _dir_embedder = await _resolve_embedder(pipeline, embedder_cache, _dir_col, config, namespace=_dir_ns)

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
                    namespace=_dir_ns,
                    chunk_ttl_seconds=chunk_ttl_seconds,
                    chunk_scopes=chunk_scopes,
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
            try:
                return [IngestResultSchema.from_result(r).model_dump(mode="json") for r in results]
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)
        except StoreBusyError as exc:
            return McpErrorResponse(error=str(exc), code="store_busy")
        except Exception as exc:
            logger.exception("ingest_directory failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def list_collections() -> list[dict[str, Any]]:
        """List all document collections with doc/chunk counts (internal fields omitted)."""
        try:
            results = await pipeline.get_all_collections_meta(_get_request_namespace())
            try:
                return [CollectionListItemSchema.from_result(r).model_dump(mode="json") for r in results]
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)
        except Exception as exc:
            logger.exception("list_collections failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def get_collections_meta(
        include_description_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        """Return public CollectionMeta for all collections.

        ``description_embedding`` is ``null`` by default because it can significantly
        increase payload size at scale (one dense vector per collection). Pass
        ``include_description_embedding=True`` to retain the field.
        """
        try:
            results = await pipeline.get_all_collections_meta(_get_request_namespace())
            try:
                return [
                    CollectionMetaMcpSchema.from_result(
                        r, include_description_embedding=include_description_embedding
                    ).model_dump(mode="json")
                    for r in results
                ]
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)
        except Exception as exc:
            logger.exception("get_collections_meta failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def get_collection_meta(name: str) -> dict[str, Any]:
        """Return public CollectionMeta for one named collection (internal fields omitted)."""
        try:
            meta = await pipeline.get_collection_meta(name, namespace=_get_request_namespace())
            if meta is None:
                return McpErrorResponse(error=f"Collection {name!r} not found", code="not_found")
            try:
                schema = CollectionDetailSchema.from_result(meta)
                return schema.model_dump(mode="json")
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)
        except Exception as exc:
            logger.exception("get_collection_meta failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def list_documents(
        collection: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """List documents in a collection."""
        try:
            items, _next_cursor, _total = await pipeline.list_documents(
                collection or default_collection, limit, cursor=cursor, namespace=_get_request_namespace()
            )
            try:
                return [DocumentInfoSchema.from_result(r).model_dump(mode="json") for r in items]
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)
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
        _ns = _get_request_namespace()
        if namespace is not None and namespace != _ns:
            return McpErrorResponse(
                error=f"namespace mismatch: caller authenticated as {_ns!r} but requested {namespace!r}",
                code="forbidden",
            )
        try:
            count = await pipeline.delete_document(
                doc_id, collection or default_collection,
                namespace=_ns,
            )
            try:
                schema = DeleteDocumentSchema(deleted=count)
                return schema.model_dump(mode="json")
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)
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
            ns: str = _get_request_namespace()
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

            try:
                schema = CollectionDetailSchema.from_result(meta)
                return schema.model_dump(mode="json")
            except ValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_SCHEMA)
        except Exception as exc:
            logger.exception("update_collection failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def export_collection(
        collection: str,
        output_path: str = "",
    ) -> dict[str, Any]:
        """Enqueue an export job for a collection. Returns immediately with a QUEUED job dict.

        The MCP client can poll GET /jobs/{job_id} for progress.
        ``output_path`` must be an absolute path inside the server's data directory.
        If omitted, defaults to ``<data_dir>/exports/``.
        """
        if job_store is None:
            return McpErrorResponse(error="job store not configured", code="internal_error")

        _export_ns = _get_request_namespace()
        exports_dir = get_data_dir() / "exports"
        raw_output = output_path if output_path else str(exports_dir)

        try:
            resolved_dir = validate_export_path(raw_output, [get_data_dir()])
        except PathUnsafeError as exc:
            return McpErrorResponse(
                error=_path_unsafe_message(exc.reason), code="path_unsafe"
            )

        meta = await pipeline.store.get_collection_meta(collection, _export_ns)
        if meta is None:
            return McpErrorResponse(
                error=f"Collection {collection!r} not found", code="not_found"
            )

        from datetime import datetime, timezone  # noqa: PLC0415
        from uuid import uuid4  # noqa: PLC0415

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        job_uuid = str(uuid4())
        archive_path = resolved_dir / f"{collection}-{timestamp}.tar.gz"
        tmp_path_val = resolved_dir / f".export-{job_uuid}.jsonl.tmp"

        job = job_store.create_export(
            collection=collection,
            output_path=str(archive_path),
            tmp_path=str(tmp_path_val),
            namespace=_export_ns,
        )
        return job_to_dict(job)

    @app.tool()
    async def import_collection(
        collection: str,
        path: str,
        force_overwrite: bool = False,
        ignore_schema_version: bool = False,
        on_error: str = "fail",
    ) -> dict[str, Any]:
        """Enqueue an import job for a collection from a .tar.gz archive.

        Returns immediately with a QUEUED job dict; poll GET /jobs/{job_id} for progress.
        ``path`` must be an absolute path to the archive, inside the server's data directory.
        ``on_error`` must be ``"fail"`` (abort on corrupt line) or ``"skip"`` (skip corrupt lines).
        """
        if job_store is None:
            return McpErrorResponse(error="job store not configured", code="internal_error")

        _import_ns = _get_request_namespace()

        if on_error not in {"fail", "skip"}:
            return McpErrorResponse(
                error="on_error must be 'fail' or 'skip'", code="validation_error"
            )

        try:
            validate_export_path(path, [get_data_dir()])
        except PathUnsafeError as exc:
            return McpErrorResponse(
                error=_path_unsafe_message(exc.reason), code="path_unsafe"
            )

        from pathlib import Path as _Path  # noqa: PLC0415

        archive_path = _Path(path)
        if not archive_path.exists():
            return McpErrorResponse(
                error=f"Archive not found: {path}", code="not_found"
            )

        try:
            with tarfile.open(archive_path, "r:gz") as tf:
                validate_archive_members(tf)
        except PathUnsafeError as exc:
            return McpErrorResponse(
                error=f"unsafe archive: {exc.reason}", code="path_unsafe"
            )
        except Exception as exc:  # noqa: BLE001
            return McpErrorResponse(
                error=f"invalid archive: {exc}", code="validation_error"
            )

        try:
            reader = ImportArchiveReader(archive_path)
            manifest = reader.read_manifest()
        except (ValueError, PathUnsafeError) as exc:
            return McpErrorResponse(
                error=f"invalid manifest: {exc}", code="validation_error"
            )

        archive_model = manifest.get("active_embedding_model", "")
        server_model = config.embedding_model if config is not None else ""
        if archive_model != server_model:
            return McpErrorResponse(
                error=(
                    f"embedding model mismatch: archive has {archive_model!r}, "
                    f"server is configured with {server_model!r}"
                ),
                code="embedding_model_mismatch",
            )

        existing_meta = await pipeline.store.get_collection_meta(collection, _import_ns)
        if existing_meta is not None and not force_overwrite:
            return McpErrorResponse(
                error=f"collection {collection!r} already exists; use force_overwrite=True to overwrite",
                code="collection_exists",
            )

        archive_schema = manifest.get("schema_version")
        if not ignore_schema_version and archive_schema != EXPORT_SCHEMA_VERSION:
            return McpErrorResponse(
                error=(
                    f"archive has schema_version={archive_schema!r}; "
                    f"server expects {EXPORT_SCHEMA_VERSION!r}; "
                    "use ignore_schema_version=True to bypass"
                ),
                code="schema_version_mismatch",
            )

        job = job_store.create_import(
            collection=collection,
            archive_path=path,
            force_overwrite=force_overwrite,
            ignore_schema_version=ignore_schema_version,
            on_error=on_error,
            namespace=_import_ns,
        )
        return job_to_dict(job)

    # -----------------------------------------------------------------------
    # Key-management tools (D7 BE-9) — registered only when key_store is set.
    # These tools mirror the REST /keys endpoints but are exposed over MCP.
    # rotate_key writes .search.env via the same atomic_write_bytes helper used
    # by POST /keys/rotate — it does NOT call the REST endpoint internally.
    # -----------------------------------------------------------------------

    if key_store is not None:
        import asyncio as _asyncio  # noqa: PLC0415
        import os as _os  # noqa: PLC0415
        import secrets as _secrets  # noqa: PLC0415

        from archon_search.constants import _validate_namespace  # noqa: PLC0415
        from archon_search._durable_io import atomic_write_bytes as _atomic_write  # noqa: PLC0415
        from archon_search.key_manager import ENV_VAR as _ENV_VAR, get_key_file as _get_key_file  # noqa: PLC0415

        @app.tool()
        async def create_key(
            namespace: str,
            label: str | None = None,
            expires_at: str | None = None,
        ) -> dict[str, Any]:
            """Issue a new managed API key.

            The raw bearer token is returned exactly once in the response ``token``
            field. It is never stored on disk — only its SHA-256 hash is persisted.

            ``expires_at`` must be an ISO-8601 datetime string with timezone offset
            (e.g. ``"2030-01-01T00:00:00Z"``), or ``null`` for no expiry.
            """
            try:
                _validate_namespace(namespace)
            except ValueError as exc:
                return McpErrorResponse(error=str(exc), code="validation_error")

            from datetime import datetime  # noqa: PLC0415

            parsed_expires_at = None
            if expires_at is not None:
                try:
                    parsed_expires_at = datetime.fromisoformat(
                        expires_at.replace("Z", "+00:00")
                    )
                except ValueError as exc:
                    return McpErrorResponse(
                        error=f"invalid expires_at: {exc}", code="validation_error"
                    )
                if parsed_expires_at.tzinfo is None:
                    return McpErrorResponse(
                        error="expires_at must be timezone-aware (include UTC offset or Z suffix)",
                        code="validation_error",
                    )

            try:
                result = await key_store.create(
                    ns=namespace,
                    label=label,
                    expires_at=parsed_expires_at,
                )
            except Exception:
                return McpErrorResponse(error="Failed to create key", code="internal_error")
            return {
                "id": result["id"],
                "token": result["token"],
                "namespace": namespace,
                "label": label,
                "created_at": str(result["created_at"]),
                "expires_at": str(parsed_expires_at) if parsed_expires_at is not None else None,
                "status": "active",
            }

        @app.tool()
        async def list_keys(
            status: str = "active",
            namespace: str | None = None,
        ) -> dict[str, Any]:
            """List managed API keys.

            ``status`` — ``"active"`` (default), ``"revoked"``, or ``"all"``.
            ``namespace`` — optional filter; when set, only keys for that namespace
            are returned and ``hidden_revoked_count`` reflects only that namespace.
            """
            if status not in {"active", "revoked", "all"}:
                return McpErrorResponse(
                    error="status must be 'active', 'revoked', or 'all'",
                    code="validation_error",
                )

            all_records = await key_store.list_keys()

            if namespace is not None:
                scope = [r for r in all_records if r.namespace == namespace]
            else:
                scope = all_records

            if status == "active":
                filtered = [r for r in scope if r.status == "active"]
                hidden_revoked_count = sum(1 for r in scope if r.status == "revoked")
            elif status == "revoked":
                filtered = [r for r in scope if r.status == "revoked"]
                hidden_revoked_count = 0
            else:  # "all"
                filtered = list(scope)
                hidden_revoked_count = 0

            return {
                "keys": [
                    {
                        "id": r.id,
                        "namespace": r.namespace,
                        "label": r.label,
                        "created_at": str(r.created_at),
                        "expires_at": str(r.expires_at) if r.expires_at is not None else None,
                        "status": r.status,
                    }
                    for r in filtered
                ],
                "hidden_revoked_count": hidden_revoked_count,
            }

        @app.tool()
        async def revoke_key(key_id: str) -> dict[str, Any]:
            """Revoke a managed API key by its ID.

            Idempotent: revoking an already-revoked key returns success.
            Returns an error for unknown IDs (key never existed).
            TOML synthetic keys (``id=null``) cannot be targeted — pass the
            literal string ``"null"`` to get a helpful error message.
            """
            if key_id == "null":
                return McpErrorResponse(
                    error=(
                        "This key is managed via archon-search.toml [namespaces] — "
                        "remove it from the config file and restart the server."
                    ),
                    code="not_found",
                )

            try:
                await key_store.revoke(key_id)
            except KeyError:
                return McpErrorResponse(
                    error=f"Key not found: {key_id!r}", code="not_found"
                )

            return {"id": key_id, "status": "revoked"}

        @app.tool()
        async def rotate_key(grace_seconds: int | None = None) -> dict[str, Any]:
            """Rotate the default API key.

            Generates a new managed API key, writes the new raw token to
            ``.search.env``, and revokes (or grace-expires) the old default key
            in ``keys.json``.

            ``grace_seconds`` overrides the TOML ``[auth].rotate_grace_seconds``
            default.  When ``null``, the TOML default is used.  When both are 0,
            the old key is immediately revoked.

            Returns 409 (conflict) when ``ARCHON_SEARCH_API_KEY`` env var is set —
            the env var overrides ``.search.env``, so rotation would be a silent
            no-op (same behaviour as ``POST /keys/rotate``, S23).

            Note: the MCP server's in-memory ``api_key`` is NOT hot-reloaded after
            rotation (S24 documented limitation) — the old token remains valid for
            the MCP auth path until the server restarts.
            """
            # Determine grace_seconds: argument wins over config default.
            _grace: int
            if grace_seconds is not None:
                if grace_seconds < 0:
                    return McpErrorResponse(
                        error="grace_seconds must be >= 0", code="validation_error"
                    )
                _grace = grace_seconds
            else:
                _grace = getattr(getattr(config, "auth", None), "rotate_grace_seconds", 0)

            # Serialise the full rotate sequence under a module-level lock to
            # prevent concurrent MCP rotate_key calls from creating orphaned keys.
            async with _mcp_rotate_lock:
                # Guard: if ARCHON_SEARCH_API_KEY is set, the env var overrides
                # .search.env so rotation is silently ineffective (S23 parity).
                if _os.environ.get(_ENV_VAR):
                    return McpErrorResponse(
                        error=(
                            "Cannot rotate: ARCHON_SEARCH_API_KEY env var is set; "
                            "unset it first and restart the server to use managed key rotation."
                        ),
                        code="conflict",
                    )

                # Read the current default key from .search.env (re-read on each call
                # so that a prior REST rotation is visible here without restart).
                current_token, _ = load_or_generate_key()

                # Generate the new raw token here so we can write .search.env FIRST
                # (same crash-safe write order as POST /keys/rotate in routes_keys.py).
                new_raw_token = _secrets.token_hex(32)  # 64 hex chars

                key_file = _get_key_file()
                key_file.parent.mkdir(parents=True, exist_ok=True)
                payload = f"{_ENV_VAR}={new_raw_token}\n".encode()
                try:
                    await _asyncio.to_thread(_atomic_write, key_file, payload, mode=0o600)
                except OSError as exc:
                    return McpErrorResponse(
                        error=f"Failed to write .search.env — rotation aborted: {exc}",
                        code="internal_error",
                    )

                try:
                    result = await key_store.rotate_default_key(
                        current_token=current_token,
                        grace_seconds=_grace,
                        new_token=new_raw_token,
                    )
                except ValueError as exc:
                    return McpErrorResponse(error=str(exc), code="validation_error")

                # Defensive: rotate_default_key must echo back the token we passed in.
                # If this ever diverges, .search.env and keys.json would be out of sync.
                if result["new_token"] != new_raw_token:
                    raise RuntimeError("rotate_default_key returned unexpected token — BUG")

                new_key_id: str = result["new_key_id"]  # type: ignore[assignment]
                old_record = result["old_record"]

                old_key_id_str = old_record.id if old_record is not None else None
                old_key_expires_at = old_record.expires_at if old_record is not None else None
                old_key_status = old_record.status if old_record is not None else None

                logger.info(
                    "mcp rotate_key: new_key_id=%s old_key_id=%s grace_seconds=%d",
                    new_key_id,
                    old_key_id_str,
                    _grace,
                )

                return {
                    "new_key_id": new_key_id,
                    "token": new_raw_token,
                    "status": "active",
                    "old_key_id": old_key_id_str,
                    "old_key_expires_at": str(old_key_expires_at) if old_key_expires_at is not None else None,
                    "old_key_status": old_key_status,
                }

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
    hyde_generator: "HyDEGenerator | None" = None,
    rag_fusion_generator: "RAGFusionGenerator | None" = None,
    key_store: "KeyStore | None" = None,
    doc_id_hasher: Callable[[str], str] | None = None,
) -> Starlette:
    """Return a Starlette HTTP app wrapping the FastMCP server with auth middleware.

    The underlying FastMCP app is exposed via the streamable HTTP transport
    (endpoint: /mcp).  APIKeyMiddleware is added so every request to /mcp
    requires a valid Bearer token; /health remains exempt per _EXEMPT_PATHS.

    ``key_store`` — when provided, managed keys are also checked in addition to
    the legacy ``api_key`` path.  The caller creates and owns the ``KeyStore``
    instance; cross-process visibility is achieved because ``active_keys()``
    re-reads from disk on every call.

    Note: TOML ``[namespaces]`` synthetic records are written to ``keys.json``
    by the HTTP app's lifespan (``create_app``).  The MCP app does not call
    ``load_synthetic_records()`` itself — it relies on the HTTP app having run
    first to populate ``keys.json``.  TOML tokens are also accepted via the
    ``namespaces=config.namespaces`` path (defense-in-depth fallback when the
    ``key_store`` path is absent or unavailable).
    """
    from archon_search.server.middleware_context import RequestContextMiddleware

    fastmcp_app = create_app(pipeline, default_collection, writer=writer, config=config, embedder_cache=embedder_cache, job_store=job_store, hyde_generator=hyde_generator, rag_fusion_generator=rag_fusion_generator, key_store=key_store, doc_id_hasher=doc_id_hasher)
    # FastMCP 3.4.x renamed ``streamable_http_app()`` to ``http_app()``; ``path='/'``
    # exposes the JSON-RPC endpoint at the sub-app root so it is reachable at the
    # ``/mcp`` mount point without an extra suffix (see ADR 09, K-1 spike).
    starlette_app: Starlette = fastmcp_app.http_app(path="/")
    api_key, _ = load_or_generate_key()
    starlette_app.add_middleware(
        APIKeyMiddleware,
        api_key=api_key,
        namespaces=config.namespaces if config is not None else {},
        key_store=key_store,
    )
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

"""FastMCP HTTP server for RAG search."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import AsyncExitStack, ExitStack
from datetime import UTC
from time import monotonic
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypedDict

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from pydantic import Field, ValidationError
from starlette.applications import Starlette
from starlette.datastructures import State
from starlette.requests import Request
from starlette.responses import JSONResponse

from archon_search._path_safety import (
    ALLOWED_ROOTS_REQUIRED,
    ALLOWED_ROOTS_REQUIRED_DETAIL,
    DENIED_LOCATION,
    OUTSIDE_ALLOWED_ROOTS,
    IngestPathPolicy,
    PathUnsafeError,
    validate_archive_file,
    validate_glob_pattern,
    validate_ingest_source,
)
from archon_search._types import IngestTooManyFilesError
from archon_search.config import resolve_active_model
from archon_search.constants import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_CONTEXT_WINDOW_MAX,
    DEFAULT_MAX_CROSS_COLLECTION_COLLECTIONS,
    DEFAULT_NAMESPACE,
)
from archon_search.embedder_cache import (
    EMBEDDER_NOT_READY_CODE,
    EMBEDDER_NOT_READY_DETAIL,
    EmbedderCache,
    EmbedderNotReadyError,
)
from archon_search.filters import SearchFilters
from archon_search.hyde import (
    HYDE_DEPENDENCY_CODE,
    HYDE_DEPENDENCY_DETAIL,
    HyDEDependencyError,
)
from archon_search.ingest_path_policy import ingest_path_policy
from archon_search.jobs.export_archive import (
    ARCHIVE_NAMESPACE_MISMATCH_DETAIL,
    EXPORT_SCHEMA_VERSION,
    INVALID_ARCHIVE_DETAIL,
    ImportArchiveReader,
    exports_dir,
    importable_archive_path,
    manifest_error_detail,
    manifest_namespace_ok,
    quote_archive_value,
)
from archon_search.jobs.model import job_to_dict
from archon_search.key_manager import (
    KEY_STORE_UNREADABLE_DETAIL,
    ROTATE_KEYS_JSON_FAILED_DETAIL,
    ROTATE_WRITE_FAILED_DETAIL,
    BlankCurrentKeyError,
    KeyFileWriteError,
    KeyRotationInputError,
    KeyStore,
    KeyStoreUnreadableError,
    RotationIncompleteError,
    load_or_generate_key,
    resolve_rotation_key_file,
    rotate_and_persist_default_key,
)
from archon_search.model_validation import (
    ModelValidationError,
    validate_embedding_model,
)
from archon_search.observability import bind_stage_recorder
from archon_search.observability import correlation_id as _correlation_id
from archon_search.paths import get_data_dir
from archon_search.pipeline import (
    METADATA_STORE_UNAVAILABLE_CODE,
    METADATA_STORE_UNAVAILABLE_DETAIL,
    CollectionNotFoundError,
    ExplainMultiCollectionNoRerankError,
    ExplainStageError,
    FanoutTimeoutError,
    GraphCommunitiesNotBuiltError,
    MetadataLookupError,
    SearchPipeline,
    context_window_error,
)
from archon_search.progress import IndexingState, IndexingStatus
from archon_search.rag_fusion import (
    RAG_FUSION_DEPENDENCY_CODE,
    RAG_FUSION_DEPENDENCY_DETAIL,
)
from archon_search.router import MultiCollectionRouter
from archon_search.server._hyde import RequestHyDE
from archon_search.server._search_budget import (
    SEARCH_TIMEOUT_SECONDS as _SEARCH_TIMEOUT_SECONDS,
)
from archon_search.server._search_budget import (
    SearchBudgetExceeded,
    run_within_budget,
    warmup_for_search,
)
from archon_search.server._validators import (
    validate_scope_filter as _check_scope_filter,
)
from archon_search.server.authz import (
    FORBIDDEN_CODE,
    AuthIdentity,
    ForbiddenError,
    authorize_key_create,
    authorize_key_listing_namespace,
    caller_may_revoke,
    can_manage_key,
    identity_of,
    require_admin,
)
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
from archon_search.server.middleware_auth import APIKeyMiddleware
from archon_search.server.routes_explain import (
    EXPLAIN_FAILED_DETAIL,
    ExplainRequest,
    ExplainResponse,
    RoutingCandidate,
    RoutingExplain,
)
from archon_search.server.schemas import (
    GRAPH_COMMUNITIES_NOT_BUILT_CODE,
    INVALID_SCOPE_FILTER_CODE,
)
from archon_search.store import CollectionOwnershipError, StoreBusyError
from archon_search.telemetry.entry import FilterFlags, TelemetryEntry
from archon_search.telemetry.writer import TelemetryWriter
from archon_search.types import JobStatus

if TYPE_CHECKING:
    from archon_search.config import SearchConfig
    from archon_search.graph_store import GraphStore
    from archon_search.hyde import HyDEGenerator
    from archon_search.jobs.store import JobStore
    from archon_search.rag_fusion import RAGFusionDependencyError, RAGFusionGenerator

logger = logging.getLogger(__name__)

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

# Context window parameter for the ``search_with_context`` tool.
# Defined at module level so FastMCP can resolve the Annotated type inside closures.
# No ge/le: the bound is enforced by _validate_context_window so out-of-range values
# return code="validation_error" rather than FastMCP's own argument error.
_ContextWindowParam = Annotated[
    int,
    Field(
        description=(
            "Number of neighbour chunks returned on each side of a hit; "
            "must be in [0, [search].context_window_max] (operator setting, "
            f"default {DEFAULT_CONTEXT_WINDOW_MAX}); 0 returns no neighbours."
        ),
        default=DEFAULT_CONTEXT_WINDOW,
    ),
]


class McpErrorResponse(TypedDict):
    error: str
    code: str


_ERR_SCHEMA = "schema_validation_error"
# Wire-safe error pair for an unexpected search / search_with_context failure. Never
# str(exc): store and model errors embed internals (e.g. LanceDB's vector-dimension
# message); the full exception is logged server-side by logger.exception.
_SEARCH_FAILED_DETAIL = "search failed"
_SEARCH_FAILED_CODE = "internal_error"
# Same for search / search_with_context Pydantic failures: a ValidationError's text
# embeds the rejected input values, so neither the filter nor the response-schema
# failure is echoed (mirrors explain's "invalid explain request").
_INVALID_SEARCH_FILTERS_DETAIL = "invalid search filters"
_INVALID_SEARCH_RESPONSE_DETAIL = "search response failed schema validation"
# Wire-safe error code for an unexpected explain failure (logged by logger.exception); its
# detail is /explain's EXPLAIN_FAILED_DETAIL.
_EXPLAIN_FAILED_CODE = "internal_error"
# explain's response-schema failure, for the same reason as search's above.
_INVALID_EXPLAIN_RESPONSE_DETAIL = "explain response failed schema validation"
_VALID_GRAPH_MODES: tuple[None | str, ...] = (None, "naive", "local", "global", "ppr")
# E2a — ingest TTL and scope validation limits (same constraints as routes_jobs.py IngestRequest validators)
_INT32_MAX: int = 2**31 - 1
_MAX_SCOPE_LIST_ITEMS: int = 100
_MAX_SCOPE_ITEM_LEN: int = 255


def _enqueue_internal_error(writer: TelemetryWriter | None, endpoint: str, start: float) -> None:
    """Enqueue the ``internal_error`` telemetry entry for a failed *endpoint* call begun at *start*."""
    if writer is None:
        return
    try:
        writer.enqueue(
            TelemetryEntry.from_error(
                endpoint=endpoint,
                status="internal_error",
                error_kind="other",
                latency_ms=(monotonic() - start) * 1000.0,
                correlation_id=_correlation_id.get(),
            )
        )
    except Exception:
        logger.warning("telemetry: %s error entry enqueue failed", endpoint, exc_info=True)


def _dependency_error_response(
    tool: str, exc: HyDEDependencyError | RAGFusionDependencyError
) -> McpErrorResponse:
    """Answer a missing HyDE or RAG Fusion package with its fixed wire pair.

    The two branches are exhaustive over *exc*'s type.  The RAG Fusion one is not
    ``isinstance``-checked: callers catch the class ``create_app`` bound lazily, which a
    reloaded ``archon_search.rag_fusion`` no longer matches by identity.  The message
    carries this interpreter's install command, so it is logged, never sent.
    """
    if isinstance(exc, HyDEDependencyError):
        logger.warning("%s: HyDE unavailable — %s", tool, exc)
        return McpErrorResponse(error=HYDE_DEPENDENCY_DETAIL, code=HYDE_DEPENDENCY_CODE)
    logger.warning("%s: RAG Fusion unavailable — %s", tool, exc)
    return McpErrorResponse(error=RAG_FUSION_DEPENDENCY_DETAIL, code=RAG_FUSION_DEPENDENCY_CODE)


def _validate_scope_filter(scope_filter: str | None) -> McpErrorResponse | None:
    """Validate scope_filter for MCP tools; wraps the shared validator into a McpErrorResponse."""
    msg = _check_scope_filter(scope_filter)
    if msg is None:
        return None
    return McpErrorResponse(error=msg, code=INVALID_SCOPE_FILTER_CODE)


_ERR_INTERNAL = "internal_error"
_ERR_VALIDATION = "validation_error"
# ``code`` of the refusal of a write to a collection name another namespace owns (the ``error`` is the store's fixed
# COLLECTION_UNAVAILABLE_DETAIL, which names no owner).
COLLECTION_UNAVAILABLE_CODE = "collection_unavailable"
# Sanitized wire text for the other tools' caught exceptions whose message may embed
# internals (store paths, model paths, pydantic ``input_value`` of server-built data).
# The full exception goes to the server log only — CLAUDE.md hard invariant.
INTERNAL_ERROR_DETAIL = "internal error; see the server log for details"
SCHEMA_VALIDATION_DETAIL = "response schema validation failed; see the server log for details"
CREATE_KEY_FAILED_DETAIL = "key creation failed; see the server log for details"
JOB_STORE_NOT_CONFIGURED_DETAIL = "job store not configured"


def _internal_error(log_message: str) -> McpErrorResponse:
    """Log the exception being handled (with traceback) and return the sanitized error."""
    logger.exception(log_message)
    return McpErrorResponse(error=INTERNAL_ERROR_DETAIL, code=_ERR_INTERNAL)


def _schema_error(tool: str) -> McpErrorResponse:
    """Log the response-schema ValidationError being handled and return the sanitized error."""
    logger.exception("%s: response schema validation failed", tool)
    return McpErrorResponse(error=SCHEMA_VALIDATION_DETAIL, code=_ERR_SCHEMA)


def _validate_context_window(context_window: int, context_window_max: int) -> McpErrorResponse | None:
    """Validate search_with_context's context_window; wraps the shared pipeline check into a McpErrorResponse.

    Checked before the pipeline call, so MCP clients get code="validation_error"
    instead of the catch-all internal_error for the ValueError the pipeline raises.
    """
    msg = context_window_error(context_window, context_window_max)
    if msg is None:
        return None
    return McpErrorResponse(error=msg, code="validation_error")


def _validate_ttl_and_scopes(
    chunk_ttl_seconds: int | None,
    chunk_scopes: list[str] | None,
) -> McpErrorResponse | None:
    """Validate chunk_ttl_seconds and chunk_scopes for MCP ingest tools.

    Returns a McpErrorResponse dict if validation fails, None if valid.
    Mirrors the validation in routes_jobs.py IngestRequest validators.
    """
    if chunk_ttl_seconds is not None and not 1 <= chunk_ttl_seconds <= _INT32_MAX:
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
    DENIED_LOCATION: "path is unsafe: this location is never ingested (server state, credentials, system files)",
    OUTSIDE_ALLOWED_ROOTS: "path is unsafe: the path is outside the directories this server is allowed to ingest from",
    ALLOWED_ROOTS_REQUIRED: ALLOWED_ROOTS_REQUIRED_DETAIL,
    "glob_empty": "glob_pattern is unsafe: the pattern is empty — use a relative glob such as '**/*'",
    "glob_too_long": "glob_pattern is unsafe: the pattern is too long",
    "glob_absolute": "glob_pattern is unsafe: the pattern is absolute — use a pattern relative to the directory",
    "glob_too_recursive": "glob_pattern is unsafe: the pattern has too many '**' segments",
}


def _path_unsafe_message(reason: str) -> str:
    """Map a PathUnsafeError reason code to an LLM-readable rejection phrase."""
    return _PATH_UNSAFE_MESSAGES.get(reason, f"path is unsafe: {reason}")



NO_AUTH_CONTEXT_DETAIL = "unauthenticated: this tool call carries no authenticated namespace"


def _get_request_identity() -> AuthIdentity:
    """Return the identity ``APIKeyMiddleware`` resolved for the current MCP request, failing closed.

    FastMCP's RequestContextMiddleware stores the current Starlette Request in the
    HTTP context. APIKeyMiddleware writes ``request.state.namespace`` / ``request.state.admin`` before calling
    next. This helper reads them so every tool closure can use the authenticated
    namespace instead of hardcoding DEFAULT_NAMESPACE.

    Raises:
        ToolError: there is no HTTP request (a tool invoked outside the authenticated transport) or the
            request carries no namespace. The call is refused — never served as the default namespace. The
            message is fixed and reaches the client (FastMCP does not mask ``ToolError``).

    Per ADR 09 (K-1 spike): the current HTTP request is set fresh on every HTTP POST
    (not frozen at session-initialize time), so this must be called on each tool
    invocation, not cached at app-creation time. ADR 17 records the switch to the
    public get_http_request() below.

    Uses fastmcp.server.dependencies.get_http_request() (public API) which handles
    the MCP SDK request_ctx, FastMCP's HTTP context, and Docket worker snapshots.
    """
    # get_http_request() raises RuntimeError when no HTTP request is active.
    try:
        req = get_http_request()
    except RuntimeError as exc:
        raise ToolError(NO_AUTH_CONTEXT_DETAIL) from exc
    identity = identity_of(req)
    if identity is None:
        raise ToolError(NO_AUTH_CONTEXT_DETAIL)
    return identity


def _get_request_namespace() -> str:
    """Return the authenticated namespace of the current MCP request (see :func:`_get_request_identity`)."""
    return _get_request_identity().namespace


def _forbidden(exc: ForbiddenError) -> McpErrorResponse:
    """Answer a refused key/control tool call with the fixed authorization detail."""
    return McpErrorResponse(error=str(exc), code=FORBIDDEN_CODE)


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
    if meta is None:
        meta = await pipeline.get_collection_meta(collection, namespace=namespace)
    active_model = resolve_active_model(meta, config)
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
    hyde_generator: HyDEGenerator | None = None,
    rag_fusion_generator: RAGFusionGenerator | None = None,
    key_store: KeyStore | None = None,
    doc_id_hasher: Callable[[str], str] | None = None,
    graph_store: GraphStore | None = None,
    api_key_state: State | None = None,
    ingest_policy: IngestPathPolicy | None = None,
) -> FastMCP:
    """Create a FastMCP app with 16 RAG tools + up to 4 optional key-management tools.

    ``config`` is required only for the collectionless ``explain`` routing path;
    when omitted, ``explain`` without a collection falls back to
    ``default_collection`` (mirroring the ``search`` tool).

    ``job_store`` is required for the ``update_collection`` tool's 409 guard
    (active reindex check).

    ``key_store`` — when provided, the four key-management tools (``create_key``,
    ``list_keys``, ``revoke_key``, ``rotate_key``) are registered and can
    create/list/revoke/rotate managed API keys stored in ``keys.json``.

    ``api_key_state`` — the object whose ``api_key`` attribute holds the server's current
    default key (the REST app's ``app.state`` when mounted). ``rotate_key`` reads the key to
    retire from it and publishes the new key to it, exactly like ``POST /keys/rotate``. When
    omitted, ``rotate_key`` re-reads ``.search.env`` and publishes nothing.
    """
    # Late-bound import: archon_search.rag_fusion may be reloaded in tests,
    # so we import lazily here (inside create_app) so the closures below always
    # capture the current class from sys.modules at app-creation time.
    from archon_search.rag_fusion import RAGFusionDependencyError

    # An ordinary exception escaping a tool reaches the client as a bare "Error calling
    # tool <name>" instead of "<name>: <exception text>". Not a full safety net: FastMCP
    # re-raises a pydantic ValidationError and any FastMCPError (ToolError, ...) unmasked,
    # and the MCP server then sends str(exc) — so the tools' own handlers still catch
    # their response-schema ValidationErrors and return the coded McpErrorResponse.
    app = FastMCP("archon-search", mask_error_details=True)
    # What the ingest tools may read: the deny list is always on, ``[ingest].allowed_roots`` scopes the paths a client
    # names. Without a config (a bare test harness) only the name-based deny list applies.
    path_policy = (
        ingest_policy
        if ingest_policy is not None
        else ingest_path_policy(config) if config is not None else IngestPathPolicy()
    )

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
        scope_filter: str | None = None,
    ) -> dict[str, Any]:
        """Search for relevant document chunks using hybrid vector + FTS search.

        ``graph_mode`` controls graph-aware retrieval:
        - ``"naive"`` — entity n-gram expansion (E1a).
        - ``"local"`` — community-scoped retrieval for the matched community (E1b).
        - ``"global"`` — corpus-wide synthesis over all community representatives (E1b).
        Requires ``[graph] enabled = true`` in the server config.

        ``scope_filter`` restricts results to chunks whose scope tags match:
        - Exact match: ``scope_filter="user:alice"`` returns only chunks tagged with
          ``"user:alice"`` in their scopes (plus unscoped chunks).
        - Prefix wildcard: ``scope_filter="user:alice*"`` returns chunks whose scope
          starts with ``"user:alice"`` (plus unscoped chunks).
        - Mutually exclusive with ``graph_mode``.

        Invalid forms (bare ``*``, leading ``*``, mid-string ``*``, multiple ``*``,
        or empty string) return ``code="invalid_scope_filter"``.
        """
        ns = _get_request_namespace()
        timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)
        start = monotonic()

        # graph_mode validation — must be None, "naive", "local", or "global".
        if graph_mode not in _VALID_GRAPH_MODES:
            return McpErrorResponse(error=f"graph_mode must be one of {[m for m in _VALID_GRAPH_MODES if m is not None]!r}", code="invalid_graph_mode")
        _graph_config = getattr(config, "graph", None)
        _graph_enabled = getattr(_graph_config, "enabled", False) if _graph_config is not None else False
        if graph_mode is not None and not _graph_enabled:
            return McpErrorResponse(error="graph_mode requires [graph] enabled=true", code="graph_disabled")

        # scope_filter syntax validation
        _sf_err = _validate_scope_filter(scope_filter)
        if _sf_err is not None:
            return _sf_err
        # scope_filter and graph_mode are mutually exclusive
        if scope_filter is not None and graph_mode is not None:
            return McpErrorResponse(
                error="scope_filter is not supported with graph_mode; use scope_filter without graph_mode or use graph_mode without scope_filter",
                code="scope_filter_graph_mode_incompatible",
            )

        _rf_config = getattr(config, "rag_fusion", None)
        if _rf_config is None:
            from archon_search.config import RAGFusionConfig
            _rf_config = RAGFusionConfig()

        _request_hyde = RequestHyDE.for_request(
            query,
            requested=hyde,
            rag_fusion=rag_fusion,
            rag_fusion_enabled=_rf_config.enabled,
            graph_mode=graph_mode,
            generator=hyde_generator,
            config=getattr(config, "hyde", None),
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )

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
                from archon_search.config import SearchConfig as _SearchConfig
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
                except ValidationError:
                    return McpErrorResponse(error=_INVALID_SEARCH_FILTERS_DETAIL, code="validation_error")
            else:
                _multi_filters = None
            # Same warm-up as the REST fan-out route: the global embedder (which
            # search_many embeds with) and the shared reranker, both bounded, so no
            # cold model build can block this tool call for its whole duration — it
            # degrades to the fused ranking (S288) or the FTS leg alone (S290).
            _warmup = await warmup_for_search(pipeline)
            try:
                # search_many embeds with the global embedder and excludes every
                # collection pinned to another model (embedding_model_mismatch), so
                # HyDE embeds with it too.
                hyde_outcome = await _request_hyde.resolve(
                    pipeline.global_embedder, embedder_warm=not _warmup.fts_only
                )
                # search_many's own _fanout_timeout_seconds bounds the per-collection
                # leg gathers only — the meta lookup, embed_one, graph-store calls and
                # the global rerank pass sit outside it, so bound the whole call.
                result_obj = await run_within_budget(
                    pipeline.search_many(
                        query, deduped, namespace=ns, query_vector=hyde_outcome.vector,
                        rag_fusion=rag_fusion,
                        rag_fusion_generator=rag_fusion_generator,
                        rag_fusion_config=_rf_config,
                        filters=_multi_filters,
                        graph_mode=graph_mode,
                        scope_filter=scope_filter,
                        rerank=_warmup.rerank,
                        fts_only=_warmup.fts_only,
                    ),
                    timeout=_SEARCH_TIMEOUT_SECONDS,
                )
            except (HyDEDependencyError, RAGFusionDependencyError) as exc:
                return _dependency_error_response("search", exc)
            except GraphCommunitiesNotBuiltError as exc:
                return McpErrorResponse(error=str(exc), code=GRAPH_COMMUNITIES_NOT_BUILT_CODE)
            except CollectionNotFoundError:
                return McpErrorResponse(error="collection not found", code="not_found")
            except (FanoutTimeoutError, SearchBudgetExceeded):
                logger.error(
                    "search pipeline timed out",
                    extra={"event_type": "search_timeout", "collections": list(deduped)},
                )
                return McpErrorResponse(error="search timed out", code="timeout")
            except MetadataLookupError:
                # Transient infrastructure error (store unavailable); mirror REST's
                # 503 semantics with a clean message rather than leaking the cause.
                logger.exception("multi-collection search: metadata lookup failed")
                return McpErrorResponse(error=METADATA_STORE_UNAVAILABLE_DETAIL, code=METADATA_STORE_UNAVAILABLE_CODE)
            except Exception:
                _enqueue_internal_error(writer, "search", start)
                logger.exception("multi-collection search failed")
                return McpErrorResponse(error=_SEARCH_FAILED_DETAIL, code=_SEARCH_FAILED_CODE)
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
                _multi_expansion_warning = hyde_outcome.expansion_warning or result_obj.rag_fusion_warning
                _multi_graph_expansion_applied = result_obj.graph_expansion_applied
                response = McpSearchResponse(
                    results=result_schemas,
                    acl_filtered=result_obj.acl_filtered,
                    excluded_collections=[
                        ExcludedCollectionMcpSchema(name=e.name, reason=e.reason)
                        for e in result_obj.excluded_collections
                    ],
                    hyde_applied=hyde_outcome.applied,
                    expansion_used=hyde_outcome.applied or result_obj.rag_fusion_applied or _multi_graph_expansion_applied,
                    expansion_warning=_multi_expansion_warning,
                    graph_expansion_applied=_multi_graph_expansion_applied,
                    ppr_entities_matched=result_obj.ppr_entities_matched,
                )
                return response.model_dump(mode="json")
            except ValidationError:
                # The ValidationError text embeds the rejected values: log it, never send it.
                logger.exception("search: response schema validation failed")
                return McpErrorResponse(error=_INVALID_SEARCH_RESPONSE_DETAIL, code=_ERR_SCHEMA)

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
            except ValidationError:
                return McpErrorResponse(error=_INVALID_SEARCH_FILTERS_DETAIL, code="validation_error")
            _col = collection or default_collection
            # Namespace gate: collection must exist in the caller's namespace.
            # Mirrors the REST search route's get_collection_meta check (routes_search.py)
            # which returns 404 when the collection is owned by a different namespace.
            _col_meta = await pipeline.get_collection_meta(_col, namespace=ns)
            if _col_meta is None:
                return McpErrorResponse(error=f"collection {_col!r} not found", code="not_found")
            _search_embedder = await _resolve_embedder(pipeline, embedder_cache, _col, config, namespace=ns, meta=_col_meta)
            # Outside the budget below by design (S184/S288/S290): a cold cross-encoder
            # built inside rerank_candidates — or a cold embedder built inside
            # embed_one — would park this tool call for the whole model build.
            # rerank=False degrades to the fused vector+FTS ranking, fts_only=True to
            # the FTS leg alone.
            _warmup = await warmup_for_search(pipeline, _search_embedder)
            # Resolved only now that the collection's embedder is known, so the
            # hypothesis lands in that collection's vector space.
            hyde_outcome = await _request_hyde.resolve(_search_embedder, embedder_warm=not _warmup.fts_only)
            with ExitStack() as stack:
                recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
                t0 = time.perf_counter()
                result_obj = await run_within_budget(
                    pipeline.search(
                        query, _col, ns, embedder=_search_embedder, filters=filters, query_vector=hyde_outcome.vector,
                        rag_fusion=rag_fusion,
                        rag_fusion_generator=rag_fusion_generator,
                        rag_fusion_config=_rf_config,
                        graph_mode=graph_mode,
                        scope_filter=scope_filter,
                        rerank=_warmup.rerank,
                        fts_only=_warmup.fts_only,
                    ),
                    timeout=_SEARCH_TIMEOUT_SECONDS,
                )
                if recorder is not None:
                    recorder.record_total(t0)
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
                _single_expansion_warning = hyde_outcome.expansion_warning or result_obj.rag_fusion_warning
                _single_graph_expansion_applied = result_obj.graph_expansion_applied
                response = McpSearchResponse(
                    results=result_schemas,
                    acl_filtered=result_obj.acl_filtered,
                    excluded_collections=[
                        ExcludedCollectionMcpSchema(name=e.name, reason=e.reason)
                        for e in result_obj.excluded_collections
                    ],
                    hyde_applied=hyde_outcome.applied,
                    expansion_used=hyde_outcome.applied or result_obj.rag_fusion_applied or _single_graph_expansion_applied,
                    expansion_warning=_single_expansion_warning,
                    graph_expansion_applied=_single_graph_expansion_applied,
                    ppr_entities_matched=result_obj.ppr_entities_matched,
                )
                return response.model_dump(mode="json")
            except ValidationError:
                # The ValidationError text embeds the rejected values: log it, never send it.
                logger.exception("search: response schema validation failed")
                return McpErrorResponse(error=_INVALID_SEARCH_RESPONSE_DETAIL, code=_ERR_SCHEMA)
        except (HyDEDependencyError, RAGFusionDependencyError) as exc:
            return _dependency_error_response("search", exc)
        except GraphCommunitiesNotBuiltError as exc:
            return McpErrorResponse(error=str(exc), code=GRAPH_COMMUNITIES_NOT_BUILT_CODE)
        except EmbedderNotReadyError as exc:
            # The model is still loading (or its load is wedged) — retryable.
            logger.warning("search: embedder not ready — %s", exc)
            return McpErrorResponse(error=EMBEDDER_NOT_READY_DETAIL, code=EMBEDDER_NOT_READY_CODE)
        except SearchBudgetExceeded:
            # Not asyncio.TimeoutError: since 3.11 that is the builtin TimeoutError (an
            # OSError), so catching it here would also swallow a socket ETIMEDOUT from
            # the store and report it as a timeout instead of an internal error.
            logger.error(
                "search pipeline timed out",
                extra={"event_type": "search_timeout", "collection": _col},
            )
            return McpErrorResponse(error="search timed out", code="timeout")
        except Exception:
            _enqueue_internal_error(writer, "search", start)
            logger.exception("search failed")
            return McpErrorResponse(error=_SEARCH_FAILED_DETAIL, code=_SEARCH_FAILED_CODE)

    @app.tool()
    async def search_with_context(
        query: str,
        collection: str | None = None,
        context_window: _ContextWindowParam = DEFAULT_CONTEXT_WINDOW,
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
        scope_filter: str | None = None,
    ) -> dict[str, Any]:
        """Search and return surrounding chunks for richer context.

        Returns ``{"results": [...], "hyde_applied": bool, "expansion_used": bool,
        "expansion_warning": str | null}``.

        ``scope_filter`` restricts results to chunks whose scope tags match:
        - Exact match: ``scope_filter="user:alice"`` returns only chunks tagged with
          ``"user:alice"`` in their scopes (plus unscoped chunks).
        - Prefix wildcard: ``scope_filter="user:alice*"`` returns chunks whose scope
          starts with ``"user:alice"`` (plus unscoped chunks).

        Invalid forms (bare ``*``, leading ``*``, mid-string ``*``, multiple ``*``,
        or empty string) return ``code="invalid_scope_filter"``.
        ``context_window`` is the number of neighbour chunks returned on each side of
        a hit; it must be in ``[0, [search].context_window_max]`` (operator setting,
        default 10), otherwise ``code="validation_error"``.
        Note: ``graph_mode`` is not supported on this tool and is rejected unconditionally.
        """
        # graph_mode on search_with_context is not supported; use the search tool instead.
        if graph_mode is not None:
            return McpErrorResponse(
                error="graph_mode (naive, local, global, ppr) on search_with_context is not supported; use the search tool instead",
                code="graph_mode_not_supported",
            )

        # scope_filter syntax validation
        _swc_sf_err = _validate_scope_filter(scope_filter)
        if _swc_sf_err is not None:
            return _swc_sf_err

        _swc_cw_err = _validate_context_window(context_window, pipeline.context_window_max)
        if _swc_cw_err is not None:
            return _swc_cw_err

        _swc_ns = _get_request_namespace()
        timings_enabled: bool = getattr(getattr(config, "observability", None), "stage_timings_enabled", False)
        start = monotonic()

        _swc_rf_config = getattr(config, "rag_fusion", None)
        if _swc_rf_config is None:
            from archon_search.config import RAGFusionConfig
            _swc_rf_config = RAGFusionConfig()

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
            except ValidationError:
                return McpErrorResponse(error=_INVALID_SEARCH_FILTERS_DETAIL, code="validation_error")
            _swc_col = collection or default_collection
            # Namespace gate: collection must exist in the caller's namespace.
            _swc_meta = await pipeline.get_collection_meta(_swc_col, namespace=_swc_ns)
            if _swc_meta is None:
                return McpErrorResponse(error=f"collection {_swc_col!r} not found", code="not_found")
            _swc_embedder = await _resolve_embedder(pipeline, embedder_cache, _swc_col, config, namespace=_swc_ns, meta=_swc_meta)
            # Resolved only now that the collection's embedder is known, so the
            # hypothesis lands in that collection's vector space. search_with_context
            # has no FTS-only degrade: its retrieval embeds on this model however cold,
            # so HyDE embeds too (embedder_warm=True) and the build is waited on once,
            # by whichever embeds first — never skipped for nothing (C2-I-1).
            swc_hyde = await RequestHyDE.for_request(
                query,
                requested=hyde,
                rag_fusion=rag_fusion,
                rag_fusion_enabled=_swc_rf_config.enabled,
                # graph_mode was rejected above, so only RAG Fusion can win here.
                graph_mode=graph_mode,
                generator=hyde_generator,
                config=getattr(config, "hyde", None),
                timeout=_SEARCH_TIMEOUT_SECONDS,
            ).resolve(_swc_embedder, embedder_warm=True)
            with ExitStack() as stack:
                recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
                t0 = time.perf_counter()
                swc_result = await pipeline.search_with_context(
                    query, _swc_col, context_window, namespace=_swc_ns, embedder=_swc_embedder, filters=filters,
                    query_vector=swc_hyde.vector,
                    rag_fusion=rag_fusion,
                    rag_fusion_generator=rag_fusion_generator,
                    rag_fusion_config=_swc_rf_config,
                    scope_filter=scope_filter,
                )
                if recorder is not None:
                    recorder.record_total(t0)
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
                _swc_expansion_warning = swc_hyde.expansion_warning or _swc_pipeline_result.rag_fusion_warning
                _swc_graph_expansion_applied = _swc_pipeline_result.graph_expansion_applied
                response = SearchWithContextResponse(
                    results=items,
                    hyde_applied=swc_hyde.applied,
                    expansion_used=swc_hyde.applied or _swc_pipeline_result.rag_fusion_applied or _swc_graph_expansion_applied,
                    expansion_warning=_swc_expansion_warning,
                )
                return response.model_dump(mode="json")
            except ValidationError:
                # The ValidationError text embeds the rejected values: log it, never send it.
                logger.exception("search_with_context: response schema validation failed")
                return McpErrorResponse(error=_INVALID_SEARCH_RESPONSE_DETAIL, code=_ERR_SCHEMA)
        except (HyDEDependencyError, RAGFusionDependencyError) as exc:
            return _dependency_error_response("search_with_context", exc)
        except EmbedderNotReadyError as exc:
            # The model is still loading (or its load is wedged) — retryable.
            logger.warning("search_with_context: embedder not ready — %s", exc)
            return McpErrorResponse(error=EMBEDDER_NOT_READY_DETAIL, code=EMBEDDER_NOT_READY_CODE)
        except Exception:
            _enqueue_internal_error(writer, "search_with_context", start)
            logger.exception("search_with_context failed")
            return McpErrorResponse(error=_SEARCH_FAILED_DETAIL, code=_SEARCH_FAILED_CODE)

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
        scope_filter: str | None = None,
    ) -> dict[str, Any]:
        """Return the per-stage retrieval/reranking trace for a query, plus the
        routing decision when no collection is pinned. Operates in the caller's
        authenticated namespace. The query is never echoed in the response or telemetry.

        ``scope_filter`` restricts results to chunks whose scope tags match:
        - Exact match: ``scope_filter="user:alice"`` returns only chunks tagged with
          ``"user:alice"`` in their scopes (plus unscoped chunks).
        - Prefix wildcard: ``scope_filter="user:alice*"`` returns chunks whose scope
          starts with ``"user:alice"`` (plus unscoped chunks).
        - Mutually exclusive with ``graph_mode``.

        Invalid forms (bare ``*``, leading ``*``, mid-string ``*``, multiple ``*``,
        or empty string) return ``code="invalid_scope_filter"``.
        """
        _explain_ns = _get_request_namespace()
        start = monotonic()

        if config is not None:
            _explain_cfg = config
        else:
            from archon_search.config import SearchConfig as _SearchConfig
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
                error=f"graph_mode must be one of {[m for m in _VALID_GRAPH_MODES if m is not None]!r}",
                code="invalid_graph_mode",
            )
        _explain_graph_config = getattr(config, "graph", None)
        _explain_graph_enabled = (
            getattr(_explain_graph_config, "enabled", False) if _explain_graph_config is not None else False
        )
        if graph_mode is not None and not _explain_graph_enabled:
            return McpErrorResponse(error="graph_mode requires [graph] enabled=true", code="graph_disabled")

        # scope_filter syntax validation
        _explain_sf_err = _validate_scope_filter(scope_filter)
        if _explain_sf_err is not None:
            return _explain_sf_err
        # scope_filter and graph_mode are mutually exclusive
        if scope_filter is not None and graph_mode is not None:
            return McpErrorResponse(
                error="scope_filter is not supported with graph_mode; use scope_filter without graph_mode or use graph_mode without scope_filter",
                code="scope_filter_graph_mode_incompatible",
            )

        _explain_rf_config = getattr(config, "rag_fusion", None)
        if _explain_rf_config is None:
            from archon_search.config import RAGFusionConfig
            _explain_rf_config = RAGFusionConfig()

        _explain_hyde = RequestHyDE.for_request(
            query,
            requested=hyde,
            rag_fusion=rag_fusion,
            rag_fusion_enabled=_explain_rf_config.enabled,
            graph_mode=graph_mode,
            generator=hyde_generator,
            config=getattr(config, "hyde", None),
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )

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
                # pipeline.explain fans out with the global embedder and excludes every
                # collection pinned to another model (embedding_model_mismatch), so HyDE
                # embeds with it too. pipeline.explain has no FTS-only degrade: its
                # retrieval embeds on the serving model however cold, so HyDE embeds too
                # (embedder_warm=True) and the build is waited on once, by whichever
                # embeds first (C2-I-1).
                explain_hyde_outcome = await _explain_hyde.resolve(pipeline.global_embedder, embedder_warm=True)
                result = await pipeline.explain(
                    query, collections=deduped, top_k=top_k, rerank=rerank, namespace=_explain_ns,
                    query_vector=explain_hyde_outcome.vector,
                    rag_fusion=rag_fusion,
                    rag_fusion_generator=rag_fusion_generator,
                    rag_fusion_config=_explain_rf_config,
                    scope_filter=scope_filter,
                )
            except (HyDEDependencyError, RAGFusionDependencyError) as exc:
                return _dependency_error_response("explain", exc)
            except GraphCommunitiesNotBuiltError as exc:
                return McpErrorResponse(error=str(exc), code=GRAPH_COMMUNITIES_NOT_BUILT_CODE)
            except CollectionNotFoundError:
                return McpErrorResponse(error="collection not found", code="not_found")
            except FanoutTimeoutError:
                return McpErrorResponse(error="search timed out", code="timeout")
            except MetadataLookupError:
                logger.exception("multi-collection explain: metadata lookup failed")
                return McpErrorResponse(error=METADATA_STORE_UNAVAILABLE_DETAIL, code=METADATA_STORE_UNAVAILABLE_CODE)
            except ExplainMultiCollectionNoRerankError as exc:
                return McpErrorResponse(error=str(exc), code="validation_error")
            except ExplainStageError as exc:
                _enqueue_internal_error(writer, "explain", start)
                logger.warning("explain stage %s failed: %s", exc.stage, exc.original, exc_info=exc.original)
                return McpErrorResponse(
                    error=f"{exc.stage} error: {type(exc.original).__name__}", code="internal_error"
                )
            except Exception:
                _enqueue_internal_error(writer, "explain", start)
                logger.exception("multi-collection explain failed")
                return McpErrorResponse(error=EXPLAIN_FAILED_DETAIL, code=_EXPLAIN_FAILED_CODE)
            try:
                response = ExplainResponse.from_pipeline_result(
                    rerank=rerank, collection="", routing=None, result=result, stage_timings_ms=None,
                    hyde_applied=explain_hyde_outcome.applied,
                    rag_fusion_applied=result.rag_fusion_applied,
                    rag_fusion_queries_used=result.rag_fusion_queries_used,
                    rag_fusion_attempted=result.rag_fusion_attempted,
                    rag_fusion_failure_reason=result.rag_fusion_failure_reason,
                    rag_fusion_sub_query_results=result.rag_fusion_sub_query_results,
                    graph_mode_applied=result.graph_mode_applied,
                    ppr_entities_matched=result.ppr_entities_matched,
                )
                result_dict = response.model_dump(mode="json", exclude_none=False)
                result_dict.pop("stage_timings_ms", None)
                return result_dict
            except ValidationError:
                # The ValidationError text embeds the rejected values: log it, never send it.
                logger.exception("explain: response schema validation failed")
                return McpErrorResponse(error=_INVALID_EXPLAIN_RESPONSE_DETAIL, code=_ERR_SCHEMA)

        try:
            req = ExplainRequest(query=query, collection=collection, top_k=top_k, rerank=rerank)
        except ValidationError:
            # Do not echo str(exc): a query-field validation failure embeds the
            # rejected input value. The code conveys the category; the query is
            # never reflected back.
            return McpErrorResponse(error="invalid explain request", code="validation_error")

        ns = _explain_ns
        routing: RoutingExplain | None = None
        routing_vector: list[float] | None = None
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
                    _explain_active_model = resolve_active_model(meta, config)
                elif config is None:
                    # No routing config — fall back to the default collection (like search).
                    chosen = default_collection
                else:
                    all_meta = await pipeline.get_all_collections_meta(namespace=ns)
                    if not all_meta:
                        return McpErrorResponse(error="no collections available", code="not_found")
                    # Route on the plain query embedding, as REST /explain does: the
                    # router's centroids live in the global model's space.
                    routing_vector = await pipeline.global_embedder.embed_one(req.query)
                    col_router = MultiCollectionRouter(
                        search_url="http://mcp",
                        embedder=pipeline.global_embedder,
                        shortlist_size=config.routing_shortlist_size,
                        confidence_threshold=config.routing_confidence_threshold,
                        embedding_model=config.embedding_model,
                    )
                    ranked = col_router.rank_with_scores(routing_vector, all_meta)
                    chosen_meta, chosen_score = ranked[0]
                    chosen = chosen_meta.name
                    _explain_active_model = resolve_active_model(chosen_meta, config)
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
                # Resolved only now that the chosen collection's embedder is known, so
                # the hypothesis lands in that collection's vector space. embedder_warm=True:
                # no FTS-only degrade here either (see the fan-out branch above).
                explain_hyde_outcome = await _explain_hyde.resolve(_explain_embedder, embedder_warm=True)
                query_vector = explain_hyde_outcome.vector
                # Without a HyDE vector, retrieval would re-embed the plain query: reuse
                # the routing vector instead when the chosen collection is on the model
                # it was embedded with. graph_mode retrieval never takes a query vector.
                # (REST /explain does not reuse it — its retrieval re-embeds the query.)
                if (
                    query_vector is None
                    and routing_vector is not None
                    and graph_mode is None
                    and _explain_active_model == config.embedding_model
                ):
                    query_vector = routing_vector
                result = await pipeline.explain(
                    req.query, chosen, top_k=req.top_k, rerank=req.rerank, namespace=ns,
                    query_vector=query_vector, embedder=_explain_embedder,
                    rag_fusion=rag_fusion,
                    rag_fusion_generator=rag_fusion_generator,
                    rag_fusion_config=_explain_rf_config,
                    graph_mode=graph_mode,
                    scope_filter=scope_filter,
                )
                if recorder is not None:
                    recorder.record_total(t0)
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
                    stage_timings_ms=stage_timings, hyde_applied=explain_hyde_outcome.applied,
                    rag_fusion_applied=result.rag_fusion_applied,
                    rag_fusion_queries_used=result.rag_fusion_queries_used,
                    rag_fusion_attempted=result.rag_fusion_attempted,
                    rag_fusion_failure_reason=result.rag_fusion_failure_reason,
                    rag_fusion_sub_query_results=result.rag_fusion_sub_query_results,
                    graph_mode_applied=result.graph_mode_applied,
                    ppr_entities_matched=result.ppr_entities_matched,
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
            except ValidationError:
                # The ValidationError text embeds the rejected values: log it, never send it.
                logger.exception("explain: response schema validation failed")
                return McpErrorResponse(error=_INVALID_EXPLAIN_RESPONSE_DETAIL, code=_ERR_SCHEMA)
        except (HyDEDependencyError, RAGFusionDependencyError) as exc:
            return _dependency_error_response("explain", exc)
        except GraphCommunitiesNotBuiltError as exc:
            return McpErrorResponse(error=str(exc), code=GRAPH_COMMUNITIES_NOT_BUILT_CODE)
        except ExplainStageError as exc:
            _enqueue_internal_error(writer, "explain", start)
            # Sanitize: the original message could echo the query (e.g. an FTS error).
            logger.warning("explain stage %s failed: %s", exc.stage, exc.original, exc_info=exc.original)
            return McpErrorResponse(error=f"{exc.stage} error: {type(exc.original).__name__}", code="internal_error")
        except EmbedderNotReadyError as exc:
            # The model is still loading (or its load is wedged) — retryable.
            logger.warning("explain: embedder not ready — %s", exc)
            return McpErrorResponse(error=EMBEDDER_NOT_READY_DETAIL, code=EMBEDDER_NOT_READY_CODE)
        except Exception:
            _enqueue_internal_error(writer, "explain", start)
            logger.exception("explain failed")
            return McpErrorResponse(error=EXPLAIN_FAILED_DETAIL, code=_EXPLAIN_FAILED_CODE)

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
            validated = await asyncio.to_thread(
                validate_ingest_source, path, path_policy, admin=_get_request_identity().admin
            )
        except PathUnsafeError as e:
            return McpErrorResponse(error=_path_unsafe_message(e.reason), code="path_unsafe")
        try:
            _ingest_col = collection or default_collection
            _ingest_embedder = await _resolve_embedder(pipeline, embedder_cache, _ingest_col, config, namespace=_ingest_ns)
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(pipeline.store.collection_writer(_ingest_col, _ingest_ns))
                recorder = stack.enter_context(bind_stage_recorder()) if timings_enabled else None
                t0 = time.perf_counter()
                result = await pipeline.ingest_file(
                    validated, _ingest_col, embedder=_ingest_embedder, ingested_by="http",
                    namespace=_ingest_ns,
                    chunk_ttl_seconds=chunk_ttl_seconds,
                    chunk_scopes=chunk_scopes,
                )
                if recorder is not None:
                    recorder.record_total(t0)
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
            except ValidationError:
                return _schema_error("ingest_file")
        except CollectionOwnershipError as exc:
            return McpErrorResponse(error=str(exc), code=COLLECTION_UNAVAILABLE_CODE)
        except StoreBusyError as exc:
            return McpErrorResponse(error=str(exc), code="store_busy")
        except EmbedderNotReadyError as exc:
            # The model is still loading (or its load is wedged) — retryable.
            logger.warning("ingest_file: embedder not ready — %s", exc)
            return McpErrorResponse(error=EMBEDDER_NOT_READY_DETAIL, code=EMBEDDER_NOT_READY_CODE)
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("ingest_file failed")

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
            validated = await asyncio.to_thread(
                validate_ingest_source, path, path_policy, admin=_get_request_identity().admin
            )
            validate_glob_pattern(glob_pattern)
        except PathUnsafeError as e:
            return McpErrorResponse(error=_path_unsafe_message(e.reason), code="path_unsafe")
        try:
            _dir_col = collection or default_collection
            _dir_embedder = await _resolve_embedder(pipeline, embedder_cache, _dir_col, config, namespace=_dir_ns)

            async def progress_cb(done: int, total: int) -> None:
                if ctx is not None:
                    await ctx.report_progress(done, total)

            async with AsyncExitStack() as stack:
                await stack.enter_async_context(pipeline.store.collection_writer(_dir_col, _dir_ns))
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
                    recorder.record_total(t0)
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
            except ValidationError:
                return _schema_error("ingest_directory")
        except IngestTooManyFilesError as exc:
            return McpErrorResponse(error=str(exc), code=_ERR_VALIDATION)
        except CollectionOwnershipError as exc:
            return McpErrorResponse(error=str(exc), code=COLLECTION_UNAVAILABLE_CODE)
        except StoreBusyError as exc:
            return McpErrorResponse(error=str(exc), code="store_busy")
        except EmbedderNotReadyError as exc:
            # The model is still loading (or its load is wedged) — retryable.
            logger.warning("ingest_directory: embedder not ready — %s", exc)
            return McpErrorResponse(error=EMBEDDER_NOT_READY_DETAIL, code=EMBEDDER_NOT_READY_CODE)
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("ingest_directory failed")

    @app.tool()
    async def list_collections() -> list[dict[str, Any]]:
        """List all document collections with doc/chunk counts (internal fields omitted)."""
        _ns = _get_request_namespace()
        try:
            results = await pipeline.get_all_collections_meta(_ns)
            try:
                return [CollectionListItemSchema.from_result(r).model_dump(mode="json") for r in results]
            except ValidationError:
                return _schema_error("list_collections")
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("list_collections failed")

    @app.tool()
    async def get_collections_meta(
        include_description_embedding: bool = False,
    ) -> list[dict[str, Any]]:
        """Return public CollectionMeta for all collections.

        ``description_embedding`` is ``null`` by default because it can significantly
        increase payload size at scale (one dense vector per collection). Pass
        ``include_description_embedding=True`` to retain the field.
        """
        _ns = _get_request_namespace()
        try:
            results = await pipeline.get_all_collections_meta(_ns)
            try:
                return [
                    CollectionMetaMcpSchema.from_result(
                        r, include_description_embedding=include_description_embedding
                    ).model_dump(mode="json")
                    for r in results
                ]
            except ValidationError:
                return _schema_error("get_collections_meta")
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("get_collections_meta failed")

    @app.tool()
    async def get_collection_meta(name: str) -> dict[str, Any]:
        """Return public CollectionMeta for one named collection (internal fields omitted)."""
        _ns = _get_request_namespace()
        try:
            meta = await pipeline.get_collection_meta(name, namespace=_ns)
            if meta is None:
                return McpErrorResponse(error=f"Collection {name!r} not found", code="not_found")
            try:
                schema = CollectionDetailSchema.from_result(meta)
                return schema.model_dump(mode="json")
            except ValidationError:
                return _schema_error("get_collection_meta")
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("get_collection_meta failed")

    @app.tool()
    async def list_documents(
        collection: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """List documents in a collection."""
        _ns = _get_request_namespace()
        try:
            items, _next_cursor, _total = await pipeline.list_documents(
                collection or default_collection, limit, cursor=cursor, namespace=_ns
            )
            try:
                return [DocumentInfoSchema.from_result(r).model_dump(mode="json") for r in items]
            except ValidationError:
                return _schema_error("list_documents")
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("list_documents failed")

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
            except ValidationError:
                return _schema_error("delete_document")
        except StoreBusyError as exc:
            logger.warning("delete_document store busy: %s", exc)
            return McpErrorResponse(error=str(exc), code="store_busy")
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("delete_document failed")

    @app.tool()
    async def update_collection(
        collection_name: str,
        embedding_model: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """Update the embedding model for a collection. Triggers reindex if needed.

        Returns the updated collection metadata dict (same shape as PATCH /collections/{name}).
        Error cases:
        - Collection not found in config or for caller's namespace, or dropped while the call ran → error dict
          (not_found).
        - Invalid embedding model → error dict (validation_error, 422-equivalent).
        - Active reindex job in progress → error dict (conflict, 409-equivalent).
        - Collection lock not acquired in time → error dict (store_busy).
        """
        ns: str = _get_request_namespace()
        try:
            store = pipeline.store

            # 404 if collection not in config
            if config is not None:
                from archon_search.server.routes_collections import (
                    _all_collection_paths,
                )
                path_to_name = _all_collection_paths(config)
                if collection_name not in path_to_name:
                    return McpErrorResponse(error=f"Collection {collection_name!r} not found", code="not_found")

            # 404 if meta not found for this namespace (read again under the lock below, where it is authoritative)
            if await store.get_collection_meta(collection_name, namespace=ns) is None:
                return McpErrorResponse(error=f"Collection {collection_name!r} not found", code="not_found")

            # Validate embedding model — 422-equivalent on ModelValidationError
            try:
                new_dim = await validate_embedding_model(embedding_model)
            except ModelValidationError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_VALIDATION)

            # Dimension mismatch guard
            stored_dim = await store.get_stored_vector_dimension(collection_name, namespace=ns)
            if stored_dim is not None and stored_dim != new_dim:
                return McpErrorResponse(
                    error=f"dimension mismatch: stored vectors have dimension {stored_dim}, "
                    f"but model {embedding_model!r} produces dimension {new_dim}",
                    code=_ERR_VALIDATION,
                )

            # Read-modify-write under the collection lock, on a row read under it: update_collection_meta upserts, so
            # a write from a row read before a concurrent DELETE dropped the collection would re-insert its row.
            async with store.locked_collection_meta(collection_name, ns) as meta:
                if meta is None:
                    return McpErrorResponse(error=f"Collection {collection_name!r} not found", code="not_found")

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
                        await store.update_collection_meta(meta, _locked_by_caller=True)
                elif pending == requested:
                    # (a') no-op — persist only if stale reindex_job_id was cleared
                    if stale_cleared:
                        await store.update_collection_meta(meta, _locked_by_caller=True)
                elif pending is not None and active == requested:
                    # (c) revert: clear pending and any stale reindex_job_id
                    meta.pending_embedding_model = None
                    meta.needs_reindex = False
                    meta.reindex_job_id = None
                    await store.update_collection_meta(meta, _locked_by_caller=True)
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
                    await store.update_collection_meta(meta, _locked_by_caller=True)

            try:
                schema = CollectionDetailSchema.from_result(meta)
                return schema.model_dump(mode="json")
            except ValidationError:
                return _schema_error("update_collection")
        except StoreBusyError as exc:
            logger.warning("update_collection store busy: %s", exc)
            return McpErrorResponse(error=str(exc), code="store_busy")
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("update_collection failed")

    @app.tool()
    async def export_collection(
        collection: str,
    ) -> dict[str, Any]:
        """Enqueue an export job for a collection. Returns immediately with a QUEUED job dict.

        The MCP client can poll GET /jobs/{job_id} for progress. The archive is written to the caller's own export
        directory, ``<data_dir>/exports/<namespace>/`` (the job's ``output_path`` names the file); the directory
        cannot be chosen.
        """
        if job_store is None:
            return McpErrorResponse(error=JOB_STORE_NOT_CONFIGURED_DETAIL, code=_ERR_INTERNAL)

        _export_ns = _get_request_namespace()
        resolved_dir = exports_dir(get_data_dir(), _export_ns)

        try:
            meta = await pipeline.store.get_collection_meta(collection, _export_ns)
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("export_collection: collection lookup failed")
        if meta is None:
            return McpErrorResponse(
                error=f"Collection {collection!r} not found", code="not_found"
            )

        from datetime import datetime
        from uuid import uuid4

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        job_uuid = str(uuid4())
        archive_path = resolved_dir / f"{collection}-{timestamp}.tar.gz"
        tmp_path_val = resolved_dir / f".export-{job_uuid}.jsonl.tmp"

        try:
            job = job_store.create_export(
                collection=collection,
                output_path=str(archive_path),
                tmp_path=str(tmp_path_val),
                namespace=_export_ns,
            )
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("export_collection: job creation failed")
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
        ``path`` must be the absolute path of an archive in the caller's own namespace: its export directory
        (``<data_dir>/exports/<namespace>/``) or its backup directory. An archive exported by another namespace is
        refused, whichever directory it sits in.
        ``on_error`` must be ``"fail"`` (abort on corrupt line) or ``"skip"`` (skip corrupt lines).
        """
        if job_store is None:
            return McpErrorResponse(error=JOB_STORE_NOT_CONFIGURED_DETAIL, code=_ERR_INTERNAL)

        _import_ns = _get_request_namespace()

        if on_error not in {"fail", "skip"}:
            return McpErrorResponse(
                error="on_error must be 'fail' or 'skip'", code=_ERR_VALIDATION
            )

        # The archive must sit in the caller's own namespace directory (its exports or its backups): a path outside
        # answers the same ``path_unsafe`` whether or not a file exists there, so it is no probe for other
        # namespaces' archives.
        backup_output_dir = config.backup.output_dir if config is not None else str(get_data_dir() / "backups")
        try:
            archive_path = importable_archive_path(path, get_data_dir(), backup_output_dir, _import_ns)
        except PathUnsafeError as exc:
            return McpErrorResponse(
                error=_path_unsafe_message(exc.reason), code="path_unsafe"
            )
        if not await asyncio.to_thread(archive_path.exists):
            return McpErrorResponse(
                error=f"Archive not found: {path}", code="not_found"
            )

        try:
            await asyncio.to_thread(validate_archive_file, archive_path)
        except PathUnsafeError as exc:
            return McpErrorResponse(
                error=f"unsafe archive: {exc.reason}", code="path_unsafe"
            )
        except Exception as exc:
            # tarfile/gzip error text can reflect the archive's own bytes: log it, send a fixed detail.
            logger.warning("import archive %s could not be opened: %s", archive_path, exc, exc_info=exc)
            return McpErrorResponse(error=INVALID_ARCHIVE_DETAIL, code=_ERR_VALIDATION)

        try:
            reader = ImportArchiveReader(archive_path)
            manifest = await asyncio.to_thread(reader.read_manifest)
        except (ValueError, PathUnsafeError) as exc:
            return McpErrorResponse(
                error=f"invalid manifest: {manifest_error_detail(exc, archive_path)}",
                code=_ERR_VALIDATION,
            )

        if not manifest_namespace_ok(manifest, _import_ns, archive_path, get_data_dir(), backup_output_dir):
            return McpErrorResponse(error=ARCHIVE_NAMESPACE_MISMATCH_DETAIL, code=_ERR_VALIDATION)

        # The mismatch texts below match POST /collections/{name}/import's `detail` verbatim.
        archive_model = manifest.get("active_embedding_model", "")
        server_model = config.embedding_model if config is not None else ""
        if archive_model != server_model:
            return McpErrorResponse(
                error=(
                    f"archive uses model {quote_archive_value(archive_model)}; "
                    f"server is configured with {server_model!r}"
                ),
                code="embedding_model_mismatch",
            )

        try:
            existing_meta = await pipeline.store.get_collection_meta(collection, _import_ns)
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("import_collection: collection lookup failed")
        # Only the caller's own collection is visible here: a name another namespace owns is refused by the import
        # job itself, with a fixed text that names no owner.
        if existing_meta is not None and not force_overwrite:
            return McpErrorResponse(
                error=f"collection {collection!r} already exists; use force_overwrite=True to overwrite",
                code="collection_exists",
            )

        archive_schema = manifest.get("schema_version")
        if not ignore_schema_version and archive_schema != EXPORT_SCHEMA_VERSION:
            return McpErrorResponse(
                error=(
                    f"archive has schema_version={quote_archive_value(archive_schema)}; "
                    f"server expects {EXPORT_SCHEMA_VERSION!r}; "
                    "use ignore_schema_version=true to bypass"
                ),
                code="schema_version_mismatch",
            )

        try:
            job = job_store.create_import(
                collection=collection,
                archive_path=str(archive_path),
                force_overwrite=force_overwrite,
                ignore_schema_version=ignore_schema_version,
                on_error=on_error,
                namespace=_import_ns,
            )
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("import_collection: job creation failed")
        return job_to_dict(job)

    # -----------------------------------------------------------------------
    # Key-management tools (D7 BE-9) — registered only when key_store is set.
    # These tools mirror the REST /keys endpoints but are exposed over MCP.
    # rotate_key runs the same key_manager.rotate_and_persist_default_key use case as
    # POST /keys/rotate — it does NOT call the REST endpoint internally.
    # -----------------------------------------------------------------------

    if key_store is not None:
        import asyncio as _asyncio
        import os as _os

        from archon_search.constants import _validate_namespace
        from archon_search.key_manager import ENV_VAR as _ENV_VAR

        @app.tool()
        async def create_key(
            namespace: str,
            label: str | None = None,
            expires_at: str | None = None,
            admin: bool = False,
        ) -> dict[str, Any]:
            """Issue a new managed API key.

            The raw bearer token is returned exactly once in the response ``token``
            field. It is never stored on disk — only its SHA-256 hash is persisted.

            ``expires_at`` must be an ISO-8601 datetime string with timezone offset
            (e.g. ``"2030-01-01T00:00:00Z"``), or ``null`` for no expiry.

            ``admin`` creates an admin key (may manage keys of every namespace and run the server-wide
            control endpoints); only an admin key may set it. A non-admin key may only create non-admin keys
            for its own namespace.
            """
            try:
                authorize_key_create(_get_request_identity(), namespace=namespace, admin=admin)
            except ForbiddenError as exc:
                return _forbidden(exc)

            try:
                _validate_namespace(namespace)
            except ValueError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_VALIDATION)

            from datetime import datetime

            parsed_expires_at = None
            if expires_at is not None:
                try:
                    parsed_expires_at = datetime.fromisoformat(
                        expires_at
                    )
                except ValueError as exc:
                    return McpErrorResponse(
                        error=f"invalid expires_at: {exc}", code=_ERR_VALIDATION
                    )
                if parsed_expires_at.tzinfo is None:
                    return McpErrorResponse(
                        error="expires_at must be timezone-aware (include UTC offset or Z suffix)",
                        code=_ERR_VALIDATION,
                    )

            try:
                created = await key_store.create(
                    ns=namespace,
                    label=label,
                    expires_at=parsed_expires_at,
                    admin=admin,
                )
            except KeyStoreUnreadableError:
                return McpErrorResponse(error=KEY_STORE_UNREADABLE_DETAIL, code=_ERR_INTERNAL)
            except Exception:
                logger.exception("create_key failed")
                return McpErrorResponse(error=CREATE_KEY_FAILED_DETAIL, code=_ERR_INTERNAL)
            return {
                "id": created.id,
                "token": created.token,
                "namespace": namespace,
                "label": label,
                "created_at": str(created.created_at),
                "expires_at": str(parsed_expires_at) if parsed_expires_at is not None else None,
                "status": "active",
                "admin": admin,
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
                    code=_ERR_VALIDATION,
                )

            identity = _get_request_identity()
            try:
                namespace = authorize_key_listing_namespace(identity, namespace)
            except ForbiddenError as exc:
                return _forbidden(exc)

            all_records = [r for r in await key_store.list_keys() if can_manage_key(identity, r)]

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
                        "admin": r.admin,
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
            identity = _get_request_identity()
            if key_id == "null":
                return McpErrorResponse(
                    error=(
                        "This key is managed via archon-search.toml [namespaces] — "
                        "remove it from the config file and restart the server."
                    ),
                    code="not_found",
                )

            # A non-admin may revoke only the non-admin keys of its own namespace; any other id is
            # reported as unknown so a key's existence in another namespace is not revealed.
            if not await caller_may_revoke(key_store, identity, key_id):
                return McpErrorResponse(error=f"Key not found: {key_id!r}", code="not_found")

            try:
                await key_store.revoke(key_id)
            except KeyError:
                return McpErrorResponse(
                    error=f"Key not found: {key_id!r}", code="not_found"
                )
            except KeyStoreUnreadableError:
                return McpErrorResponse(error=KEY_STORE_UNREADABLE_DETAIL, code=_ERR_INTERNAL)
            except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
                return _internal_error("revoke_key failed")

            return {"id": key_id, "status": "revoked"}

        @app.tool()
        async def rotate_key(grace_seconds: int | None = None) -> dict[str, Any]:
            """Rotate the default API key (admin key required: it replaces the server-wide default key).

            Generates a new managed API key, writes the new raw token to
            ``.search.env``, and revokes (or grace-expires) the old default key
            in ``keys.json``.

            ``grace_seconds`` overrides the TOML ``[auth].rotate_grace_seconds``
            default.  When ``null``, the TOML default is used.  When both are 0,
            the old key is immediately revoked.

            Returns 409 (conflict) when ``ARCHON_SEARCH_API_KEY`` env var is set —
            the env var overrides ``.search.env``, so rotation would be a silent
            no-op (same behaviour as ``POST /keys/rotate``, S23).

            Retires the server's current default key and makes the new key current for
            this MCP endpoint. When MCP is mounted on the REST server (``/mcp``), that is
            the key ``POST /keys/rotate`` would retire, and the new key is current for REST too.
            """
            try:
                require_admin(_get_request_identity())
            except ForbiddenError as exc:
                return _forbidden(exc)

            # Determine grace_seconds: argument wins over config default.
            _grace: int
            if grace_seconds is not None:
                _grace = grace_seconds
            else:
                _grace = getattr(getattr(config, "auth", None), "rotate_grace_seconds", 0)

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

            async def read_current_token() -> str:
                if api_key_state is not None:
                    # The same current default key POST /keys/rotate reads and publishes.
                    return api_key_state.api_key
                # No server-held key: re-read .search.env. The use case validates
                # grace_seconds before calling this, because load_or_generate_key may
                # create .search.env.
                current_token, _ = await _asyncio.to_thread(load_or_generate_key)
                return current_token

            def publish_new_token(new_token: str) -> None:
                # Runs inside the rotation lock, only after both writes succeeded.
                if api_key_state is not None:
                    api_key_state.api_key = new_token

            # The use case serialises the whole rotation under key_store.rotation_lock,
            # shared with POST /keys/rotate. An out-of-range grace_seconds argument is the
            # caller's error; a blank current key comes from server state (a server fault).
            try:
                result = await rotate_and_persist_default_key(
                    key_store,
                    key_file=resolve_rotation_key_file(),
                    read_current_token=read_current_token,
                    grace_seconds=_grace,
                    publish_new_token=publish_new_token,
                )
            except BlankCurrentKeyError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_INTERNAL)
            except KeyRotationInputError as exc:
                return McpErrorResponse(error=str(exc), code=_ERR_VALIDATION)
            except KeyStoreUnreadableError:
                return McpErrorResponse(error=KEY_STORE_UNREADABLE_DETAIL, code=_ERR_INTERNAL)
            except KeyFileWriteError:
                return McpErrorResponse(error=ROTATE_WRITE_FAILED_DETAIL, code=_ERR_INTERNAL)
            except RotationIncompleteError:
                return McpErrorResponse(error=ROTATE_KEYS_JSON_FAILED_DETAIL, code=_ERR_INTERNAL)
            except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
                return _internal_error("rotate_key failed")

            new_key_id = result.new_key_id
            old_record = result.old_record

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
                "token": result.new_token,
                "status": "active",
                "old_key_id": old_key_id_str,
                "old_key_expires_at": str(old_key_expires_at) if old_key_expires_at is not None else None,
                "old_key_status": old_key_status,
            }

    # -----------------------------------------------------------------------
    # Graph inspection tools (E2b) — always registered; guard at runtime.
    # -----------------------------------------------------------------------

    from archon_search.graph_inspector import (
        _edge_sort_key,
        inspect_collection,
        inspect_cross_collection,
    )
    from archon_search.graph_types import DEFAULT_IMPACT_DEPTH, ImpactDirection

    async def _resolve_salience_context(
        resolved_salience_mode: str,
        ns: str,
    ) -> tuple[dict[str, Any] | None, int]:
        """Resolve tfidf entity-presence data and collection count for graph inspection tools.

        Returns ``(entity_presence, num_collections)``. Only populates
        ``entity_presence`` when ``resolved_salience_mode == "tfidf"`` (fetches
        presence across every collection in *ns*); otherwise returns ``(None, 1)``.
        """
        if resolved_salience_mode != "tfidf":
            return None, 1
        all_meta = await pipeline.get_all_collections_meta(ns)
        all_ns_collection_names = [m.name for m in all_meta]
        entity_presence = await graph_store.get_entity_presence_across_collections(
            all_ns_collection_names, ns=ns
        )
        return entity_presence, len(all_ns_collection_names)

    def _build_top_nodes(view: Any) -> list[dict[str, Any]]:
        """Build the top-20 node summaries for graph inspection tool responses.

        ``view.nodes`` is already ordered by the active salience_mode (via
        ``_truncate_graph``'s ``_node_sort_key``) — take the first 20 directly
        rather than re-sorting by raw salience, which would be wrong for
        ``salience_mode="importance"`` (ordering key is pagerank_score, not
        salience).
        """
        return [
            {
                "entity_id": n.entity_id,
                "entity_name": n.entity_name,
                "chunk_count": n.chunk_count,
                "salience": n.salience,
                "pagerank_score": n.pagerank_score,
            }
            for n in view.nodes[:20]
        ]

    @app.tool()
    async def get_graph(
        collection: str,
        salience_mode: Literal["frequency", "tfidf", "importance"] | None = None,
    ) -> dict[str, Any]:
        """Return a summary of the entity graph for a single collection.

        Returns node_count, edge_count, entity_type_distribution, and top-20
        nodes (by salience) and edges (by weight) for inspection and visualization.

        ``salience_mode`` (default "frequency" when omitted): "frequency" (chunk
        ratio), "tfidf" (TF×IDF across namespace collections), or "importance"
        (persisted PageRank score over code-symbol edges — E2g BE-7, nulls-last).
        """
        # Guard: graph must be enabled and graph_store must be available
        _gg_config = getattr(config, "graph", None)
        _gg_enabled = getattr(_gg_config, "enabled", False) if _gg_config is not None else False
        if not _gg_enabled or graph_store is None:
            return McpErrorResponse(
                error="graph inspection requires [graph] enabled=true in server config",
                code="graph_disabled",
            )

        ns = _get_request_namespace()
        resolved_salience_mode = salience_mode or "frequency"
        try:
            meta = await pipeline.get_collection_meta(collection, namespace=ns)
            if meta is None:
                return McpErrorResponse(error=f"collection {collection!r} not found", code="not_found")

            total_chunk_count = meta.chunk_count
            max_nodes = getattr(_gg_config, "max_inspection_nodes", 5000)
            max_edges = getattr(_gg_config, "max_inspection_edges", 25000)

            entity_presence, num_collections = await _resolve_salience_context(resolved_salience_mode, ns)

            view = await inspect_collection(
                graph_store=graph_store,
                collection=collection,
                total_chunk_count=total_chunk_count,
                max_nodes=max_nodes,
                max_edges=max_edges,
                salience_mode=resolved_salience_mode,
                entity_presence=entity_presence,
                num_collections=num_collections,
                ns=ns,
                visibility_source=pipeline.store,
            )

            # Build entity_type_distribution from nodes
            # For now, we count entities by extracting type from entity_id
            # (e.g., "PERSON:alice" → type is "PERSON")
            entity_type_dist: dict[str, int] = {}
            for node in view.nodes:
                # Extract entity type (prefix before ':')
                if ":" in node.entity_id:
                    entity_type = node.entity_id.split(":", 1)[0]
                else:
                    entity_type = "unknown"
                entity_type_dist[entity_type] = entity_type_dist.get(entity_type, 0) + 1

            top_nodes = _build_top_nodes(view)

            # Top edges: sorted by weight desc, take first 20
            top_edges_list = sorted(view.edges, key=_edge_sort_key)[:20]
            top_edges = [
                {
                    "edge_id": e.edge_id,
                    "source_entity_id": e.source_entity_id,
                    "target_entity_id": e.target_entity_id,
                    "weight": e.weight,
                }
                for e in top_edges_list
            ]

            return {
                "node_count": view.node_count,
                "edge_count": view.edge_count,
                "entity_type_distribution": entity_type_dist,
                "top_nodes": top_nodes,
                "top_edges": top_edges,
            }
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("get_graph failed")

    @app.tool()
    async def get_graph_cross_collection(
        collections: list[str],
        salience_mode: Literal["frequency", "tfidf", "importance"] | None = None,
    ) -> dict[str, Any]:
        """Return a summary of the merged entity graph across multiple collections.

        Returns the same fields as get_graph but for a merged view across collections.
        Requires at least 2 collections; same-named entities and edges are merged.

        ``salience_mode`` (default "frequency" when omitted): "frequency" (chunk
        ratio), "tfidf" (TF×IDF across namespace collections), or "importance"
        (persisted PageRank score over code-symbol edges — E2g BE-7, nulls-last).
        """
        # Guard: graph must be enabled and graph_store must be available
        _ggc_config = getattr(config, "graph", None)
        _ggc_enabled = getattr(_ggc_config, "enabled", False) if _ggc_config is not None else False
        if not _ggc_enabled or graph_store is None:
            return McpErrorResponse(
                error="graph inspection requires [graph] enabled=true in server config",
                code="graph_disabled",
            )

        ns = _get_request_namespace()
        resolved_salience_mode = salience_mode or "frequency"
        try:
            # Validate collections parameter: at least 2 required
            if not collections or len(collections) < 2:
                return McpErrorResponse(
                    error="collections must contain at least 2 distinct collection names",
                    code=_ERR_VALIDATION,
                )

            # Deduplicate collection names
            deduped_collections = []
            seen: set[str] = set()
            for col in collections:
                if col not in seen:
                    deduped_collections.append(col)
                    seen.add(col)

            if len(deduped_collections) < 2:
                return McpErrorResponse(
                    error="collections must contain at least 2 distinct collection names after deduplication",
                    code=_ERR_VALIDATION,
                )

            max_collections = getattr(_ggc_config, "max_cross_collection_collections", DEFAULT_MAX_CROSS_COLLECTION_COLLECTIONS)
            if len(deduped_collections) > max_collections:
                return McpErrorResponse(
                    error=f"collections may name at most {max_collections} collections ([graph].max_cross_collection_collections)",
                    code=_ERR_VALIDATION,
                )

            # Fetch metadata for each collection and build total_chunk_counts
            total_chunk_counts: dict[str, int] = {}
            for col in deduped_collections:
                meta = await pipeline.get_collection_meta(col, namespace=ns)
                if meta is None:
                    return McpErrorResponse(error=f"collection {col!r} not found", code="not_found")
                total_chunk_counts[col] = meta.chunk_count

            max_nodes = getattr(_ggc_config, "max_inspection_nodes", 5000)
            max_edges = getattr(_ggc_config, "max_inspection_edges", 25000)

            entity_presence, num_collections = await _resolve_salience_context(resolved_salience_mode, ns)

            view = await inspect_cross_collection(
                graph_store=graph_store,
                collections=deduped_collections,
                total_chunk_counts=total_chunk_counts,
                max_nodes=max_nodes,
                max_edges=max_edges,
                salience_mode=resolved_salience_mode,
                entity_presence=entity_presence,
                num_collections=num_collections,
                ns=ns,
                visibility_source=pipeline.store,
            )

            # Build entity_type_distribution from merged nodes
            entity_type_dist: dict[str, int] = {}
            for node in view.nodes:
                if ":" in node.entity_id:
                    entity_type = node.entity_id.split(":", 1)[0]
                else:
                    entity_type = "unknown"
                entity_type_dist[entity_type] = entity_type_dist.get(entity_type, 0) + 1

            top_nodes = _build_top_nodes(view)

            # Top edges: sorted by weight desc, take first 20
            top_edges_list = sorted(view.edges, key=_edge_sort_key)[:20]
            top_edges = [
                {
                    "edge_id": e.edge_id,
                    "source_entity_id": e.source_entity_id,
                    "target_entity_id": e.target_entity_id,
                    "weight": e.weight,
                }
                for e in top_edges_list
            ]

            return {
                "node_count": view.node_count,
                "edge_count": view.edge_count,
                "entity_type_distribution": entity_type_dist,
                "top_nodes": top_nodes,
                "top_edges": top_edges,
            }
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("get_graph_cross_collection failed")

    def _impact_edge_to_dict(e: Any) -> dict[str, Any]:
        """Convert one impact-traversal edge entry to a dict — E2g BE-9."""
        return {
            "entity_id": e.entity_id,
            "entity_name": e.entity_name,
            "relationship_type": e.relationship_type,
            "extraction_method": e.extraction_method,
            "depth": e.depth,
        }

    def _impact_group_to_dict(group: Any) -> dict[str, Any]:
        """Convert an ``ImpactGroup`` to a dict, mirroring the REST response shape — E2g BE-9."""
        return {
            "direct": [_impact_edge_to_dict(e) for e in group.direct],
            "indirect": [_impact_edge_to_dict(e) for e in group.indirect],
            "truncated": group.truncated,
            "omitted_count": group.omitted_count,
        }

    @app.tool()
    async def graph_impact(
        collection: str,
        symbol: str,
        file_path: str | None = None,
        depth: int | None = None,
        direction: str | None = None,
        extraction_method_filter: str | None = None,
    ) -> dict[str, Any]:
        """Blast-radius (caller/callee) impact analysis for a code symbol — E2g BE-9.

        ``file_path`` disambiguates same-named symbols to the one defined in that file.
        ``depth`` (default 2, hard-capped server-side at MAX_IMPACT_DEPTH) and
        ``direction`` ("callers", "callees", or "both"; default "both") are optional.
        ``extraction_method_filter`` restricts traversal to edges with that extraction method.

        Returns a dict mirroring GraphStore.compute_impact's ImpactResult 1:1
        (symbol, callers, callees, depth_used), or an McpErrorResponse.
        """
        # Guard: graph must be enabled and graph_store must be available
        _gi_config = getattr(config, "graph", None)
        _gi_enabled = getattr(_gi_config, "enabled", False) if _gi_config is not None else False
        if not _gi_enabled or graph_store is None:
            return McpErrorResponse(
                error="graph inspection requires [graph] enabled=true in server config",
                code="graph_disabled",
            )

        ns = _get_request_namespace()
        try:
            meta = await pipeline.get_collection_meta(collection, namespace=ns)
            if meta is None:
                return McpErrorResponse(error=f"collection {collection!r} not found", code="not_found")

            if depth is not None and depth < 1:
                return McpErrorResponse(error="depth must be >= 1", code=_ERR_VALIDATION)

            # BE-9 fills depth/direction defaults at this surface's Presentation->Adapter boundary
            effective_depth = depth if depth is not None else DEFAULT_IMPACT_DEPTH
            try:
                effective_direction = (
                    ImpactDirection(direction) if direction is not None else ImpactDirection.both
                )
            except ValueError:
                return McpErrorResponse(
                    error=f"invalid direction {direction!r}; must be one of 'callers', 'callees', 'both'",
                    code=_ERR_VALIDATION,
                )

            result = await graph_store.compute_impact(
                collection,
                symbol,
                effective_depth,
                effective_direction,
                extraction_method_filter,
                file_path,
                ns,
                visibility=await pipeline.store.get_chunk_visibility(collection, ns),
            )

            return {
                "symbol": result.symbol,
                "callers": _impact_group_to_dict(result.callers),
                "callees": _impact_group_to_dict(result.callees),
                "depth_used": result.depth_used,
            }
        except Exception:  # noqa: BLE001 — MCP tool boundary: any failure becomes a sanitized internal-error response
            return _internal_error("graph_impact failed")

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
    hyde_generator: HyDEGenerator | None = None,
    rag_fusion_generator: RAGFusionGenerator | None = None,
    key_store: KeyStore | None = None,
    doc_id_hasher: Callable[[str], str] | None = None,
    graph_store: GraphStore | None = None,
    api_key_state: State | None = None,
    ingest_policy: IngestPathPolicy | None = None,
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

    ``api_key_state`` — the object whose ``api_key`` attribute holds the server's current
    default key; ``create_app`` (REST) passes its ``app.state``, so the auth middleware and
    ``rotate_key`` here read, and ``rotate_key`` publishes, the same key as ``POST /keys/rotate``.
    When omitted, a private ``State`` seeded with ``load_or_generate_key()`` plays that role.
    """
    from archon_search.server.middleware_context import RequestContextMiddleware

    if api_key_state is None:
        api_key_state = State({"api_key": load_or_generate_key()[0]})
    fastmcp_app = create_app(pipeline, default_collection, writer=writer, config=config, embedder_cache=embedder_cache, job_store=job_store, hyde_generator=hyde_generator, rag_fusion_generator=rag_fusion_generator, key_store=key_store, doc_id_hasher=doc_id_hasher, graph_store=graph_store, api_key_state=api_key_state, ingest_policy=ingest_policy)
    # FastMCP 3.4.x renamed ``streamable_http_app()`` to ``http_app()``; ``path='/'``
    # exposes the JSON-RPC endpoint at the sub-app root so it is reachable at the
    # ``/mcp`` mount point without an extra suffix (see ADR 09, K-1 spike).
    starlette_app: Starlette = fastmcp_app.http_app(path="/")
    starlette_app.add_middleware(
        APIKeyMiddleware,
        api_key=api_key_state.api_key,
        namespaces=config.namespaces if config is not None else {},
        key_store=key_store,
        api_key_state=api_key_state,
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

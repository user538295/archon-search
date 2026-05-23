"""FastMCP HTTP server for RAG search."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path
from time import monotonic
from typing import Any, TypedDict

from fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from archon_search._path_safety import PathUnsafeError, validate_ingest_path
from archon_search.store import StoreBusyError
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.key_manager import load_or_generate_key
from archon_search.pipeline import SearchPipeline
from archon_search.progress import IndexingState, IndexingStatus
from archon_search.server.middleware_auth import APIKeyMiddleware
from archon_search.telemetry.entry import ErrorKind, FilterFlags, TelemetryEntry
from archon_search.telemetry.writer import TelemetryWriter

logger = logging.getLogger("archon.search")


class McpErrorResponse(TypedDict):
    error: str
    code: str


def _path_unsafe_message(reason: str) -> str:
    """Map a PathUnsafeError reason code to an LLM-readable error phrase.

    Five mappings — one per reason code from validate_ingest_path.
    """
    _messages = {
        "empty": "path is unsafe: path is empty — provide an absolute file path",
        "whitespace_only": "path is unsafe: path contains only whitespace — provide an absolute file path",
        "nul_byte": "path is unsafe: path contains a NUL byte — use a standard absolute path",
        "contains_dotdot": "path is unsafe: input contains '..' segment — use an absolute path without traversal",
        "not_absolute": "path is unsafe: path is not absolute — provide a full absolute path (not relative)",
    }
    return _messages.get(reason, f"path is unsafe: {reason}")


def _chunk_to_context_dict(chunk: Any) -> dict[str, Any]:
    """Serialize a ChunkRecord for MCP ``search_with_context`` payloads, dropping
    the ``vector`` field — raw embeddings should not leak over MCP and add no
    value to context-window consumers."""
    d = asdict(chunk)
    d.pop("vector", None)
    return d


def create_app(
    pipeline: SearchPipeline,
    default_collection: str,
    writer: TelemetryWriter | None = None,
    config: SearchConfig | None = None,
) -> FastMCP:
    """Create a FastMCP app with 10 RAG tools registered."""
    app = FastMCP("archon-search")

    @app.tool()
    async def search(
        query: str,
        collection: str | None = None,
        file_type: str | None = None,
        source_path_prefix: str | None = None,
        source_path_glob: str | None = None,
        indexed_after: str | None = None,
        indexed_before: str | None = None,
        include_metadata: bool = False,
    ) -> dict[str, Any]:
        """Search for relevant document chunks using hybrid vector + FTS search."""
        from archon_search.filters import SearchFilters  # noqa: PLC0415
        from pydantic import ValidationError  # noqa: PLC0415

        start = monotonic()
        try:
            filters: SearchFilters | None = None
            if any(v is not None for v in [file_type, source_path_prefix, source_path_glob, indexed_after, indexed_before]) or include_metadata:
                try:
                    filters = SearchFilters(
                        file_type=file_type,
                        source_path_prefix=source_path_prefix,
                        source_path_glob=source_path_glob,
                        indexed_after=indexed_after,
                        indexed_before=indexed_before,
                        include_metadata=include_metadata,
                    )
                except ValidationError as exc:
                    return McpErrorResponse(error=str(exc), code="validation_error")
            result_obj = await pipeline.search(query, collection or default_collection, filters=filters)
            results = [asdict(r) for r in result_obj.results]
            if not include_metadata:
                for r in results:
                    r["metadata"] = {}
            if writer is not None:
                try:
                    _ff = FilterFlags(
                        file_type=bool(file_type is not None),
                        source_path_prefix=bool(source_path_prefix is not None),
                        source_path_glob=bool(source_path_glob is not None),
                        indexed_after=bool(indexed_after is not None),
                        indexed_before=bool(indexed_before is not None),
                        include_metadata=bool(include_metadata),
                    )
                    writer.enqueue(
                        TelemetryEntry.from_search_tool_result(
                            endpoint="search",
                            collection=collection or default_collection,
                            result_doc_ids=[r["doc_id"] for r in results],
                            latency_ms=(monotonic() - start) * 1000.0,
                            filter_flags=_ff,
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search entry enqueue failed", exc_info=True)
            return {"results": results, "acl_filtered": result_obj.acl_filtered}
        except Exception as exc:
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
            logger.exception("search failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def search_with_context(
        query: str,
        collection: str | None = None,
        context_window: int = 1,
        file_type: str | None = None,
        source_path_prefix: str | None = None,
        source_path_glob: str | None = None,
        indexed_after: str | None = None,
        indexed_before: str | None = None,
        include_metadata: bool = False,
    ) -> list[dict[str, Any]]:
        """Search and return surrounding chunks for richer context."""
        from archon_search.filters import SearchFilters  # noqa: PLC0415
        from pydantic import ValidationError  # noqa: PLC0415

        start = monotonic()
        try:
            filters: SearchFilters | None = None
            if any(v is not None for v in [file_type, source_path_prefix, source_path_glob, indexed_after, indexed_before]) or include_metadata:
                try:
                    filters = SearchFilters(
                        file_type=file_type,
                        source_path_prefix=source_path_prefix,
                        source_path_glob=source_path_glob,
                        indexed_after=indexed_after,
                        indexed_before=indexed_before,
                        include_metadata=include_metadata,
                    )
                except ValidationError as exc:
                    return McpErrorResponse(error=str(exc), code="validation_error")
            results = await pipeline.search_with_context(
                query, collection or default_collection, context_window, filters=filters
            )
            if writer is not None:
                try:
                    _ff = FilterFlags(
                        file_type=bool(file_type is not None),
                        source_path_prefix=bool(source_path_prefix is not None),
                        source_path_glob=bool(source_path_glob is not None),
                        indexed_after=bool(indexed_after is not None),
                        indexed_before=bool(indexed_before is not None),
                        include_metadata=bool(include_metadata),
                    )
                    writer.enqueue(
                        TelemetryEntry.from_search_tool_result(
                            endpoint="search_with_context",
                            collection=collection or default_collection,
                            result_doc_ids=[r["result"].doc_id for r in results],
                            latency_ms=(monotonic() - start) * 1000.0,
                            filter_flags=_ff,
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search_with_context entry enqueue failed", exc_info=True)
            output = []
            for r in results:
                result_dict = asdict(r["result"])
                if not include_metadata:
                    result_dict["metadata"] = {}
                ctx_before = [_chunk_to_context_dict(c) for c in r["context_before"]]
                ctx_after = [_chunk_to_context_dict(c) for c in r["context_after"]]
                if not include_metadata:
                    for ctx in ctx_before + ctx_after:
                        ctx.pop("metadata", None)
                output.append({
                    "result": result_dict,
                    "context_before": ctx_before,
                    "context_after": ctx_after,
                })
            return output
        except Exception as exc:
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_error(
                            endpoint="search_with_context",
                            status="internal_error",
                            error_kind=ErrorKind.other,
                            latency_ms=(monotonic() - start) * 1000.0,
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search_with_context error entry enqueue failed", exc_info=True)
            logger.exception("search_with_context failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def ingest_file(
        path: str,
        collection: str | None = None,
    ) -> dict[str, Any]:
        """Ingest a single file into the RAG store."""
        try:
            validated_path = validate_ingest_path(path)
        except PathUnsafeError as e:
            return McpErrorResponse(error=_path_unsafe_message(e.reason), code="path_unsafe")
        try:
            result = await pipeline.ingest_file(
                validated_path, collection or default_collection, ingested_by="http",
            )
            return asdict(result)
        except StoreBusyError:
            return McpErrorResponse(error="store busy — reindex in progress; retry later", code="store_busy")
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
        try:
            validated_path = validate_ingest_path(path)
        except PathUnsafeError as e:
            return McpErrorResponse(error=_path_unsafe_message(e.reason), code="path_unsafe")
        try:
            async def progress_cb(done: int, total: int) -> None:
                if ctx is not None:
                    await ctx.report_progress(done, total)

            results = await pipeline.ingest_directory(
                validated_path,
                collection or default_collection,
                glob_pattern=glob_pattern,
                progress_cb=progress_cb,
                ingested_by="http",
            )
            return [asdict(r) for r in results]
        except StoreBusyError:
            return McpErrorResponse(error="store busy — reindex in progress; retry later", code="store_busy")
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
                output.append(d)
            return output
        except Exception as exc:
            logger.exception("list_collections failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def get_collections_meta() -> list[dict[str, Any]]:
        """Return full CollectionMeta for all collections, including centroid vectors."""
        try:
            results = await pipeline.get_all_collections_meta()
            return [asdict(r) for r in results]
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
    ) -> dict[str, Any]:
        """Delete all chunks for a document from the store."""
        try:
            count = await pipeline.delete_document(
                doc_id, collection or default_collection
            )
            return {"deleted": count}
        except Exception as exc:
            logger.exception("delete_document failed")
            return McpErrorResponse(error=str(exc), code="internal_error")

    @app.tool()
    async def explain(
        query: str,
        collection: str | None = None,
        top_k: int = 5,
        rerank: bool = True,
    ) -> dict[str, Any]:
        """Return ranked search results with full score provenance for debugging."""
        from archon_search.server.routes_explain import (  # noqa: PLC0415
            ExplainResponse,
            _EXPLAIN_TIMEOUT_SECONDS,
            _build_routing_explain,
            _enqueue_explain_error,
            _enqueue_explain_success,
        )
        import json  # noqa: PLC0415

        start = monotonic()
        if not query or not query.strip():
            _enqueue_explain_error(writer, start, "validation_error", ErrorKind.validation_error)
            return McpErrorResponse(error="query must not be empty", code="validation_error")
        if not (1 <= top_k <= 100):
            _enqueue_explain_error(writer, start, "validation_error", ErrorKind.validation_error)
            return McpErrorResponse(error="top_k must be between 1 and 100", code="validation_error")

        _cfg = config if config is not None else SearchConfig()

        try:
            if collection is not None:
                # Pinned path
                try:
                    meta = await pipeline.get_collection_meta(collection, namespace=DEFAULT_NAMESPACE)
                except Exception as exc:
                    logger.error("explain: meta lookup failed for collection %r: %s", collection, exc, exc_info=True)
                    _enqueue_explain_error(writer, start, "internal_error", ErrorKind.other)
                    return McpErrorResponse(error="service unavailable", code="service_unavailable")
                if meta is None:
                    _enqueue_explain_error(writer, start, "validation_error", ErrorKind.validation_error)
                    return McpErrorResponse(
                        error=f"Collection {collection!r} not found", code="not_found"
                    )
                try:
                    pipeline_result = await asyncio.wait_for(
                        pipeline.explain(
                            query, collection, top_k=top_k, rerank=rerank, namespace=DEFAULT_NAMESPACE
                        ),
                        timeout=_EXPLAIN_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "explain timed out after %.1fs for collection %r (mcp)",
                        _EXPLAIN_TIMEOUT_SECONDS,
                        collection,
                    )
                    _enqueue_explain_error(writer, start, "timeout", ErrorKind.timeout)
                    return McpErrorResponse(error="explain timed out", code="timeout")
                routing_block = None
                chosen_collection = collection
            else:
                # Collectionless path — use shared helper
                routing_block, query_vector, error_response = await _build_routing_explain(
                    pipeline=pipeline,
                    query=query,
                    ns=DEFAULT_NAMESPACE,
                    config=_cfg,
                    writer=writer,
                    start=start,
                )
                if error_response is not None:
                    detail = json.loads(error_response.body).get("detail", "error")
                    status_code = error_response.status_code
                    if status_code == 404:
                        code = "not_found"
                    elif status_code == 422:
                        code = "validation_error"
                    elif status_code == 503:
                        code = "service_unavailable"
                    else:
                        code = "internal_error"
                    return McpErrorResponse(error=detail, code=code)

                chosen_collection = routing_block.chosen_collection  # type: ignore[union-attr]
                try:
                    pipeline_result = await asyncio.wait_for(
                        pipeline.explain(
                            query,
                            chosen_collection,
                            top_k=top_k,
                            rerank=rerank,
                            namespace=DEFAULT_NAMESPACE,
                            query_vector=query_vector,
                        ),
                        timeout=_EXPLAIN_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "explain timed out after %.1fs for collection %r (mcp)",
                        _EXPLAIN_TIMEOUT_SECONDS,
                        chosen_collection,
                    )
                    _enqueue_explain_error(writer, start, "timeout", ErrorKind.timeout)
                    return McpErrorResponse(error="explain timed out", code="timeout")

            response = ExplainResponse.from_pipeline_result(
                pipeline_result=pipeline_result,
                collection=chosen_collection,
                rerank=rerank,
                routing=routing_block,
            )

            _enqueue_explain_success(
                writer,
                start,
                TelemetryEntry.from_explain_result(
                    collection=chosen_collection,
                    result_count=len(response.results),
                    latency_ms=(monotonic() - start) * 1000.0,
                ),
            )

            return response.model_dump(mode="json", exclude_none=False)

        except Exception as exc:
            _enqueue_explain_error(writer, start, "internal_error", ErrorKind.other)
            logger.exception("explain failed")
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
) -> Starlette:
    """Return a Starlette HTTP app wrapping the FastMCP server with auth middleware.

    The underlying FastMCP app is exposed via the streamable HTTP transport
    (endpoint: /mcp).  APIKeyMiddleware is added so every request to /mcp
    requires a valid Bearer token; /health remains exempt per _EXEMPT_PATHS.
    """
    fastmcp_app = create_app(pipeline, default_collection, writer=writer, config=config)
    starlette_app: Starlette = fastmcp_app.streamable_http_app()
    api_key, _ = load_or_generate_key()
    starlette_app.add_middleware(APIKeyMiddleware, api_key=api_key, namespaces={})
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

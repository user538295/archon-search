"""FastMCP HTTP server for Archon RAG (FEAT-019 Task 5.1)."""
from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from time import monotonic
from typing import Any

from fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from archon_search.pipeline import SearchPipeline
from archon_search.progress import IndexingState, IndexingStatus
from archon_search.telemetry.entry import TelemetryEntry
from archon_search.telemetry.writer import TelemetryWriter

logger = logging.getLogger("archon.search")


def create_app(
    pipeline: SearchPipeline,
    default_collection: str,
    writer: TelemetryWriter | None = None,
) -> FastMCP:
    """Create a FastMCP app with 9 RAG tools registered."""
    app = FastMCP("archon-search")

    @app.tool()
    async def search(
        query: str,
        collection: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for relevant document chunks using hybrid vector + FTS search."""
        start = monotonic()
        try:
            results = await pipeline.search(query, collection or default_collection)
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_search_tool_result(
                            endpoint="search",
                            collection=collection or default_collection,
                            result_doc_ids=[r.doc_id for r in results],
                            latency_ms=(monotonic() - start) * 1000.0,
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search entry enqueue failed", exc_info=True)
            return [asdict(r) for r in results]
        except Exception as exc:
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_error(
                            endpoint="search",
                            status="internal_error",
                            error_kind="other",
                            latency_ms=(monotonic() - start) * 1000.0,
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search error entry enqueue failed", exc_info=True)
            logger.exception("search failed")
            return [{"error": str(exc)}]

    @app.tool()
    async def search_with_context(
        query: str,
        collection: str | None = None,
        context_window: int = 1,
    ) -> list[dict[str, Any]]:
        """Search and return surrounding chunks for richer context."""
        start = monotonic()
        try:
            results = await pipeline.search_with_context(
                query, collection or default_collection, context_window
            )
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_search_tool_result(
                            endpoint="search_with_context",
                            collection=collection or default_collection,
                            result_doc_ids=[r["result"].doc_id for r in results],
                            latency_ms=(monotonic() - start) * 1000.0,
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search_with_context entry enqueue failed", exc_info=True)
            return [
                {
                    "result": asdict(r["result"]),
                    "context_before": [asdict(c) for c in r["context_before"]],
                    "context_after": [asdict(c) for c in r["context_after"]],
                }
                for r in results
            ]
        except Exception as exc:
            if writer is not None:
                try:
                    writer.enqueue(
                        TelemetryEntry.from_error(
                            endpoint="search_with_context",
                            status="internal_error",
                            error_kind="other",
                            latency_ms=(monotonic() - start) * 1000.0,
                        )
                    )
                except Exception:
                    logger.warning("telemetry: search_with_context error entry enqueue failed", exc_info=True)
            logger.exception("search_with_context failed")
            return [{"error": str(exc)}]

    @app.tool()
    async def ingest_file(
        path: str,
        collection: str | None = None,
    ) -> dict[str, Any]:
        """Ingest a single file into the RAG store."""
        try:
            result = await pipeline.ingest_file(
                Path(path), collection or default_collection
            )
            return asdict(result)
        except Exception as exc:
            logger.exception("ingest_file failed")
            return {"error": str(exc)}

    @app.tool()
    async def ingest_directory(
        path: str,
        glob_pattern: str = "**/*",
        collection: str | None = None,
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        """Ingest all files in a directory into the RAG store."""
        try:
            async def progress_cb(done: int, total: int) -> None:
                if ctx is not None:
                    await ctx.report_progress(done, total)

            results = await pipeline.ingest_directory(
                Path(path),
                collection or default_collection,
                glob_pattern=glob_pattern,
                progress_cb=progress_cb,
            )
            return [asdict(r) for r in results]
        except Exception as exc:
            logger.exception("ingest_directory failed")
            return [{"error": str(exc)}]

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
            return [{"error": str(exc)}]

    @app.tool()
    async def get_collections_meta() -> list[dict[str, Any]]:
        """Return full CollectionMeta for all collections, including centroid vectors."""
        try:
            results = await pipeline.get_all_collections_meta()
            return [asdict(r) for r in results]
        except Exception as exc:
            logger.exception("get_collections_meta failed")
            return [{"error": str(exc)}]

    @app.tool()
    async def get_collection_meta(name: str) -> dict[str, Any]:
        """Return full CollectionMeta for one named collection, including centroid."""
        try:
            meta = await pipeline.get_collection_meta(name)
            if meta is None:
                return {"error": f"Collection {name!r} not found"}
            return asdict(meta)
        except Exception as exc:
            logger.exception("get_collection_meta failed")
            return {"error": str(exc)}

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
            return [{"error": str(exc)}]

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
            return {"error": str(exc)}

    @app.custom_route("/health", methods=["GET"])
    async def health_check(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return app


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

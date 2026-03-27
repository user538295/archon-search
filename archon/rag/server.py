"""FastMCP HTTP server for Archon RAG (FEAT-019 Task 5.1).

Usage:
    python -m archon.rag.server
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp import Context, FastMCP

from archon.rag.pipeline import RagPipeline, create_pipeline

if TYPE_CHECKING:
    pass

logger = logging.getLogger("archon.rag")


def create_app(pipeline: RagPipeline, default_collection: str) -> FastMCP:
    """Create a FastMCP app with 7 RAG tools registered."""
    app = FastMCP("archon-rag")

    @app.tool()
    async def search(
        query: str,
        collection: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for relevant document chunks using hybrid vector + FTS search."""
        try:
            results = await pipeline.search(query, collection or default_collection)
            return [asdict(r) for r in results]
        except Exception as exc:
            logger.exception("search failed")
            return [{"error": str(exc)}]

    @app.tool()
    async def search_with_context(
        query: str,
        collection: str | None = None,
        context_window: int = 1,
    ) -> list[dict[str, Any]]:
        """Search and return surrounding chunks for richer context."""
        try:
            results = await pipeline.search_with_context(
                query, collection or default_collection, context_window
            )
            return [
                {
                    "result": asdict(r["result"]),
                    "context_before": [asdict(c) for c in r["context_before"]],
                    "context_after": [asdict(c) for c in r["context_after"]],
                }
                for r in results
            ]
        except Exception as exc:
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
        """List all document collections with doc/chunk counts."""
        try:
            results = await pipeline.list_collections()
            return [asdict(r) for r in results]
        except Exception as exc:
            logger.exception("list_collections failed")
            return [{"error": str(exc)}]

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

    return app


async def main() -> None:
    """Start the RAG MCP server from config."""
    from pathlib import Path  # noqa: PLC0415

    from archon.config.loader import load_config  # noqa: PLC0415
    from archon.rag.sync import path_to_collection_name  # noqa: PLC0415

    cfg = load_config()
    history_col = path_to_collection_name(
        str(Path(cfg.history.directory).expanduser() / "sessions")
    )
    pipeline = create_pipeline(cfg.rag)
    await pipeline.store.connect()

    app = create_app(pipeline, history_col)

    try:
        await app.run_http_async(host=cfg.rag.host, port=cfg.rag.port)
    finally:
        await pipeline.store.disconnect()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

"""FastMCP HTTP server for Archon RAG (FEAT-019 Task 5.1).

Usage:
    python -m archon.search.server
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from archon.search.pipeline import SearchPipeline, create_pipeline
from archon.search.progress import IndexingStateStore
from archon.search.sync import SearchCollectionSync, path_to_collection_name

if TYPE_CHECKING:
    pass

logger = logging.getLogger("archon.search")


def create_app(pipeline: SearchPipeline, default_collection: str) -> FastMCP:
    """Create a FastMCP app with 9 RAG tools registered."""
    app = FastMCP("archon-search")

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


async def main() -> None:
    """Start the RAG MCP server from config."""
    import asyncio  # noqa: PLC0415

    from archon.config.loader import load_config  # noqa: PLC0415

    cfg = load_config(require_token=False)
    history_col = path_to_collection_name(
        str(Path(cfg.history.directory).expanduser() / "sessions")
    )
    pipeline = create_pipeline(cfg.search)
    await pipeline.store.connect()

    # Startup sync
    state_store = IndexingStateStore(Path(cfg.search.db_path).expanduser())
    sync = SearchCollectionSync(
        pipeline,
        state_store=state_store,
        pinned_collections=cfg.search.pinned_collections,
        embedding_model=cfg.search.embedding_model,
        chunk_size=cfg.search.chunk_size,
        auto_reindex_on_chunk_size_change=cfg.search.auto_reindex_on_chunk_size_change,
    )
    existing_state = state_store.read()
    if existing_state is None or not existing_state.collections:
        try:
            state_store.set_trigger("install")
        except Exception as exc:
            logger.warning("Startup sync: failed to write install trigger (notification may not fire): %s", exc)
    def _log_sync_error(task: asyncio.Task) -> None:
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            logger.error("Background startup sync failed: %s", exc, exc_info=exc)

    sync_timeout = cfg.search.sync_timeout_seconds
    if sync_timeout == 0:
        task = asyncio.create_task(sync.sync(cfg.search.collections))
        task.add_done_callback(_log_sync_error)
        logger.info("Startup sync deferred to background task (sync_timeout_seconds=0).")
    else:
        try:
            result = await asyncio.wait_for(sync.sync(cfg.search.collections), timeout=sync_timeout)
            logger.info(
                "Startup sync complete: %d added, %d removed, %d unchanged, %d errors.",
                len(result.added), len(result.removed), len(result.unchanged), len(result.errors),
            )
            if result.errors:
                logger.warning("Startup sync errors: %s", result.errors)
        except asyncio.TimeoutError:
            logger.warning(
                "Startup sync timed out after %ds — continuing in background.", sync_timeout
            )
            task = asyncio.create_task(sync.sync(cfg.search.collections))
            task.add_done_callback(_log_sync_error)

    watcher_manager = None
    if cfg.search.watch:
        from archon.search.watcher import WatcherManager  # lazy import — watchdog may not be installed
        desired = sync.build_desired(cfg.search.collections)
        loop = asyncio.get_running_loop()

        async def _on_change(col_name: str) -> None:
            path_str = desired.get(col_name)
            if path_str:
                try:
                    await sync.sync_collection(col_name, Path(path_str))
                except Exception as exc:
                    logger.error("Watch-triggered sync for %r raised: %r", col_name, exc)

        watcher_manager = WatcherManager(on_change=_on_change, loop=loop)
        for col_name, path_str in desired.items():
            watcher_manager.add(col_name, Path(path_str))
        logger.info(
            "Watch mode active: monitoring %d collection(s) for file changes",
            len(desired),
        )

    app = create_app(pipeline, history_col)

    try:
        await app.run_http_async(host=cfg.search.host, port=cfg.search.port)
    finally:
        if watcher_manager is not None:
            try:
                await watcher_manager.stop_all()
            except Exception:
                logger.exception("Error stopping watcher manager")
        await pipeline.store.disconnect()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

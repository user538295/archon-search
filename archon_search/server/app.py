"""FastAPI app factory for archon-search REST control plane."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI

from archon_search.config import SearchConfig
from archon_search.embedder import Embedder, ModelEmbedder
from archon_search.jobs.store import JobStore
from archon_search.progress import IndexingStateStore
from archon_search.server.routes_collections import router as collections_router
from archon_search.server.routes_health import router as health_router
from archon_search.server.routes_jobs import router as jobs_router
from archon_search.server.routes_route import router as route_router
from archon_search.server.routes_state import router as state_router
from archon_search.server.routes_status import router as status_router

logger = logging.getLogger("archon-search")


def create_app(
    config: SearchConfig,
    job_store: JobStore,
    config_path: Path | str | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application instance."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        yield
        # Shutdown: cancel all in-flight background tasks
        tasks = list(app.state._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="archon-search", lifespan=lifespan)
    app.state.config = config
    app.state.job_store = job_store
    app.state.config_path = Path(config_path) if config_path is not None else None
    app.state._background_tasks: set = set()
    app.state.state_store = IndexingStateStore(config.db_path)
    app.state.search_store = None
    app.state.embedder = Embedder(ModelEmbedder(config.embedding_model, providers=config.providers or None))
    app.include_router(collections_router)
    app.include_router(health_router)
    app.include_router(jobs_router)
    app.include_router(status_router)
    app.include_router(state_router)
    app.include_router(route_router)
    return app


def run_server(config: SearchConfig) -> None:
    """Create JobStore, build the app, and start the uvicorn server."""
    job_store = JobStore()
    app = create_app(config, job_store)
    uvicorn.run(app, host=config.host, port=config.port)

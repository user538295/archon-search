"""FastAPI app factory for archon-search REST control plane."""
from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.routes_health import router as health_router

logger = logging.getLogger("archon-search")


def create_app(config: SearchConfig, job_store: JobStore) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    app = FastAPI(title="archon-search")
    app.state.config = config
    app.state.job_store = job_store
    app.include_router(health_router)
    return app


def run_server(config: SearchConfig) -> None:
    """Create JobStore, build the app, and start the uvicorn server."""
    job_store = JobStore()
    app = create_app(config, job_store)
    uvicorn.run(app, host=config.host, port=config.port)

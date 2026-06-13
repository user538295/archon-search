"""Integration tests for JobScheduler lifespan wiring — Task 3.2."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.scheduler import JobScheduler
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.store import SearchStore
from archon_search.types import ExportJob, ImportJob


def _noop_dispatch(job: ExportJob | ImportJob) -> None:
    pass


def _make_config(tmp_path: Path) -> SearchConfig:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    return cfg


def _make_job_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def _store_patches():
    return (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "migrate_namespace", new=AsyncMock()),
        patch.object(SearchStore, "migrate_description_embedding", new=AsyncMock()),
        patch.object(SearchStore, "migrate_acl", new=AsyncMock()),
        patch.object(SearchStore, "migrate_centroid_sum", new=AsyncMock()),
        patch.object(SearchStore, "migrate_per_collection_model", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    )


# ---------------------------------------------------------------------------
# test_scheduler_starts_with_app
# ---------------------------------------------------------------------------


def test_scheduler_starts_with_app(tmp_path: Path) -> None:
    """create_app(scheduler=scheduler_instance) lifespan starts the scheduler;
    scheduler.active_count == 0 initially."""
    cfg = _make_config(tmp_path)
    job_store = _make_job_store(tmp_path)
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=1,
        dispatch_fn=_noop_dispatch,
    )

    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "migrate_namespace", new=AsyncMock()),
        patch.object(SearchStore, "migrate_description_embedding", new=AsyncMock()),
        patch.object(SearchStore, "migrate_acl", new=AsyncMock()),
        patch.object(SearchStore, "migrate_centroid_sum", new=AsyncMock()),
        patch.object(SearchStore, "migrate_per_collection_model", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store, scheduler=scheduler)
        with TestClient(app):
            # Scheduler should be stored in app state
            assert app.state.scheduler is scheduler
            # No tasks dispatched yet — active_count is 0
            assert scheduler.active_count == 0


# ---------------------------------------------------------------------------
# test_scheduler_cancelled_on_shutdown
# ---------------------------------------------------------------------------


def test_scheduler_cancelled_on_shutdown(tmp_path: Path) -> None:
    """Lifespan exit cancels the scheduler task cleanly (no CancelledError propagates)."""
    cfg = _make_config(tmp_path)
    job_store = _make_job_store(tmp_path)
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=1,
        dispatch_fn=_noop_dispatch,
    )

    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "migrate_namespace", new=AsyncMock()),
        patch.object(SearchStore, "migrate_description_embedding", new=AsyncMock()),
        patch.object(SearchStore, "migrate_acl", new=AsyncMock()),
        patch.object(SearchStore, "migrate_centroid_sum", new=AsyncMock()),
        patch.object(SearchStore, "migrate_per_collection_model", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store, scheduler=scheduler)
        # Use TestClient as context manager — lifespan runs startup/shutdown
        with TestClient(app):
            pass  # shutdown happens on __exit__; must not raise


# ---------------------------------------------------------------------------
# test_create_app_without_scheduler_sets_none
# ---------------------------------------------------------------------------


def test_create_app_without_scheduler_sets_none(tmp_path: Path) -> None:
    """create_app() without scheduler argument sets app.state.scheduler to None."""
    cfg = _make_config(tmp_path)
    job_store = _make_job_store(tmp_path)

    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "migrate_namespace", new=AsyncMock()),
        patch.object(SearchStore, "migrate_description_embedding", new=AsyncMock()),
        patch.object(SearchStore, "migrate_acl", new=AsyncMock()),
        patch.object(SearchStore, "migrate_centroid_sum", new=AsyncMock()),
        patch.object(SearchStore, "migrate_per_collection_model", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store)
        with TestClient(app):
            assert app.state.scheduler is None

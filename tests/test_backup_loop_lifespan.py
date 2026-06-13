"""Integration tests for BackupLoop lifespan wiring — Task 3.2."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.backup_loop import BackupLoop
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
    cfg.backup.output_dir = str(tmp_path / "backups")
    return cfg


def _make_job_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def test_backup_loop_stored_on_app_state(tmp_path: Path) -> None:
    """After create_app() startup, app.state.backup_loop is a BackupLoop instance."""
    cfg = _make_config(tmp_path)
    cfg.backup.interval_hours = 1
    job_store = _make_job_store(tmp_path)
    scheduler = JobScheduler(
        store=job_store, max_concurrent=1, dispatch_fn=_noop_dispatch
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
            assert isinstance(app.state.backup_loop, BackupLoop)


def test_backup_loop_is_running_as_background_task(tmp_path: Path) -> None:
    """A task running backup_loop.run() is in app.state._background_tasks."""
    cfg = _make_config(tmp_path)
    cfg.backup.interval_hours = 1
    job_store = _make_job_store(tmp_path)
    scheduler = JobScheduler(
        store=job_store, max_concurrent=1, dispatch_fn=_noop_dispatch
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
            tasks = app.state._background_tasks
            # At least one task should be non-done (the backup loop)
            non_done = [t for t in tasks if not t.done()]
            assert len(non_done) >= 1, "expected at least one running background task"


def test_backup_loop_cancelled_on_shutdown(tmp_path: Path) -> None:
    """Lifespan shutdown cancels the backup_loop task without error."""
    cfg = _make_config(tmp_path)
    cfg.backup.interval_hours = 1
    job_store = _make_job_store(tmp_path)
    scheduler = JobScheduler(
        store=job_store, max_concurrent=1, dispatch_fn=_noop_dispatch
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
            pass  # exit triggers shutdown — must not raise


def test_backup_loop_disabled_when_interval_zero(tmp_path: Path) -> None:
    """With interval_hours=0, BackupLoop is still present and started; the
    trigger loop self-exits but the completion loop keeps running."""
    cfg = _make_config(tmp_path)
    cfg.backup.interval_hours = 0
    job_store = _make_job_store(tmp_path)
    scheduler = JobScheduler(
        store=job_store, max_concurrent=1, dispatch_fn=_noop_dispatch
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
            assert isinstance(app.state.backup_loop, BackupLoop)
            # No backup jobs should have been enqueued
            queued = job_store.list_queued_bulk()
            backup_jobs = [j for j in queued if getattr(j, "source", "user") == "backup"]
            assert backup_jobs == []

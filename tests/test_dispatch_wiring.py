"""Tests for the real dispatch closure wiring in create_app() lifespan — Task 2.1.

These tests verify that:
1. ``JobScheduler.dispatch_fn`` is a public, reassignable attribute (was ``_dispatch_fn``).
2. After ``create_app()`` lifespan startup, ``scheduler.dispatch_fn`` is replaced
   with a real closure (not the placeholder/no-op).
3. Invoking the real dispatch closure schedules ``_export_task`` /
   ``_import_task`` as asyncio tasks and registers them with the scheduler.

The previous behaviour — a no-op closure that immediately marked every job
FAILED with ``workers_not_deployed`` — is now gone; we assert its absence.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.scheduler import JobScheduler
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.store import SearchStore
from archon_search.types import ExportJob, ImportJob, JobStatus


def _placeholder_dispatch(job: ExportJob | ImportJob) -> None:  # pragma: no cover - never called
    raise AssertionError("placeholder dispatch should have been replaced by lifespan")


def _make_config(tmp_path: Path) -> SearchConfig:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    return cfg


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
# Scheduler attribute reassignability
# ---------------------------------------------------------------------------


def test_scheduler_dispatch_fn_is_public_reassignable_attribute(tmp_path: Path) -> None:
    """JobScheduler.dispatch_fn must be a public attribute that can be reassigned
    after construction (Task 2.1 requirement so the lifespan can install the
    real closure once app state is ready)."""
    store = JobStore(path=tmp_path / "jobs.json")
    sched = JobScheduler(store=store, max_concurrent=1, dispatch_fn=_placeholder_dispatch)

    assert hasattr(sched, "dispatch_fn"), "dispatch_fn must be public, not _dispatch_fn"
    assert sched.dispatch_fn is _placeholder_dispatch

    calls: list = []

    def new_dispatch(job: ExportJob | ImportJob) -> None:
        calls.append(job)

    sched.dispatch_fn = new_dispatch
    assert sched.dispatch_fn is new_dispatch


def test_scheduler_tick_uses_reassigned_dispatch_fn(tmp_path: Path) -> None:
    """Reassigning scheduler.dispatch_fn after construction must take effect
    immediately — _tick() must read the attribute, not a closed-over local."""
    store = JobStore(path=tmp_path / "jobs.json")
    sched = JobScheduler(store=store, max_concurrent=1, dispatch_fn=_placeholder_dispatch)

    calls: list = []

    def real_dispatch(job: ExportJob | ImportJob) -> None:
        calls.append(job.job_id)

    sched.dispatch_fn = real_dispatch

    # Enqueue a backup-source export job so list_queued_bulk returns it.
    job = store.create_export(
        collection="docs",
        output_path=str(tmp_path / "out.tar.gz"),
        tmp_path=str(tmp_path / "out.tmp"),
    )

    # Run a single tick — must invoke the newly assigned dispatch_fn.
    sched._tick()
    assert calls == [job.job_id]


# ---------------------------------------------------------------------------
# Lifespan replaces the placeholder with the real closure
# ---------------------------------------------------------------------------


def test_scheduler_dispatch_fn_is_real_after_lifespan(tmp_path: Path) -> None:
    """After create_app() lifespan startup completes, scheduler.dispatch_fn
    is no longer the placeholder — it's the real closure built inside lifespan."""
    cfg = _make_config(tmp_path)
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store, max_concurrent=1, dispatch_fn=_placeholder_dispatch,
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
            assert scheduler.dispatch_fn is not _placeholder_dispatch
            # Must be callable
            assert callable(scheduler.dispatch_fn)


# ---------------------------------------------------------------------------
# Real dispatch invokes _export_task / _import_task
# ---------------------------------------------------------------------------


def test_real_dispatch_invokes_export_task_for_export_job(tmp_path: Path) -> None:
    """The real dispatch closure, when called with an ExportJob, must create an
    asyncio.Task that runs _export_task(job, job_store, search_store, config)
    and register it with the scheduler."""
    cfg = _make_config(tmp_path)
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store, max_concurrent=1, dispatch_fn=_placeholder_dispatch,
    )

    captured: dict = {}

    async def fake_export_task(job, store, search_store, config):  # type: ignore[no-untyped-def]
        captured["export"] = (job, store, search_store, config)

    async def fake_import_task(*args, **kwargs):  # type: ignore[no-untyped-def]  # pragma: no cover
        pass

    with (
        patch("archon_search.server.routes_export._export_task", new=fake_export_task),
        patch("archon_search.server.routes_export._import_task", new=fake_import_task),
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
            export_job = job_store.create_export(
                collection="docs",
                output_path=str(tmp_path / "out.tar.gz"),
                tmp_path=str(tmp_path / "out.tmp"),
            )

            # Drive the dispatch on the running event loop and let the task run.
            async def _run() -> None:
                scheduler.dispatch_fn(export_job)
                # Yield so the scheduled task can execute.
                await asyncio.sleep(0)
                await asyncio.sleep(0)

            # The lifespan owns the loop via TestClient; use portal pattern.
            from anyio.from_thread import start_blocking_portal
            with start_blocking_portal() as portal:
                portal.call(_run)

    assert "export" in captured, "real dispatch did not invoke _export_task"
    job_arg, store_arg, search_store_arg, config_arg = captured["export"]
    assert job_arg is export_job
    assert store_arg is job_store
    assert config_arg is cfg


def test_real_dispatch_invokes_import_task_for_import_job(tmp_path: Path) -> None:
    """The real dispatch closure, when called with an ImportJob, must create an
    asyncio.Task that runs _import_task(job, job_store, search_store, pipeline,
    embedder_cache, config)."""
    cfg = _make_config(tmp_path)
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store, max_concurrent=1, dispatch_fn=_placeholder_dispatch,
    )

    captured: dict = {}

    async def fake_export_task(*args, **kwargs):  # type: ignore[no-untyped-def]  # pragma: no cover
        pass

    async def fake_import_task(job, store, search_store, pipeline, embedder_cache, config):  # type: ignore[no-untyped-def]
        captured["import"] = (job, store, search_store, pipeline, embedder_cache, config)

    with (
        patch("archon_search.server.routes_export._export_task", new=fake_export_task),
        patch("archon_search.server.routes_export._import_task", new=fake_import_task),
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
            import_job = job_store.create_import(
                collection="docs",
                archive_path=str(tmp_path / "in.tar.gz"),
                force_overwrite=False,
                ignore_schema_version=False,
                on_error="skip",
            )

            async def _run() -> None:
                scheduler.dispatch_fn(import_job)
                await asyncio.sleep(0)
                await asyncio.sleep(0)

            from anyio.from_thread import start_blocking_portal
            with start_blocking_portal() as portal:
                portal.call(_run)

    assert "import" in captured, "real dispatch did not invoke _import_task"
    job_arg, store_arg, search_store_arg, pipeline_arg, embedder_cache_arg, config_arg = captured["import"]
    assert job_arg is import_job
    assert store_arg is job_store
    assert config_arg is cfg
    assert pipeline_arg is app.state.pipeline
    assert embedder_cache_arg is app.state.embedder_cache


# ---------------------------------------------------------------------------
# Real dispatch registers the created task with the scheduler
# ---------------------------------------------------------------------------


def test_real_dispatch_registers_task_with_scheduler(tmp_path: Path) -> None:
    """The real dispatch closure must call scheduler.register_task(task) so the
    scheduler can track active concurrency and not over-dispatch."""
    cfg = _make_config(tmp_path)
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store, max_concurrent=1, dispatch_fn=_placeholder_dispatch,
    )

    async def fake_export_task(job, store, search_store, config):  # type: ignore[no-untyped-def]
        # Stay alive long enough to be observable as an active task.
        await asyncio.sleep(0.05)

    async def fake_import_task(*args, **kwargs):  # type: ignore[no-untyped-def]  # pragma: no cover
        pass

    with (
        patch("archon_search.server.routes_export._export_task", new=fake_export_task),
        patch("archon_search.server.routes_export._import_task", new=fake_import_task),
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
            export_job = job_store.create_export(
                collection="docs",
                output_path=str(tmp_path / "out.tar.gz"),
                tmp_path=str(tmp_path / "out.tmp"),
            )

            assert scheduler.active_count == 0

            async def _run() -> int:
                scheduler.dispatch_fn(export_job)
                # Observe active_count BEFORE the task finishes.
                return scheduler.active_count

            from anyio.from_thread import start_blocking_portal
            with start_blocking_portal() as portal:
                count_after_dispatch = portal.call(_run)

    assert count_after_dispatch == 1, (
        "real dispatch did not register the created task with the scheduler"
    )


def test_real_dispatch_raises_typeerror_for_unsupported_job_type(tmp_path: Path) -> None:
    """The real dispatch closure must reject job types it cannot route, so a
    future bulk job type isn't silently routed to _import_task."""
    cfg = _make_config(tmp_path)
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store, max_concurrent=1, dispatch_fn=_placeholder_dispatch,
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
            import pytest  # local import to avoid top-level dependency churn
            sentinel = MagicMock(name="UnknownJob")

            async def _run() -> None:
                with pytest.raises(TypeError, match="unsupported job type"):
                    scheduler.dispatch_fn(sentinel)

            from anyio.from_thread import start_blocking_portal
            with start_blocking_portal() as portal:
                portal.call(_run)


# ---------------------------------------------------------------------------
# Confirm the no-op placeholder is gone
# ---------------------------------------------------------------------------


def test_workers_not_deployed_error_is_gone(tmp_path: Path) -> None:
    """The previous no-op closure marked dispatched jobs FAILED with
    error="workers_not_deployed". After Task 2.1 the lifespan installs a real
    dispatch, so a dispatched export job must NOT be marked FAILED with that
    sentinel string."""
    cfg = _make_config(tmp_path)
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store, max_concurrent=1, dispatch_fn=_placeholder_dispatch,
    )

    async def fake_export_task(job, store, search_store, config):  # type: ignore[no-untyped-def]
        # Simulate a successful export — mark DONE.
        store.update(job.job_id, status=JobStatus.DONE)

    async def fake_import_task(*args, **kwargs):  # type: ignore[no-untyped-def]  # pragma: no cover
        pass

    with (
        patch("archon_search.server.routes_export._export_task", new=fake_export_task),
        patch("archon_search.server.routes_export._import_task", new=fake_import_task),
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
            export_job = job_store.create_export(
                collection="docs",
                output_path=str(tmp_path / "out.tar.gz"),
                tmp_path=str(tmp_path / "out.tmp"),
            )

            async def _run() -> None:
                scheduler.dispatch_fn(export_job)
                await asyncio.sleep(0)
                await asyncio.sleep(0)

            from anyio.from_thread import start_blocking_portal
            with start_blocking_portal() as portal:
                portal.call(_run)

            final = job_store.get(export_job.job_id)
            assert final is not None
            assert final.error != "workers_not_deployed"
            assert final.status == JobStatus.DONE

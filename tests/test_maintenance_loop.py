"""Tests for MaintenanceLoop skeleton (BE-2) and FTS optimize policy (BE-5).

Plan: Documentation/Backlog/D5-maintenance-jobs-policies-team-plan.md Tasks BE-2, BE-5.

TDD: tests written first, then MaintenanceLoop implementation in
archon_search/jobs/maintenance_loop.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search._types import CollectionInfo
from archon_search.config import MaintenanceConfig
from archon_search.jobs.maintenance_loop import MaintenanceLoop
from archon_search.store import FTSIndexNotFoundError


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_collection_info(
    name: str,
    namespace: str = "default",
    doc_count: int = 1,
    chunk_count: int = 5,
) -> CollectionInfo:
    return CollectionInfo(
        name=name,
        doc_count=doc_count,
        chunk_count=chunk_count,
        namespace=namespace,
    )


def _make_loop(
    tmp_path: Path,
    *,
    interval_hours: int = 0,
    fts_optimize: bool = True,
    orphan_cleanup: bool = True,
    failed_ingest_retry: bool = True,
    retry_max_attempts: int = 3,
    retry_max_age_hours: int = 72,
    exclude: list[str] | None = None,
    job_store: Any = None,
    search_store: Any = None,
) -> MaintenanceLoop:
    cfg = MaintenanceConfig(
        interval_hours=interval_hours,
        fts_optimize=fts_optimize,
        orphan_cleanup=orphan_cleanup,
        failed_ingest_retry=failed_ingest_retry,
        retry_max_attempts=retry_max_attempts,
        retry_max_age_hours=retry_max_age_hours,
        exclude=exclude or [],
    )
    js = job_store if job_store is not None else MagicMock()
    ss = search_store if search_store is not None else MagicMock()
    return MaintenanceLoop(job_store=js, search_store=ss, config=cfg, data_dir=tmp_path)


# ---------------------------------------------------------------------------
# Trigger loop tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_loop_waits_on_event_indefinitely(tmp_path: Path) -> None:
    """S1: interval_hours=0 — loop waits on _trigger_event without firing _run_one_pass."""
    loop = _make_loop(tmp_path, interval_hours=0)

    run_one_pass_calls: list[None] = []

    async def _fake_run_one_pass() -> None:
        run_one_pass_calls.append(None)

    loop._run_one_pass = _fake_run_one_pass  # type: ignore[method-assign]

    # The trigger loop should block on _trigger_event.wait() without calling _run_one_pass.
    # Cancel after a short time to prove the loop is alive but not firing passes.
    task = asyncio.create_task(loop._trigger_loop())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
    except asyncio.TimeoutError:
        pass  # expected — loop is alive waiting on the event

    # Without setting the event, no pass should have fired.
    assert run_one_pass_calls == []

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_trigger_loop_fires_on_event(tmp_path: Path) -> None:
    """S2: setting _trigger_event causes a pass to run immediately."""
    loop = _make_loop(tmp_path, interval_hours=0)

    pass_done = asyncio.Event()

    async def _fake_run_one_pass() -> None:
        pass_done.set()

    loop._run_one_pass = _fake_run_one_pass  # type: ignore[method-assign]

    task = asyncio.create_task(loop._trigger_loop())
    # Let the loop settle into its wait.
    await asyncio.sleep(0)

    # Trigger a pass.
    loop._trigger_event.set()

    await asyncio.wait_for(pass_done.wait(), timeout=2.0)
    assert pass_done.is_set()

    # Give the loop a tick to clear the event after the pass.
    await asyncio.sleep(0.05)
    assert not loop._trigger_event.is_set()

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_trigger_loop_fires_on_interval_timeout(tmp_path: Path) -> None:
    """S2: with interval_hours>0, asyncio.wait_for timeout causes _run_one_pass."""
    import archon_search.jobs.maintenance_loop as ml_mod

    loop = _make_loop(tmp_path, interval_hours=1)

    pass_called = asyncio.Event()

    async def _fake_run_one_pass() -> None:
        pass_called.set()

    loop._run_one_pass = _fake_run_one_pass  # type: ignore[method-assign]

    with patch.object(ml_mod, "_SECONDS_PER_HOUR", 0.05):
        task = asyncio.create_task(loop._trigger_loop())
        await asyncio.wait_for(pass_called.wait(), timeout=2.0)

    assert pass_called.is_set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# State file tests
# ---------------------------------------------------------------------------


def test_load_state_missing_file_returns_empty(tmp_path: Path) -> None:
    """S3: missing .maintenance-state.json returns empty state dict."""
    loop = _make_loop(tmp_path)
    state = loop._load_state()
    assert state == {
        "last_run_at": None,
        "next_run_at": None,
        "collection_health": {},
        "retry_counts": {},
        "last_expired_pruned_at": None,
        "last_graph_gc_at": None,
        "stale_mention_count": 0,
    }


def test_load_state_corrupt_file_returns_empty_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """S4: corrupt .maintenance-state.json returns empty state and logs WARNING."""
    state_file = tmp_path / ".maintenance-state.json"
    state_file.write_text("{{NOT VALID JSON", encoding="utf-8")

    loop = _make_loop(tmp_path)
    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        state = loop._load_state()

    assert state == {
        "last_run_at": None,
        "next_run_at": None,
        "collection_health": {},
        "retry_counts": {},
        "last_expired_pruned_at": None,
        "last_graph_gc_at": None,
        "stale_mention_count": 0,
    }
    assert any("WARNING" in r.levelname or r.levelno >= logging.WARNING for r in caplog.records)


def test_save_state_writes_atomically(tmp_path: Path) -> None:
    """C3: state file is written atomically — no partial writes."""
    loop = _make_loop(tmp_path)
    state = {
        "last_run_at": "2025-01-01T00:00:00+00:00",
        "next_run_at": None,
        "collection_health": {},
        "retry_counts": {},
    }
    loop._save_state(state)

    state_file = tmp_path / ".maintenance-state.json"
    assert state_file.exists()
    # No temp file should remain
    tmp_file = tmp_path / ".maintenance-state.json.tmp"
    assert not tmp_file.exists()

    loaded = json.loads(state_file.read_text(encoding="utf-8"))
    assert loaded["last_run_at"] == "2025-01-01T00:00:00+00:00"


def test_save_state_conforms_to_c3_schema(tmp_path: Path) -> None:
    """C3: state file has exactly top-level keys: last_run_at, next_run_at, collection_health, retry_counts."""
    loop = _make_loop(tmp_path)
    now_str = datetime.now(timezone.utc).isoformat()
    health = {
        "default/docs": {
            "fts_optimized_at": now_str,
            "orphans_removed_last_run": 0,
            "last_retry_at": None,
            "last_error": None,
            "meta_chunk_count": 5,
        }
    }
    state = {
        "last_run_at": now_str,
        "next_run_at": None,
        "collection_health": health,
        "retry_counts": {"default/docs/some/file.txt": 1},
    }
    loop._save_state(state)

    state_file = tmp_path / ".maintenance-state.json"
    loaded = json.loads(state_file.read_text(encoding="utf-8"))

    assert set(loaded.keys()) == {"last_run_at", "next_run_at", "collection_health", "retry_counts"}
    assert isinstance(loaded["collection_health"], dict)
    # The key is {ns}/{col}
    assert "default/docs" in loaded["collection_health"]
    assert isinstance(loaded["retry_counts"], dict)


# ---------------------------------------------------------------------------
# _run_one_pass tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_one_pass_no_collections(tmp_path: Path) -> None:
    """No collections → _run_one_pass completes without error; state written with empty collection_health."""
    ss = AsyncMock()
    ss.list_collections = AsyncMock(return_value=[])
    loop = _make_loop(tmp_path, search_store=ss)

    # Stub out per-collection and pass-level policies
    loop._run_fts_optimize = AsyncMock()  # type: ignore[method-assign]
    loop._run_orphan_cleanup = AsyncMock()  # type: ignore[method-assign]
    loop._run_expired_chunk_pruning = AsyncMock()  # type: ignore[method-assign]
    loop._run_graph_gc = AsyncMock(return_value=(0, None))  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = AsyncMock()  # type: ignore[method-assign]

    await loop._run_one_pass()

    state_file = tmp_path / ".maintenance-state.json"
    assert state_file.exists()
    loaded = json.loads(state_file.read_text(encoding="utf-8"))
    assert loaded["collection_health"] == {}
    loop._run_fts_optimize.assert_not_called()
    loop._run_orphan_cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_run_one_pass_get_collection_meta_returns_none(tmp_path: Path) -> None:
    """If get_collection_meta returns None, meta_chunk_count=0; no exception."""
    ss = AsyncMock()
    info = _make_collection_info("docs", namespace="default")
    ss.list_collections = AsyncMock(return_value=[info])
    ss.get_collection_meta = AsyncMock(return_value=None)

    loop = _make_loop(tmp_path, search_store=ss)
    loop._run_fts_optimize = AsyncMock()  # type: ignore[method-assign]
    loop._run_orphan_cleanup = AsyncMock()  # type: ignore[method-assign]
    loop._run_expired_chunk_pruning = AsyncMock()  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = AsyncMock()  # type: ignore[method-assign]

    await loop._run_one_pass()

    state_file = tmp_path / ".maintenance-state.json"
    loaded = json.loads(state_file.read_text(encoding="utf-8"))
    health = loaded["collection_health"].get("default/docs", {})
    assert health.get("meta_chunk_count") in (0, None)
    # Must not propagate exception


@pytest.mark.asyncio
async def test_exclude_exact_ns_col(tmp_path: Path) -> None:
    """S23: exclude pattern 'ns1/col-a' skips exactly that collection."""
    ss = AsyncMock()
    info_a = _make_collection_info("col-a", namespace="ns1")
    info_b = _make_collection_info("col-b", namespace="ns1")
    ss.list_collections = AsyncMock(return_value=[info_a, info_b])
    ss.get_collection_meta = AsyncMock(return_value=None)

    loop = _make_loop(tmp_path, search_store=ss, exclude=["ns1/col-a"])
    processed: list[str] = []

    async def _fake_fts_optimize(col: str, ns: str) -> None:
        processed.append(f"{ns}/{col}")

    loop._run_fts_optimize = _fake_fts_optimize  # type: ignore[method-assign]
    loop._run_orphan_cleanup = AsyncMock()  # type: ignore[method-assign]
    loop._run_expired_chunk_pruning = AsyncMock()  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = AsyncMock()  # type: ignore[method-assign]

    await loop._run_one_pass()

    assert "ns1/col-a" not in processed
    assert "ns1/col-b" in processed


@pytest.mark.asyncio
async def test_exclude_bare_col_all_namespaces(tmp_path: Path) -> None:
    """S24: bare exclude pattern 'col-a' skips all collections named 'col-a' in any namespace."""
    ss = AsyncMock()
    info_ns1 = _make_collection_info("col-a", namespace="ns1")
    info_ns2 = _make_collection_info("col-a", namespace="ns2")
    info_b = _make_collection_info("col-b", namespace="ns1")
    ss.list_collections = AsyncMock(return_value=[info_ns1, info_ns2, info_b])
    ss.get_collection_meta = AsyncMock(return_value=None)

    loop = _make_loop(tmp_path, search_store=ss, exclude=["col-a"])
    processed: list[str] = []

    async def _fake_fts_optimize(col: str, ns: str) -> None:
        processed.append(f"{ns}/{col}")

    loop._run_fts_optimize = _fake_fts_optimize  # type: ignore[method-assign]
    loop._run_orphan_cleanup = AsyncMock()  # type: ignore[method-assign]
    loop._run_expired_chunk_pruning = AsyncMock()  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = AsyncMock()  # type: ignore[method-assign]

    await loop._run_one_pass()

    assert "ns1/col-a" not in processed
    assert "ns2/col-a" not in processed
    assert "ns1/col-b" in processed


@pytest.mark.asyncio
async def test_run_one_pass_continues_after_per_collection_exception(tmp_path: Path) -> None:
    """Error in first collection sets last_error; second collection processed normally."""
    ss = AsyncMock()
    info_a = _make_collection_info("col-a", namespace="default")
    info_b = _make_collection_info("col-b", namespace="default")
    ss.list_collections = AsyncMock(return_value=[info_a, info_b])
    ss.get_collection_meta = AsyncMock(return_value=None)

    loop = _make_loop(tmp_path, search_store=ss)
    processed: list[str] = []

    async def _fake_fts_optimize(col: str, ns: str) -> None:
        if col == "col-a":
            raise RuntimeError("boom from col-a")
        processed.append(f"{ns}/{col}")

    loop._run_fts_optimize = _fake_fts_optimize  # type: ignore[method-assign]
    loop._run_orphan_cleanup = AsyncMock()  # type: ignore[method-assign]
    loop._run_expired_chunk_pruning = AsyncMock()  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = AsyncMock()  # type: ignore[method-assign]

    await loop._run_one_pass()  # must not raise

    state_file = tmp_path / ".maintenance-state.json"
    loaded = json.loads(state_file.read_text(encoding="utf-8"))
    health = loaded["collection_health"]
    # col-a should have last_error set
    col_a_health = health.get("default/col-a", {})
    assert col_a_health.get("last_error") is not None
    assert "boom from col-a" in col_a_health["last_error"]
    # col-b should have been processed (fts_optimize succeeded)
    assert "default/col-b" in processed


@pytest.mark.asyncio
async def test_run_one_pass_policy_exception_does_not_abort_other_policies(tmp_path: Path) -> None:
    """Per-policy try/except: if _run_fts_optimize raises, _run_orphan_cleanup still runs,
    and pass-level _run_failed_ingest_retry still runs."""
    ss = AsyncMock()
    info = _make_collection_info("docs", namespace="default")
    ss.list_collections = AsyncMock(return_value=[info])
    ss.get_collection_meta = AsyncMock(return_value=None)

    loop = _make_loop(tmp_path, search_store=ss)

    fts_mock = AsyncMock(side_effect=RuntimeError("fts failed"))
    orphan_mock = AsyncMock()
    prune_mock = AsyncMock()
    retry_mock = AsyncMock()

    loop._run_fts_optimize = fts_mock  # type: ignore[method-assign]
    loop._run_orphan_cleanup = orphan_mock  # type: ignore[method-assign]
    loop._run_expired_chunk_pruning = prune_mock  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = retry_mock  # type: ignore[method-assign]

    await loop._run_one_pass()  # must not raise

    orphan_mock.assert_called_once()
    retry_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Integration test: lifespan wiring
# ---------------------------------------------------------------------------


def test_maintenance_loop_lifespan(tmp_path: Path) -> None:
    """S1, S18: app.state.maintenance_loop is set; task is cancellable; interval_hours=0 still sets the attribute."""
    from unittest.mock import patch

    from starlette.testclient import TestClient

    from archon_search.config import SearchConfig
    from archon_search.jobs.maintenance_loop import MaintenanceLoop
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app
    from archon_search.store import SearchStore
    from archon_search.types import ExportJob, ImportJob

    def _noop_dispatch(job: ExportJob | ImportJob) -> None:
        pass

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.backup.output_dir = str(tmp_path / "backups")
    cfg.maintenance.interval_hours = 0  # disabled — but loop must still be present

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(store=job_store, max_concurrent=1, dispatch_fn=_noop_dispatch)

    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
    ):
        app = create_app(cfg, job_store, scheduler=scheduler)
        with TestClient(app):
            assert hasattr(app.state, "maintenance_loop")
            assert isinstance(app.state.maintenance_loop, MaintenanceLoop)
        # exit must not raise — tasks are cancelled gracefully


# ---------------------------------------------------------------------------
# Additional contract tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_one_pass_no_state_written_on_list_collections_failure(tmp_path: Path) -> None:
    """When list_collections raises, _run_one_pass returns without writing state file."""
    ss = AsyncMock()
    ss.list_collections = AsyncMock(side_effect=RuntimeError("db down"))

    loop = _make_loop(tmp_path, search_store=ss)

    state_file = tmp_path / ".maintenance-state.json"
    assert not state_file.exists()  # precondition

    await loop._run_one_pass()  # must not raise

    # State file must NOT be written on early return
    assert not state_file.exists()


@pytest.mark.asyncio
async def test_run_one_pass_health_entry_conforms_to_c3_schema(tmp_path: Path) -> None:
    """Health entry keys written by _run_one_pass match the C3 contract exactly."""
    ss = AsyncMock()
    info = _make_collection_info("docs", namespace="default")
    ss.list_collections = AsyncMock(return_value=[info])
    ss.get_collection_meta = AsyncMock(return_value=None)

    loop = _make_loop(tmp_path, search_store=ss)
    loop._run_fts_optimize = AsyncMock()  # type: ignore[method-assign]
    loop._run_orphan_cleanup = AsyncMock()  # type: ignore[method-assign]
    loop._run_expired_chunk_pruning = AsyncMock()  # type: ignore[method-assign]
    loop._run_graph_gc = AsyncMock(return_value=(0, None))  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = AsyncMock()  # type: ignore[method-assign]

    await loop._run_one_pass()

    state_file = tmp_path / ".maintenance-state.json"
    loaded = json.loads(state_file.read_text(encoding="utf-8"))
    health_entry = loaded["collection_health"]["default/docs"]
    expected_keys = {
        "fts_optimized_at",
        "orphans_removed_last_run",
        "last_retry_at",
        "last_error",
        "meta_chunk_count",
        "mutations_since_recompute",
        "expired_chunks_removed_last_run",
        "communities_invalidated",
    }
    assert set(health_entry.keys()) == expected_keys


def test_trigger_event_is_accessible(tmp_path: Path) -> None:
    """_trigger_event is an asyncio.Event accessible for route handlers."""
    loop = _make_loop(tmp_path)
    assert isinstance(loop._trigger_event, asyncio.Event)
    assert not loop._trigger_event.is_set()


@pytest.mark.asyncio
async def test_run_is_cancellable(tmp_path: Path) -> None:
    """run() delegates to _trigger_loop() and is cancellable via task.cancel()."""
    loop = _make_loop(tmp_path, interval_hours=0)

    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0)  # let the task start

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass  # expected
    assert task.done()


# ---------------------------------------------------------------------------
# BE-5: FTS optimize policy tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fts_optimize_happy_path(tmp_path: Path) -> None:
    """S5: _run_fts_optimize calls optimize_fts and updates fts_optimized_at in health."""
    ss = AsyncMock()
    ss.optimize_fts = AsyncMock()
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)

    loop = _make_loop(tmp_path, fts_optimize=True, search_store=ss)

    # Inject a health dict to be mutated by _run_fts_optimize.
    health: dict[str, Any] = {"fts_optimized_at": None, "orphans_removed_last_run": 0, "last_retry_at": None, "last_error": None, "meta_chunk_count": 0}
    loop._current_health = health  # type: ignore[attr-defined]

    async def _check_lock_held(*args: Any, **kwargs: Any) -> None:
        assert lock.locked()

    ss.optimize_fts = AsyncMock(side_effect=_check_lock_held)

    await loop._run_fts_optimize("docs", "default")

    ss.optimize_fts.assert_called_once_with("docs")
    assert health["fts_optimized_at"] is not None
    assert not lock.locked()  # lock must be released


@pytest.mark.asyncio
async def test_fts_optimize_index_not_found_warns_and_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """S6: FTSIndexNotFoundError → WARNING logged; fts_optimized_at not updated; no propagation."""
    ss = AsyncMock()
    ss.optimize_fts = AsyncMock(side_effect=FTSIndexNotFoundError("no index"))
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)

    loop = _make_loop(tmp_path, fts_optimize=True, search_store=ss)
    health: dict[str, Any] = {"fts_optimized_at": None, "orphans_removed_last_run": 0, "last_retry_at": None, "last_error": None, "meta_chunk_count": 0}
    loop._current_health = health  # type: ignore[attr-defined]

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_fts_optimize("docs", "default")  # must not raise

    assert health["fts_optimized_at"] is None
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    assert not lock.locked()  # lock must be released even on FTSIndexNotFoundError


@pytest.mark.asyncio
async def test_fts_optimize_locked_collection_skips(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """S7: asyncio.TimeoutError from lock acquisition → DEBUG logged; no exception propagates."""
    ss = AsyncMock()
    ss.optimize_fts = AsyncMock()
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)

    loop = _make_loop(tmp_path, fts_optimize=True, search_store=ss)
    health: dict[str, Any] = {"fts_optimized_at": None, "orphans_removed_last_run": 0, "last_retry_at": None, "last_error": None, "meta_chunk_count": 0}
    loop._current_health = health  # type: ignore[attr-defined]

    # Simulate lock already held so wait_for times out.
    import archon_search.jobs.maintenance_loop as ml_mod

    with (
        caplog.at_level(logging.DEBUG, logger="archon_search.jobs.maintenance_loop"),
        patch.object(ml_mod, "INGEST_LOCK_TIMEOUT_S", 0.05),
    ):
        # Acquire the lock externally so the method times out waiting for it.
        await lock.acquire()
        try:
            await loop._run_fts_optimize("docs", "default")  # must not raise
        finally:
            lock.release()

    ss.optimize_fts.assert_not_called()
    assert health["fts_optimized_at"] is None
    assert any(r.levelno == logging.DEBUG for r in caplog.records)


@pytest.mark.asyncio
async def test_fts_optimize_disabled_by_config(tmp_path: Path) -> None:
    """When fts_optimize=False, optimize_fts is never called."""
    ss = AsyncMock()
    ss.optimize_fts = AsyncMock()
    ss.lock_for = MagicMock(return_value=asyncio.Lock())

    loop = _make_loop(tmp_path, fts_optimize=False, search_store=ss)
    health: dict[str, Any] = {"fts_optimized_at": None, "orphans_removed_last_run": 0, "last_retry_at": None, "last_error": None, "meta_chunk_count": 0}
    loop._current_health = health  # type: ignore[attr-defined]

    await loop._run_fts_optimize("docs", "default")

    ss.optimize_fts.assert_not_called()
    assert health["fts_optimized_at"] is None


@pytest.mark.asyncio
async def test_fts_optimize_unexpected_exception_propagates_and_releases_lock(
    tmp_path: Path,
) -> None:
    """Non-FTSIndexNotFoundError from optimize_fts propagates; lock is still released."""
    ss = AsyncMock()
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)
    ss.optimize_fts = AsyncMock(side_effect=RuntimeError("LanceDB corrupt"))

    loop = _make_loop(tmp_path, fts_optimize=True, search_store=ss)
    health: dict[str, Any] = {"fts_optimized_at": None, "orphans_removed_last_run": 0, "last_retry_at": None, "last_error": None, "meta_chunk_count": 0}
    loop._current_health = health  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="LanceDB corrupt"):
        await loop._run_fts_optimize("docs", "default")

    # Lock must be released even when optimize_fts raises unexpectedly.
    assert not lock.locked()
    # fts_optimized_at must not be updated on failure.
    assert health["fts_optimized_at"] is None


@pytest.mark.asyncio
async def test_fts_optimize_no_current_health_does_not_crash(tmp_path: Path) -> None:
    """If _current_health is not set (called outside _run_one_pass), optimize still runs
    and fts_optimized_at update is silently skipped — no AttributeError raised."""
    ss = AsyncMock()
    lock = asyncio.Lock()
    ss.lock_for = MagicMock(return_value=lock)
    ss.optimize_fts = AsyncMock()

    loop = _make_loop(tmp_path, fts_optimize=True, search_store=ss)
    # Deliberately do NOT set loop._current_health

    await loop._run_fts_optimize("docs", "default")  # must not raise

    ss.optimize_fts.assert_called_once_with("docs")
    # No fts_optimized_at to check — just verify no crash and lock released.
    assert not lock.locked()


# ---------------------------------------------------------------------------
# BE-7: Graph garbage collection policy tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_graph_gc_fetches_live_chunk_ids_with_correct_namespace(
    tmp_path: Path,
) -> None:
    """BE-7 S1: _run_graph_gc calls store.list_chunks_raw with correct collection and namespace."""
    ss = AsyncMock()

    async def _fake_list_chunks_raw(collection: str, namespace: str):
        # Capture the parameters passed
        _fake_list_chunks_raw.called_with_args = (collection, namespace)
        # Return empty iterator
        return
        yield  # Make it an async generator

    ss.list_chunks_raw = _fake_list_chunks_raw

    gs = MagicMock()
    gs.count_stale_mentions = AsyncMock(return_value=0)
    gs.prune_stale_mentions = AsyncMock(return_value=0)
    gs.delete_orphan_nodes_and_edges = AsyncMock(return_value=MagicMock(orphan_nodes_removed=0, orphan_edges_removed=0))

    cfg = MaintenanceConfig(interval_hours=0)
    loop = MaintenanceLoop(job_store=MagicMock(), search_store=ss, config=cfg, data_dir=tmp_path)
    loop._graph_store = gs
    loop._config_graph_enabled = True

    # This will fail if _run_graph_gc is not implemented yet, which is expected in TDD phase
    result = await loop._run_graph_gc("docs", "tenant_x")

    # Verify the namespace was passed through to list_chunks_raw
    # (This test will only pass once _run_graph_gc is implemented)
    assert _fake_list_chunks_raw.called_with_args == ("docs", "tenant_x")


@pytest.mark.asyncio
async def test_run_graph_gc_calls_prune_then_delete_orphans(tmp_path: Path) -> None:
    """BE-7 S2: _run_graph_gc calls count_stale_mentions, prune_stale_mentions, delete_orphan_nodes_and_edges in order."""
    call_order = []

    ss = AsyncMock()

    async def _fake_list_chunks_raw(collection: str, namespace: str):
        return
        yield  # Empty iterator

    ss.list_chunks_raw = _fake_list_chunks_raw

    gs = MagicMock()
    gs.count_stale_mentions = AsyncMock(
        side_effect=lambda *args, **kwargs: (call_order.append("count_stale_mentions"), 5)[1]
    )
    gs.prune_stale_mentions = AsyncMock(
        side_effect=lambda *args, **kwargs: (call_order.append("prune_stale_mentions"), 5)[1]
    )
    gc_result = MagicMock(orphan_nodes_removed=0, orphan_edges_removed=0, communities_invalidated=False)
    gs.delete_orphan_nodes_and_edges = AsyncMock(
        side_effect=lambda *args, **kwargs: (call_order.append("delete_orphan_nodes_and_edges"), gc_result)[1]
    )

    cfg = MaintenanceConfig(interval_hours=0)
    loop = MaintenanceLoop(job_store=MagicMock(), search_store=ss, config=cfg, data_dir=tmp_path)
    loop._graph_store = gs
    loop._config_graph_enabled = True

    result = await loop._run_graph_gc("docs", "default")

    assert call_order == ["count_stale_mentions", "prune_stale_mentions", "delete_orphan_nodes_and_edges"]


@pytest.mark.asyncio
async def test_run_graph_gc_sets_communities_invalidated_when_nodes_removed(tmp_path: Path) -> None:
    """BE-7 S3: when orphan_nodes_removed > 0, communities_invalidated = True in result."""
    ss = AsyncMock()

    async def _fake_list_chunks_raw(collection: str, namespace: str):
        return
        yield

    ss.list_chunks_raw = _fake_list_chunks_raw

    gs = MagicMock()
    gs.count_stale_mentions = AsyncMock(return_value=0)
    gs.prune_stale_mentions = AsyncMock(return_value=0)
    gc_result = MagicMock(orphan_nodes_removed=1, orphan_edges_removed=2, communities_invalidated=True)
    gs.delete_orphan_nodes_and_edges = AsyncMock(return_value=gc_result)

    cfg = MaintenanceConfig(interval_hours=0)
    loop = MaintenanceLoop(job_store=MagicMock(), search_store=ss, config=cfg, data_dir=tmp_path)
    loop._graph_store = gs
    loop._config_graph_enabled = True
    # Prevent fire-and-forget rebuild task leak; this test asserts the result flag, not the rebuild side effect.
    loop._spawn_rebuild_task = MagicMock()

    stale_count, gc_result_returned = await loop._run_graph_gc("docs", "default")

    assert gc_result_returned is not None
    assert gc_result_returned.communities_invalidated is True


@pytest.mark.asyncio
async def test_run_graph_gc_skips_when_graph_disabled(tmp_path: Path) -> None:
    """BE-7 S8a: graph_store=None → no calls, no exception."""
    ss = AsyncMock()
    cfg = MaintenanceConfig(interval_hours=0)
    loop = MaintenanceLoop(job_store=MagicMock(), search_store=ss, config=cfg, data_dir=tmp_path)
    # graph_store NOT set
    loop._config_graph_enabled = False

    stale_count, gc_result_returned = await loop._run_graph_gc("docs", "default")

    # Should skip silently — returns (0, None)
    assert stale_count == 0
    assert gc_result_returned is None


@pytest.mark.asyncio
async def test_run_graph_gc_skips_when_graph_enabled_false_with_live_store(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """BE-7 S8b: non-None graph_store + config graph.enabled=False → zero graph_store calls, no error logged."""
    ss = AsyncMock()
    gs = MagicMock()

    cfg = MaintenanceConfig(interval_hours=0)
    loop = MaintenanceLoop(job_store=MagicMock(), search_store=ss, config=cfg, data_dir=tmp_path)
    loop._graph_store = gs
    loop._config_graph_enabled = False

    with caplog.at_level(logging.ERROR, logger="archon_search.jobs.maintenance_loop"):
        result = await loop._run_graph_gc("docs", "default")

    gs.count_stale_mentions.assert_not_called()
    gs.prune_stale_mentions.assert_not_called()
    gs.delete_orphan_nodes_and_edges.assert_not_called()

    # No ERROR logs
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 0


@pytest.mark.asyncio
async def test_run_graph_gc_aborts_when_list_chunks_raises_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """BE-7 S10: list_chunks_raw raises RuntimeError → WARNING logged, prune_stale_mentions NOT called."""
    ss = AsyncMock()
    ss.list_chunks_raw = AsyncMock(side_effect=RuntimeError("db connection lost"))

    gs = MagicMock()
    # C1-I-4: Use AsyncMock so an early-return regression produces a clean
    # assert_not_called() failure rather than a TypeError on await.
    gs.count_stale_mentions = AsyncMock()
    gs.prune_stale_mentions = AsyncMock()
    gs.delete_orphan_nodes_and_edges = AsyncMock()

    cfg = MaintenanceConfig(interval_hours=0)
    loop = MaintenanceLoop(job_store=MagicMock(), search_store=ss, config=cfg, data_dir=tmp_path)
    loop._graph_store = gs
    loop._config_graph_enabled = True

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        result = await loop._run_graph_gc("docs", "default")

    # Should not call prune_stale_mentions
    gs.prune_stale_mentions.assert_not_called()
    # Should log WARNING
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_run_graph_gc_prunes_when_collection_genuinely_empty(tmp_path: Path) -> None:
    """BE-7 S11: list_chunks_raw returns empty iterator (no exception) → prune_stale_mentions IS called with empty live_chunk_ids."""
    ss = AsyncMock()

    async def _empty_iterator(*args, **kwargs):
        return
        yield

    ss.list_chunks_raw = _empty_iterator

    gs = MagicMock()
    gs.count_stale_mentions = AsyncMock(return_value=5)
    gs.prune_stale_mentions = AsyncMock(return_value=5)
    gc_result = MagicMock(orphan_nodes_removed=0, orphan_edges_removed=0, communities_invalidated=False)
    gs.delete_orphan_nodes_and_edges = AsyncMock(return_value=gc_result)

    cfg = MaintenanceConfig(interval_hours=0)
    loop = MaintenanceLoop(job_store=MagicMock(), search_store=ss, config=cfg, data_dir=tmp_path)
    loop._graph_store = gs
    loop._config_graph_enabled = True
    # Prevent fire-and-forget rebuild task leak; this test asserts the result flag, not the rebuild side effect.
    loop._spawn_rebuild_task = MagicMock()

    result = await loop._run_graph_gc("docs", "default")

    # Should call prune_stale_mentions with empty frozenset
    gs.prune_stale_mentions.assert_called_once()
    call_args = gs.prune_stale_mentions.call_args
    assert call_args is not None
    # Plan spec (BE-7): prune IS called with live_chunk_ids == frozenset() —
    # an empty live set from a successful list_chunks_raw read is trusted, not aborted.
    live_chunk_ids_arg = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs["live_chunk_ids"]
    assert live_chunk_ids_arg == frozenset()


@pytest.mark.asyncio
async def test_run_graph_gc_no_invalidation_when_zero_orphans(tmp_path: Path) -> None:
    """BE-7 S13: orphan_nodes_removed=0 → communities_invalidated=False, no rebuild task created."""
    ss = AsyncMock()

    async def _empty_iterator(*args, **kwargs):
        return
        yield

    ss.list_chunks_raw = _empty_iterator

    gs = MagicMock()
    gs.count_stale_mentions = AsyncMock(return_value=0)
    gs.prune_stale_mentions = AsyncMock(return_value=0)
    gc_result = MagicMock(orphan_nodes_removed=0, orphan_edges_removed=0, communities_invalidated=False)
    gs.delete_orphan_nodes_and_edges = AsyncMock(return_value=gc_result)

    cfg = MaintenanceConfig(interval_hours=0)
    loop = MaintenanceLoop(job_store=MagicMock(), search_store=ss, config=cfg, data_dir=tmp_path)
    loop._graph_store = gs
    loop._config_graph_enabled = True

    stale_count, gc_result_returned = await loop._run_graph_gc("docs", "default")

    assert gc_result_returned is not None
    assert gc_result_returned.communities_invalidated is False


@pytest.mark.asyncio
async def test_maintenance_state_writes_last_graph_gc_at(tmp_path: Path) -> None:
    """BE-7 S15: after GC pass, state file has last_graph_gc_at, stale_mention_count, and per-collection communities_invalidated."""
    ss = AsyncMock()
    info = _make_collection_info("docs", namespace="default")
    ss.list_collections = AsyncMock(return_value=[info])
    ss.get_collection_meta = AsyncMock(return_value=None)

    async def _empty_iterator(*args, **kwargs):
        return
        yield

    ss.list_chunks_raw = _empty_iterator

    gs = MagicMock()
    # C1-I-3: gs.count_stale_mentions / prune_stale_mentions / delete_orphan_nodes_and_edges
    # are dead configuration here — loop._run_graph_gc is stubbed wholesale below.
    # Removed dead mock-assignment lines; gs itself is kept for _graph_store assignment.

    gc_result = MagicMock(orphan_nodes_removed=1, orphan_edges_removed=2, communities_invalidated=True)

    cfg = MaintenanceConfig(interval_hours=0, prune_expired_chunks=False)
    loop = MaintenanceLoop(job_store=MagicMock(), search_store=ss, config=cfg, data_dir=tmp_path)
    loop._graph_store = gs
    loop._config_graph_enabled = True

    # Stub the GC method — returns (stale_count, gc_result) tuple
    loop._run_graph_gc = AsyncMock(return_value=(3, gc_result))  # type: ignore[method-assign]

    loop._run_fts_optimize = AsyncMock()  # type: ignore[method-assign]
    loop._run_orphan_cleanup = AsyncMock()  # type: ignore[method-assign]
    loop._run_expired_chunk_pruning = AsyncMock()  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = AsyncMock()  # type: ignore[method-assign]

    await loop._run_one_pass()

    state_file = tmp_path / ".maintenance-state.json"
    assert state_file.exists()
    loaded = json.loads(state_file.read_text(encoding="utf-8"))

    # Check for new fields in state
    assert "last_graph_gc_at" in loaded
    assert "stale_mention_count" in loaded
    health_entry = loaded["collection_health"].get("default/docs", {})
    assert "communities_invalidated" in health_entry


@pytest.mark.asyncio
async def test_stale_mention_count_is_sum_across_collections(tmp_path: Path) -> None:
    """BE-7 S16: stale_mention_count = SUM of stale counts across all GC'd collections."""
    ss = AsyncMock()
    info_a = _make_collection_info("col-a", namespace="default")
    info_b = _make_collection_info("col-b", namespace="default")
    ss.list_collections = AsyncMock(return_value=[info_a, info_b])
    ss.get_collection_meta = AsyncMock(return_value=None)

    async def _empty_iterator(*args, **kwargs):
        return
        yield

    ss.list_chunks_raw = _empty_iterator

    gs = MagicMock()
    # C1-I-3: gs.count_stale_mentions / prune_stale_mentions / delete_orphan_nodes_and_edges
    # are dead configuration here — loop._run_graph_gc is stubbed wholesale below.
    # Removed dead mock-assignment lines; gs itself is kept for _graph_store assignment.

    gc_result = MagicMock(orphan_nodes_removed=0, orphan_edges_removed=0, communities_invalidated=False)

    cfg = MaintenanceConfig(interval_hours=0, prune_expired_chunks=False)
    loop = MaintenanceLoop(job_store=MagicMock(), search_store=ss, config=cfg, data_dir=tmp_path)
    loop._graph_store = gs
    loop._config_graph_enabled = True

    # _run_graph_gc returns (stale_count, gc_result) — 3 stale per collection
    loop._run_graph_gc = AsyncMock(return_value=(3, gc_result))  # type: ignore[method-assign]
    loop._run_fts_optimize = AsyncMock()  # type: ignore[method-assign]
    loop._run_orphan_cleanup = AsyncMock()  # type: ignore[method-assign]
    loop._run_expired_chunk_pruning = AsyncMock()  # type: ignore[method-assign]
    loop._run_failed_ingest_retry = AsyncMock()  # type: ignore[method-assign]

    await loop._run_one_pass()

    state_file = tmp_path / ".maintenance-state.json"
    loaded = json.loads(state_file.read_text(encoding="utf-8"))

    # Should sum to 6 (3 + 3)
    assert loaded.get("stale_mention_count") == 6


# ---------------------------------------------------------------------------
# BE-7 (S12/S15) — MaintenanceLoop._rebuild_communities_async serialises with
# the route path via the shared module-level rebuild-lock registry.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _clear_rebuild_locks():
    """Reset the module-level registry so this test doesn't leak locks."""
    from archon_search.community_builder import _rebuild_locks

    _rebuild_locks.clear()
    yield
    _rebuild_locks.clear()


@pytest.mark.asyncio
async def test_maintenance_loop_uses_shared_rebuild_lock(tmp_path: Path, _clear_rebuild_locks) -> None:
    """BE-7/S12: the GC path's fresh CommunityBuilder serialises against the
    route path's fresh CommunityBuilder via the shared module-level registry.

    ``MaintenanceLoop._rebuild_communities_async`` constructs its own
    ``CommunityBuilder`` and calls ``build()`` with no separate lock of its
    own (verified: the method acquires no lock before calling
    ``builder.build()``). This test proves that call and a second,
    independently-constructed ``CommunityBuilder.build()`` call (standing in
    for the REST route's task) resolve the SAME lock object for the same
    ``(ns, collection)`` key and therefore serialise -- would fail (interleave)
    against a per-instance lock design.
    """
    from unittest.mock import AsyncMock, MagicMock

    from archon_search.community_builder import CommunityBuilder
    from archon_search.config import GraphConfig
    from archon_search.graph_types import EntityType, GraphNode

    ns = "default"
    col = "shared-col"
    events: list[str] = []

    def make_node(node_id: str) -> GraphNode:
        return GraphNode(
            id=node_id,
            entity_name=node_id,
            entity_type=EntityType.concept,
            source_doc_id="doc-1",
            collection_name=col,
        )

    def make_graph_store(delay: float, label: str):
        store = MagicMock()

        async def _get_all_nodes(*args, **kwargs):
            events.append(f"{label}-start")
            if delay:
                await asyncio.sleep(delay)
            events.append(f"{label}-end")
            # Single node: _build_locked's < 2 short-circuit still calls
            # get_all_nodes and write_communities (inside the lock), but
            # never touches get_all_edges or the real-Leiden path -- so this
            # test doesn't require the optional leidenalg extra.
            return [make_node("a")]

        store.get_all_nodes = _get_all_nodes
        store.get_all_edges = AsyncMock(return_value=[])
        store.write_communities = AsyncMock(return_value=None)
        return store

    graph_config = GraphConfig()

    # GC path: drive MaintenanceLoop._rebuild_communities_async exactly as
    # _spawn_rebuild_task does -- it constructs its own fresh CommunityBuilder
    # internally from loop._graph_store / loop._graph_config / loop._search_store.
    gc_loop = MaintenanceLoop(
        job_store=MagicMock(),
        search_store=MagicMock(),
        config=MaintenanceConfig(interval_hours=0),
        data_dir=tmp_path,
    )
    gc_loop._graph_store = make_graph_store(0.05, "gc")
    gc_loop._graph_config = graph_config
    # No search_store needed for MMR representative-chunk selection in this
    # test (CommunityBuilder treats None as "skip MMR, return no candidates").
    gc_loop._search_store = None

    # Route path stand-in: a second, wholly independent CommunityBuilder
    # instance (mirrors _community_rebuild_task constructing its own). Both
    # sides use a non-trivial delay so that, absent a shared lock, the two
    # builds would provably interleave (see negative control below).
    route_store = make_graph_store(0.05, "route")
    route_builder = CommunityBuilder(route_store, graph_config)

    await asyncio.gather(
        gc_loop._rebuild_communities_async(ns, col),
        route_builder.build(col, ns=ns),
    )

    # Both builds must have actually run to completion (start+end each) --
    # otherwise a build that raised early inside the lock could vacuously
    # satisfy the non-interleaving check below.
    assert len(events) == 4, f"expected both builds to emit start+end markers: {events}"
    assert gc_loop._graph_store.write_communities.await_count == 1
    assert route_store.write_communities.await_count == 1

    # Serialised execution: one build's start+end must both appear before the
    # other build's start -- no interleaving of "start"/"end" markers.
    gc_start = events.index("gc-start")
    gc_end = events.index("gc-end")
    route_start = events.index("route-start")
    route_end = events.index("route-end")

    assert (gc_end < route_start) or (route_end < gc_start), (
        f"GC-path and route-path builds interleaved instead of serialising: {events}"
    )

    # --- Negative control (proves the assertion above has teeth) -----------
    # Re-run the same two concurrent builds, but defeat the shared-lock
    # registry by making _get_rebuild_lock hand out a BRAND-NEW asyncio.Lock
    # on every call -- so the two builds resolve DISTINCT locks and cannot
    # serialise against each other. With both delays at 0.05, the two
    # _get_all_nodes coroutines both append "-start", both sleep, then both
    # append "-end": a genuinely interleaved sequence. If this negative run
    # did NOT interleave, the positive assertion above would be meaningless
    # (it could pass by scheduling luck rather than by real serialisation).
    from archon_search.community_builder import _rebuild_locks

    _rebuild_locks.clear()
    neg_events: list[str] = []

    def make_neg_graph_store(delay: float, label: str):
        store = MagicMock()

        async def _get_all_nodes(*args, **kwargs):
            neg_events.append(f"{label}-start")
            if delay:
                await asyncio.sleep(delay)
            neg_events.append(f"{label}-end")
            return [make_node("a")]

        store.get_all_nodes = _get_all_nodes
        store.get_all_edges = AsyncMock(return_value=[])
        store.write_communities = AsyncMock(return_value=None)
        return store

    neg_gc_loop = MaintenanceLoop(
        job_store=MagicMock(),
        search_store=MagicMock(),
        config=MaintenanceConfig(interval_hours=0),
        data_dir=tmp_path,
    )
    # Distinct delays (not equal) so the interleave proof rests on STRUCTURE,
    # not on CPython's FIFO wake-ordering of two equal timers: with distinct
    # locks neither racer blocks on acquire, so both reach their "-start"
    # marker before either sleeps out to "-end" regardless of sleep lengths.
    neg_gc_loop._graph_store = make_neg_graph_store(0.05, "gc")
    neg_gc_loop._graph_config = graph_config
    neg_gc_loop._search_store = None

    neg_route_store = make_neg_graph_store(0.15, "route")
    neg_route_builder = CommunityBuilder(neg_route_store, graph_config)

    with patch(
        "archon_search.community_builder._get_rebuild_lock",
        side_effect=lambda ns, collection: asyncio.Lock(),
    ):
        await asyncio.gather(
            neg_gc_loop._rebuild_communities_async(ns, col),
            neg_route_builder.build(col, ns=ns),
        )

    _rebuild_locks.clear()

    neg_gc_start = neg_events.index("gc-start")
    neg_gc_end = neg_events.index("gc-end")
    neg_route_start = neg_events.index("route-start")
    neg_route_end = neg_events.index("route-end")

    # Structural interleave proof: with distinct locks BOTH builds enter (emit
    # their "-start") before EITHER completes (emits any "-end"). This holds
    # for any delays and does not depend on equal-timer wake ordering.
    assert max(neg_gc_start, neg_route_start) < min(neg_gc_end, neg_route_end), (
        f"negative control expected interleaving (distinct locks defeat "
        f"serialisation) but markers did not interleave: {neg_events}"
    )

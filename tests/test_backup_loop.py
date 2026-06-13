"""Tests for BackupLoop — scheduled backup orchestrator.

Plan: Documentation/Backlog/D2-scheduled-backup-plan.md Task 3.1.

TDD: tests written first, then BackupLoop implementation in
archon_search/jobs/backup_loop.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._types import CollectionInfo
from archon_search.config import BackupConfig
from archon_search.jobs.backup_loop import BackupLoop
from archon_search.jobs.model import JobStatus
from archon_search.types import ExportJob


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_loop(
    tmp_path: Path,
    *,
    interval_hours: int = 0,
    keep: int = 7,
    exclude: list[str] | None = None,
    job_store: Any = None,
    search_store: Any = None,
) -> BackupLoop:
    output_dir = tmp_path / "backups"
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg = BackupConfig(
        interval_hours=interval_hours,
        keep=keep,
        exclude=exclude or [],
        output_dir=str(output_dir),
    )
    js = job_store if job_store is not None else MagicMock()
    ss = search_store if search_store is not None else MagicMock()
    return BackupLoop(job_store=js, search_store=ss, config=cfg, data_dir=tmp_path)


def _make_export_job(
    job_id: str,
    *,
    collection: str = "docs",
    namespace: str = "default",
    status: JobStatus = JobStatus.QUEUED,
    source: str = "backup",
    output_path: str = "",
    updated_at: str | None = None,
    error: str | None = None,
) -> ExportJob:
    now = updated_at or datetime.now(timezone.utc).isoformat()
    return ExportJob(
        job_id=job_id,
        status=status,
        created_at=now,
        updated_at=now,
        namespace=namespace,
        collection=collection,
        output_path=output_path,
        tmp_path="",
        source=source,  # type: ignore[arg-type]
        error=error,
    )


# ---------------------------------------------------------------------------
# In-flight tracking
# ---------------------------------------------------------------------------


def test_is_collection_in_flight_true(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.track("job-1", "default", "docs")
    assert loop.is_collection_in_flight("default", "docs") is True


def test_is_collection_in_flight_namespace_scoped(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.track("job-1", "default", "docs")
    assert loop.is_collection_in_flight("tenants", "docs") is False


def test_track_adds_to_in_flight(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    assert loop.is_collection_in_flight("default", "docs") is False
    loop.track("job-1", "default", "docs")
    assert loop.is_collection_in_flight("default", "docs") is True


# ---------------------------------------------------------------------------
# Exclusion patterns
# ---------------------------------------------------------------------------


def test_is_excluded_bare_pattern(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, exclude=["docs"])
    assert loop._is_excluded("default", "docs") is True
    assert loop._is_excluded("tenants", "docs") is True


def test_is_excluded_qualified_pattern(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, exclude=["default/docs"])
    assert loop._is_excluded("default", "docs") is True
    assert loop._is_excluded("tenants", "docs") is False


def test_is_excluded_unknown_collection_not_excluded(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, exclude=["docs"])
    assert loop._is_excluded("default", "other") is False


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def _touch_archive(ns_dir: Path, name: str) -> Path:
    ns_dir.mkdir(parents=True, exist_ok=True)
    p = ns_dir / name
    p.write_bytes(b"x")
    return p


def test_rotate_keep_n_deletes_oldest(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    loop = _make_loop(tmp_path, keep=2)
    ns_dir = Path(loop._config.output_dir) / "default"
    files = [
        _touch_archive(ns_dir, "docs.backup.20240101T000000Z.tar.gz"),
        _touch_archive(ns_dir, "docs.backup.20240102T000000Z.tar.gz"),
        _touch_archive(ns_dir, "docs.backup.20240103T000000Z.tar.gz"),
        _touch_archive(ns_dir, "docs.backup.20240104T000000Z.tar.gz"),
    ]
    with caplog.at_level(logging.INFO, logger="archon_search.jobs.backup_loop"):
        loop._rotate("default", "docs")
    # Two oldest deleted, two newest kept.
    assert not files[0].exists()
    assert not files[1].exists()
    assert files[2].exists()
    assert files[3].exists()
    # Two INFO log lines for the deletions.
    msgs = [r.message for r in caplog.records if "Rotation" in r.message]
    assert len(msgs) == 2


def test_rotate_keep_zero_does_nothing(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, keep=0)
    ns_dir = Path(loop._config.output_dir) / "default"
    f = _touch_archive(ns_dir, "docs.backup.20240101T000000Z.tar.gz")
    loop._rotate("default", "docs")
    assert f.exists()


def test_rotate_no_dir_is_noop(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path, keep=2)
    # Should not raise even if the namespace dir doesn't exist.
    loop._rotate("ghost", "docs")


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


def test_load_state_missing_file_returns_empty(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    assert loop._load_state() == {}


def test_load_state_corrupt_file_returns_empty(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop._state_file.write_text("not json{{{")
    assert loop._load_state() == {}


def test_save_state_atomic_write(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    state = {"default/docs": "2024-01-01T00:00:00+00:00"}
    loop._save_state(state)
    assert loop._state_file.exists()
    assert json.loads(loop._state_file.read_text()) == state
    # tmp file removed after replace.
    tmp_file = loop._state_file.with_suffix(loop._state_file.suffix + ".tmp")
    assert not tmp_file.exists()


# ---------------------------------------------------------------------------
# Dedup in the trigger loop
# ---------------------------------------------------------------------------


def _patch_one_tick(loop: BackupLoop) -> None:
    """Helper: directly invoke the trigger tick path by calling internal method."""
    # We use a public-ish helper: BackupLoop exposes `_run_one_tick` for testability.
    asyncio.run(loop._run_one_tick())


def test_dedup_check_in_flight(tmp_path: Path) -> None:
    js = MagicMock()
    js.list_queued_bulk.return_value = []
    ss = MagicMock()
    ss.list_collections = AsyncMock(
        return_value=[CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace="default")]
    )
    loop = _make_loop(tmp_path, job_store=js, search_store=ss)
    loop.track("existing-job", "default", "docs")
    _patch_one_tick(loop)
    js.create_export.assert_not_called()


def test_dedup_check_queued_bulk(tmp_path: Path) -> None:
    js = MagicMock()
    js.list_queued_bulk.return_value = [
        _make_export_job("queued-1", collection="docs", namespace="default", source="backup")
    ]
    ss = MagicMock()
    ss.list_collections = AsyncMock(
        return_value=[CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace="default")]
    )
    loop = _make_loop(tmp_path, job_store=js, search_store=ss)
    _patch_one_tick(loop)
    js.create_export.assert_not_called()


def test_trigger_loop_enqueues_when_not_in_flight_or_queued(tmp_path: Path) -> None:
    js = MagicMock()
    js.list_queued_bulk.return_value = []
    new_job = _make_export_job("new-1", collection="docs", namespace="default", source="backup")
    js.create_export.return_value = new_job
    ss = MagicMock()
    ss.list_collections = AsyncMock(
        return_value=[CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace="default")]
    )
    loop = _make_loop(tmp_path, job_store=js, search_store=ss)
    _patch_one_tick(loop)
    js.create_export.assert_called_once()
    kwargs = js.create_export.call_args.kwargs
    assert kwargs["namespace"] == "default"
    assert kwargs["source"] == "backup"
    assert loop.is_collection_in_flight("default", "docs") is True


# ---------------------------------------------------------------------------
# Completion loop
# ---------------------------------------------------------------------------


def test_completion_loop_removes_done_job(tmp_path: Path) -> None:
    done_job = _make_export_job(
        "done-1",
        collection="docs",
        namespace="default",
        status=JobStatus.DONE,
        output_path=str(tmp_path / "archive.tar.gz"),
        updated_at="2024-06-01T00:00:00+00:00",
    )
    js = MagicMock()
    js.get.return_value = done_job
    loop = _make_loop(tmp_path, job_store=js)
    loop.track("done-1", "default", "docs")
    loop._drain_completed()
    assert loop.is_collection_in_flight("default", "docs") is False
    state = loop._load_state()
    assert state.get("default/docs") == "2024-06-01T00:00:00+00:00"


def test_completion_loop_removes_failed_job_preserves_last_backup_at(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    failed_job = _make_export_job(
        "fail-1",
        collection="docs",
        namespace="default",
        status=JobStatus.FAILED,
        error="boom",
    )
    js = MagicMock()
    js.get.return_value = failed_job
    loop = _make_loop(tmp_path, job_store=js)
    loop.track("fail-1", "default", "docs")
    with caplog.at_level(logging.ERROR, logger="archon_search.jobs.backup_loop"):
        loop._drain_completed()
    assert loop.is_collection_in_flight("default", "docs") is False
    # No state file written, since no DONE happened.
    assert loop._load_state() == {}
    assert any("Backup failed" in r.message for r in caplog.records)


def test_completion_loop_removes_cancelled_job(tmp_path: Path) -> None:
    cancelled_job = _make_export_job(
        "cancel-1",
        collection="docs",
        namespace="default",
        status=JobStatus.CANCELLED,
    )
    js = MagicMock()
    js.get.return_value = cancelled_job
    loop = _make_loop(tmp_path, job_store=js)
    loop.track("cancel-1", "default", "docs")
    loop._drain_completed()
    assert loop.is_collection_in_flight("default", "docs") is False
    assert loop._load_state() == {}


def test_completion_loop_done_triggers_rotation(tmp_path: Path) -> None:
    done_job = _make_export_job(
        "done-1",
        collection="docs",
        namespace="default",
        status=JobStatus.DONE,
        updated_at="2024-06-01T00:00:00+00:00",
    )
    js = MagicMock()
    js.get.return_value = done_job
    loop = _make_loop(tmp_path, keep=1, job_store=js)
    ns_dir = Path(loop._config.output_dir) / "default"
    f_old = _touch_archive(ns_dir, "docs.backup.20240101T000000Z.tar.gz")
    f_new = _touch_archive(ns_dir, "docs.backup.20240601T000000Z.tar.gz")
    loop.track("done-1", "default", "docs")
    loop._drain_completed()
    assert not f_old.exists()
    assert f_new.exists()


# ---------------------------------------------------------------------------
# Trigger loop control flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_loop_interval_zero_exits_after_startup(tmp_path: Path) -> None:
    js = MagicMock()
    js.list_queued_bulk.return_value = []
    ss = MagicMock()
    ss.list_collections = AsyncMock(return_value=[])
    loop = _make_loop(tmp_path, interval_hours=0, job_store=js, search_store=ss)
    # Should return promptly without ticking periodically.
    await asyncio.wait_for(loop._trigger_loop(), timeout=2.0)


@pytest.mark.asyncio
async def test_trigger_loop_fires_immediate_if_overdue(tmp_path: Path) -> None:
    # Pre-seed state file with a stale last-backup-at.
    js = MagicMock()
    js.list_queued_bulk.return_value = []
    new_job = _make_export_job("new-1", collection="docs", namespace="default")
    js.create_export.return_value = new_job
    ss = MagicMock()
    ss.list_collections = AsyncMock(
        return_value=[CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace="default")]
    )
    loop = _make_loop(tmp_path, interval_hours=24, job_store=js, search_store=ss)
    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    loop._save_state({"default/docs": stale})

    # Run one tick directly to verify overdue triggers a backup.
    await loop._run_one_tick()
    js.create_export.assert_called_once()


@pytest.mark.asyncio
async def test_trigger_loop_skips_tick_on_list_collections_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    js = MagicMock()
    js.list_queued_bulk.return_value = []
    ss = MagicMock()
    ss.list_collections = AsyncMock(side_effect=RuntimeError("boom"))
    loop = _make_loop(tmp_path, interval_hours=24, job_store=js, search_store=ss)
    with caplog.at_level(logging.ERROR, logger="archon_search.jobs.backup_loop"):
        await loop._run_one_tick()
    # No job enqueued.
    js.create_export.assert_not_called()
    # ERROR logged.
    assert any("boom" in r.message or "tick" in r.message.lower() for r in caplog.records)

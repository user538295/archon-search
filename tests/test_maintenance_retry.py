"""Tests for MaintenanceLoop failed-ingest retry policy (BE-8).

Plan: Documentation/Backlog/D5-maintenance-jobs-policies-team-plan.md Task BE-8

TDD: tests written first, then _run_failed_ingest_retry implementation in
archon_search/jobs/maintenance_loop.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from archon_search.config import MaintenanceConfig
from archon_search.jobs.maintenance_loop import MaintenanceLoop
from archon_search.jobs.store import JobStore
from archon_search.types import IngestJob, JobStatus


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_loop(
    tmp_path: Path,
    *,
    interval_hours: int = 0,
    fts_optimize: bool = False,
    orphan_cleanup: bool = False,
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


def _make_failed_ingest_job(
    source_path: str,
    collection: str = "docs",
    namespace: str = "default",
    *,
    age_hours: float = 1.0,
    retry_count: int = 0,
) -> IngestJob:
    """Create a FAILED IngestJob with the given age."""
    created_at = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    return IngestJob(
        job_id=f"job-{source_path[-8:]}",
        status=JobStatus.FAILED,
        created_at=created_at,
        updated_at=created_at,
        namespace=namespace,
        source_path=source_path,
        collection=collection,
        retry_count=retry_count,
    )


def _make_done_ingest_job(
    source_path: str,
    collection: str = "docs",
    namespace: str = "default",
) -> IngestJob:
    """Create a DONE IngestJob for reset-on-done testing."""
    now = datetime.now(timezone.utc).isoformat()
    return IngestJob(
        job_id=f"done-{source_path[-8:]}",
        status=JobStatus.DONE,
        created_at=now,
        updated_at=now,
        namespace=namespace,
        source_path=source_path,
        collection=collection,
    )


def _inject_state(loop: MaintenanceLoop, retry_counts: dict[str, int]) -> None:
    """Pre-populate the state file with retry_counts for tests."""
    state: dict[str, Any] = {
        "last_run_at": None,
        "next_run_at": None,
        "collection_health": {},
        "retry_counts": retry_counts,
    }
    loop._save_state(state)


# ---------------------------------------------------------------------------
# Unit tests: core retry logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_eligible_job_is_reenqueued(tmp_path: Path) -> None:
    """S13: FAILED job within age and attempt limit is re-enqueued with source='maintenance'."""
    job = _make_failed_ingest_job("/data/file.txt", collection="docs", namespace="ns1")
    js = MagicMock()
    js.list.return_value = [job]
    js.create.return_value = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=72)

    health: dict = {}
    retry_counts: dict = {}
    await loop._run_failed_ingest_retry(health, retry_counts)

    js.create.assert_called_once_with(
        path="/data/file.txt",
        collection="docs",
        namespace="ns1",
        source="maintenance",
    )

    # Retry count should be incremented in the in-memory dict
    key = "ns1/docs//data/file.txt"
    assert retry_counts.get(key, 0) == 1


@pytest.mark.asyncio
async def test_retry_max_attempts_reached_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """S14: job with retry count at max → WARNING logged; no new job created."""
    job = _make_failed_ingest_job("/data/file.txt", collection="docs", namespace="default")
    js = MagicMock()
    js.list.return_value = [job]

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3)
    # Pre-seed retry_counts at max via the state file; load them to pass in.
    _inject_state(loop, {"default/docs//data/file.txt": 3})
    state = loop._load_state()

    health: dict = {}
    retry_counts: dict = dict(state.get("retry_counts", {}))

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health, retry_counts)

    js.create.assert_not_called()
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_retry_too_old_filtered(tmp_path: Path) -> None:
    """S15: job older than retry_max_age_hours is not re-enqueued."""
    # Create a job that is 100 hours old with retry_max_age_hours=72
    job = _make_failed_ingest_job("/data/old.txt", collection="docs", namespace="default", age_hours=100.0)
    js = MagicMock()
    js.list.return_value = [job]

    loop = _make_loop(tmp_path, job_store=js, retry_max_age_hours=72)

    health: dict = {}
    retry_counts: dict = {}
    await loop._run_failed_ingest_retry(health, retry_counts)

    js.create.assert_not_called()


@pytest.mark.asyncio
async def test_retry_count_reset_on_done(tmp_path: Path) -> None:
    """S16: when latest job for a source_path is DONE, retry count is reset to 0."""
    done_job = _make_done_ingest_job("/data/fixed.txt", collection="docs", namespace="default")
    js = MagicMock()
    js.list.return_value = [done_job]

    loop = _make_loop(tmp_path, job_store=js)
    # Pre-seed a retry count of 2
    _inject_state(loop, {"default/docs//data/fixed.txt": 2})
    state = loop._load_state()

    health: dict = {}
    retry_counts: dict = dict(state.get("retry_counts", {}))
    await loop._run_failed_ingest_retry(health, retry_counts)

    key = "default/docs//data/fixed.txt"
    assert retry_counts.get(key, 0) == 0


@pytest.mark.asyncio
async def test_retry_no_failed_jobs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No FAILED jobs → no new job created; no WARNING logged."""
    done_job = _make_done_ingest_job("/data/ok.txt")
    js = MagicMock()
    js.list.return_value = [done_job]

    loop = _make_loop(tmp_path, job_store=js)
    health: dict = {}
    retry_counts: dict = {}

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health, retry_counts)

    js.create.assert_not_called()
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_retry_disabled_by_config(tmp_path: Path) -> None:
    """failed_ingest_retry=False → JobStore.list() never called."""
    js = MagicMock()
    js.list.return_value = []

    loop = _make_loop(tmp_path, job_store=js, failed_ingest_retry=False)

    health: dict = {}
    retry_counts: dict = {}
    await loop._run_failed_ingest_retry(health, retry_counts)

    js.list.assert_not_called()


@pytest.mark.asyncio
async def test_retry_skips_jobs_with_empty_source_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Pre-D5 FAILED IngestJob with source_path='' → skipped; DEBUG logged; no JobStore.create()."""
    job = IngestJob(
        job_id="pre-d5-job",
        status=JobStatus.FAILED,
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        namespace="default",
        source_path="",  # pre-D5 job has no source path
        collection="docs",
    )
    js = MagicMock()
    js.list.return_value = [job]

    loop = _make_loop(tmp_path, job_store=js)

    health: dict = {}
    retry_counts: dict = {}
    with caplog.at_level(logging.DEBUG, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health, retry_counts)

    js.create.assert_not_called()
    assert any(r.levelno == logging.DEBUG for r in caplog.records)


@pytest.mark.asyncio
async def test_retry_ingest_file_raises_during_reenqueue(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """JobStore.create() raises during retry → WARNING logged; retry_count still incremented;
    pass continues to next job (no exception propagation)."""
    job1 = _make_failed_ingest_job("/data/fail.txt", collection="docs", namespace="default")
    job2 = _make_failed_ingest_job("/data/ok.txt", collection="docs", namespace="default")
    js = MagicMock()
    js.list.return_value = [job1, job2]
    js.create.side_effect = [RuntimeError("db write failed"), MagicMock()]

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3)

    health: dict = {}
    retry_counts: dict = {}
    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health, retry_counts)  # must not raise

    # Both jobs should have been attempted
    assert js.create.call_count == 2

    # Retry count should be incremented even for the failed reenqueue
    assert retry_counts.get("default/docs//data/fail.txt", 0) == 1
    assert retry_counts.get("default/docs//data/ok.txt", 0) == 1

    # WARNING logged for the create failure
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# Unit tests: retry_counts pruning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_counts_pruned_when_absent_from_job_store_and_zero(
    tmp_path: Path,
) -> None:
    """retry_counts key with count=0 and no matching job in JobStore → key is pruned."""
    js = MagicMock()
    js.list.return_value = []  # no jobs in store

    loop = _make_loop(tmp_path, job_store=js)
    _inject_state(loop, {"default/docs//data/orphan.txt": 0})
    state = loop._load_state()

    health: dict = {}
    retry_counts: dict = dict(state.get("retry_counts", {}))
    await loop._run_failed_ingest_retry(health, retry_counts)

    assert "default/docs//data/orphan.txt" not in retry_counts


@pytest.mark.asyncio
async def test_retry_counts_not_pruned_when_present_in_job_store(
    tmp_path: Path,
) -> None:
    """retry_counts key with count=0 but a matching job exists in JobStore → key is NOT pruned."""
    job = _make_failed_ingest_job("/data/active.txt", collection="docs", namespace="default")
    js = MagicMock()
    js.list.return_value = [job]

    loop = _make_loop(tmp_path, job_store=js)
    _inject_state(loop, {"default/docs//data/active.txt": 0})
    state = loop._load_state()

    health: dict = {}
    retry_counts: dict = dict(state.get("retry_counts", {}))
    await loop._run_failed_ingest_retry(health, retry_counts)

    # Key is still present (count incremented to 1 since the job was re-enqueued)
    assert "default/docs//data/active.txt" in retry_counts


@pytest.mark.asyncio
async def test_retry_same_file_different_collections_separate_counts(
    tmp_path: Path,
) -> None:
    """Same source_path in two different collections tracked independently."""
    job_a = _make_failed_ingest_job("/data/file.txt", collection="col-a", namespace="ns1")
    job_b = _make_failed_ingest_job("/data/file.txt", collection="col-b", namespace="ns1")
    js = MagicMock()
    js.list.return_value = [job_a, job_b]
    js.create.return_value = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3)

    health: dict = {}
    retry_counts: dict = {}
    await loop._run_failed_ingest_retry(health, retry_counts)

    key_a = "ns1/col-a//data/file.txt"
    key_b = "ns1/col-b//data/file.txt"
    assert retry_counts.get(key_a) == 1
    assert retry_counts.get(key_b) == 1


# ---------------------------------------------------------------------------
# Unit tests: last_retry_at update in health state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_updates_last_retry_at_in_health_state(tmp_path: Path) -> None:
    """When a job is re-enqueued, last_retry_at is set in the health state for that collection."""
    job = _make_failed_ingest_job("/data/file.txt", collection="docs", namespace="default")
    js = MagicMock()
    js.list.return_value = [job]
    js.create.return_value = MagicMock()

    loop = _make_loop(tmp_path, job_store=js)

    health: dict = {}
    retry_counts: dict = {}
    await loop._run_failed_ingest_retry(health, retry_counts)

    col_health = health.get("default/docs", {})
    assert col_health.get("last_retry_at") is not None


# ---------------------------------------------------------------------------
# Integration test: re-enqueues into real JobStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_retry_reenqueues_into_job_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """make_real_pipeline: insert FAILED IngestJob; run _run_failed_ingest_retry();
    assert new job in JobStore.list() with source='maintenance'."""
    from tests.integration.conftest import make_real_pipeline

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    store, _pipeline = await make_real_pipeline(tmp_path, monkeypatch)
    job_store = JobStore(path=tmp_path / "jobs.json")

    # Insert a FAILED IngestJob directly
    failed_job = job_store.create(
        namespace="default",
        source="user",
        path=str(tmp_path / "test_file.txt"),
        collection="docs",
    )
    job_store.update(failed_job.job_id, status=JobStatus.FAILED, error="ingest error")

    # Build maintenance loop with the real job_store
    loop = _make_loop(
        tmp_path,
        job_store=job_store,
        search_store=store,
        retry_max_attempts=3,
        retry_max_age_hours=72,
    )

    health: dict = {}
    retry_counts: dict = {}
    await loop._run_failed_ingest_retry(health, retry_counts)

    await store.disconnect()

    # Assert that a new job with source="maintenance" was created
    all_jobs = job_store.list()
    maintenance_jobs = [j for j in all_jobs if j.source == "maintenance"]
    assert len(maintenance_jobs) >= 1, (
        f"Expected at least 1 job with source='maintenance'; got: "
        f"{[(j.job_id, j.source, j.status) for j in all_jobs]}"
    )
    # The new job should reference the same file path and collection
    new_job = maintenance_jobs[0]
    assert new_job.source_path == str(tmp_path / "test_file.txt")
    assert new_job.collection == "docs"
    assert new_job.namespace == "default"


# ---------------------------------------------------------------------------
# Unit tests: deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_deduplication_multiple_failed_jobs_same_path(tmp_path: Path) -> None:
    """Two FAILED jobs for same path should only trigger ONE re-enqueue per pass."""
    job1 = _make_failed_ingest_job("/data/file.txt", collection="docs", namespace="ns1")
    job2 = _make_failed_ingest_job("/data/file.txt", collection="docs", namespace="ns1")
    job2.job_id = "job-2"  # ensure different IDs
    js = MagicMock()
    js.list.return_value = [job1, job2]
    js.create.return_value = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3)
    health: dict = {}
    retry_counts: dict = {}
    await loop._run_failed_ingest_retry(health, retry_counts)

    # Only ONE create() call for the path, not two
    assert js.create.call_count == 1
    # Count incremented once, not twice
    key = "ns1/docs//data/file.txt"
    assert retry_counts.get(key) == 1


# ---------------------------------------------------------------------------
# S499: retry_max_age_hours = 0 disables retry entirely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_max_age_hours_zero_expires_all_jobs(tmp_path: Path) -> None:
    """S499: retry_max_age_hours=0 means every FAILED job is aged out immediately.

    No job is ever young enough → all transition to FAILED_EXPIRED, none re-enqueued.
    """
    job = _make_failed_ingest_job(
        "/data/file.txt", collection="docs", namespace="default", age_hours=0.001
    )
    js = MagicMock()
    js.list.return_value = [job]
    js.transition.return_value = MagicMock()

    loop = _make_loop(
        tmp_path, job_store=js, retry_max_age_hours=0, retry_max_attempts=3
    )

    health: dict = {}
    retry_counts: dict = {}
    await loop._run_failed_ingest_retry(health, retry_counts)

    # No re-enqueue — retry is disabled
    js.create.assert_not_called()

    # Job should be transitioned to FAILED_EXPIRED
    js.transition.assert_called_once_with(
        job.job_id,
        from_statuses={JobStatus.FAILED},
        to_status=JobStatus.FAILED_EXPIRED,
    )

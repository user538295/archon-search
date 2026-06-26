"""Tests for BE-5 — FAILED_EXPIRED transition in MaintenanceLoop.

TDD: tests written before implementation.

Covers S11, S11b, S12, S12b:
- S11: FAILED within age, retry_count < max → re-enqueued (stays FAILED until next attempt)
- S11b: FAILED within age, retry_count >= max → transitions to FAILED_EXPIRED
- S12: FAILED older than cutoff, retry_count >= max → transitions to FAILED_EXPIRED
- S12b: FAILED older than cutoff, retry_count < max → transitions to FAILED_EXPIRED (aged-out)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

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
    """Create a FAILED IngestJob with the given age and retry_count."""
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


def _make_failed_expired_job(
    source_path: str,
    collection: str = "docs",
    namespace: str = "default",
) -> IngestJob:
    """Create a FAILED_EXPIRED IngestJob."""
    now = datetime.now(timezone.utc).isoformat()
    return IngestJob(
        job_id=f"job-fe-{source_path[-8:]}",
        status=JobStatus.FAILED_EXPIRED,
        created_at=now,
        updated_at=now,
        namespace=namespace,
        source_path=source_path,
        collection=collection,
    )


# ---------------------------------------------------------------------------
# Unit tests: FAILED_EXPIRED transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintenance_loop_skips_failed_job_within_age_limit(tmp_path: Path) -> None:
    """S11: FAILED job within cutoff AND retry_count < max_attempts → re-enqueued (not transitioned to FAILED_EXPIRED)."""
    job = _make_failed_ingest_job(
        "/data/file.txt",
        collection="docs",
        namespace="default",
        age_hours=1.0,  # within 72-hour cutoff
        retry_count=0,  # < max_attempts=3
    )
    js = MagicMock()
    js.list.return_value = [job]
    js.create.return_value = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=72)

    health: dict = {}
    retry_counts: dict = {}
    await loop._run_failed_ingest_retry(health, retry_counts)

    # Should re-enqueue (not transition to FAILED_EXPIRED)
    js.create.assert_called_once()
    js.transition.assert_not_called()

    # retry count incremented
    key = "default/docs//data/file.txt"
    assert retry_counts.get(key) == 1


@pytest.mark.asyncio
async def test_maintenance_loop_within_age_but_retries_exhausted_transitions_to_failed_expired(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S11b: FAILED job within cutoff AND retry_count >= max_attempts → transitions to FAILED_EXPIRED."""
    job = _make_failed_ingest_job(
        "/data/file.txt",
        collection="docs",
        namespace="default",
        age_hours=1.0,  # within 72-hour cutoff
        retry_count=3,  # >= max_attempts=3
    )
    js = MagicMock()
    js.list.return_value = [job]

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=72)

    health: dict = {}
    retry_counts: dict = {"default/docs//data/file.txt": 3}

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health, retry_counts)

    # Should NOT re-enqueue
    js.create.assert_not_called()
    # Should call transition() to move to FAILED_EXPIRED
    js.transition.assert_called_once_with(job.job_id, from_statuses={JobStatus.FAILED}, to_status=JobStatus.FAILED_EXPIRED)
    # WARNING must be logged
    assert any("FAILED_EXPIRED" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


@pytest.mark.asyncio
async def test_maintenance_loop_transitions_failed_job_to_failed_expired(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S12: FAILED job older than cutoff, retry_count >= max_attempts → transitions to FAILED_EXPIRED."""
    job = _make_failed_ingest_job(
        "/data/old.txt",
        collection="docs",
        namespace="default",
        age_hours=100.0,  # older than 72-hour cutoff
        retry_count=3,    # >= max_attempts=3
    )
    js = MagicMock()
    js.list.return_value = [job]

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=72)

    health: dict = {}
    retry_counts: dict = {"default/docs//data/old.txt": 3}

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health, retry_counts)

    js.create.assert_not_called()
    js.transition.assert_called_once_with(job.job_id, from_statuses={JobStatus.FAILED}, to_status=JobStatus.FAILED_EXPIRED)
    assert any("FAILED_EXPIRED" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


@pytest.mark.asyncio
async def test_maintenance_loop_aged_job_under_max_attempts_transitions_to_failed_expired(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S12b: FAILED job older than cutoff, retry_count < max_attempts → transitions to FAILED_EXPIRED (cannot retry due to age)."""
    job = _make_failed_ingest_job(
        "/data/aged.txt",
        collection="docs",
        namespace="default",
        age_hours=100.0,  # older than 72-hour cutoff
        retry_count=1,    # < max_attempts=3
    )
    js = MagicMock()
    js.list.return_value = [job]

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=72)

    health: dict = {}
    retry_counts: dict = {"default/docs//data/aged.txt": 1}

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health, retry_counts)

    # Even though retry_count < max_attempts, job is too old — FAILED_EXPIRED
    js.create.assert_not_called()
    js.transition.assert_called_once_with(job.job_id, from_statuses={JobStatus.FAILED}, to_status=JobStatus.FAILED_EXPIRED)
    assert any("FAILED_EXPIRED" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


@pytest.mark.asyncio
async def test_maintenance_loop_does_not_reenqueue_failed_expired(tmp_path: Path) -> None:
    """FAILED_EXPIRED job → no new job created (already terminal)."""
    job = _make_failed_expired_job("/data/expired.txt", collection="docs", namespace="default")
    js = MagicMock()
    js.list.return_value = [job]

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=72)

    health: dict = {}
    retry_counts: dict = {}
    await loop._run_failed_ingest_retry(health, retry_counts)

    js.create.assert_not_called()
    js.transition.assert_not_called()


# ---------------------------------------------------------------------------
# New tests: Fix 4 additions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintenance_loop_exhausted_check_uses_retry_counts_dict_not_job_field(tmp_path: Path) -> None:
    """Retry-exhausted check uses retry_counts dict, not job.retry_count field.

    job.retry_count=0 but retry_counts["key"]=3 → FAILED_EXPIRED (dict is authoritative).
    """
    job = _make_failed_ingest_job("/data/file.txt", age_hours=1.0, retry_count=0)  # job field says 0
    js = MagicMock()
    js.list.return_value = [job]
    js.transition.return_value = MagicMock()  # non-None = success
    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=72)
    # Dict says 3 (authoritative)
    retry_counts = {"default/docs//data/file.txt": 3}
    await loop._run_failed_ingest_retry({}, retry_counts)
    js.create.assert_not_called()
    js.transition.assert_called_once_with(job.job_id, from_statuses={JobStatus.FAILED}, to_status=JobStatus.FAILED_EXPIRED)


@pytest.mark.asyncio
async def test_maintenance_loop_no_age_limit_expired_via_retry_exhaustion(tmp_path: Path) -> None:
    """retry_max_age_hours=0 disables age filter; FAILED_EXPIRED still fires via retry exhaustion."""
    job = _make_failed_ingest_job("/data/file.txt", age_hours=200.0, retry_count=0)
    js = MagicMock()
    js.list.return_value = [job]
    js.transition.return_value = MagicMock()
    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=0)
    retry_counts = {"default/docs//data/file.txt": 3}
    await loop._run_failed_ingest_retry({}, retry_counts)
    js.create.assert_not_called()
    js.transition.assert_called_once_with(job.job_id, from_statuses={JobStatus.FAILED}, to_status=JobStatus.FAILED_EXPIRED)


@pytest.mark.asyncio
async def test_maintenance_loop_no_age_limit_eligible_job_is_reenqueued(tmp_path: Path) -> None:
    """retry_max_age_hours=0 disables age filter; very old job with remaining retries is still re-enqueued."""
    job = _make_failed_ingest_job("/data/file.txt", age_hours=200.0, retry_count=0)
    js = MagicMock()
    js.list.return_value = [job]
    js.create.return_value = MagicMock()
    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=0)
    retry_counts: dict = {}  # dict says 0 (within attempts)
    await loop._run_failed_ingest_retry({}, retry_counts)
    js.create.assert_called_once()
    js.transition.assert_not_called()


@pytest.mark.asyncio
async def test_maintenance_loop_failed_expired_dedup_same_key(tmp_path: Path) -> None:
    """Two aged FAILED jobs with same retry_key: only first is transitioned to FAILED_EXPIRED per pass."""
    job1 = _make_failed_ingest_job("/data/file.txt", age_hours=100.0, retry_count=0)
    job2 = _make_failed_ingest_job("/data/file.txt", age_hours=50.0, retry_count=0)
    job2.job_id = "job-file-2"
    js = MagicMock()
    js.list.return_value = [job1, job2]
    js.transition.return_value = MagicMock()
    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=72)
    await loop._run_failed_ingest_retry({}, {})
    # Only one transition call (dedup prevents second)
    assert js.transition.call_count == 1
    js.create.assert_not_called()


@pytest.mark.asyncio
async def test_maintenance_loop_invalid_created_at_skips_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Job with unparseable created_at is skipped; WARNING is logged."""
    now = datetime.now(timezone.utc).isoformat()
    job = IngestJob(
        job_id="job-corrupt",
        status=JobStatus.FAILED,
        created_at="not-a-valid-timestamp",
        updated_at=now,
        namespace="default",
        source_path="/data/corrupt.txt",
        collection="docs",
    )
    js = MagicMock()
    js.list.return_value = [job]
    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=72)
    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry({}, {})
    js.create.assert_not_called()
    js.transition.assert_not_called()
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# Integration tests: real JobStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_maintenance_loop_failed_expired_via_real_job_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real JobStore: aged FAILED job (retry-exhausted) transitions to FAILED_EXPIRED after maintenance pass."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    job_store = JobStore(path=tmp_path / "jobs.json")

    # Seed: aged FAILED IngestJob with retry_count already at max
    old_created = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    job = IngestJob(
        job_id="test-aged-job",
        status=JobStatus.FAILED,
        created_at=old_created,
        updated_at=old_created,
        namespace="default",
        source_path=str(tmp_path / "aged_file.txt"),
        collection="docs",
        retry_count=3,
    )
    job_store.create_job(job)

    search_store = MagicMock()

    loop = _make_loop(
        tmp_path,
        job_store=job_store,
        search_store=search_store,
        retry_max_attempts=3,
        retry_max_age_hours=72,
    )

    # Pre-seed retry_counts to match job's retry_count
    retry_counts: dict = {f"default/docs/{tmp_path / 'aged_file.txt'}": 3}
    health: dict = {}
    await loop._run_failed_ingest_retry(health, retry_counts)

    # The job must be transitioned to FAILED_EXPIRED
    stored_job = job_store.get("test-aged-job")
    assert stored_job is not None
    assert stored_job.status == JobStatus.FAILED_EXPIRED, (
        f"Expected FAILED_EXPIRED, got {stored_job.status!r}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_maintenance_loop_recent_failed_job_stays_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real JobStore: recent FAILED job (within cutoff, retry_count < max) stays FAILED (is re-enqueued)."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    job_store = JobStore(path=tmp_path / "jobs.json")

    # Seed: recent FAILED IngestJob within cutoff
    file_path = str(tmp_path / "recent_file.txt")
    failed_job = job_store.create(
        namespace="default",
        source="user",
        path=file_path,
        collection="docs",
    )
    job_store.update(failed_job.job_id, status=JobStatus.FAILED, error="ingest error")

    search_store = MagicMock()

    loop = _make_loop(
        tmp_path,
        job_store=job_store,
        search_store=search_store,
        retry_max_attempts=3,
        retry_max_age_hours=72,
    )

    health: dict = {}
    retry_counts: dict = {}
    await loop._run_failed_ingest_retry(health, retry_counts)

    # Original job should still be FAILED (not transitioned to FAILED_EXPIRED)
    original = job_store.get(failed_job.job_id)
    assert original is not None
    assert original.status == JobStatus.FAILED, (
        f"Expected FAILED, got {original.status!r}"
    )

    # A new maintenance job should have been created
    all_jobs = job_store.list()
    maintenance_jobs = [j for j in all_jobs if j.source == "maintenance"]
    assert len(maintenance_jobs) >= 1, (
        f"Expected at least 1 maintenance re-enqueue; got: {[j.source for j in all_jobs]}"
    )

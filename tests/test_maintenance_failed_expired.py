"""Tests for BE-5 — FAILED_EXPIRED transition in MaintenanceLoop.

TDD: tests written before implementation.

Covers S11, S11b, S12, S12b:
- S11: FAILED within age, retry_count < max → re-enqueued (stays FAILED until next attempt)
- S11b: FAILED within age, retry_count >= max → transitions to FAILED_EXPIRED
- S12: FAILED older than cutoff, retry_count >= max → transitions to FAILED_EXPIRED
- S12b: FAILED older than cutoff, retry_count < max → transitions to FAILED_EXPIRED (aged-out)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from archon_search.config import MaintenanceConfig
from archon_search.jobs.maintenance_loop import MaintenanceLoop
from archon_search.jobs.store import JobStore
from archon_search.types import IngestJob, IngestOptions, JobStatus

#: The POST /ingest request a retryable job carries (a job without one is never retried).
_OPTIONS = IngestOptions(submitted_path="/data/file.txt", ingested_by="http")

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
    return MaintenanceLoop(dispatch_retry=lambda _job: None, job_store=js, search_store=ss, config=cfg, data_dir=tmp_path)


def _make_failed_ingest_job(
    source_path: str,
    collection: str = "docs",
    namespace: str = "default",
    *,
    age_hours: float = 1.0,
    retry_count: int = 0,
) -> IngestJob:
    """Create a FAILED IngestJob with the given age and retry_count — a maintenance retry when retry_count > 0."""
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
        source="maintenance" if retry_count else "user",
        ingest_options=_OPTIONS,
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
    await loop._run_failed_ingest_retry(health)

    # Should re-enqueue (not transition to FAILED_EXPIRED)
    js.create.assert_called_once()
    js.transition.assert_not_called()

    assert js.create.call_args.kwargs["retry_count"] == 1


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

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health)

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

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health)

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

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health)

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
    await loop._run_failed_ingest_retry(health)

    js.create.assert_not_called()
    js.transition.assert_not_called()


# ---------------------------------------------------------------------------
# New tests: Fix 4 additions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("retry_count", "exhausted"), [(2, False), (3, True)])
async def test_maintenance_loop_exhausted_check_reads_the_submissions_retry_count(
    tmp_path: Path, retry_count: int, exhausted: bool
) -> None:
    """The highest ``retry_count`` among the submission's retries (which attempt it was) is the attempt count: below
    ``retry_max_attempts`` the path is retried, at it the chain ends FAILED_EXPIRED."""
    job = _make_failed_ingest_job("/data/file.txt", age_hours=1.0, retry_count=retry_count)
    js = MagicMock()
    js.list.return_value = [job]
    js.transition.return_value = MagicMock()  # non-None = success
    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=72)
    await loop._run_failed_ingest_retry({})
    assert js.create.called is not exhausted
    assert js.transition.called is exhausted


@pytest.mark.asyncio
async def test_maintenance_loop_age_zero_expires_regardless_of_retry_budget(tmp_path: Path) -> None:
    """retry_max_age_hours=0 means cutoff=now, so every job is immediately aged out (FAILED_EXPIRED)."""
    job = _make_failed_ingest_job("/data/file.txt", age_hours=200.0, retry_count=0)
    js = MagicMock()
    js.list.return_value = [job]
    js.transition.return_value = MagicMock()
    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=0)
    await loop._run_failed_ingest_retry({})
    js.create.assert_not_called()
    js.transition.assert_called_once_with(job.job_id, from_statuses={JobStatus.FAILED}, to_status=JobStatus.FAILED_EXPIRED)


@pytest.mark.asyncio
async def test_maintenance_loop_failed_expired_dedup_same_key(tmp_path: Path) -> None:
    """Two aged FAILED jobs with same retry_key: the key is decided once, via the newest job, and ending its chain
    expires both in that pass — one transition each, nothing re-enqueued."""
    job1 = _make_failed_ingest_job("/data/file.txt", age_hours=100.0, retry_count=0)
    job2 = _make_failed_ingest_job("/data/file.txt", age_hours=80.0, retry_count=0)
    job2.job_id = "job-file-2"
    js = MagicMock()
    js.list.return_value = [job1, job2]
    js.transition.return_value = MagicMock()
    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=72)
    await loop._run_failed_ingest_retry({})
    expired = {"from_statuses": {JobStatus.FAILED}, "to_status": JobStatus.FAILED_EXPIRED}
    assert sorted((c.args[0], c.kwargs == expired) for c in js.transition.call_args_list) == [
        ("job-file-2", True), ("job-file.txt", True),
    ]
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
        ingest_options=_OPTIONS,
    )
    js = MagicMock()
    js.list.return_value = [job]
    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=72)
    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry({})
    js.create.assert_not_called()
    js.transition.assert_not_called()
    assert any(
        r.levelno == logging.WARNING and "job-corrupt" in r.getMessage() and "unparseable created_at" in r.getMessage()
        for r in caplog.records
    )


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
        ingest_options=_OPTIONS,
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

    health: dict = {}
    await loop._run_failed_ingest_retry(health)

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
        ingest_options=IngestOptions(submitted_path=file_path, ingested_by="http"),
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
    await loop._run_failed_ingest_retry(health)

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


@pytest.mark.asyncio
async def test_failed_expired_transition_write_failure_keeps_job_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Real JobStore: when the FAILED->FAILED_EXPIRED persist fails, the pass logs it (WARNING) and goes on, and the
    job stays FAILED in memory and on disk (consistent), so the next pass retries the expiry instead of skipping a job
    memory calls expired."""
    from tests._job_store_faults import failing_job_writes, persisted_statuses

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    jobs_path = tmp_path / "jobs.json"
    job_store = JobStore(path=jobs_path)
    aged = job_store.create_job(_make_failed_ingest_job(str(tmp_path / "old_file.txt"), age_hours=100.0))
    loop = _make_loop(tmp_path, job_store=job_store, search_store=MagicMock(), retry_max_age_hours=72)

    with (
        failing_job_writes() as writes,
        caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"),
    ):
        await loop._run_failed_ingest_retry({})  # must not raise

    assert any(f"could not expire job {aged.job_id}" in r.getMessage() for r in caplog.records)

    assert writes.statuses_of(aged.job_id)[0] == JobStatus.FAILED_EXPIRED.value  # the expiry was attempted
    failed = job_store.get(aged.job_id)
    assert failed is not None
    assert failed.status == JobStatus.FAILED
    assert persisted_statuses(jobs_path)[aged.job_id] == JobStatus.FAILED.value

    await loop._run_failed_ingest_retry({})  # next pass, writes succeed

    expired = job_store.get(aged.job_id)
    assert expired is not None
    assert expired.status == JobStatus.FAILED_EXPIRED
    assert persisted_statuses(jobs_path)[aged.job_id] == JobStatus.FAILED_EXPIRED.value

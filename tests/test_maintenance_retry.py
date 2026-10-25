"""Tests for MaintenanceLoop failed-ingest retry policy (BE-8).

Plan: Documentation/Backlog/D5-maintenance-jobs-policies-team-plan.md Task BE-8

TDD: tests written first, then _run_failed_ingest_retry implementation in
archon_search/jobs/maintenance_loop.py.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from archon_search.config import MaintenanceConfig
from archon_search.jobs.maintenance_loop import (
    RETRY_DISPATCH_FAILED_DETAIL,
    MaintenanceLoop,
)
from archon_search.jobs.store import (
    NOT_STARTED_BEFORE_RESTART_DETAIL,
    PROCESS_RESTART_ERROR,
    JobStore,
)
from archon_search.types import IngestJob, IngestOptions, JobStatus, ReindexJob
from tests._internal_error_leak import LEAK_NEEDLE

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
    dispatch_retry: Any = None,
) -> MaintenanceLoop:
    """*dispatch_retry* defaults to a MagicMock that records the retry jobs it is handed."""
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
    return MaintenanceLoop(
        job_store=js,
        search_store=ss,
        config=cfg,
        data_dir=tmp_path,
        dispatch_retry=dispatch_retry if dispatch_retry is not None else MagicMock(),
    )


#: The original request's options a retry must carry (``IngestJob.ingest_options``).
_OPTIONS = IngestOptions(submitted_path="~/file.txt", ingested_by="cli", chunk_ttl_seconds=3600, chunk_scopes=("team-a",))


def _make_failed_ingest_job(
    source_path: str,
    collection: str = "docs",
    namespace: str = "default",
    *,
    age_hours: float = 1.0,
    retry_count: int = 0,
    status: JobStatus = JobStatus.FAILED,
    job_id: str | None = None,
    created_at: str | None = None,
    ingest_options: IngestOptions | None = _OPTIONS,
    **fields: Any,
) -> IngestJob:
    """Create an IngestJob, FAILED unless *status* says otherwise, created *age_hours* ago (or at *created_at*),
    carrying the POST /ingest request *ingest_options* (``None``: a legacy job whose request is unknown).

    *fields* sets any other ``IngestJob`` field (``source``, ``error``, ...).
    """
    created = created_at or (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    return IngestJob(
        job_id=job_id or f"job-{source_path[-8:]}",
        status=status,
        created_at=created,
        updated_at=created,
        namespace=namespace,
        source_path=source_path,
        collection=collection,
        retry_count=retry_count,
        ingest_options=ingest_options,
        **fields,
    )


def _make_done_ingest_job(
    source_path: str,
    collection: str = "docs",
    namespace: str = "default",
) -> IngestJob:
    """Create a DONE IngestJob for reset-on-done testing."""
    now = datetime.now(UTC).isoformat()
    return IngestJob(
        job_id=f"done-{source_path[-8:]}",
        status=JobStatus.DONE,
        created_at=now,
        updated_at=now,
        namespace=namespace,
        source_path=source_path,
        collection=collection,
    )


# ---------------------------------------------------------------------------
# Unit tests: core retry logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_eligible_job_is_reenqueued(tmp_path: Path) -> None:
    """S13: FAILED job within age and attempt limit is re-enqueued with source='maintenance' and the original
    request's options — a retry without its chunk_scopes would write chunks every scope filter matches."""
    job = _make_failed_ingest_job("/data/file.txt", collection="docs", namespace="ns1")
    js = MagicMock()
    js.list.return_value = [job]
    js.create.return_value = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3, retry_max_age_hours=72)

    health: dict = {}
    await loop._run_failed_ingest_retry(health)

    js.create.assert_called_once_with(
        path="/data/file.txt",
        collection="docs",
        namespace="ns1",
        source="maintenance",
        ingest_options=_OPTIONS,
        retry_count=1,
    )


@pytest.mark.asyncio
async def test_retry_max_attempts_reached_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """S14: the newest FAILED job is the last allowed retry (``retry_count`` at max) → WARNING logged; no new job."""
    job = _make_failed_ingest_job(
        "/data/file.txt", collection="docs", namespace="default", retry_count=3, source="maintenance"
    )
    js = MagicMock()
    js.list.return_value = [job]

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3)
    health: dict = {}

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health)

    js.create.assert_not_called()
    assert any(
        r.levelno == logging.WARNING and "has reached max retry attempts (3)" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_retry_too_old_filtered(tmp_path: Path) -> None:
    """S15: job older than retry_max_age_hours is not re-enqueued."""
    # Create a job that is 100 hours old with retry_max_age_hours=72
    job = _make_failed_ingest_job("/data/old.txt", collection="docs", namespace="default", age_hours=100.0)
    js = MagicMock()
    js.list.return_value = [job]

    loop = _make_loop(tmp_path, job_store=js, retry_max_age_hours=72)

    health: dict = {}
    await loop._run_failed_ingest_retry(health)

    js.create.assert_not_called()


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

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health)

    js.create.assert_not_called()
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_retry_disabled_by_config(tmp_path: Path) -> None:
    """failed_ingest_retry=False → JobStore.list() never called."""
    js = MagicMock()
    js.list.return_value = []

    loop = _make_loop(tmp_path, job_store=js, failed_ingest_retry=False)

    health: dict = {}
    await loop._run_failed_ingest_retry(health)

    js.list.assert_not_called()


@pytest.mark.asyncio
async def test_retry_skips_jobs_with_empty_source_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Pre-D5 FAILED IngestJob with source_path='' → skipped; DEBUG logged; no JobStore.create()."""
    job = IngestJob(
        job_id="pre-d5-job",
        status=JobStatus.FAILED,
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
        namespace="default",
        source_path="",  # pre-D5 job has no source path
        collection="docs",
    )
    js = MagicMock()
    js.list.return_value = [job]

    loop = _make_loop(tmp_path, job_store=js)

    health: dict = {}
    with caplog.at_level(logging.DEBUG, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health)

    js.create.assert_not_called()
    assert any("skipping pre-D5 FAILED job pre-d5-job" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_retry_ingest_file_raises_during_reenqueue(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """JobStore.create() raises during retry → WARNING logged; pass continues to next job (no exception
    propagation). No job was created, so no attempt was used: the next pass tries the same attempt again."""
    job1 = _make_failed_ingest_job("/data/fail.txt", collection="docs", namespace="default")
    job2 = _make_failed_ingest_job("/data/ok.txt", collection="docs", namespace="default")
    js = MagicMock()
    js.list.return_value = [job1, job2]
    js.create.side_effect = [RuntimeError("db write failed"), MagicMock()]

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3)

    health: dict = {}
    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry(health)  # must not raise

    # Both jobs should have been attempted
    assert js.create.call_count == 2

    assert [c.kwargs["retry_count"] for c in js.create.call_args_list] == [1, 1]

    # WARNING logged for the create failure
    assert any(
        r.levelno == logging.WARNING and "failed to re-enqueue job for /data/fail.txt" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Unit tests: retry keys
# ---------------------------------------------------------------------------


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
    await loop._run_failed_ingest_retry(health)

    assert [(c.kwargs["collection"], c.kwargs["retry_count"]) for c in js.create.call_args_list] == [
        ("col-a", 1), ("col-b", 1),
    ]


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
    await loop._run_failed_ingest_retry(health)

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
        ingest_options=_OPTIONS,
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
    await loop._run_failed_ingest_retry(health)

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
    assert new_job.ingest_options == _OPTIONS
    assert new_job.retry_count == 1, "GET /jobs reports which retry attempt the job is"


# ---------------------------------------------------------------------------
# Unit tests: deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_deduplication_multiple_failed_jobs_same_path(tmp_path: Path) -> None:
    """Two FAILED jobs for same path should only trigger ONE re-enqueue per pass."""
    created_at = datetime.now(UTC).isoformat()  # one timestamp: the tie-break, not the clock, picks one
    job1 = _make_failed_ingest_job("/data/file.txt", collection="docs", namespace="ns1", created_at=created_at)
    job2 = _make_failed_ingest_job(
        "/data/file.txt", collection="docs", namespace="ns1", created_at=created_at, job_id="job-2"
    )
    js = MagicMock()
    js.list.return_value = [job1, job2]
    js.create.return_value = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3)
    health: dict = {}
    await loop._run_failed_ingest_retry(health)

    # Only ONE create() call for the path, not two
    assert js.create.call_count == 1
    assert js.create.call_args.kwargs["retry_count"] == 1


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
    await loop._run_failed_ingest_retry(health)

    # No re-enqueue — retry is disabled
    js.create.assert_not_called()

    # Job should be transitioned to FAILED_EXPIRED
    js.transition.assert_called_once_with(
        job.job_id,
        from_statuses={JobStatus.FAILED},
        to_status=JobStatus.FAILED_EXPIRED,
    )


# ---------------------------------------------------------------------------
# Retry execution: the re-enqueued job is handed to the injected dispatch_retry
# ---------------------------------------------------------------------------

_KEY = "default/docs//data/file.txt"


@pytest.mark.asyncio
async def test_retry_dispatches_the_reenqueued_job(tmp_path: Path) -> None:
    """The retry job JobStore.create() returns is handed to the dispatcher, which runs it."""
    job = _make_failed_ingest_job("/data/file.txt")
    retry_job = MagicMock()
    js = MagicMock()
    js.list.return_value = [job]
    js.create.return_value = retry_job
    dispatch = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch)
    await loop._run_failed_ingest_retry({})

    dispatch.assert_called_once_with(retry_job)


@pytest.mark.asyncio
async def test_retry_not_dispatched_when_create_fails(tmp_path: Path) -> None:
    """No job was created, so nothing is dispatched."""
    js = MagicMock()
    js.list.return_value = [_make_failed_ingest_job("/data/file.txt")]
    js.create.side_effect = RuntimeError("db write failed")
    dispatch = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch)
    await loop._run_failed_ingest_retry({})

    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_retry_dispatch_failure_marks_retry_failed_sanitized(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A dispatch that raises must not leave a PENDING retry nothing runs: it is persisted FAILED with the
    sanitized detail (never the exception text), the cause is logged, and the pass continues."""
    jobs_path = tmp_path / "jobs.json"
    js = JobStore(path=jobs_path)
    original = js.create(path="/data/file.txt", collection="docs", ingest_options=_OPTIONS)
    js.update(original.job_id, status=JobStatus.FAILED, error="boom")
    dispatch = MagicMock(side_effect=RuntimeError(LEAK_NEEDLE))

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch)
    with caplog.at_level(logging.ERROR, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry({})

    (retry,) = [j for j in JobStore(path=jobs_path).list() if j.source == "maintenance"]
    assert (retry.status, retry.error) == (JobStatus.FAILED, RETRY_DISPATCH_FAILED_DETAIL)
    assert LEAK_NEEDLE not in jobs_path.read_text(), "the exception text must never reach the persisted job"
    assert any(r.exc_info for r in caplog.records), "the dispatch failure must be logged with its traceback"


@pytest.mark.asyncio
async def test_retry_dispatch_and_failed_write_both_raising_does_not_propagate(tmp_path: Path) -> None:
    """When the dispatch raises AND the FAILED write raises too, the pass still completes: ``last_retry_at`` is
    recorded, and the collection's ``last_error`` names the stuck retry (sanitized)."""
    js = MagicMock()
    js.list.return_value = [_make_failed_ingest_job("/data/file.txt")]
    js.update.side_effect = OSError(LEAK_NEEDLE)
    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=MagicMock(side_effect=RuntimeError(LEAK_NEEDLE)))

    health: dict[str, Any] = {}
    await loop._run_failed_ingest_retry(health)

    js.update.assert_called_once()
    assert health["default/docs"]["last_error"] == RETRY_DISPATCH_FAILED_DETAIL
    assert health["default/docs"]["last_retry_at"] is not None


@pytest.mark.asyncio
async def test_retry_dispatch_failure_of_one_job_still_dispatches_the_next(tmp_path: Path) -> None:
    """A dispatch failure for one path neither stops nor skips the next path's retry."""
    js = MagicMock()
    js.list.return_value = [_make_failed_ingest_job("/data/one.txt"), _make_failed_ingest_job("/data/two.txt")]
    first, second = MagicMock(name="retry-one"), MagicMock(name="retry-two")
    js.create.side_effect = [first, second]
    dispatch = MagicMock(side_effect=[RuntimeError("boom"), None])

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch)
    await loop._run_failed_ingest_retry({})

    assert dispatch.call_args_list == [call(first), call(second)]


# ---------------------------------------------------------------------------
# Supersession: only the newest job of a retry key is retried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_status", [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.DONE])
async def test_retry_skips_failed_job_superseded_by_newer_job(tmp_path: Path, newer_status: JobStatus) -> None:
    """A FAILED job with a newer job for the same path is not retried — nor expired: that retry is still
    outstanding (PENDING/RUNNING) or already succeeded (DONE); otherwise every pass would re-ingest the file."""
    failed = _make_failed_ingest_job("/data/file.txt", job_id="old", age_hours=2.0)
    newer = _make_failed_ingest_job("/data/file.txt", status=newer_status, job_id="new", age_hours=1.0)
    js = MagicMock()
    js.list.return_value = [failed, newer]
    dispatch = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch)
    await loop._run_failed_ingest_retry({})

    js.create.assert_not_called()
    js.transition.assert_not_called()
    dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_status", [JobStatus.CANCELLED, JobStatus.CANCELLING, JobStatus.FAILED_EXPIRED])
async def test_retry_newer_job_without_an_outcome_does_not_supersede(tmp_path: Path, newer_status: JobStatus) -> None:
    """A newer CANCELLED, CANCELLING or FAILED_EXPIRED job settled nothing about the work: the FAILED job is
    still retried."""
    failed = _make_failed_ingest_job("/data/file.txt", job_id="old", age_hours=2.0)
    newer = _make_failed_ingest_job("/data/file.txt", status=newer_status, job_id="new", age_hours=1.0)
    js = MagicMock()
    js.list.return_value = [failed, newer]

    loop = _make_loop(tmp_path, job_store=js)
    await loop._run_failed_ingest_retry({})

    js.create.assert_called_once()
    assert js.create.call_args.kwargs["path"] == "/data/file.txt"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("collection", "namespace"), [("other-col", "default"), ("docs", "other-ns")], ids=["collection", "namespace"]
)
async def test_retry_newer_job_for_another_key_does_not_supersede(
    tmp_path: Path, collection: str, namespace: str
) -> None:
    """A newer DONE job for the same path in another collection or namespace is another retry key."""
    failed = _make_failed_ingest_job("/data/file.txt", job_id="old", age_hours=2.0)
    newer = _make_failed_ingest_job(
        "/data/file.txt", collection, namespace, status=JobStatus.DONE, job_id="new", age_hours=1.0
    )
    js = MagicMock()
    js.list.return_value = [failed, newer]

    loop = _make_loop(tmp_path, job_store=js)
    await loop._run_failed_ingest_retry({})

    js.create.assert_called_once()
    assert (js.create.call_args.kwargs["collection"], js.create.call_args.kwargs["namespace"]) == ("docs", "default")


@pytest.mark.asyncio
async def test_retry_retries_newest_failed_job_when_older_one_superseded(tmp_path: Path) -> None:
    """A newer FAILED job supersedes the older one: the path is retried once, via the newest job — with ITS request
    options (here a re-submission with other scopes), never the older job's."""
    older = _make_failed_ingest_job("/data/file.txt", job_id="old", age_hours=2.0)
    newest_options = IngestOptions(submitted_path="/data/file.txt", ingested_by="http", chunk_scopes=("team-b",))
    newest = _make_failed_ingest_job("/data/file.txt", job_id="new", age_hours=1.0, ingest_options=newest_options)
    retry_job = MagicMock()
    js = MagicMock()
    js.list.return_value = [older, newest]
    js.create.return_value = retry_job
    dispatch = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch)
    await loop._run_failed_ingest_retry({})

    js.create.assert_called_once_with(
        path="/data/file.txt", collection="docs", namespace="default", source="maintenance",
        ingest_options=newest_options, retry_count=1,
    )
    dispatch.assert_called_once_with(retry_job)
    js.transition.assert_not_called()


@pytest.mark.asyncio
async def test_retry_aged_out_older_job_does_not_block_young_newest_resubmission(tmp_path: Path) -> None:
    """An aged-out FAILED job listed BEFORE a young, newer FAILED re-submission of the same path neither expires
    the key nor claims the pass: the newest job is retried in the same pass, and the old one is left alone."""
    older = _make_failed_ingest_job("/data/file.txt", job_id="old", age_hours=100.0)
    newest = _make_failed_ingest_job("/data/file.txt", job_id="new", age_hours=1.0)
    js = MagicMock()
    js.list.return_value = [older, newest]

    loop = _make_loop(tmp_path, job_store=js, retry_max_age_hours=72)
    await loop._run_failed_ingest_retry({})

    js.create.assert_called_once()
    js.transition.assert_not_called()


@pytest.mark.asyncio
async def test_retry_superseded_original_is_not_expired_after_successful_retry(tmp_path: Path) -> None:
    """Once a retry succeeded, the aged-out original FAILED job is superseded — not expired, so it raises no
    ``failed_expired_ingest_count`` alarm for a file that is in fact ingested."""
    original = _make_failed_ingest_job("/data/file.txt", job_id="old", age_hours=100.0)
    retry_done = _make_failed_ingest_job(
        "/data/file.txt", status=JobStatus.DONE, job_id="new", age_hours=1.0, source="maintenance"
    )
    js = MagicMock()
    js.list.return_value = [original, retry_done]

    loop = _make_loop(tmp_path, job_store=js, retry_max_age_hours=72)
    await loop._run_failed_ingest_retry({})

    js.transition.assert_not_called()
    js.create.assert_not_called()


@pytest.mark.asyncio
async def test_retry_age_is_measured_from_the_original_failure(tmp_path: Path) -> None:
    """``retry_max_age_hours`` bounds the whole retry chain: a young FAILED retry of an ingest submitted past the
    cutoff is expired, not retried again — the age does not restart with each retry. Ending the chain expires every
    FAILED job of the path in the same pass, the superseded original included."""
    original = _make_failed_ingest_job("/data/file.txt", job_id="old", age_hours=80.0)
    young_retry = _make_failed_ingest_job("/data/file.txt", job_id="new", age_hours=1.0, source="maintenance")
    js = MagicMock()
    js.list.return_value = [original, young_retry]

    loop = _make_loop(tmp_path, job_store=js, retry_max_age_hours=72)
    await loop._run_failed_ingest_retry({})

    js.create.assert_not_called()
    assert _expired_ids(js) == ["new", "old"]


@pytest.mark.asyncio
async def test_retry_latest_job_tie_prefers_the_settled_outcome(tmp_path: Path) -> None:
    """On equal created_at a DONE job, not the FAILED one, is the latest: the FAILED job is not retried."""
    created_at = datetime.now(UTC).isoformat()
    failed = _make_failed_ingest_job("/data/file.txt", job_id="zzz", created_at=created_at)
    done = _make_failed_ingest_job("/data/file.txt", status=JobStatus.DONE, job_id="aaa", created_at=created_at)
    js = MagicMock()
    js.list.return_value = [failed, done]

    loop = _make_loop(tmp_path, job_store=js)
    await loop._run_failed_ingest_retry({})

    js.create.assert_not_called()


@pytest.mark.asyncio
async def test_retry_unparseable_created_at_does_not_hide_the_latest_job(tmp_path: Path) -> None:
    """One job with an unparseable created_at must not make every FAILED job of the key look newest: the
    newer DONE job still supersedes them, so nothing is retried."""
    failed = _make_failed_ingest_job("/data/file.txt", job_id="old", age_hours=2.0)
    garbled = _make_failed_ingest_job("/data/file.txt", job_id="garbled", created_at="not-a-timestamp")
    done = _make_failed_ingest_job("/data/file.txt", status=JobStatus.DONE, job_id="new", age_hours=1.0)
    js = MagicMock()
    js.list.return_value = [failed, garbled, done]

    loop = _make_loop(tmp_path, job_store=js)
    await loop._run_failed_ingest_retry({})

    js.create.assert_not_called()
    js.transition.assert_not_called()


def _expired_ids(js: MagicMock) -> list[str]:
    """Sorted ids of the jobs the loop transitioned FAILED -> FAILED_EXPIRED on the mocked job store."""
    expired = {"from_statuses": {JobStatus.FAILED}, "to_status": JobStatus.FAILED_EXPIRED}
    return sorted(c.args[0] for c in js.transition.call_args_list if c.kwargs == expired)


# ---------------------------------------------------------------------------
# Jobs whose original request is unknown, or that crashed the process, are never retried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_job_without_ingest_options_is_expired_never_retried(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A FAILED job with no stored request (saved by an older server) is expired: a retry would re-ingest without its
    chunk_scopes, making the chunks match every scope filter."""
    js = MagicMock()
    js.list.return_value = [_make_failed_ingest_job("/data/file.txt", job_id="legacy", ingest_options=None)]
    dispatch = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch)
    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry({})

    js.create.assert_not_called()
    dispatch.assert_not_called()
    assert _expired_ids(js) == ["legacy"]
    assert any(
        r.levelno == logging.WARNING and "job legacy" in r.getMessage() and "original request is unknown" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_retry_job_that_crashed_the_process_is_expired_never_retried(tmp_path: Path) -> None:
    """A FAILED ``process_restart`` ingest died with the process: retrying it would re-enter the workload the startup
    crash-loop guard keeps the server out of, so it is expired."""
    js = MagicMock()
    js.list.return_value = [_make_failed_ingest_job("/data/file.txt", job_id="crashed", error=PROCESS_RESTART_ERROR)]
    dispatch = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch)
    await loop._run_failed_ingest_retry({})

    js.create.assert_not_called()
    dispatch.assert_not_called()
    assert _expired_ids(js) == ["crashed"]


def _seed_jobs_file(jobs_path: Path, *rows: dict[str, Any]) -> None:
    """Write raw job rows as a server (this one or an older one) would have saved them."""
    now = datetime.now(UTC).isoformat()
    base = {"created_at": now, "updated_at": now, "result": None, "error": None, "namespace": "default",
            "source": "user", "collection": "docs", "job_type": "ingest"}
    jobs_path.write_text(json.dumps([{**base, **row} for row in rows]))


_OPTIONS_JSON = {"submitted_path": "~/file.txt", "ingested_by": "cli", "chunk_ttl_seconds": 3600,
                 "chunk_scopes": ["team-a"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_status", ["FAILED", "PENDING"])
async def test_retry_legacy_rows_loaded_from_an_older_server_are_expired_never_dispatched(
    tmp_path: Path, saved_status: str
) -> None:
    """Rows an older server saved (no ``ingest_options``) — FAILED, or PENDING (loaded as FAILED, never started) —
    load without options and the next pass expires them without dispatching anything."""
    jobs_path = tmp_path / "jobs.json"
    _seed_jobs_file(jobs_path, {"job_id": "legacy", "status": saved_status, "source_path": "/data/file.txt"})
    js = JobStore(path=jobs_path)
    dispatch = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch)
    await loop._run_failed_ingest_retry({})

    dispatch.assert_not_called()
    assert [(j.job_id, j.status) for j in JobStore(path=jobs_path).list()] == [("legacy", JobStatus.FAILED_EXPIRED)]


@pytest.mark.asyncio
async def test_retry_crashed_row_is_expired_and_never_started_row_is_retried(tmp_path: Path) -> None:
    """Of two rows with stored options, the one RUNNING when the process died (``process_restart``, arms the
    crash-loop guard) is expired; the one still PENDING never ran, so it cannot be the crash's cause and is retried."""
    jobs_path = tmp_path / "jobs.json"
    _seed_jobs_file(
        jobs_path,
        {"job_id": "crashed", "status": "RUNNING", "source_path": "/data/crash.txt", "ingest_options": _OPTIONS_JSON},
        {"job_id": "unstarted", "status": "PENDING", "source_path": "/data/file.txt", "ingest_options": _OPTIONS_JSON},
    )
    js = JobStore(path=jobs_path)
    assert js.crashed_ingest_on_load
    dispatch = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch)
    await loop._run_failed_ingest_retry({})

    crashed, unstarted = js.get("crashed"), js.get("unstarted")
    assert crashed is not None and unstarted is not None
    assert (crashed.status, crashed.error) == (JobStatus.FAILED_EXPIRED, PROCESS_RESTART_ERROR)
    assert (unstarted.status, unstarted.error) == (JobStatus.FAILED, NOT_STARTED_BEFORE_RESTART_DETAIL)
    (retry_job,) = [call_.args[0] for call_ in dispatch.call_args_list]
    assert (retry_job.source_path, retry_job.ingest_options) == ("/data/file.txt", _OPTIONS)


# ---------------------------------------------------------------------------
# [maintenance].exclude, chain expiry, age window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["docs", "default/docs"], ids=["bare", "qualified"])
async def test_retry_skips_excluded_collections(tmp_path: Path, pattern: str) -> None:
    """``[maintenance].exclude`` keeps every maintenance policy off a collection, retry included: its FAILED job is
    neither retried nor expired, while another collection's job still is retried."""
    excluded = _make_failed_ingest_job("/data/file.txt", job_id="excluded")
    other = _make_failed_ingest_job("/data/file.txt", collection="other", job_id="other")
    aged = _make_failed_ingest_job("/data/aged.txt", job_id="aged", age_hours=100.0)
    js = MagicMock()
    js.list.return_value = [excluded, other, aged]
    dispatch = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch, exclude=[pattern])
    await loop._run_failed_ingest_retry({})

    js.create.assert_called_once()
    assert js.create.call_args.kwargs["collection"] == "other"
    dispatch.assert_called_once()
    js.transition.assert_not_called()


@pytest.mark.asyncio
async def test_retry_exhausted_chain_expires_every_failed_job_of_the_path(tmp_path: Path) -> None:
    """When the newest FAILED job is retry ``retry_max_attempts``, every FAILED job of the path is expired in that
    pass — not one per pass; other paths' jobs are left alone."""
    original = _make_failed_ingest_job("/data/file.txt", job_id="original", age_hours=3.0)
    first_retry = _make_failed_ingest_job("/data/file.txt", job_id="retry-1", age_hours=2.0, source="maintenance")
    last_retry = _make_failed_ingest_job(
        "/data/file.txt", job_id="retry-2", age_hours=1.0, source="maintenance", retry_count=2
    )
    other_path = _make_failed_ingest_job("/data/other.txt", job_id="other", age_hours=1.0)
    js = MagicMock()
    js.list.return_value = [original, first_retry, last_retry, other_path]

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=2)
    await loop._run_failed_ingest_retry({})

    assert _expired_ids(js) == ["original", "retry-1", "retry-2"]
    js.create.assert_called_once()
    assert js.create.call_args.kwargs["path"] == "/data/other.txt"


@pytest.mark.asyncio
async def test_retry_newer_cancelled_request_does_not_restart_the_age_window(tmp_path: Path) -> None:
    """The age window counts from the failure's lineage: a newer client request that was CANCELLED (no outcome, so
    it supersedes nothing) does not move the window's start, so the aged-out FAILED job is expired."""
    failed = _make_failed_ingest_job("/data/file.txt", job_id="failed", age_hours=80.0)
    cancelled = _make_failed_ingest_job("/data/file.txt", status=JobStatus.CANCELLED, job_id="cancelled", age_hours=1.0)
    js = MagicMock()
    js.list.return_value = [failed, cancelled]

    loop = _make_loop(tmp_path, job_store=js, retry_max_age_hours=72)
    await loop._run_failed_ingest_retry({})

    js.create.assert_not_called()
    assert _expired_ids(js) == ["failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("oldest_age_hours", "retried"), [(80.0, False), (2.0, True)], ids=["aged", "young"])
async def test_retry_age_window_falls_back_to_the_oldest_retry(
    tmp_path: Path, oldest_age_hours: float, retried: bool
) -> None:
    """With the client's request already evicted (only maintenance retries left), the window counts from the oldest
    retry of the path; a young chain is retried as its next attempt."""
    oldest = _make_failed_ingest_job(
        "/data/file.txt", job_id="retry-1", age_hours=oldest_age_hours, retry_count=1, source="maintenance"
    )
    newest = _make_failed_ingest_job(
        "/data/file.txt", job_id="retry-2", age_hours=1.0, retry_count=2, source="maintenance"
    )
    js = MagicMock()
    js.list.return_value = [oldest, newest]

    loop = _make_loop(tmp_path, job_store=js, retry_max_age_hours=72)
    await loop._run_failed_ingest_retry({})

    assert js.create.called is retried
    if retried:
        assert js.create.call_args.kwargs["retry_count"] == 3
    assert _expired_ids(js) == ([] if retried else ["retry-1", "retry-2"])


# ---------------------------------------------------------------------------
# Attempt numbering: every retry of the current submission uses one attempt (its ``retry_count``)
# ---------------------------------------------------------------------------


def _pass_loop(tmp_path: Path, js: JobStore, dispatch: MagicMock, **kwargs: Any) -> MaintenanceLoop:
    """A loop whose full pass (``_run_one_pass``) sees no collections, so only the failed-ingest retry acts."""
    search_store = MagicMock()
    search_store.list_collections = AsyncMock(return_value=[])
    search_store.get_all_collections_meta = AsyncMock(return_value=[])
    return _make_loop(tmp_path, job_store=js, search_store=search_store, dispatch_retry=dispatch, **kwargs)


def _failing_dispatch(js: JobStore) -> MagicMock:
    """A dispatcher whose every retry fails at once, as an ingest that keeps failing would."""
    return MagicMock(side_effect=lambda job: js.update(job.job_id, status=JobStatus.FAILED, error="ingest failed"))


def _failed_submission(js: JobStore, path: str = "/data/file.txt") -> IngestJob:
    """A client's POST /ingest job for *path* that failed."""
    job = js.create(path=path, collection="docs", ingest_options=_OPTIONS)
    return js.update(job.job_id, status=JobStatus.FAILED, error="ingest failed")


def _dispatched_attempts(dispatch: MagicMock) -> list[int]:
    return [call_.args[0].retry_count for call_ in dispatch.call_args_list]


@pytest.mark.asyncio
async def test_retry_attempts_are_numbered_and_a_resubmission_gets_a_full_set(tmp_path: Path) -> None:
    """Each retry is the next attempt of its chain (1, 2, 3); the exhausted chain then ends. A new client submission
    of the path starts a new chain with the full ``retry_max_attempts`` — the old chain's attempts do not count."""
    js = JobStore(path=tmp_path / "jobs.json")
    _failed_submission(js)
    dispatch = _failing_dispatch(js)
    loop = _pass_loop(tmp_path, js, dispatch, retry_max_attempts=3)

    for _ in range(4):
        await loop._run_one_pass()

    assert _dispatched_attempts(dispatch) == [1, 2, 3]
    assert {j.status for j in js.list()} == {JobStatus.FAILED_EXPIRED}

    _failed_submission(js)
    dispatch.reset_mock()
    for _ in range(4):
        await loop._run_one_pass()

    assert _dispatched_attempts(dispatch) == [1, 2, 3]


@pytest.mark.asyncio
async def test_retry_state_file_keeps_no_per_path_counts(tmp_path: Path) -> None:
    """The attempt number lives on the jobs, so the state file carries no per-path count that could outlive them —
    and a state file an older server wrote with ``retry_counts`` loads, its counts ignored and dropped on write."""
    (tmp_path / ".maintenance-state.json").write_text(
        json.dumps({"last_run_at": None, "collection_health": {}, "retry_counts": {"default/docs/data/file.txt": 3}})
    )
    js = JobStore(path=tmp_path / "jobs.json")
    _failed_submission(js)
    dispatch = _failing_dispatch(js)
    loop = _pass_loop(tmp_path, js, dispatch)

    await loop._run_one_pass()

    assert _dispatched_attempts(dispatch) == [1]
    state = json.loads((tmp_path / ".maintenance-state.json").read_text())
    assert state["last_run_at"] is not None
    assert "retry_counts" not in state


@pytest.mark.asyncio
async def test_retry_of_a_failed_retry_is_its_next_attempt(tmp_path: Path) -> None:
    """The newest FAILED job being retry 2 makes the new job retry 3, carrying the options."""
    js = MagicMock()
    js.list.return_value = [_make_failed_ingest_job("/data/file.txt", retry_count=2, source="maintenance")]

    loop = _make_loop(tmp_path, job_store=js, retry_max_attempts=3)
    await loop._run_failed_ingest_retry({})

    js.create.assert_called_once_with(
        path="/data/file.txt", collection="docs", namespace="default", source="maintenance",
        ingest_options=_OPTIONS, retry_count=3,
    )


@pytest.mark.asyncio
async def test_retry_whose_dispatch_failed_is_retried_next_pass_with_its_options(tmp_path: Path) -> None:
    """A retry whose dispatch raised is FAILED with ``RETRY_DISPATCH_FAILED_DETAIL`` and keeps the options, so the
    next pass retries it as the next attempt, with those options."""
    js = JobStore(path=tmp_path / "jobs.json")
    _failed_submission(js)
    dispatch = MagicMock(side_effect=[RuntimeError("boom"), None])
    loop = _pass_loop(tmp_path, js, dispatch)

    await loop._run_one_pass()
    (stuck,) = [j for j in js.list() if j.source == "maintenance"]
    assert (stuck.status, stuck.error, stuck.ingest_options) == (JobStatus.FAILED, RETRY_DISPATCH_FAILED_DETAIL, _OPTIONS)

    await loop._run_one_pass()

    assert _dispatched_attempts(dispatch) == [1, 2]
    assert dispatch.call_args.args[0].ingest_options == _OPTIONS


@pytest.mark.asyncio
async def test_retry_a_shutdown_cancelled_consumes_its_attempt(tmp_path: Path) -> None:
    """A retry the shutdown cancelled while it ran (CANCELLED, no ``cancel_requested``) never settles its chain, yet it
    still uses its attempt: the next retry is the next number and ``retry_max_attempts`` stays a hard bound."""
    js = JobStore(path=tmp_path / "jobs.json")
    original = _failed_submission(js)
    dispatch = MagicMock(side_effect=lambda job: js.update(job.job_id, status=JobStatus.CANCELLED))
    loop = _pass_loop(tmp_path, js, dispatch, retry_max_attempts=3)

    for _ in range(5):
        await loop._run_one_pass()

    assert _dispatched_attempts(dispatch) == [1, 2, 3]
    expired = js.get(original.job_id)
    assert expired is not None
    assert expired.status == JobStatus.FAILED_EXPIRED


@pytest.mark.asyncio
async def test_retry_attempts_of_an_older_submission_do_not_count(tmp_path: Path) -> None:
    """Only the retries of the current submission count: an older chain's shutdown-cancelled retry is no attempt of
    a newer client submission of the path."""
    old = _make_failed_ingest_job("/data/file.txt", job_id="old", age_hours=3.0)
    old_retry = _make_failed_ingest_job(
        "/data/file.txt", status=JobStatus.CANCELLED, job_id="old-retry", age_hours=2.5, source="maintenance",
        retry_count=2,
    )
    resubmitted = _make_failed_ingest_job("/data/file.txt", job_id="new", age_hours=1.0)
    js = MagicMock()
    js.list.return_value = [old, old_retry, resubmitted]

    await _make_loop(tmp_path, job_store=js, retry_max_attempts=3)._run_failed_ingest_retry({})

    assert js.create.call_args.kwargs["retry_count"] == 1


@pytest.mark.asyncio
async def test_retry_ignores_a_failed_subclass_job_with_a_source_path(tmp_path: Path) -> None:
    """Only plain ingest jobs are retried: a FAILED reindex job that carries a source_path and options is neither
    retried nor expired."""
    created = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    reindex = ReindexJob(
        job_id="reindex", status=JobStatus.FAILED, created_at=created, updated_at=created,
        source_path="/data/file.txt", collection="docs", ingest_options=_OPTIONS,
    )
    js = MagicMock()
    js.list.return_value = [reindex]
    dispatch = MagicMock()

    await _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch)._run_failed_ingest_retry({})

    js.create.assert_not_called()
    js.transition.assert_not_called()
    dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# A user's cancel ends the chain; a cancel the shutdown wrote does not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_status", [JobStatus.CANCELLING, JobStatus.CANCELLED])
async def test_retry_newer_job_a_user_cancelled_supersedes(tmp_path: Path, newer_status: JobStatus) -> None:
    """A newer job whose cancel a client requested (DELETE /jobs/{id}: ``cancel_requested``) supersedes the FAILED
    job: no new retry starts — whether the cancel is still settling (CANCELLING) or done (CANCELLED)."""
    failed = _make_failed_ingest_job("/data/file.txt", job_id="old", age_hours=2.0)
    cancelled = _make_failed_ingest_job(
        "/data/file.txt", status=newer_status, job_id="new", age_hours=1.0, source="maintenance", retry_count=1,
        cancel_requested=True,
    )
    js = MagicMock()
    js.list.return_value = [failed, cancelled]
    dispatch = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch)
    await loop._run_failed_ingest_retry({})

    js.create.assert_not_called()
    js.transition.assert_not_called()
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_retry_failed_job_a_user_cancelled_is_expired_never_retried(tmp_path: Path) -> None:
    """A job that failed after its cancel was requested ends its chain: the client asked for no more work."""
    js = MagicMock()
    js.list.return_value = [_make_failed_ingest_job("/data/file.txt", job_id="cancelled", cancel_requested=True)]
    dispatch = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch)
    await loop._run_failed_ingest_retry({})

    js.create.assert_not_called()
    dispatch.assert_not_called()
    assert _expired_ids(js) == ["cancelled"]


# ---------------------------------------------------------------------------
# Crash recovery and the loop share one marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_job_recovered_as_crashed_by_the_store_is_never_retried(tmp_path: Path) -> None:
    """End to end through ``JobStore`` load and a full pass: the error the store's crash recovery writes is the one
    the loop reads as "died with the process", so a reworded marker cannot silently re-enable retrying a crash."""
    jobs_path = tmp_path / "jobs.json"
    _seed_jobs_file(
        jobs_path,
        {"job_id": "crashed", "status": "RUNNING", "source_path": "/data/file.txt", "ingest_options": _OPTIONS_JSON},
    )
    js = JobStore(path=jobs_path)
    dispatch = MagicMock()

    await _pass_loop(tmp_path, js, dispatch)._run_one_pass()

    dispatch.assert_not_called()
    crashed = js.get("crashed")
    assert crashed is not None
    assert crashed.status == JobStatus.FAILED_EXPIRED


# ---------------------------------------------------------------------------
# Ending a chain is best-effort per job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_expiry_write_failure_neither_aborts_the_chain_nor_the_pass(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed FAILED_EXPIRED write is logged (WARNING, with traceback) and skipped: the key's other FAILED job is
    still expired and another path is still retried in the same pass."""
    older = _make_failed_ingest_job("/data/aged.txt", job_id="aged-1", age_hours=100.0)
    newer = _make_failed_ingest_job("/data/aged.txt", job_id="aged-2", age_hours=90.0)
    young = _make_failed_ingest_job("/data/young.txt", job_id="young")
    js = MagicMock()
    js.list.return_value = [older, newer, young]
    js.transition.side_effect = [OSError("disk full"), MagicMock()]
    dispatch = MagicMock()

    loop = _make_loop(tmp_path, job_store=js, dispatch_retry=dispatch, retry_max_age_hours=72)
    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.maintenance_loop"):
        await loop._run_failed_ingest_retry({})

    assert _expired_ids(js) == ["aged-1", "aged-2"]
    dispatch.assert_called_once()
    assert js.create.call_args.kwargs["path"] == "/data/young.txt"
    assert any(r.levelno == logging.WARNING and r.exc_info for r in caplog.records)

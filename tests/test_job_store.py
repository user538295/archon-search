"""Tests for JobStore — TDD for ."""
from __future__ import annotations

import dataclasses
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from archon_search.jobs.model import IngestJob, JobStatus, get_jobs_file
from archon_search.jobs.store import JobStore


UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def test_create_returns_job_with_uuid(store: JobStore) -> None:
    job = store.create()
    assert UUID4_RE.match(job.job_id), f"Expected UUIDv4, got {job.job_id!r}"


def test_create_status_is_pending(store: JobStore) -> None:
    job = store.create()
    assert job.status == JobStatus.PENDING


def test_update_changes_status(store: JobStore) -> None:
    job = store.create()
    updated = store.update(job.job_id, status=JobStatus.RUNNING)
    assert updated.status == JobStatus.RUNNING


def test_atomic_write(tmp_path: Path) -> None:
    from unittest.mock import patch

    jobs_path = tmp_path / "jobs.json"
    s = JobStore(path=jobs_path)
    with patch("archon_search.jobs.store.atomic_write_json") as mock_write:
        job = s.create()
    expected = [
        {
            **dataclasses.asdict(job),
            "status": job.status.value,
            "job_type": "ingest",
        }
    ]
    mock_write.assert_called_once_with(jobs_path, expected)


def test_crash_recovery_running_to_failed(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    # Pre-populate a RUNNING job
    now = datetime.now(timezone.utc).isoformat()
    data = [
        {
            "job_id": str(uuid.uuid4()),
            "status": "RUNNING",
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
        }
    ]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    jobs = store.list()
    assert len(jobs) == 1
    assert jobs[0].status == JobStatus.FAILED
    assert jobs[0].error == "process_restart"


def test_crash_recovery_cancelling_to_failed(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    now = datetime.now(timezone.utc).isoformat()
    data = [
        {
            "job_id": str(uuid.uuid4()),
            "status": "CANCELLING",
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
        }
    ]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    jobs = store.list()
    assert len(jobs) == 1
    assert jobs[0].status == JobStatus.FAILED
    assert jobs[0].error == "process_restart"


def test_corrupt_file_resets(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text("not valid json {{{")
    store = JobStore(path=jobs_path)
    assert store.list() == []


def test_eviction_removes_old_jobs(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    old_time = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    old_id = str(uuid.uuid4())
    data = [
        {
            "job_id": old_id,
            "status": "DONE",
            "created_at": old_time,
            "updated_at": old_time,
            "result": None,
            "error": None,
        }
    ]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    assert store.get(old_id) is None
    assert store.list() == []


def test_eviction_keeps_recent_jobs(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    recent_time = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    recent_id = str(uuid.uuid4())
    data = [
        {
            "job_id": recent_id,
            "status": "DONE",
            "created_at": recent_time,
            "updated_at": recent_time,
            "result": None,
            "error": None,
        }
    ]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    job = store.get(recent_id)
    assert job is not None
    assert job.job_id == recent_id


def test_get_unknown_returns_none(store: JobStore) -> None:
    assert store.get("nonexistent-id") is None


def test_update_unknown_raises_key_error(store: JobStore) -> None:
    with pytest.raises(KeyError):
        store.update("nonexistent-id", status=JobStatus.RUNNING)


def test_list_returns_all_jobs(store: JobStore) -> None:
    store.create()
    store.create()
    assert len(store.list()) == 2


@pytest.mark.archon_unset_data_dir
def test_jobs_file_default_path() -> None:
    assert get_jobs_file() == Path.home() / ".archon-search" / "archon-search-jobs.json"


# ---------------------------------------------------------------------------
# Gap tests 
# ---------------------------------------------------------------------------


# new store, no file → _load returns False, _jobs empty
def test_load_no_file_returns_false_and_empty(tmp_path: Path) -> None:
    jobs_path = tmp_path / "nonexistent.json"
    assert not jobs_path.exists()
    s = JobStore(path=jobs_path)
    assert s.list() == []


# corrupt JSON → empty store, error logged
def test_corrupt_json_logs_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text("{corrupt{{{")
    import logging

    with caplog.at_level(logging.DEBUG, logger="archon"):
        s = JobStore(path=jobs_path)
    assert s.list() == []
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("resetting" in r.message.lower() for r in error_records), (
        "Expected an ERROR-level log mentioning 'resetting' on corrupt JSON"
    )


# valid JSON list but item missing required key → empty store
def test_missing_required_key_resets_store(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    # "status" key present, but "job_id" is missing — IngestJob(**item) will raise TypeError
    data = [{"status": "DONE", "created_at": "2024-01-01T00:00:00+00:00", "updated_at": "2024-01-01T00:00:00+00:00"}]
    jobs_path.write_text(json.dumps(data))
    s = JobStore(path=jobs_path)
    assert s.list() == []


# valid JSON but root is dict, not list → empty store
def test_wrong_root_type_resets_store(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(json.dumps({"job_id": "abc", "status": "DONE"}))
    s = JobStore(path=jobs_path)
    assert s.list() == []


# RUNNING job that already has error="prior" → error becomes "process_restart"
def test_crash_recovery_preserves_process_restart_over_prior_error(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    now = datetime.now(timezone.utc).isoformat()
    data = [
        {
            "job_id": str(uuid.uuid4()),
            "status": "RUNNING",
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": "prior",
        }
    ]
    jobs_path.write_text(json.dumps(data))
    s = JobStore(path=jobs_path)
    jobs = s.list()
    assert len(jobs) == 1
    assert jobs[0].status == JobStatus.FAILED
    assert jobs[0].error == "process_restart"


# updated_at exactly 7 days + 1 second ago → evicted
def test_eviction_boundary_one_second_over(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    old_time = (datetime.now(timezone.utc) - timedelta(days=7, seconds=1)).isoformat()
    old_id = str(uuid.uuid4())
    data = [
        {
            "job_id": old_id,
            "status": "DONE",
            "created_at": old_time,
            "updated_at": old_time,
            "result": None,
            "error": None,
        }
    ]
    jobs_path.write_text(json.dumps(data))
    s = JobStore(path=jobs_path)
    assert s.get(old_id) is None
    assert s.list() == []


# updated_at exactly 7 days ago → NOT evicted (strict < cutoff, equality is kept)
def test_eviction_boundary_exactly_seven_days(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    # Production uses strict < cutoff (cutoff = now - 7 days).
    # We place the job 1 second inside the boundary to avoid a race with _evict_old()
    # recomputing "now" slightly later.  1s margin is far smaller than the 7-day window
    # yet large enough to survive any test execution latency.
    boundary_time = (datetime.now(timezone.utc) - timedelta(days=7) + timedelta(seconds=1)).isoformat()
    recent_id = str(uuid.uuid4())
    data = [
        {
            "job_id": recent_id,
            "status": "DONE",
            "created_at": boundary_time,
            "updated_at": boundary_time,
            "result": None,
            "error": None,
        }
    ]
    jobs_path.write_text(json.dumps(data))
    s = JobStore(path=jobs_path)
    assert s.get(recent_id) is not None


# updated_at is not a valid ISO date → no crash, handled gracefully
def test_invalid_date_no_crash(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    now = datetime.now(timezone.utc).isoformat()
    data = [
        {
            "job_id": str(uuid.uuid4()),
            "status": "DONE",
            "created_at": now,
            "updated_at": "not-a-date",
            "result": None,
            "error": None,
        }
    ]
    jobs_path.write_text(json.dumps(data))
    # Must not raise; store may reset or keep the job — either is acceptable
    s = JobStore(path=jobs_path)
    # Just verify no exception was raised and the store is usable
    _ = s.list()


# job is DONE, transition(from_statuses={RUNNING}) → returns None, unchanged
def test_transition_wrong_source_status_returns_none(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    s = JobStore(path=jobs_path)
    job = s.create()
    s.update(job.job_id, status=JobStatus.DONE)
    result = s.transition(job.job_id, from_statuses={JobStatus.RUNNING}, to_status=JobStatus.FAILED)
    assert result is None
    assert s.get(job.job_id) is not None
    assert s.get(job.job_id).status == JobStatus.DONE  # type: ignore[union-attr]


# sequential double-transition: second PENDING→RUNNING returns None
def test_double_transition_second_rejected(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    s = JobStore(path=jobs_path)
    job = s.create()
    assert job.status == JobStatus.PENDING

    first = s.transition(job.job_id, from_statuses={JobStatus.PENDING}, to_status=JobStatus.RUNNING)
    assert first is not None
    assert first.status == JobStatus.RUNNING

    second = s.transition(job.job_id, from_statuses={JobStatus.PENDING}, to_status=JobStatus.RUNNING)
    assert second is None
    # Job is still RUNNING
    assert s.get(job.job_id).status == JobStatus.RUNNING  # type: ignore[union-attr]


def test_write_atomic_failure_reraises(tmp_path: Path) -> None:
    """A helper-raised OSError propagates out of _write_atomic (the helper unlinks its own tmp)."""
    from unittest.mock import patch

    jobs_path = tmp_path / "jobs.json"
    s = JobStore(path=jobs_path)

    with patch("archon_search.jobs.store.atomic_write_json", side_effect=OSError("simulated write failure")):
        with pytest.raises(OSError, match="simulated write failure"):
            s.create()


# ---------------------------------------------------------------------------
# JobStore.create(namespace=...) parameter
# ---------------------------------------------------------------------------


def test_job_store_create_with_namespace(tmp_path: Path) -> None:
    s = JobStore(path=tmp_path / "jobs.json")
    job = s.create(namespace="tenantA")
    assert job.namespace == "tenantA"


def test_job_store_create_default_namespace(tmp_path: Path) -> None:
    from archon_search.constants import DEFAULT_NAMESPACE

    s = JobStore(path=tmp_path / "jobs.json")
    job = s.create()
    assert job.namespace == DEFAULT_NAMESPACE


def test_job_store_persists_namespace(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    s = JobStore(path=jobs_path)
    job = s.create(namespace="tenantB")

    # Reload from disk
    s2 = JobStore(path=jobs_path)
    reloaded = s2.get(job.job_id)
    assert reloaded is not None
    assert reloaded.namespace == "tenantB"


def test_job_store_load_pre_5c_json(tmp_path: Path) -> None:
    """JSON entries lacking 'namespace' key should load with DEFAULT_NAMESPACE."""
    from archon_search.constants import DEFAULT_NAMESPACE

    from datetime import datetime, timezone

    jobs_path = tmp_path / "jobs.json"
    now = datetime.now(timezone.utc).isoformat()
    data = [
        {
            "job_id": "aaaa-bbbb",
            "status": "DONE",
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
            # intentionally no "namespace" key
        }
    ]
    jobs_path.write_text(json.dumps(data))
    s = JobStore(path=jobs_path)
    job = s.get("aaaa-bbbb")
    assert job is not None
    assert job.namespace == DEFAULT_NAMESPACE


# ===========================================================================
# count_by_status — Task 3.1 (B2)
# ===========================================================================


def test_count_by_status_empty_store(tmp_path: Path) -> None:
    store = JobStore(path=tmp_path / "jobs.json")
    counts = store.count_by_status()
    for status in JobStatus:
        assert counts[status] == 0, f"Expected 0 for {status}, got {counts[status]}"


def test_count_by_status_includes_all_status_members(tmp_path: Path) -> None:
    store = JobStore(path=tmp_path / "jobs.json")
    counts = store.count_by_status()
    assert set(counts.keys()) == set(JobStatus)


def test_count_by_status_mixed(tmp_path: Path) -> None:
    store = JobStore(path=tmp_path / "jobs.json")
    # Create 2 PENDING jobs
    j1 = store.create()
    j2 = store.create()
    # Move 1 to RUNNING
    store.update(j1.job_id, status=JobStatus.RUNNING)
    # Create 1 DONE job
    j3 = store.create()
    store.update(j3.job_id, status=JobStatus.DONE)
    # Create 1 FAILED job
    j4 = store.create()
    store.update(j4.job_id, status=JobStatus.FAILED)
    # Create 1 CANCELLED job
    j5 = store.create()
    store.update(j5.job_id, status=JobStatus.CANCELLED)

    # Create 1 CANCELLING job (valid transient state)
    j6 = store.create()
    store.update(j6.job_id, status=JobStatus.CANCELLING)

    counts = store.count_by_status()
    assert counts[JobStatus.PENDING] == 1  # j2 still PENDING
    assert counts[JobStatus.RUNNING] == 1  # j1 moved to RUNNING
    assert counts[JobStatus.DONE] == 1
    assert counts[JobStatus.FAILED] == 1
    assert counts[JobStatus.CANCELLED] == 1
    assert counts[JobStatus.CANCELLING] == 1  # j6 in CANCELLING state


def test_count_by_status_excludes_evicted_jobs(tmp_path: Path) -> None:
    """Evicted jobs (older than 7 days) must not appear in count_by_status."""
    jobs_path = tmp_path / "jobs.json"
    old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    data = [
        {
            "job_id": str(uuid.uuid4()),
            "status": "DONE",
            "created_at": old_ts,
            "updated_at": old_ts,
            "result": None,
            "error": None,
            "namespace": "default",
        }
    ]
    jobs_path.write_text(json.dumps(data))
    # Loading triggers _evict_old; the aged job must be removed
    store = JobStore(path=jobs_path)
    counts = store.count_by_status()
    for status in JobStatus:
        assert counts[status] == 0, f"Expected 0 for {status} after eviction, got {counts[status]}"

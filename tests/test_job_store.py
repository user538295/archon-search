"""Tests for JobStore — TDD for Task 5.1 (FEAT-038)."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from archon_search.jobs.model import JOBS_FILE, IngestJob, JobStatus
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


def test_atomic_write(store: JobStore, tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    s = JobStore(path=jobs_path)
    s.create()
    # The .tmp file must NOT exist after the write
    tmp_file = jobs_path.with_suffix(".tmp")
    assert not tmp_file.exists(), "Temp file should be cleaned up after atomic rename"
    assert jobs_path.exists(), "Jobs file should exist after write"


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


def test_jobs_file_default_path() -> None:
    assert JOBS_FILE == Path.home() / ".archon" / "archon-search-jobs.json"

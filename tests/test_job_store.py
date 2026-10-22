"""Tests for JobStore — TDD for ."""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from archon_search.jobs.model import IngestJob, JobStatus, get_jobs_file
from archon_search.jobs.store import JobStore
from archon_search.types import (
    CommunityRebuildJob,
    DeleteJob,
    ExportJob,
    ImportJob,
    IngestOptions,
    MetadataReindexJob,
    MigrationJob,
    MigrationKind,
    ReindexJob,
    SyncJob,
)
from tests._job_store_faults import SIMULATED_WRITE_FAILURE, failing_job_writes, persisted_statuses, recording_job_writes


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
    fields = dataclasses.asdict(job)
    del fields["ingest_options"], fields["cancel_requested"]  # unset: written without the keys
    expected = [{**fields, "status": job.status.value, "job_type": "ingest"}]
    mock_write.assert_called_once_with(jobs_path, expected)


def _seed_job_file(
    jobs_path: Path,
    *,
    status: str,
    created_at: str,
    updated_at: str,
    job_id: str | None = None,
    job_type: str | None = None,
) -> str:
    """Write a one-job jobs file and return that job's id (no ``job_type`` key unless *job_type* is given)."""
    job_id = job_id or str(uuid.uuid4())
    row = {
        "job_id": job_id,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "result": None,
        "error": None,
    }
    if job_type is not None:
        row["job_type"] = job_type
    jobs_path.write_text(json.dumps([row]))
    return job_id


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


@pytest.mark.parametrize(
    "create_call",
    [
        pytest.param(lambda s: s.create(), id="create"),
        pytest.param(lambda s: s.create_reindex("c"), id="create_reindex"),
        pytest.param(lambda s: s.create_job(IngestJob(
            job_id=str(uuid.uuid4()), status=JobStatus.PENDING, created_at="", updated_at="",
        )), id="create_job"),
        pytest.param(lambda s: s.create_export("c", "/out", "/tmp-out"), id="create_export"),
        pytest.param(lambda s: s.create_import("c", "/in.tar", False, False, "abort"), id="create_import"),
        pytest.param(lambda s: s.create_migration("c", MigrationKind.IN_PLACE, None), id="create_migration"),
        pytest.param(lambda s: s.create_community_rebuild("c"), id="create_community_rebuild"),
        pytest.param(lambda s: s.create_sync(), id="create_sync"),
        pytest.param(lambda s: s.create_metadata_reindex("c"), id="create_metadata_reindex"),
    ],
)
def test_create_write_failure_leaves_no_ghost_job(tmp_path: Path, create_call) -> None:
    """A failed persist must not leave the never-saved job in memory (no ghost PENDING/QUEUED job),
    and must not drop an already-saved job (in memory or on disk)."""
    from unittest.mock import patch

    s = JobStore(path=tmp_path / "jobs.json")
    saved = s.create_export("c", "/out", "/tmp-out")  # QUEUED is kept on reload (a PENDING ingest/reindex is not)

    with patch("archon_search.jobs.store.atomic_write_json", side_effect=OSError("simulated write failure")):
        with pytest.raises(OSError, match="simulated write failure"):
            create_call(s)

    assert s.list() == [saved]
    assert JobStore(path=tmp_path / "jobs.json").list() == [saved]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        pytest.param(lambda s, job_id: s.update(job_id, status=JobStatus.DONE), {"status": JobStatus.DONE}, id="update"),
        pytest.param(
            lambda s, job_id: s.update_progress(job_id, 1, 2, "embed"),
            {"progress": {"processed": 1, "total": 2, "phase": "embed"}},
            id="update_progress",
        ),
    ],
)
def test_update_write_failure_keeps_update_and_next_write_persists_it(
    tmp_path: Path, mutate, expected: dict[str, object]
) -> None:
    """A failed persist re-raises but keeps the update in memory, so the next successful write
    flushes it to disk (self-heals) instead of pinning the job to its last-saved state forever."""
    from unittest.mock import patch

    s = JobStore(path=tmp_path / "jobs.json")
    job = s.create_export("c", "/out", "/tmp-out")  # QUEUED is kept on reload (a PENDING ingest/reindex is not)

    with patch("archon_search.jobs.store.atomic_write_json", side_effect=OSError("simulated write failure")):
        with pytest.raises(OSError, match="simulated write failure"):
            mutate(s, job.job_id)

    in_memory = s.get(job.job_id)
    assert in_memory is not None
    assert {field: getattr(in_memory, field) for field in expected} == expected

    s.create()  # next successful write

    assert JobStore(path=tmp_path / "jobs.json").get(job.job_id) == in_memory


def test_transition_write_failure_reraises_and_keeps_old_status(tmp_path: Path) -> None:
    """transition() is a compare-and-set claim: when its persist fails the claim did not happen, so the
    job must stay at the OLD status in memory AND on disk — even after a later successful write flushes
    every in-memory job (otherwise the never-acknowledged status is persisted behind the caller's back)."""
    jobs_path = tmp_path / "jobs.json"
    s = JobStore(path=jobs_path)
    job = s.create()

    with failing_job_writes(), pytest.raises(OSError, match=SIMULATED_WRITE_FAILURE):
        s.transition(job.job_id, from_statuses={JobStatus.PENDING}, to_status=JobStatus.RUNNING)

    in_memory = s.get(job.job_id)
    assert in_memory is not None
    assert in_memory.status == JobStatus.PENDING
    assert persisted_statuses(jobs_path)[job.job_id] == JobStatus.PENDING.value

    s.create()  # next successful write serializes every in-memory job

    assert persisted_statuses(jobs_path)[job.job_id] == JobStatus.PENDING.value


def test_transition_raising_after_rename_rewrites_old_status(tmp_path: Path) -> None:
    """``atomic_write_json`` can raise after its rename, leaving the new status on disk although the
    transition raised. The corrective rewrite must put the old status back on disk, matching memory."""
    jobs_path = tmp_path / "jobs.json"
    s = JobStore(path=jobs_path)
    job = s.create()

    with failing_job_writes(count=1, fail_after_write=1) as writes, pytest.raises(OSError, match=SIMULATED_WRITE_FAILURE):
        s.transition(job.job_id, from_statuses={JobStatus.PENDING}, to_status=JobStatus.RUNNING)

    assert writes.statuses_of(job.job_id) == [JobStatus.RUNNING.value, JobStatus.PENDING.value]
    in_memory = s.get(job.job_id)
    assert in_memory is not None
    assert in_memory.status == JobStatus.PENDING
    assert persisted_statuses(jobs_path)[job.job_id] == JobStatus.PENDING.value


@pytest.mark.parametrize(
    ("mutate", "expected_status"),
    [
        pytest.param(
            lambda s, job_id: s.transition(job_id, from_statuses={JobStatus.PENDING}, to_status=JobStatus.RUNNING),
            JobStatus.PENDING,
            id="transition",
        ),
        pytest.param(lambda s, job_id: s.create(), JobStatus.PENDING, id="create_job"),
    ],
)
def test_failed_corrective_rewrite_does_not_mask_the_original_persist_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, mutate, expected_status: JobStatus
) -> None:
    """When the corrective rewrite fails with a DIFFERENT exception, the caller still sees the ORIGINAL persist
    error, memory is rolled back, and the rewrite failure is logged as one WARNING line without a traceback."""
    s = JobStore(path=tmp_path / "jobs.json")
    job = s.create()
    caplog.set_level(logging.WARNING, logger="archon_search.jobs.store")

    with (
        failing_job_writes(count=2, exc=[OSError(SIMULATED_WRITE_FAILURE), RuntimeError("rewrite failed")]),
        pytest.raises(OSError, match=SIMULATED_WRITE_FAILURE),
    ):
        mutate(s, job.job_id)

    assert [j.job_id for j in s.list()] == [job.job_id]  # a failed create leaves no ghost job behind
    assert s.get(job.job_id).status == expected_status  # type: ignore[union-attr]
    (record,) = [r for r in caplog.records if r.name == "archon_search.jobs.store"]
    assert record.levelno == logging.WARNING
    assert "RuntimeError" in record.getMessage()
    assert record.exc_info is None


def test_transition_double_fail_residual_leaves_new_status_on_disk(tmp_path: Path) -> None:
    """Documented residual: the transition's write raises AFTER its rename (new status on disk) and the
    corrective rewrite fails too. Memory holds the old status; the raw file keeps the new one until the next
    successful write (or, for RUNNING, until the next load closes it out as FAILED ``process_restart``)."""
    jobs_path = tmp_path / "jobs.json"
    s = JobStore(path=jobs_path)
    job = s.create()

    with failing_job_writes(fail_after_write=1), pytest.raises(OSError, match=SIMULATED_WRITE_FAILURE):
        s.transition(job.job_id, from_statuses={JobStatus.PENDING}, to_status=JobStatus.RUNNING)

    assert s.get(job.job_id).status == JobStatus.PENDING  # type: ignore[union-attr]
    assert persisted_statuses(jobs_path)[job.job_id] == JobStatus.RUNNING.value


def test_transition_cancelled_mid_write_keeps_old_status(tmp_path: Path) -> None:
    """A BaseException out of the persist (asyncio.CancelledError) is no exception to the claim rule: the job
    keeps its old status in memory, the corrective rewrite restores it on disk, and the cancellation still
    propagates.

    Only the original write raises (after reaching disk, so the file held RUNNING): the corrective rewrite
    succeeds, so the raw-file assertion is not satisfied by an untouched file."""
    import asyncio

    jobs_path = tmp_path / "jobs.json"
    s = JobStore(path=jobs_path)
    job = s.create()

    with (
        failing_job_writes(count=1, exc=asyncio.CancelledError(), fail_after_write=1) as writes,
        pytest.raises(asyncio.CancelledError),
    ):
        s.transition(job.job_id, from_statuses={JobStatus.PENDING}, to_status=JobStatus.RUNNING)

    assert writes.attempts == 2  # the failed transition write, then the corrective rewrite
    assert writes.statuses_of(job.job_id) == [JobStatus.RUNNING.value, JobStatus.PENDING.value]
    in_memory = s.get(job.job_id)
    assert in_memory is not None
    assert in_memory.status == JobStatus.PENDING
    assert persisted_statuses(jobs_path)[job.job_id] == JobStatus.PENDING.value


# Every create a route (or the startup sync) runs itself, as that caller makes it: directly RUNNING.
_RUNNING_CREATES = [
    pytest.param(
        lambda s: s.create_migration("c", MigrationKind.REWRITE, True, status=JobStatus.RUNNING), id="create_migration"
    ),
    pytest.param(lambda s: s.create_community_rebuild("c"), id="create_community_rebuild"),
    pytest.param(lambda s: s.create_sync(), id="create_sync"),
    pytest.param(lambda s: s.create_metadata_reindex("c"), id="create_metadata_reindex"),
]


@pytest.mark.parametrize("create_call", _RUNNING_CREATES)
def test_running_create_persists_running_in_one_write(tmp_path: Path, create_call) -> None:
    """A job its caller runs itself is created RUNNING with a single persist — no QUEUED row ever exists."""
    jobs_path = tmp_path / "jobs.json"
    s = JobStore(path=jobs_path)

    with recording_job_writes() as writes:
        job = create_call(s)

    assert writes.attempts == 1
    assert job.status == JobStatus.RUNNING
    assert persisted_statuses(jobs_path) == {job.job_id: JobStatus.RUNNING.value}


@pytest.mark.parametrize("create_call", _RUNNING_CREATES)
def test_running_create_raising_after_rename_rewrites_file_without_the_job(tmp_path: Path, create_call) -> None:
    """``atomic_write_json`` can raise after its rename: the RUNNING row is on disk although the create raised.
    The corrective rewrite must remove it again, so memory and file agree the job does not exist."""
    jobs_path = tmp_path / "jobs.json"
    s = JobStore(path=jobs_path)

    with failing_job_writes(count=1, fail_after_write=1) as writes, pytest.raises(OSError, match=SIMULATED_WRITE_FAILURE):
        create_call(s)

    assert writes.attempts == 2
    assert s.list() == []
    assert persisted_statuses(jobs_path) == {}


@pytest.mark.parametrize("create_call", _RUNNING_CREATES)
def test_running_create_raising_after_rename_loads_as_failed_process_restart(tmp_path: Path, create_call) -> None:
    """Residual: when the corrective rewrite fails too, the RUNNING row stays on disk although the create
    raised. The next load must close it out as FAILED ``process_restart`` — never leave it for the scheduler.

    Known residual for ``create_sync``: that leftover SyncJob row is an ingest-family crash marker, so the
    reload also arms the crash-loop guard (``crashed_ingest_on_load``) although no sync ever ran."""
    jobs_path = tmp_path / "jobs.json"
    s = JobStore(path=jobs_path)

    with failing_job_writes(fail_after_write=1), pytest.raises(OSError, match=SIMULATED_WRITE_FAILURE):
        create_call(s)

    assert s.list() == []
    reloaded_store = JobStore(path=jobs_path)
    (reloaded,) = reloaded_store.list()
    assert (reloaded.status, reloaded.error) == (JobStatus.FAILED, "process_restart")
    assert reloaded_store.crashed_ingest_on_load is isinstance(reloaded, SyncJob)


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
    store.create()
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


@pytest.mark.parametrize("crash_status", ["RUNNING", "CANCELLING"])
def test_crash_recovery_stamps_updated_at(tmp_path: Path, crash_status: str) -> None:
    """Crash-recovery marking must refresh updated_at, keeping created_at intact.

    The load-time rewrite to FAILED/"process_restart" is a status transition like
    any other, but it bypasses ``JobStore.update`` and so never stamps
    ``updated_at``. A recovered job therefore keeps its pre-crash timestamp — and
    ``_evict_old`` (7-day cutoff on ``updated_at``) silently deletes the record on
    the very next load once that stale timestamp ages out.
    """
    jobs_path = tmp_path / "jobs.json"
    created_at = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    stale_updated_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    job_id = _seed_job_file(
        jobs_path, status=crash_status, created_at=created_at, updated_at=stale_updated_at
    )

    load_start_time = datetime.now(timezone.utc)
    store = JobStore(path=jobs_path)

    job = store.get(job_id)
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert job.error == "process_restart"
    assert job.created_at == created_at, "created_at must survive crash recovery unchanged"
    assert datetime.fromisoformat(job.updated_at) >= load_start_time, (
        "crash recovery must stamp updated_at with the recovery time; got "
        f"{job.updated_at!r} (seeded pre-crash value was {stale_updated_at!r}), "
        f"which predates the load at {load_start_time.isoformat()!r}"
    )
    persisted = json.loads(jobs_path.read_text())
    assert persisted[0]["updated_at"] == job.updated_at, (
        "the refreshed updated_at must be persisted to disk, not only held in memory"
    )


@pytest.mark.parametrize("crash_status", ["RUNNING", "CANCELLING"])
def test_crash_recovery_survives_eviction_cutoff(tmp_path: Path, crash_status: str) -> None:
    """A crash job whose pre-crash updated_at predates the 7-day cutoff must survive load.

    Recovery rewrites the status to FAILED, which is a terminal status and therefore
    eligible for _evict_old. Without the refreshed updated_at the row would be deleted
    on the very same load that recovered it.
    """
    jobs_path = tmp_path / "jobs.json"
    ancient = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    job_id = _seed_job_file(jobs_path, status=crash_status, created_at=ancient, updated_at=ancient)

    job = JobStore(path=jobs_path).get(job_id)

    assert job is not None, "crash-recovered job was evicted despite the updated_at refresh"
    assert job.status == JobStatus.FAILED
    assert job.error == "process_restart"


@pytest.mark.parametrize("terminal_status", ["DONE", "FAILED", "CANCELLED"])
def test_load_leaves_non_crash_jobs_untouched(tmp_path: Path, terminal_status: str) -> None:
    """Only crash statuses are rewritten — a terminal job keeps its status and timestamps."""
    jobs_path = tmp_path / "jobs.json"
    created_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    updated_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    job_id = _seed_job_file(
        jobs_path, status=terminal_status, created_at=created_at, updated_at=updated_at
    )

    job = JobStore(path=jobs_path).get(job_id)

    assert job is not None
    assert job.status == JobStatus(terminal_status)
    assert job.error is None
    assert job.created_at == created_at
    assert job.updated_at == updated_at, "non-crash jobs must not have updated_at rewritten on load"


# ===========================================================================
# Ingest request options persisted for a MaintenanceLoop retry
# ===========================================================================


def test_create_persists_ingest_options_across_reload(tmp_path: Path) -> None:
    """The POST /ingest options a retry must re-apply survive a reload of the jobs file, as one nested object."""
    jobs_path = tmp_path / "jobs.json"
    options = IngestOptions(
        submitted_path="~/doc.md", ingested_by="cli", chunk_ttl_seconds=3600, chunk_scopes=("team-a",)
    )
    job = JobStore(path=jobs_path).create(path="/data/doc.md", collection="docs", ingest_options=options)

    reloaded = JobStore(path=jobs_path).get(job.job_id)

    assert reloaded is not None
    assert reloaded.ingest_options == options
    (row,) = json.loads(jobs_path.read_text())
    assert row["ingest_options"] == {
        "submitted_path": "~/doc.md", "ingested_by": "cli", "chunk_ttl_seconds": 3600, "chunk_scopes": ["team-a"],
    }


def test_load_tolerates_job_without_ingest_options(tmp_path: Path) -> None:
    """A row written before the options were stored loads with ``ingest_options`` None: its request is unknown."""
    jobs_path = tmp_path / "jobs.json"
    now = datetime.now(timezone.utc).isoformat()
    job_id = _seed_job_file(jobs_path, status="DONE", created_at=now, updated_at=now)

    job = JobStore(path=jobs_path).get(job_id)

    assert job is not None
    assert job.ingest_options is None


def test_jobs_without_ingest_options_are_written_without_the_key(tmp_path: Path) -> None:
    """Subclass rows, and base rows with no options, carry no ``ingest_options`` key: their persisted shape is
    unchanged."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)
    store.create_reindex("c")
    store.create(collection="docs")

    rows = json.loads(jobs_path.read_text())

    assert len(rows) == 2
    assert all("ingest_options" not in row for row in rows)


@pytest.mark.parametrize("job_type", [None, "reindex"], ids=["ingest", "reindex"])
def test_load_recovers_pending_job_as_failed_not_started(tmp_path: Path, job_type: str | None) -> None:
    """A base ingest or reindex job still PENDING at load never started, and no task of this process runs it (only
    its route spawns one; a shutdown cancels a reindex task while it loads its embedder, leaving the job PENDING):
    it is rewritten FAILED with the sanitized not-started text — without arming the crash-loop guard (nothing ran).
    A reindex left PENDING would otherwise block DELETE /collections/{name} forever."""
    from archon_search.jobs.store import NOT_STARTED_BEFORE_RESTART_DETAIL

    jobs_path = tmp_path / "jobs.json"
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    job_id = _seed_job_file(jobs_path, status="PENDING", created_at=stale, updated_at=stale, job_type=job_type)

    load_start_time = datetime.now(timezone.utc)
    store = JobStore(path=jobs_path)

    job = store.get(job_id)
    assert job is not None
    assert (job.status, job.error) == (JobStatus.FAILED, NOT_STARTED_BEFORE_RESTART_DETAIL)
    assert datetime.fromisoformat(job.updated_at) >= load_start_time
    assert store.crashed_ingest_on_load is False, "a never-started job must not arm the crash-loop guard"
    assert persisted_statuses(jobs_path)[job_id] == JobStatus.FAILED.value


def test_load_keeps_pending_job_of_other_kinds(tmp_path: Path) -> None:
    """Only base ingest and reindex jobs are recovered: other kinds (the legacy delete job) keep their saved
    PENDING status."""
    job_type = "delete"
    jobs_path = tmp_path / "jobs.json"
    now = datetime.now(timezone.utc).isoformat()
    jobs_path.write_text(json.dumps([{
        "job_id": "j1", "status": "PENDING", "created_at": now, "updated_at": now,
        "result": None, "error": None, "job_type": job_type,
    }]))

    job = JobStore(path=jobs_path).get("j1")

    assert job is not None
    assert job.status == JobStatus.PENDING


def test_reindex_job_collection_survives_reload(tmp_path: Path) -> None:
    """A reindex job's ``collection`` is persisted and reloaded: DELETE /collections/{name} finds the job by it."""
    jobs_path = tmp_path / "jobs.json"
    job = JobStore(path=jobs_path).create_reindex("docs", namespace="team-a", target_embedding_model="m")

    reloaded = JobStore(path=jobs_path).get(job.job_id)

    assert isinstance(reloaded, ReindexJob)
    assert (reloaded.collection, reloaded.namespace, reloaded.target_embedding_model) == ("docs", "team-a", "m")


# ===========================================================================
# cancel_ingest_retries — DELETE /collections/{name} ends the dropped collection's retry candidates
# ===========================================================================


def _job_on(
    job_id: str,
    status: JobStatus,
    *,
    collection: str = "docs",
    namespace: str = "default",
    job_class: type[IngestJob] = IngestJob,
) -> IngestJob:
    now = datetime.now(timezone.utc).isoformat()
    return job_class(
        job_id=job_id, status=status, created_at=now, updated_at=now, namespace=namespace, collection=collection
    )


def test_cancel_ingest_retries_cancels_the_collections_failed_and_pending_base_ingests(tmp_path: Path) -> None:
    """FAILED and PENDING base ingest jobs on the collection — in every namespace, as the table is shared by name —
    end CANCELLED; other statuses, other collections and subclass jobs on the same collection are untouched."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)
    jobs = [
        _job_on("failed", JobStatus.FAILED),
        _job_on("pending", JobStatus.PENDING),
        _job_on("other-namespace", JobStatus.FAILED, namespace="other-team"),
        _job_on("done", JobStatus.DONE),
        _job_on("running", JobStatus.RUNNING),
        _job_on("expired", JobStatus.FAILED_EXPIRED),
        _job_on("other-collection", JobStatus.FAILED, collection="other"),
        _job_on("reindex", JobStatus.FAILED, job_class=ReindexJob),
        _job_on("export", JobStatus.PENDING, job_class=ExportJob),
    ]
    for job in jobs:
        store.create_job(job)

    store.cancel_ingest_retries("docs")

    assert persisted_statuses(jobs_path) == {
        "failed": JobStatus.CANCELLED.value,
        "pending": JobStatus.CANCELLED.value,
        "other-namespace": JobStatus.CANCELLED.value,
        "done": JobStatus.DONE.value,
        "running": JobStatus.RUNNING.value,
        "expired": JobStatus.FAILED_EXPIRED.value,
        "other-collection": JobStatus.FAILED.value,
        "reindex": JobStatus.FAILED.value,
        "export": JobStatus.PENDING.value,
    }


def test_cancel_ingest_retries_writes_every_cancel_at_once(tmp_path: Path) -> None:
    """All of the collection's cancels are persisted in ONE write, so the jobs file never holds half of them."""
    store = JobStore(path=tmp_path / "jobs.json")
    store.create_job(_job_on("failed", JobStatus.FAILED))
    store.create_job(_job_on("pending", JobStatus.PENDING))

    with recording_job_writes() as writes:
        store.cancel_ingest_retries("docs")

    assert writes.attempts == 1
    assert writes.statuses_of("failed") == writes.statuses_of("pending") == [JobStatus.CANCELLED.value]


def test_cancel_ingest_retries_failed_write_is_logged_and_leaves_every_job_as_it_was(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Best-effort and all-or-nothing: a failed write is logged at WARNING and raises nothing; memory and the file
    keep every job's old status, consistent with each other."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)
    store.create_job(_job_on("first", JobStatus.FAILED))
    store.create_job(_job_on("second", JobStatus.PENDING))

    with failing_job_writes(count=1), caplog.at_level(logging.WARNING, logger="archon_search.jobs.store"):
        store.cancel_ingest_retries("docs")

    first, second = store.get("first"), store.get("second")
    assert first is not None and second is not None
    assert (first.status, second.status) == (JobStatus.FAILED, JobStatus.PENDING)
    assert persisted_statuses(jobs_path) == {"first": JobStatus.FAILED.value, "second": JobStatus.PENDING.value}
    assert any(
        r.levelno == logging.WARNING and "could not cancel the ingest jobs of dropped collection docs" in r.getMessage()
        for r in caplog.records
    )


# ===========================================================================
# find_active_collection_job / may_write_collection — DELETE /collections/{name} refuses while a job may write it
# ===========================================================================


@dataclasses.dataclass
class _UnclassifiedJob(IngestJob):
    """A job kind in neither COLLECTION_WRITING_JOB_STATUSES nor NEVER_BLOCKING_JOB_KINDS."""


def _archon_job_kinds() -> set[type[IngestJob]]:
    """``IngestJob`` and every subclass of it, at any depth, defined in an ``archon_search`` module (kinds defined by
    tests, such as ``_UnclassifiedJob``, are left out)."""
    kinds: set[type[IngestJob]] = set()
    pending = [IngestJob]
    while pending:
        kind = pending.pop()
        if kind.__module__.startswith("archon_search.") and kind not in kinds:
            kinds.add(kind)
            pending.extend(kind.__subclasses__())
    return kinds


#: Job kind -> the statuses in which it blocks DELETE /collections/{name}, spelled out independently of the table
#: under test. An empty set: the kind never blocks.
_EXPECTED_BLOCKING_STATUSES: dict[type[IngestJob], frozenset[JobStatus]] = {
    IngestJob: frozenset({JobStatus.RUNNING, JobStatus.CANCELLING}),
    ReindexJob: frozenset({JobStatus.PENDING, JobStatus.RUNNING, JobStatus.CANCELLING}),
    ImportJob: frozenset({JobStatus.RUNNING, JobStatus.CANCELLING}),
    MigrationJob: frozenset({JobStatus.RUNNING, JobStatus.CANCELLING}),
    MetadataReindexJob: frozenset({JobStatus.RUNNING, JobStatus.CANCELLING}),
    CommunityRebuildJob: frozenset({JobStatus.RUNNING, JobStatus.CANCELLING}),
    ExportJob: frozenset(),
    DeleteJob: frozenset(),
    SyncJob: frozenset(),
}


def test_every_job_kind_is_classified_for_the_delete_guard() -> None:
    """Every job kind is listed either in COLLECTION_WRITING_JOB_STATUSES or in NEVER_BLOCKING_JOB_KINDS, never
    both: a new kind must be classified before DELETE /collections/{name} can tell whether it writes the collection."""
    from archon_search.jobs.store import COLLECTION_WRITING_JOB_STATUSES, NEVER_BLOCKING_JOB_KINDS

    kinds = _archon_job_kinds()
    assert {IngestJob, ReindexJob, ExportJob, SyncJob}.issubset(kinds), "precondition: kinds collected"
    assert _UnclassifiedJob not in kinds, "precondition: test-defined kinds are left out"

    blocking = set(COLLECTION_WRITING_JOB_STATUSES)
    assert blocking.isdisjoint(NEVER_BLOCKING_JOB_KINDS)
    assert blocking | NEVER_BLOCKING_JOB_KINDS == kinds


@pytest.mark.parametrize("status", list(JobStatus), ids=lambda status: status.value)
@pytest.mark.parametrize("kind", sorted(_EXPECTED_BLOCKING_STATUSES, key=lambda kind: kind.__name__),
                         ids=lambda kind: kind.__name__)
def test_may_write_collection_matches_the_expected_blocking_table(kind: type[IngestJob], status: JobStatus) -> None:
    """Every job kind x every status: the job may still write its collection (and blocks DELETE) exactly in the
    statuses the expected table names — e.g. a PENDING reindex blocks, a PENDING ingest (the lock covers it) and a
    QUEUED import / migration / legacy metadata-reindex or rebuild row (no task runs it) do not."""
    from archon_search.jobs.store import may_write_collection

    assert set(_EXPECTED_BLOCKING_STATUSES) == _archon_job_kinds(), "precondition: the expected table lists every kind"

    assert may_write_collection(_job_on("job", status, job_class=kind)) is (status in _EXPECTED_BLOCKING_STATUSES[kind])


@pytest.mark.parametrize("status", [JobStatus.RUNNING, JobStatus.CANCELLING], ids=lambda status: status.value)
def test_may_write_collection_exempts_only_a_dry_run_metadata_reindex(
    tmp_path: Path, status: JobStatus
) -> None:
    """A dry-run metadata reindex only reads its collection, so it never blocks DELETE; the same kind not dry-run
    still does (the presence anchor: the exemption cannot be a kind-wide one)."""
    from archon_search.jobs.store import may_write_collection

    store = JobStore(path=tmp_path / "jobs.json")
    dry_run = store.create_metadata_reindex("docs", dry_run=True)
    writing = store.create_metadata_reindex("docs")
    store.update(dry_run.job_id, status=status)
    store.update(writing.job_id, status=status)

    assert may_write_collection(dry_run) is False
    assert may_write_collection(writing) is True
    active = store.find_active_collection_job("docs", "default")
    assert active is not None and active.job_id == writing.job_id


def test_metadata_reindex_dry_run_survives_a_reload_and_defaults_false_for_older_rows(tmp_path: Path) -> None:
    """``dry_run`` is persisted in the jobs file, so a reloaded job keeps it; a row saved before the field existed
    loads as not dry-run (the fail-safe side: it still blocks DELETE)."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)
    dry_run = store.create_metadata_reindex("docs", dry_run=True)
    writing = store.create_metadata_reindex("docs")
    store.update(dry_run.job_id, status=JobStatus.DONE)
    store.update(writing.job_id, status=JobStatus.DONE)
    reloaded = JobStore(path=jobs_path)
    assert [reloaded.get(job.job_id).dry_run for job in (dry_run, writing)] == [True, False]
    rows = json.loads(jobs_path.read_text())
    assert {row["job_id"]: row["dry_run"] for row in rows} == {dry_run.job_id: True, writing.job_id: False}

    for row in rows:
        del row["dry_run"]
    jobs_path.write_text(json.dumps(rows))
    older = JobStore(path=jobs_path)
    assert [older.get(job.job_id).dry_run for job in (dry_run, writing)] == [False, False]


@pytest.mark.parametrize(
    ("status", "may_write"),
    [
        (JobStatus.QUEUED, True),
        (JobStatus.PENDING, True),
        (JobStatus.RUNNING, True),
        (JobStatus.CANCELLING, True),
        (JobStatus.DONE, False),
        (JobStatus.FAILED, False),
        (JobStatus.FAILED_EXPIRED, False),
        (JobStatus.CANCELLED, False),
    ],
)
def test_may_write_collection_treats_an_unclassified_kind_as_writing_until_terminal(
    caplog: pytest.LogCaptureFixture, status: JobStatus, may_write: bool
) -> None:
    """An unclassified kind fails closed: while not terminal it may write its collection, and the gap is logged at
    ERROR (naming the kind) so it gets classified; a terminal one writes nothing more."""
    from archon_search.jobs.store import may_write_collection

    job = _job_on("unclassified", status, job_class=_UnclassifiedJob)

    with caplog.at_level(logging.ERROR, logger="archon_search.jobs.store"):
        assert may_write_collection(job) is may_write

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == (["JobStore: job unclassified has the unclassified kind _UnclassifiedJob; treating it as "
                       "writing collection 'docs' until it is terminal"] if may_write else [])


def test_find_active_collection_job_prefers_the_callers_namespace(tmp_path: Path) -> None:
    """Any namespace's job on the collection counts (the table is shared by name); the caller's own is preferred,
    whichever was created first."""
    store = JobStore(path=tmp_path / "jobs.json")
    foreign = store.create_job(_job_on("foreign", JobStatus.RUNNING, namespace="other-team", job_class=ReindexJob))
    own = store.create_job(_job_on("own", JobStatus.RUNNING))

    assert store.find_active_collection_job("docs", "default") == own
    assert store.find_active_collection_job("docs", "third-team") == foreign


def test_find_active_collection_job_ignores_other_collections_and_jobs_that_cannot_write(tmp_path: Path) -> None:
    """Only a job on *collection* whose kind and status may still write it is returned."""
    store = JobStore(path=tmp_path / "jobs.json")
    store.create_job(_job_on("other-collection", JobStatus.RUNNING, collection="other"))
    store.create_job(_job_on("pending-ingest", JobStatus.PENDING))  # the collection lock covers it
    store.create_job(_job_on("done-reindex", JobStatus.DONE, job_class=ReindexJob))
    store.create_job(_job_on("running-export", JobStatus.RUNNING, job_class=ExportJob))

    assert store.find_active_collection_job("docs", "default") is None


# ===========================================================================
# Per-row tolerance of a stored ingest_options value, and the off-wire cancel marker
# ===========================================================================


@pytest.mark.parametrize(
    "bad_options",
    [
        "garbage",
        {"submitted_path": "~/a.md", "ingested_by": "cli", "unknown_key": 1},
        {"ingested_by": "cli"},
        {"submitted_path": "~/a.md", "ingested_by": "cli", "chunk_scopes": "team-a"},
        {"submitted_path": "~/a.md", "ingested_by": "cli", "chunk_scopes": ["team-a", 7]},
        {"submitted_path": "~/a.md", "ingested_by": "cli", "chunk_scopes": [["team-a"]]},
        {"submitted_path": "~/a.md", "ingested_by": "cli", "chunk_ttl_seconds": "3600"},
        {"submitted_path": "~/a.md", "ingested_by": "cli", "chunk_ttl_seconds": True},
        {"submitted_path": 7, "ingested_by": "cli"},
        {"submitted_path": "~/a.md", "ingested_by": None},
    ],
    ids=[
        "not-an-object", "unknown-key", "missing-key", "scopes-a-string", "scopes-non-string-item",
        "scopes-nested-list", "ttl-a-string", "ttl-a-bool", "submitted-path-not-a-string", "ingested-by-not-a-string",
    ],
)
def test_load_drops_one_rows_unusable_ingest_options_and_keeps_the_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, bad_options: object
) -> None:
    """An unusable ``ingest_options`` value costs only that row its options (``None``: never retried), logged at
    WARNING — never a reset of the whole jobs file."""
    jobs_path = tmp_path / "jobs.json"
    now = datetime.now(timezone.utc).isoformat()
    row = {"status": "FAILED", "created_at": now, "updated_at": now, "result": None, "error": None}
    jobs_path.write_text(json.dumps([
        {**row, "job_id": "bad", "ingest_options": bad_options},
        {**row, "job_id": "good", "ingest_options": {
            "submitted_path": "~/b.md", "ingested_by": "cli", "chunk_scopes": [], "chunk_ttl_seconds": None,
        }},
    ]))

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.store"):
        store = JobStore(path=jobs_path)

    bad, good = store.get("bad"), store.get("good")
    assert bad is not None and good is not None
    assert (bad.status, bad.ingest_options) == (JobStatus.FAILED, None)
    # Explicit empty scopes and a null TTL are valid values: they round-trip (``[]`` as an empty tuple, not ``None``).
    assert good.ingest_options == IngestOptions(
        submitted_path="~/b.md", ingested_by="cli", chunk_ttl_seconds=None, chunk_scopes=()
    )
    assert any(r.levelno == logging.WARNING and "job bad" in r.getMessage() for r in caplog.records)


def test_cancel_requested_persists_only_when_set(tmp_path: Path) -> None:
    """``cancel_requested`` (set by DELETE /jobs/{id}) survives a reload; a job without it keeps the row shape it had
    before the marker existed."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)
    cancelled = store.create(collection="docs")
    plain = store.create(collection="docs")
    store.transition(cancelled.job_id, {JobStatus.PENDING}, JobStatus.CANCELLING, cancel_requested=True)

    reloaded = JobStore(path=jobs_path)

    loaded = reloaded.get(cancelled.job_id)
    assert loaded is not None
    assert loaded.cancel_requested is True
    rows = {row["job_id"]: row for row in json.loads(jobs_path.read_text())}
    assert "cancel_requested" not in rows[plain.job_id]


@pytest.mark.parametrize("bad_marker", ["false", 1, None], ids=["string", "int", "null"])
def test_load_treats_a_non_bool_cancel_requested_as_unset(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, bad_marker: object
) -> None:
    """A ``cancel_requested`` that is not a JSON boolean is no client's cancel: the row loads with it False (a truthy
    ``"false"`` would otherwise end a retry chain), logged at WARNING; the rest of the file loads unharmed."""
    jobs_path = tmp_path / "jobs.json"
    now = datetime.now(timezone.utc).isoformat()
    row = {"status": "FAILED", "created_at": now, "updated_at": now, "result": None, "error": None}
    jobs_path.write_text(json.dumps([
        {**row, "job_id": "bad", "cancel_requested": bad_marker},
        {**row, "job_id": "good", "cancel_requested": True},
    ]))

    with caplog.at_level(logging.WARNING, logger="archon_search.jobs.store"):
        store = JobStore(path=jobs_path)

    bad, good = store.get("bad"), store.get("good")
    assert bad is not None and good is not None
    assert (bad.cancel_requested, good.cancel_requested) == (False, True)
    assert any(r.levelno == logging.WARNING and "job bad" in r.getMessage() for r in caplog.records)

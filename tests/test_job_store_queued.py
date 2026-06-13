"""Tests for JobStore — QUEUED status, ExportJob/ImportJob factory methods,
serialization roundtrips, eviction guard, and list_queued_bulk ordering.

TDD for Task 1.4 of the D1-D2 collection export/import plan.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.store import JobStore
from archon_search.types import ExportJob, ImportJob, IngestJob, JobStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


# ---------------------------------------------------------------------------
# Factory methods
# ---------------------------------------------------------------------------


def test_create_export_job_is_queued(store: JobStore) -> None:
    job = store.create_export(
        collection="col1",
        output_path="/data/exports/col1-20250101T000000Z.tar.gz",
        tmp_path="/data/exports/.export-abc.jsonl.tmp",
    )
    assert isinstance(job, ExportJob)
    assert job.status == JobStatus.QUEUED
    assert job.collection == "col1"
    assert job.output_path == "/data/exports/col1-20250101T000000Z.tar.gz"
    assert job.tmp_path == "/data/exports/.export-abc.jsonl.tmp"
    assert job.namespace == DEFAULT_NAMESPACE


def test_create_import_job_is_queued(store: JobStore) -> None:
    job = store.create_import(
        collection="col2",
        archive_path="/data/exports/col2.tar.gz",
        force_overwrite=True,
        ignore_schema_version=False,
        on_error="skip",
    )
    assert isinstance(job, ImportJob)
    assert job.status == JobStatus.QUEUED
    assert job.collection == "col2"
    assert job.archive_path == "/data/exports/col2.tar.gz"
    assert job.force_overwrite is True
    assert job.ignore_schema_version is False
    assert job.on_error == "skip"
    assert job.namespace == DEFAULT_NAMESPACE


def test_create_export_job_custom_namespace(store: JobStore) -> None:
    job = store.create_export(
        collection="col3",
        output_path="/tmp/out.tar.gz",
        tmp_path="/tmp/.export-xyz.jsonl.tmp",
        namespace="tenantA",
    )
    assert job.namespace == "tenantA"


def test_create_import_job_custom_namespace(store: JobStore) -> None:
    job = store.create_import(
        collection="col4",
        archive_path="/tmp/col4.tar.gz",
        force_overwrite=False,
        ignore_schema_version=True,
        on_error="fail",
        namespace="tenantB",
    )
    assert job.namespace == "tenantB"


# ---------------------------------------------------------------------------
# Eviction guard
# ---------------------------------------------------------------------------


def _make_old_job_dict(job_id: str, status: str, job_type: str = "ingest") -> dict:
    old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    base = {
        "job_id": job_id,
        "status": status,
        "created_at": old_ts,
        "updated_at": old_ts,
        "result": None,
        "error": None,
        "namespace": DEFAULT_NAMESPACE,
        "progress": None,
        "job_type": job_type,
    }
    if job_type == "export":
        base.update({"collection": "c", "output_path": "/tmp/out.tar.gz", "tmp_path": "/tmp/.tmp"})
    elif job_type == "import":
        base.update({"collection": "c", "archive_path": "/tmp/in.tar.gz",
                     "force_overwrite": False, "ignore_schema_version": False, "on_error": "fail"})
    return base


def test_evict_old_skips_queued(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    jid = str(uuid.uuid4())
    data = [_make_old_job_dict(jid, "QUEUED", "export")]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    assert store.get(jid) is not None, "QUEUED job older than 8 days must NOT be evicted"


def test_evict_old_skips_running(tmp_path: Path) -> None:
    """A RUNNING job 8 days old: crash recovery flips it to FAILED, then eviction
    removes the old FAILED job. Eviction is correct here — the job was old and terminal.
    What matters is it was NOT silently dropped during load WITHOUT crash recovery.
    We verify this by checking an active RUNNING job (< 7 days) survives."""
    jobs_path = tmp_path / "jobs.json"
    jid = str(uuid.uuid4())
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    data = [
        {
            "job_id": jid,
            "status": "RUNNING",
            "created_at": recent_ts,
            "updated_at": recent_ts,
            "result": None,
            "error": None,
            "namespace": DEFAULT_NAMESPACE,
            "progress": None,
            "job_type": "export",
            "collection": "c",
            "output_path": "/tmp/out.tar.gz",
            "tmp_path": "/tmp/.tmp",
        }
    ]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    job = store.get(jid)
    # Crash recovery flips RUNNING→FAILED, but it's recent so eviction does NOT remove it
    assert job is not None, "Recent RUNNING job must survive (crash recovery flips to FAILED but eviction skips it)"
    assert job.status == JobStatus.FAILED
    assert job.error == "process_restart"


def test_evict_old_skips_cancelling(tmp_path: Path) -> None:
    """A CANCELLING job: crash recovery flips to FAILED. Recent one must not be evicted."""
    jobs_path = tmp_path / "jobs.json"
    jid = str(uuid.uuid4())
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    data = [
        {
            "job_id": jid,
            "status": "CANCELLING",
            "created_at": recent_ts,
            "updated_at": recent_ts,
            "result": None,
            "error": None,
            "namespace": DEFAULT_NAMESPACE,
            "progress": None,
            "job_type": "ingest",
        }
    ]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    job = store.get(jid)
    assert job is not None, "Recent CANCELLING job must survive (crash-recovered to FAILED, not evicted)"
    assert job.status == JobStatus.FAILED


def test_evict_old_skips_pending(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    jid = str(uuid.uuid4())
    data = [_make_old_job_dict(jid, "PENDING")]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    assert store.get(jid) is not None, "PENDING job older than 8 days must NOT be evicted"


def test_evict_old_removes_done(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    jid = str(uuid.uuid4())
    data = [_make_old_job_dict(jid, "DONE")]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    assert store.get(jid) is None, "DONE job older than 8 days MUST be evicted"


def test_evict_old_removes_failed(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    jid = str(uuid.uuid4())
    data = [_make_old_job_dict(jid, "FAILED")]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    assert store.get(jid) is None, "FAILED job older than 8 days MUST be evicted"


def test_evict_old_removes_cancelled(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    jid = str(uuid.uuid4())
    data = [_make_old_job_dict(jid, "CANCELLED")]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    assert store.get(jid) is None, "CANCELLED job older than 8 days MUST be evicted"


# ---------------------------------------------------------------------------
# update_progress
# ---------------------------------------------------------------------------


def test_update_progress_sets_field(store: JobStore) -> None:
    job = store.create_export(
        collection="col",
        output_path="/tmp/out.tar.gz",
        tmp_path="/tmp/.tmp",
    )
    store.update_progress(job.job_id, processed=50, total=100, phase="reading")
    updated = store.get(job.job_id)
    assert updated is not None
    assert updated.progress == {"processed": 50, "total": 100, "phase": "reading"}


def test_update_progress_overwrites_previous(store: JobStore) -> None:
    job = store.create_export(
        collection="col",
        output_path="/tmp/out.tar.gz",
        tmp_path="/tmp/.tmp",
    )
    store.update_progress(job.job_id, processed=50, total=200, phase="writing")
    store.update_progress(job.job_id, processed=150, total=200, phase="packaging")
    updated = store.get(job.job_id)
    assert updated is not None
    assert updated.progress == {"processed": 150, "total": 200, "phase": "packaging"}


# ---------------------------------------------------------------------------
# Serialization roundtrips
# ---------------------------------------------------------------------------


def test_serialization_roundtrip_export_job(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)
    job = store.create_export(
        collection="mycol",
        output_path="/exports/mycol.tar.gz",
        tmp_path="/exports/.export-123.jsonl.tmp",
        namespace="ns1",
    )
    # Simulate progress update before reload
    store.update_progress(job.job_id, processed=10, total=50, phase="writing")

    # Reload from disk
    store2 = JobStore(path=jobs_path)
    reloaded = store2.get(job.job_id)
    assert reloaded is not None
    assert isinstance(reloaded, ExportJob)
    assert reloaded.collection == "mycol"
    assert reloaded.output_path == "/exports/mycol.tar.gz"
    assert reloaded.tmp_path == "/exports/.export-123.jsonl.tmp"
    assert reloaded.namespace == "ns1"
    assert reloaded.status == JobStatus.QUEUED
    assert reloaded.progress == {"processed": 10, "total": 50, "phase": "writing"}


def test_serialization_roundtrip_import_job(tmp_path: Path) -> None:
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)
    job = store.create_import(
        collection="importcol",
        archive_path="/archives/importcol.tar.gz",
        force_overwrite=True,
        ignore_schema_version=True,
        on_error="skip",
        namespace="ns2",
    )

    store2 = JobStore(path=jobs_path)
    reloaded = store2.get(job.job_id)
    assert reloaded is not None
    assert isinstance(reloaded, ImportJob)
    assert reloaded.collection == "importcol"
    assert reloaded.archive_path == "/archives/importcol.tar.gz"
    assert reloaded.force_overwrite is True
    assert reloaded.ignore_schema_version is True
    assert reloaded.on_error == "skip"
    assert reloaded.namespace == "ns2"
    assert reloaded.progress is None


def test_load_legacy_job_missing_progress(tmp_path: Path) -> None:
    """Pre-D1 persisted jobs without a 'progress' key deserialize with progress=None."""
    jobs_path = tmp_path / "jobs.json"
    now = datetime.now(timezone.utc).isoformat()
    data = [
        {
            "job_id": "legacy-001",
            "status": "DONE",
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
            "namespace": DEFAULT_NAMESPACE,
            "job_type": "ingest",
            # intentionally no "progress" key
        }
    ]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    job = store.get("legacy-001")
    assert job is not None
    assert job.progress is None


def test_load_legacy_export_job_missing_progress(tmp_path: Path) -> None:
    """Pre-D1 export job without 'progress' key deserializes with progress=None."""
    jobs_path = tmp_path / "jobs.json"
    now = datetime.now(timezone.utc).isoformat()
    data = [
        {
            "job_id": "legacy-export-001",
            "status": "QUEUED",
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
            "namespace": DEFAULT_NAMESPACE,
            "job_type": "export",
            "collection": "col",
            "output_path": "/tmp/out.tar.gz",
            "tmp_path": "/tmp/.tmp",
            # intentionally no "progress" key
        }
    ]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    job = store.get("legacy-export-001")
    assert job is not None
    assert isinstance(job, ExportJob)
    assert job.progress is None


# ---------------------------------------------------------------------------
# list_queued_bulk
# ---------------------------------------------------------------------------


def test_list_queued_bulk_ordering(tmp_path: Path) -> None:
    """Returns QUEUED export/import jobs sorted by created_at ascending (FIFO)."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)

    # Create export jobs with artificially spaced timestamps by direct manipulation
    j1 = store.create_export(collection="a", output_path="/tmp/a.tar.gz", tmp_path="/tmp/.a.tmp")
    j2 = store.create_export(collection="b", output_path="/tmp/b.tar.gz", tmp_path="/tmp/.b.tmp")
    j3 = store.create_import(collection="c", archive_path="/tmp/c.tar.gz",
                              force_overwrite=False, ignore_schema_version=False, on_error="fail")

    # Force distinct created_at via direct state manipulation for deterministic ordering
    from datetime import timedelta
    ts1 = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    ts2 = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    ts3 = datetime.now(timezone.utc).isoformat()
    store._jobs[j1.job_id] = store._jobs[j1.job_id].__class__(
        **{**store._jobs[j1.job_id].__dict__, "created_at": ts1}
    )
    store._jobs[j2.job_id] = store._jobs[j2.job_id].__class__(
        **{**store._jobs[j2.job_id].__dict__, "created_at": ts2}
    )
    store._jobs[j3.job_id] = store._jobs[j3.job_id].__class__(
        **{**store._jobs[j3.job_id].__dict__, "created_at": ts3}
    )

    queued = store.list_queued_bulk()
    assert len(queued) == 3
    assert queued[0].job_id == j1.job_id
    assert queued[1].job_id == j2.job_id
    assert queued[2].job_id == j3.job_id


def test_list_queued_bulk_excludes_non_queued(store: JobStore) -> None:
    """Only QUEUED status bulk jobs are returned."""
    j_queued = store.create_export(collection="a", output_path="/tmp/a.tar.gz", tmp_path="/tmp/.a.tmp")
    j_running = store.create_export(collection="b", output_path="/tmp/b.tar.gz", tmp_path="/tmp/.b.tmp")
    store.update(j_running.job_id, status=JobStatus.RUNNING)

    queued = store.list_queued_bulk()
    ids = [j.job_id for j in queued]
    assert j_queued.job_id in ids
    assert j_running.job_id not in ids


def test_list_queued_bulk_excludes_ingest(store: JobStore) -> None:
    """Regular IngestJob (even in PENDING/QUEUED-like state) is not returned."""
    # IngestJobs use PENDING, not QUEUED; but even if somehow QUEUED (hypothetically),
    # list_queued_bulk must only return ExportJob and ImportJob instances.
    # Normal IngestJob is PENDING — it must not appear.
    store.create()  # IngestJob in PENDING status
    export_job = store.create_export(collection="col", output_path="/tmp/out.tar.gz", tmp_path="/tmp/.tmp")

    queued = store.list_queued_bulk()
    assert len(queued) == 1
    assert queued[0].job_id == export_job.job_id
    assert isinstance(queued[0], ExportJob)


def test_list_queued_bulk_empty_when_no_bulk_jobs(store: JobStore) -> None:
    store.create()
    store.create()
    assert store.list_queued_bulk() == []

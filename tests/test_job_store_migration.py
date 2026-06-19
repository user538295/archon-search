"""Tests for BE-10 — MigrationJob integration into JobStore.

Covers:
- _write_atomic() / _load() discriminator round-trip (serialisation)
- Crash recovery: RUNNING MigrationJob → FAILED on load, QUEUED survives
- create_migration() factory
- list_queued_bulk() includes MigrationJob alongside ExportJob/ImportJob
- job_to_dict() exposes migrations_applied and backup_confirmed (None for other jobs)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.model import job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.types import ExportJob, ImportJob, JobStatus, MigrationJob, MigrationKind


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


# ---------------------------------------------------------------------------
# Serialisation round-trip
# ---------------------------------------------------------------------------


def test_migration_job_serialization_round_trip(tmp_path: Path) -> None:
    """_write_atomic() + _load() round-trip preserves all MigrationJob fields."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)

    job = store.create_migration(
        collection="my-col",
        kind=MigrationKind.REWRITE,
        backup_confirmed=True,
        namespace="ns1",
    )

    # Simulate progress being written mid-run
    store.update_progress(job.job_id, processed=100, total=500, phase="rewriting")
    # Set migrations_applied as if mid-job
    store.update(job.job_id, migrations_applied=["some_migration"], status=JobStatus.RUNNING)

    # Reload from disk (crash recovery will flip RUNNING → FAILED, but we check fields)
    store2 = JobStore(path=jobs_path)
    reloaded = store2.get(job.job_id)

    assert reloaded is not None
    assert isinstance(reloaded, MigrationJob)
    assert reloaded.collection == "my-col"
    assert reloaded.kind == MigrationKind.REWRITE
    assert reloaded.backup_confirmed is True
    assert reloaded.namespace == "ns1"
    assert reloaded.migrations_applied == ["some_migration"]
    assert reloaded.progress == {"processed": 100, "total": 500, "phase": "rewriting"}
    assert reloaded.source == "user"
    # Crash recovery: RUNNING → FAILED
    assert reloaded.status == JobStatus.FAILED
    assert reloaded.error == "process_restart"


def test_migration_job_serialization_preserves_kind_enum(tmp_path: Path) -> None:
    """kind is stored as wire string and reloaded as MigrationKind enum."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)

    job = store.create_migration(
        collection="col",
        kind=MigrationKind.IN_PLACE,
        backup_confirmed=None,
    )

    store2 = JobStore(path=jobs_path)
    reloaded = store2.get(job.job_id)
    assert reloaded is not None
    assert isinstance(reloaded, MigrationJob)
    assert reloaded.kind is MigrationKind.IN_PLACE


def test_migration_job_serialization_preserves_export_rebuild_kind(tmp_path: Path) -> None:
    """export_rebuild kind survives round-trip."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)

    job = store.create_migration(
        collection="col",
        kind=MigrationKind.EXPORT_REBUILD,
        backup_confirmed=True,
    )

    store2 = JobStore(path=jobs_path)
    reloaded = store2.get(job.job_id)
    assert reloaded is not None
    assert isinstance(reloaded, MigrationJob)
    assert reloaded.kind is MigrationKind.EXPORT_REBUILD


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


def test_migration_job_crash_recovery_running_to_failed(tmp_path: Path) -> None:
    """MigrationJob in RUNNING loaded as FAILED with error='process_restart'; progress preserved."""
    jobs_path = tmp_path / "jobs.json"
    now = _now()
    jid = str(uuid.uuid4())
    data = [
        {
            "job_id": jid,
            "status": "RUNNING",
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
            "namespace": DEFAULT_NAMESPACE,
            "progress": {"processed": 200, "total": 1000, "phase": "rewriting"},
            "collection": "my-col",
            "kind": "rewrite",
            "migrations_applied": [],
            "backup_confirmed": True,
            "source": "user",
            "job_type": "migration",
        }
    ]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)

    job = store.get(jid)
    assert job is not None
    assert isinstance(job, MigrationJob)
    assert job.status == JobStatus.FAILED
    assert job.error == "process_restart"
    # Progress checkpoint preserved
    assert job.progress == {"processed": 200, "total": 1000, "phase": "rewriting"}


def test_migration_job_crash_recovery_cancelling_to_failed(tmp_path: Path) -> None:
    """MigrationJob in CANCELLING loaded as FAILED with error='process_restart'."""
    jobs_path = tmp_path / "jobs.json"
    now = _now()
    jid = str(uuid.uuid4())
    data = [
        {
            "job_id": jid,
            "status": "CANCELLING",
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
            "namespace": DEFAULT_NAMESPACE,
            "progress": None,
            "collection": "my-col",
            "kind": "rewrite",
            "migrations_applied": [],
            "backup_confirmed": True,
            "source": "user",
            "job_type": "migration",
        }
    ]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)

    job = store.get(jid)
    assert job is not None
    assert isinstance(job, MigrationJob)
    assert job.status == JobStatus.FAILED
    assert job.error == "process_restart"


def test_migration_job_queued_survives_restart(tmp_path: Path) -> None:
    """QUEUED MigrationJob is NOT set to FAILED on load."""
    jobs_path = tmp_path / "jobs.json"
    now = _now()
    jid = str(uuid.uuid4())
    data = [
        {
            "job_id": jid,
            "status": "QUEUED",
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
            "namespace": DEFAULT_NAMESPACE,
            "progress": None,
            "collection": "my-col",
            "kind": "in_place",
            "migrations_applied": [],
            "backup_confirmed": None,
            "source": "user",
            "job_type": "migration",
        }
    ]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)

    job = store.get(jid)
    assert job is not None
    assert isinstance(job, MigrationJob)
    assert job.status == JobStatus.QUEUED
    assert job.error is None


# ---------------------------------------------------------------------------
# create_migration() factory
# ---------------------------------------------------------------------------


def test_create_migration_returns_queued_job(store: JobStore) -> None:
    """Factory returns MigrationJob with status=QUEUED."""
    job = store.create_migration(
        collection="col1",
        kind=MigrationKind.IN_PLACE,
        backup_confirmed=None,
    )
    assert isinstance(job, MigrationJob)
    assert job.status == JobStatus.QUEUED
    assert job.collection == "col1"
    assert job.kind == MigrationKind.IN_PLACE
    assert job.backup_confirmed is None
    assert job.namespace == DEFAULT_NAMESPACE


def test_create_migration_with_rewrite_kind_and_backup(store: JobStore) -> None:
    """Factory stores backup_confirmed=True for rewrite migrations."""
    job = store.create_migration(
        collection="big-col",
        kind=MigrationKind.REWRITE,
        backup_confirmed=True,
        namespace="tenant-x",
    )
    assert isinstance(job, MigrationJob)
    assert job.kind == MigrationKind.REWRITE
    assert job.backup_confirmed is True
    assert job.namespace == "tenant-x"


def test_create_migration_job_is_persisted(tmp_path: Path) -> None:
    """Created MigrationJob is persisted to disk immediately."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)
    job = store.create_migration(
        collection="col",
        kind=MigrationKind.IN_PLACE,
        backup_confirmed=None,
    )

    store2 = JobStore(path=jobs_path)
    reloaded = store2.get(job.job_id)
    assert reloaded is not None
    assert isinstance(reloaded, MigrationJob)
    assert reloaded.collection == "col"


def test_create_migration_default_migrations_applied_empty(store: JobStore) -> None:
    """Factory creates job with empty migrations_applied list."""
    job = store.create_migration(
        collection="col",
        kind=MigrationKind.IN_PLACE,
        backup_confirmed=None,
    )
    assert job.migrations_applied == []


# ---------------------------------------------------------------------------
# list_queued_bulk includes MigrationJob
# ---------------------------------------------------------------------------


def test_list_queued_bulk_includes_migration_job(store: JobStore) -> None:
    """list_queued_bulk() returns MigrationJob alongside ExportJob/ImportJob."""
    export_job = store.create_export(
        collection="col-a", output_path="/tmp/a.tar.gz", tmp_path="/tmp/.a.tmp"
    )
    migration_job = store.create_migration(
        collection="col-b", kind=MigrationKind.REWRITE, backup_confirmed=True
    )
    import_job = store.create_import(
        collection="col-c",
        archive_path="/tmp/c.tar.gz",
        force_overwrite=False,
        ignore_schema_version=False,
        on_error="fail",
    )

    queued = store.list_queued_bulk()
    ids = [j.job_id for j in queued]
    assert export_job.job_id in ids
    assert migration_job.job_id in ids
    assert import_job.job_id in ids
    assert len(queued) == 3


def test_list_queued_bulk_migration_job_excluded_when_not_queued(store: JobStore) -> None:
    """Non-QUEUED MigrationJob is excluded from list_queued_bulk()."""
    job = store.create_migration(
        collection="col", kind=MigrationKind.IN_PLACE, backup_confirmed=None
    )
    store.update(job.job_id, status=JobStatus.RUNNING)

    queued = store.list_queued_bulk()
    assert len(queued) == 0


def test_list_queued_bulk_returns_migration_job_instances(store: JobStore) -> None:
    """MigrationJob returned from list_queued_bulk() is a proper MigrationJob instance."""
    store.create_migration(
        collection="col", kind=MigrationKind.REWRITE, backup_confirmed=True
    )

    queued = store.list_queued_bulk()
    assert len(queued) == 1
    assert isinstance(queued[0], MigrationJob)


# ---------------------------------------------------------------------------
# job_to_dict — migrations_applied and backup_confirmed
# ---------------------------------------------------------------------------


def test_job_to_dict_includes_migration_fields_for_migration_job() -> None:
    """migrations_applied and backup_confirmed appear in dict for MigrationJob."""
    now = _now()
    job = MigrationJob(
        job_id="j-migration",
        status=JobStatus.QUEUED,
        created_at=now,
        updated_at=now,
        collection="col",
        kind=MigrationKind.REWRITE,
        migrations_applied=["migrate_description_embedding"],
        backup_confirmed=True,
    )
    d = job_to_dict(job)
    assert d["migrations_applied"] == ["migrate_description_embedding"]
    assert d["backup_confirmed"] is True
    assert d["kind"] == "rewrite"


def test_job_to_dict_migration_fields_none_for_ingest_job() -> None:
    """Existing IngestJob gets None for both new migration fields."""
    from archon_search.types import IngestJob

    now = _now()
    job = IngestJob(
        job_id="j-ingest",
        status=JobStatus.RUNNING,
        created_at=now,
        updated_at=now,
    )
    d = job_to_dict(job)
    assert d["migrations_applied"] is None
    assert d["backup_confirmed"] is None
    assert d["kind"] is None


def test_job_to_dict_migration_fields_none_for_export_job() -> None:
    """ExportJob gets None for both new migration fields."""
    now = _now()
    job = ExportJob(
        job_id="j-export",
        status=JobStatus.QUEUED,
        created_at=now,
        updated_at=now,
        collection="col",
        output_path="",
        tmp_path="/tmp/.tmp",
    )
    d = job_to_dict(job)
    assert d["migrations_applied"] is None
    assert d["backup_confirmed"] is None
    assert d["kind"] is None


def test_job_to_dict_migration_fields_none_for_import_job() -> None:
    """ImportJob gets None for both new migration fields."""
    now = _now()
    job = ImportJob(
        job_id="j-import",
        status=JobStatus.QUEUED,
        created_at=now,
        updated_at=now,
        collection="col",
        archive_path="/tmp/foo.tar.gz",
    )
    d = job_to_dict(job)
    assert d["migrations_applied"] is None
    assert d["backup_confirmed"] is None
    assert d["kind"] is None


def test_job_to_dict_migration_backup_confirmed_false() -> None:
    """backup_confirmed=False is preserved as False (not None or truthy coercion)."""
    now = _now()
    job = MigrationJob(
        job_id="j-mig-false",
        status=JobStatus.QUEUED,
        created_at=now,
        updated_at=now,
        collection="col",
        kind=MigrationKind.REWRITE,
        migrations_applied=[],
        backup_confirmed=False,
    )
    d = job_to_dict(job)
    assert d["backup_confirmed"] is False


def test_job_to_dict_migration_empty_applied_list() -> None:
    """Empty migrations_applied list is preserved (not coerced to None)."""
    now = _now()
    job = MigrationJob(
        job_id="j-mig",
        status=JobStatus.QUEUED,
        created_at=now,
        updated_at=now,
        collection="col",
        kind=MigrationKind.IN_PLACE,
        migrations_applied=[],
        backup_confirmed=None,
    )
    d = job_to_dict(job)
    assert d["migrations_applied"] == []
    assert d["backup_confirmed"] is None

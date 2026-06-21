"""BE-7 — IngestJob base-class source, source_path, collection, retry_count fields.

Tests for the four new fields added to IngestJob base class and the extended
JobStore.create() optional parameters.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from archon_search.jobs.model import IngestJob, JobStatus, job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.types import ExportJob, ImportJob, MigrationJob, MigrationKind, ReindexJob


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


# ---------------------------------------------------------------------------
# IngestJob base-class field defaults
# ---------------------------------------------------------------------------


def test_ingest_job_source_default() -> None:
    """IngestJob() has source='user' by default."""
    job = IngestJob(
        job_id="j1",
        status=JobStatus.PENDING,
        created_at=_now(),
        updated_at=_now(),
    )
    assert job.source == "user"


def test_ingest_job_source_maintenance() -> None:
    """IngestJob(source='maintenance') round-trips through job_to_dict."""
    job = IngestJob(
        job_id="j2",
        status=JobStatus.PENDING,
        created_at=_now(),
        updated_at=_now(),
        source="maintenance",
    )
    assert job.source == "maintenance"
    d = job_to_dict(job)
    assert d["source"] == "maintenance"


def test_ingest_job_source_literal() -> None:
    """source='maintenance' is valid on IngestJob. Invalid source raises ValueError at runtime."""
    # Valid values
    for src in ("user", "backup", "maintenance"):
        job = IngestJob(
            job_id="j-valid",
            status=JobStatus.PENDING,
            created_at=_now(),
            updated_at=_now(),
            source=src,  # type: ignore[arg-type]
        )
        assert job.source == src

    # Invalid value raises ValueError (runtime enforcement via __post_init__)
    with pytest.raises(ValueError, match="Invalid source"):
        IngestJob(
            job_id="j-invalid",
            status=JobStatus.PENDING,
            created_at=_now(),
            updated_at=_now(),
            source="unknown",  # type: ignore[arg-type]
        )


def test_ingest_job_source_path_and_collection_fields() -> None:
    """source_path, collection, retry_count default to empty/0 and round-trip."""
    job = IngestJob(
        job_id="j6",
        status=JobStatus.PENDING,
        created_at=_now(),
        updated_at=_now(),
        source_path="/some/file.txt",
        collection="my-col",
        retry_count=2,
    )
    assert job.source_path == "/some/file.txt"
    assert job.collection == "my-col"
    assert job.retry_count == 2

    d = dataclasses.asdict(job)
    assert d["source_path"] == "/some/file.txt"
    assert d["collection"] == "my-col"
    assert d["retry_count"] == 2


def test_ingest_job_new_fields_default_to_empty() -> None:
    """source_path, collection, retry_count all have empty/0 defaults."""
    job = IngestJob(
        job_id="j7",
        status=JobStatus.PENDING,
        created_at=_now(),
        updated_at=_now(),
    )
    assert job.source_path == ""
    assert job.collection == ""
    assert job.retry_count == 0


# ---------------------------------------------------------------------------
# job_to_dict includes new fields
# ---------------------------------------------------------------------------


def test_ingest_job_dict_source_is_user() -> None:
    """After BE-7, base IngestJob serializes source='user', not None."""
    job = IngestJob(
        job_id="j-ingest",
        status=JobStatus.RUNNING,
        created_at=_now(),
        updated_at=_now(),
    )
    d = job_to_dict(job)
    assert d["source"] == "user"
    assert d["collection"] == ""
    assert d["output_path"] is None
    assert d["archive_path"] is None


def test_ingest_job_dict_source_path_and_retry_count() -> None:
    """job_to_dict includes source_path and retry_count from IngestJob base."""
    job = IngestJob(
        job_id="j-base",
        status=JobStatus.PENDING,
        created_at=_now(),
        updated_at=_now(),
        source="maintenance",
        source_path="/data/docs/readme.txt",
        collection="docs",
        retry_count=1,
    )
    d = job_to_dict(job)
    assert d["source"] == "maintenance"
    assert d["source_path"] == "/data/docs/readme.txt"
    assert d["collection"] == "docs"
    assert d["retry_count"] == 1


# ---------------------------------------------------------------------------
# Subclass Literals — ExportJob, ImportJob, MigrationJob do NOT widen to "maintenance"
# ---------------------------------------------------------------------------


def test_export_job_source_literal_is_narrower_than_ingest_job() -> None:
    """ExportJob.source Literal is 'user'|'backup' — narrower than IngestJob's
    'user'|'backup'|'maintenance'. Verified by static type checking; runtime
    check confirms the field value is accessible and defaults correctly."""
    job = ExportJob(
        job_id="ej1",
        status=JobStatus.QUEUED,
        created_at=_now(),
        updated_at=_now(),
    )
    # ExportJob.source shadows base with Literal["user", "backup"]
    assert job.source == "user"


def test_import_job_source_literal_is_narrower_than_ingest_job() -> None:
    """ImportJob.source Literal is 'user'|'backup' — narrower than IngestJob's
    'user'|'backup'|'maintenance'. Verified by static type checking; runtime
    check confirms the field value is accessible and defaults correctly."""
    job = ImportJob(
        job_id="ij1",
        status=JobStatus.QUEUED,
        created_at=_now(),
        updated_at=_now(),
    )
    assert job.source == "user"


def test_migration_job_source_literal_is_narrower_than_ingest_job() -> None:
    """MigrationJob.source Literal is 'user'|'backup' — narrower than IngestJob's
    'user'|'backup'|'maintenance'. Verified by static type checking; runtime
    check confirms the field value is accessible and defaults correctly."""
    job = MigrationJob(
        job_id="mj1",
        status=JobStatus.QUEUED,
        created_at=_now(),
        updated_at=_now(),
        collection="col1",
        kind=MigrationKind.IN_PLACE,
    )
    assert job.source == "user"


def test_reindex_job_inherits_source_user() -> None:
    """ReindexJob inherits source='user' from IngestJob base — no own source field needed."""
    job = ReindexJob(
        job_id="rj1",
        status=JobStatus.PENDING,
        created_at=_now(),
        updated_at=_now(),
    )
    assert job.source == "user"


# ---------------------------------------------------------------------------
# JobStore.create() with new optional parameters
# ---------------------------------------------------------------------------


def test_job_store_create_with_source_maintenance(store: JobStore) -> None:
    """JobStore.create(source='maintenance', path=..., collection=..., namespace=...) works."""
    job = store.create(
        source="maintenance",
        path="/some/file.txt",
        collection="my-col",
        namespace="ns1",
    )
    assert job.source == "maintenance"
    assert job.source_path == "/some/file.txt"
    assert job.collection == "my-col"
    assert job.namespace == "ns1"


def test_job_store_create_defaults_unchanged(store: JobStore) -> None:
    """JobStore.create() with no new args still returns source='user', empty fields."""
    job = store.create()
    assert job.source == "user"
    assert job.source_path == ""
    assert job.collection == ""
    assert job.retry_count == 0


def test_job_store_create_persists_new_fields(tmp_path: Path) -> None:
    """New fields survive a round-trip through disk."""
    jobs_path = tmp_path / "jobs.json"
    s1 = JobStore(path=jobs_path)
    job = s1.create(source="maintenance", path="/a/b/c.txt", collection="col-x", namespace="ns2")

    s2 = JobStore(path=jobs_path)
    reloaded = s2.get(job.job_id)
    assert reloaded is not None
    assert reloaded.source == "maintenance"
    assert reloaded.source_path == "/a/b/c.txt"
    assert reloaded.collection == "col-x"
    assert reloaded.namespace == "ns2"
    assert reloaded.retry_count == 0


# ---------------------------------------------------------------------------
# Backward-compat: pre-D5 JSON without new fields loads cleanly
# ---------------------------------------------------------------------------


def test_ingest_job_from_dict_missing_new_fields(tmp_path: Path) -> None:
    """Pre-D5 JSON without source/source_path/collection/retry_count keys loads
    with defaults: source='user', source_path='', collection='', retry_count=0."""
    jobs_path = tmp_path / "jobs.json"
    now = _now()
    data = [
        {
            "job_id": "pre-d5-job",
            "status": "DONE",
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
            "namespace": "default",
            "progress": None,
            "job_type": "ingest",
            # intentionally missing: source, source_path, collection, retry_count
        }
    ]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)
    job = store.get("pre-d5-job")
    assert job is not None
    assert isinstance(job, IngestJob)
    assert job.source == "user"
    assert job.source_path == ""
    assert job.collection == ""
    assert job.retry_count == 0


def test_retry_count_nonzero_survives_disk_round_trip(tmp_path: Path) -> None:
    """retry_count=3 set via store.update() survives a reload from disk."""
    jobs_path = tmp_path / "jobs.json"
    s1 = JobStore(path=jobs_path)
    job = s1.create(source="maintenance", path="/file.txt", collection="col")
    s1.update(job.job_id, retry_count=3)

    s2 = JobStore(path=jobs_path)
    reloaded = s2.get(job.job_id)
    assert reloaded is not None
    assert reloaded.retry_count == 3


def test_export_job_inherited_fields_survive_disk_round_trip(tmp_path: Path) -> None:
    """ExportJob inherits source_path and retry_count from IngestJob; they survive disk round-trip."""
    jobs_path = tmp_path / "jobs.json"
    s1 = JobStore(path=jobs_path)
    job = s1.create_export(
        collection="col1",
        output_path="/out/export.tar.gz",
        tmp_path="/tmp/export.tmp",
        namespace="ns1",
        source="backup",
    )
    # Set inherited base fields via update
    s1.update(job.job_id, source_path="/data/source.txt", retry_count=1)

    s2 = JobStore(path=jobs_path)
    reloaded = s2.get(job.job_id)
    assert reloaded is not None
    assert reloaded.source_path == "/data/source.txt"
    assert reloaded.retry_count == 1
    assert reloaded.source == "backup"  # ExportJob keeps its own narrower Literal


def test_import_job_inherited_fields_survive_disk_round_trip(tmp_path: Path) -> None:
    """ImportJob inherits source_path and retry_count from IngestJob; they survive disk round-trip."""
    jobs_path = tmp_path / "jobs.json"
    s1 = JobStore(path=jobs_path)
    job = s1.create_import(
        collection="col2",
        archive_path="/archives/data.tar.gz",
        force_overwrite=False,
        ignore_schema_version=False,
        on_error="fail",
        namespace="ns1",
    )
    s1.update(job.job_id, source_path="/data/source.txt", retry_count=2)

    s2 = JobStore(path=jobs_path)
    reloaded = s2.get(job.job_id)
    assert reloaded is not None
    assert reloaded.source_path == "/data/source.txt"
    assert reloaded.retry_count == 2

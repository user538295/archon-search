"""Tests for Task 1.1 — QUEUED status, progress field, ExportJob, ImportJob in types.py."""
import pytest

from archon_search.types import (
    DeleteJob,
    ExportJob,
    ImportJob,
    IngestJob,
    JobStatus,
    ReindexJob,
)


def test_queued_status_in_enum():
    """JobStatus.QUEUED exists and round-trips through the string constructor."""
    assert JobStatus.QUEUED == JobStatus("QUEUED")
    assert JobStatus.QUEUED.value == "QUEUED"


def test_ingest_job_progress_defaults_none():
    """IngestJob constructed without progress has progress=None."""
    job = IngestJob(job_id="x", status=JobStatus.PENDING, created_at="t", updated_at="t")
    assert job.progress is None


def test_ingest_job_with_progress():
    """IngestJob can be constructed with a progress dict."""
    progress = {"processed": 5, "total": 10, "phase": "reading"}
    job = IngestJob(
        job_id="x",
        status=JobStatus.RUNNING,
        created_at="t",
        updated_at="t",
        progress=progress,
    )
    assert job.progress == progress


def test_export_job_fields():
    """ExportJob has all IngestJob fields plus collection, output_path, tmp_path."""
    job = ExportJob(
        job_id="ej1",
        status=JobStatus.QUEUED,
        created_at="t",
        updated_at="t",
    )
    # IngestJob inherited defaults
    assert job.result is None
    assert job.error is None
    assert job.progress is None
    # ExportJob-specific fields with defaults
    assert job.collection == ""
    assert job.output_path == ""
    assert job.tmp_path == ""

    # Also verify we can set them
    job2 = ExportJob(
        job_id="ej2",
        status=JobStatus.QUEUED,
        created_at="t",
        updated_at="t",
        collection="my_col",
        output_path="/data/exports/my_col.tar.gz",
        tmp_path="/data/exports/.export-ej2.jsonl.tmp",
    )
    assert job2.collection == "my_col"
    assert job2.output_path == "/data/exports/my_col.tar.gz"
    assert job2.tmp_path == "/data/exports/.export-ej2.jsonl.tmp"


def test_import_job_fields():
    """ImportJob has all IngestJob fields plus import-specific fields."""
    job = ImportJob(
        job_id="ij1",
        status=JobStatus.QUEUED,
        created_at="t",
        updated_at="t",
    )
    # IngestJob inherited defaults
    assert job.result is None
    assert job.error is None
    assert job.progress is None
    # ImportJob-specific fields with defaults
    assert job.collection == ""
    assert job.archive_path == ""
    assert job.force_overwrite is False
    assert job.ignore_schema_version is False
    assert job.on_error == "fail"

    # Verify we can set them
    job2 = ImportJob(
        job_id="ij2",
        status=JobStatus.QUEUED,
        created_at="t",
        updated_at="t",
        collection="target_col",
        archive_path="/data/exports/my_col.tar.gz",
        force_overwrite=True,
        ignore_schema_version=True,
        on_error="skip",
    )
    assert job2.collection == "target_col"
    assert job2.archive_path == "/data/exports/my_col.tar.gz"
    assert job2.force_overwrite is True
    assert job2.ignore_schema_version is True
    assert job2.on_error == "skip"


def test_existing_job_construction_unchanged():
    """IngestJob, ReindexJob, DeleteJob construct the same as before (no regression)."""
    ingest = IngestJob(
        job_id="i1",
        status=JobStatus.PENDING,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        result={"count": 3},
        error=None,
        namespace="default",
    )
    assert ingest.job_id == "i1"
    assert ingest.status == JobStatus.PENDING
    assert ingest.result == {"count": 3}
    assert ingest.namespace == "default"
    # progress defaults to None even when other fields are explicitly set
    assert ingest.progress is None

    reindex = ReindexJob(
        job_id="r1",
        status=JobStatus.RUNNING,
        created_at="t",
        updated_at="t",
        target_embedding_model="model-v2",
    )
    assert reindex.target_embedding_model == "model-v2"

    delete_job = DeleteJob(
        job_id="d1",
        status=JobStatus.DONE,
        created_at="t",
        updated_at="t",
        deleted_ids=["doc1", "doc2"],
    )
    assert delete_job.deleted_ids == ["doc1", "doc2"]

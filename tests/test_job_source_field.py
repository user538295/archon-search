"""Tests for `source` field on ExportJob/ImportJob (Task 1.2 of D2-scheduled-backup plan).

The `source` field distinguishes user-triggered jobs (default) from backup-loop-
triggered jobs. Used by the priority sort in `list_queued_bulk()` and by
`GET /jobs?source=backup`. Legacy serialized jobs missing the key must load
with `source="user"` via setdefault in `_load()`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from archon_search.jobs.store import JobStore
from archon_search.types import ExportJob, ImportJob, IngestJob


def _recent_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


# ---------------------------------------------------------------------------
# Default source on create
# ---------------------------------------------------------------------------


def test_export_job_default_source_is_user(store: JobStore) -> None:
    job = store.create_export(
        collection="col1",
        output_path="/tmp/col1.tar.gz",
        tmp_path="/tmp/.export-abc.jsonl.tmp",
    )
    assert isinstance(job, ExportJob)
    assert job.source == "user"


def test_export_job_backup_source(store: JobStore) -> None:
    job = store.create_export(
        collection="col1",
        output_path="/tmp/col1.tar.gz",
        tmp_path="/tmp/.export-abc.jsonl.tmp",
        source="backup",
    )
    assert job.source == "backup"


def test_import_job_default_source_is_user(store: JobStore) -> None:
    job = store.create_import(
        collection="col1",
        archive_path="/tmp/col1.tar.gz",
        force_overwrite=False,
        ignore_schema_version=False,
        on_error="fail",
    )
    assert isinstance(job, ImportJob)
    assert job.source == "user"


def test_import_job_backup_source(store: JobStore) -> None:
    job = store.create_import(
        collection="col1",
        archive_path="/tmp/col1.tar.gz",
        force_overwrite=False,
        ignore_schema_version=False,
        on_error="fail",
        source="backup",
    )
    assert job.source == "backup"


# ---------------------------------------------------------------------------
# Backward-compat loading
# ---------------------------------------------------------------------------


def _write_legacy_jobs(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items))


def test_load_legacy_export_job_gets_user_source(tmp_path: Path) -> None:
    """ExportJob serialized before the source field existed must load with source='user'."""
    jobs_file = tmp_path / "jobs.json"
    _write_legacy_jobs(
        jobs_file,
        [
            {
                "job_id": "legacy-export-1",
                "status": "QUEUED",
                "created_at": _recent_iso(),
                "updated_at": _recent_iso(),
                "namespace": "default",
                "collection": "col1",
                "output_path": "/tmp/col1.tar.gz",
                "tmp_path": "/tmp/.export-x.jsonl.tmp",
                "job_type": "export",
            },
        ],
    )
    store = JobStore(path=jobs_file)
    job = store.get("legacy-export-1")
    assert isinstance(job, ExportJob)
    assert job.source == "user"


def test_load_legacy_import_job_gets_user_source(tmp_path: Path) -> None:
    jobs_file = tmp_path / "jobs.json"
    _write_legacy_jobs(
        jobs_file,
        [
            {
                "job_id": "legacy-import-1",
                "status": "QUEUED",
                "created_at": _recent_iso(),
                "updated_at": _recent_iso(),
                "namespace": "default",
                "collection": "col1",
                "archive_path": "/tmp/col1.tar.gz",
                "force_overwrite": False,
                "ignore_schema_version": False,
                "on_error": "fail",
                "job_type": "import",
            },
        ],
    )
    store = JobStore(path=jobs_file)
    job = store.get("legacy-import-1")
    assert isinstance(job, ImportJob)
    assert job.source == "user"


def test_load_does_not_add_source_to_ingest_job(tmp_path: Path) -> None:
    """IngestJob has no source field — the setdefault must not be applied to ingest type,
    otherwise IngestJob(**item) would raise TypeError on the unexpected kwarg."""
    jobs_file = tmp_path / "jobs.json"
    _write_legacy_jobs(
        jobs_file,
        [
            {
                "job_id": "ingest-1",
                "status": "DONE",
                "created_at": _recent_iso(),
                "updated_at": _recent_iso(),
                "namespace": "default",
                "job_type": "ingest",
            },
        ],
    )
    store = JobStore(path=jobs_file)
    job = store.get("ingest-1")
    assert isinstance(job, IngestJob)
    # IngestJob has no `source` attribute
    assert not hasattr(job, "source")

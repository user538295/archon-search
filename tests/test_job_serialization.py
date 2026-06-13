"""Tests for `job_to_dict()` subclass field serialization + lancedb_version in
manifest (Task 1.4 of D2-scheduled-backup plan).

`job_to_dict()` must include four new nullable fields read via `getattr`:
  - source       (ExportJob / ImportJob; None for IngestJob)
  - collection   (ExportJob / ImportJob; None for IngestJob)
  - output_path  (ExportJob only)
  - archive_path (ImportJob only)

Export manifest must include `lancedb_version` (string from
`importlib.metadata.version("lancedb")`, or None on PackageNotFoundError).
"""
from __future__ import annotations

import importlib.metadata
import logging
from datetime import datetime, timezone
from unittest.mock import patch

from archon_search.jobs.model import job_to_dict
from archon_search.types import ExportJob, ImportJob, IngestJob, JobStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_export(**kw: object) -> ExportJob:
    defaults: dict = dict(
        job_id="j-export",
        status=JobStatus.QUEUED,
        created_at=_now(),
        updated_at=_now(),
        collection="col1",
        output_path="",
        tmp_path="/tmp/.export-abc.jsonl.tmp",
    )
    defaults.update(kw)
    return ExportJob(**defaults)  # type: ignore[arg-type]


def _make_import(**kw: object) -> ImportJob:
    defaults: dict = dict(
        job_id="j-import",
        status=JobStatus.QUEUED,
        created_at=_now(),
        updated_at=_now(),
        collection="col1",
        archive_path="/tmp/archive.tar.gz",
    )
    defaults.update(kw)
    return ImportJob(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# job_to_dict — ExportJob
# ---------------------------------------------------------------------------


def test_export_job_dict_includes_collection() -> None:
    d = job_to_dict(_make_export(collection="docs"))
    assert d["collection"] == "docs"


def test_export_job_dict_includes_output_path() -> None:
    d = job_to_dict(_make_export(output_path=""))
    assert d["output_path"] == ""


def test_export_job_dict_includes_source() -> None:
    d = job_to_dict(_make_export())
    assert d["source"] == "user"


def test_export_job_dict_source_backup() -> None:
    d = job_to_dict(_make_export(source="backup"))
    assert d["source"] == "backup"


# ---------------------------------------------------------------------------
# job_to_dict — ImportJob
# ---------------------------------------------------------------------------


def test_import_job_dict_includes_archive_path() -> None:
    d = job_to_dict(_make_import(archive_path="/tmp/foo.tar.gz"))
    assert d["archive_path"] == "/tmp/foo.tar.gz"


def test_import_job_dict_includes_collection_and_source() -> None:
    d = job_to_dict(_make_import(collection="docs", source="backup"))
    assert d["collection"] == "docs"
    assert d["source"] == "backup"


# ---------------------------------------------------------------------------
# job_to_dict — IngestJob (subclass fields must be None)
# ---------------------------------------------------------------------------


def test_ingest_job_dict_source_is_null() -> None:
    job = IngestJob(
        job_id="j-ingest",
        status=JobStatus.RUNNING,
        created_at=_now(),
        updated_at=_now(),
    )
    d = job_to_dict(job)
    assert d["source"] is None
    assert d["collection"] is None
    assert d["output_path"] is None
    assert d["archive_path"] is None


# ---------------------------------------------------------------------------
# Manifest — lancedb_version
# ---------------------------------------------------------------------------


def test_lancedb_version_in_manifest() -> None:
    """The exported manifest dict built in routes_export._export_task must
    include a `lancedb_version` key (string when the package is installed)."""
    # The version resolution lives in archon_search.jobs.export_archive as a
    # helper. Call it directly — the routes_export task will use the same
    # helper.
    from archon_search.jobs.export_archive import get_lancedb_version

    version = get_lancedb_version()
    # lancedb is a hard dependency, so this must be a non-empty string locally.
    assert isinstance(version, str)
    assert version


def test_lancedb_version_null_on_package_not_found(
    caplog: "logging.LogCaptureFixture",
) -> None:
    from archon_search.jobs import export_archive

    def _raise(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    with patch.object(importlib.metadata, "version", _raise):
        with caplog.at_level(logging.WARNING, logger=export_archive.__name__):
            version = export_archive.get_lancedb_version()

    assert version is None
    assert any("lancedb version" in rec.message.lower() for rec in caplog.records)

"""Job domain model — re-exports from types plus the default store path.

Path resolution (C9 Task 2.4): the jobs JSON file location is resolved lazily
via ``get_jobs_file()`` on every call so ``ARCHON_SEARCH_DATA_DIR`` (the
container-friendly base data dir) redirects the jobs file. No module-level
capture of the env var: a stale binding would break tests that flip the env
after import and the container bootstrap where the env is set after the
package is loaded.
"""
from __future__ import annotations

from pathlib import Path

from archon_search.paths import get_data_dir
from archon_search.types import (
    CommunityRebuildJob,
    DeleteJob,
    ExportJob,
    ImportJob,
    IngestJob,
    JobKind,
    JobStatus,
    MetadataReindexJob,
    MigrationJob,
    ReindexJob,
    SyncJob,
)


def get_jobs_file() -> Path:
    """Return the jobs JSON file path, resolved fresh on every call.

    Always derived from ``get_data_dir()``; there is no per-path env var
    override (deliberately scoped to ``ARCHON_SEARCH_DATA_DIR`` only — see
    the Phase 2 env-var-scope note in the C9 plan).
    """
    return get_data_dir() / "archon-search-jobs.json"


def _job_type(job: IngestJob) -> str:
    """Return the canonical job_type string for any IngestJob subclass."""
    if isinstance(job, MigrationJob):
        return "migration"
    if isinstance(job, ExportJob):
        return "export"
    if isinstance(job, ImportJob):
        return "import"
    if isinstance(job, ReindexJob):
        return "reindex"
    if isinstance(job, DeleteJob):
        return "delete"
    if isinstance(job, CommunityRebuildJob):
        return "community_rebuild"
    if isinstance(job, SyncJob):
        return JobKind.sync.value
    if isinstance(job, MetadataReindexJob):
        return JobKind.metadata_reindex.value
    return "ingest"


def job_to_dict(job: IngestJob) -> dict:
    """Serialize an IngestJob to a plain dict for JSON responses.

    Base ``IngestJob`` fields (``source``, ``source_path``, ``collection``,
    ``retry_count``) are accessed directly since D5-BE-7 moved them to the
    base class. Subclass-specific fields that remain on subclasses only are
    surfaced via :func:`getattr`:

    - D2-1.4: ``output_path``, ``archive_path`` (``ExportJob`` / ``ImportJob``)
    - D3: ``kind``, ``migrations_applied``, ``backup_confirmed`` (``MigrationJob``)
    """
    k = getattr(job, "kind", None)
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "result": job.result,
        "error": job.error,
        "namespace": job.namespace,
        "progress": job.progress,
        "source": job.source,
        "source_path": job.source_path,
        "collection": job.collection,
        "retry_count": job.retry_count,
        "output_path": getattr(job, "output_path", None),
        "archive_path": getattr(job, "archive_path", None),
        "kind": k.value if k is not None else None,
        "migrations_applied": getattr(job, "migrations_applied", None),
        "backup_confirmed": getattr(job, "backup_confirmed", None),
        "job_type": _job_type(job),
    }


__all__ = ["IngestJob", "JobStatus", "get_jobs_file", "job_to_dict"]

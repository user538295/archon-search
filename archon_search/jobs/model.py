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
from archon_search.types import IngestJob, JobStatus


def get_jobs_file() -> Path:
    """Return the jobs JSON file path, resolved fresh on every call.

    Always derived from ``get_data_dir()``; there is no per-path env var
    override (deliberately scoped to ``ARCHON_SEARCH_DATA_DIR`` only — see
    the Phase 2 env-var-scope note in the C9 plan).
    """
    return get_data_dir() / "archon-search-jobs.json"


def job_to_dict(job: IngestJob) -> dict:
    """Serialize an IngestJob to a plain dict for JSON responses.

    Subclass-specific fields are surfaced via :func:`getattr` so that base
    ``IngestJob`` instances serialize them as ``None`` while the relevant
    subclass instances carry real values:

    - D2-1.4: ``source``, ``collection``, ``output_path``, ``archive_path``
      (``ExportJob`` / ``ImportJob``)
    - D3: ``kind``, ``migrations_applied``, ``backup_confirmed``
      (``MigrationJob``; ``kind`` is serialized as the enum ``.value`` string)
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
        "source": getattr(job, "source", None),
        "collection": getattr(job, "collection", None),
        "output_path": getattr(job, "output_path", None),
        "archive_path": getattr(job, "archive_path", None),
        "kind": k.value if k is not None else None,
        "migrations_applied": getattr(job, "migrations_applied", None),
        "backup_confirmed": getattr(job, "backup_confirmed", None),
    }


__all__ = ["IngestJob", "JobStatus", "get_jobs_file", "job_to_dict"]

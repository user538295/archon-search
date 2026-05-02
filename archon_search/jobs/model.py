"""Job domain model — re-exports from types plus the default store path."""
from __future__ import annotations

from pathlib import Path

from archon_search.types import IngestJob, JobStatus

JOBS_FILE: Path = Path.home() / ".archon" / "archon-search-jobs.json"


def job_to_dict(job: IngestJob) -> dict:
    """Serialize an IngestJob to a plain dict for JSON responses."""
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "result": job.result,
        "error": job.error,
    }


__all__ = ["IngestJob", "JobStatus", "JOBS_FILE", "job_to_dict"]

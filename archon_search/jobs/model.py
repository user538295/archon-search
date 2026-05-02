"""Job domain model — re-exports from types plus the default store path."""
from __future__ import annotations

from pathlib import Path

from archon_search.types import IngestJob, JobStatus

JOBS_FILE: Path = Path.home() / ".archon" / "archon-search-jobs.json"

__all__ = ["IngestJob", "JobStatus", "JOBS_FILE"]

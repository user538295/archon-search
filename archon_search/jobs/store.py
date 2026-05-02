"""JobStore — persistent, crash-safe job lifecycle store."""
from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from archon_search.jobs.model import JOBS_FILE, IngestJob, JobStatus

logger = logging.getLogger("archon")

_CRASH_STATUSES = {JobStatus.RUNNING, JobStatus.CANCELLING}
_EVICTION_DAYS = 7


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """Persistent JSON-backed store for ingest jobs."""

    def __init__(self, path: Path = JOBS_FILE) -> None:
        self._path = path
        self._jobs: dict[str, IngestJob] = {}
        self._load()
        self._evict_old()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, **kwargs: object) -> IngestJob:
        now = _now_iso()
        job = dataclasses.replace(
            IngestJob(
                job_id=str(uuid.uuid4()),
                status=JobStatus.PENDING,
                created_at=now,
                updated_at=now,
            ),
            **kwargs,  # type: ignore[arg-type]
        )
        self._jobs[job.job_id] = job
        self._write_atomic()
        return job

    def update(self, job_id: str, **kwargs: object) -> IngestJob:
        if job_id not in self._jobs:
            raise KeyError(job_id)
        job = self._jobs[job_id]
        updated = dataclasses.replace(job, updated_at=_now_iso(), **kwargs)  # type: ignore[arg-type]
        self._jobs[job_id] = updated
        self._write_atomic()
        return updated

    def get(self, job_id: str) -> IngestJob | None:
        return self._jobs.get(job_id)

    def list(self) -> list[IngestJob]:
        return list(self._jobs.values())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
            for item in raw:
                item["status"] = JobStatus(item["status"])
                job = IngestJob(**item)
                if job.status in _CRASH_STATUSES:
                    job = dataclasses.replace(
                        job, status=JobStatus.FAILED, error="process_restart"
                    )
                self._jobs[job.job_id] = job
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.error("JobStore: corrupt jobs file %s — resetting (%s)", self._path, exc)
            self._jobs = {}

    def _write_atomic(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        data = [dataclasses.asdict(job) for job in self._jobs.values()]
        # Convert JobStatus enum values to strings for JSON serialisation
        for item in data:
            item["status"] = item["status"].value if hasattr(item["status"], "value") else item["status"]
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(self._path)
        self._evict_old()

    def _evict_old(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_EVICTION_DAYS)
        to_remove = [
            job_id
            for job_id, job in self._jobs.items()
            if datetime.fromisoformat(job.updated_at) < cutoff
        ]
        for job_id in to_remove:
            del self._jobs[job_id]

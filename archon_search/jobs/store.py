"""JobStore — persistent, crash-safe job lifecycle store."""
from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from archon_search._durable_io import atomic_write_json
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.model import IngestJob, JobStatus, get_jobs_file
from archon_search.types import DeleteJob, ExportJob, ImportJob, MigrationJob, MigrationKind, ReindexJob

logger = logging.getLogger(__name__)

_CRASH_STATUSES = {JobStatus.RUNNING, JobStatus.CANCELLING}
# QUEUED is intentionally excluded from crash statuses: QUEUED bulk jobs survive
# a server restart and will be re-dispatched by the scheduler on next tick.
_EVICTION_DAYS = 7
# Only terminal jobs are eligible for eviction; non-terminal jobs (PENDING, QUEUED,
# RUNNING, CANCELLING) are retained regardless of age.
_TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.FAILED, JobStatus.FAILED_EXPIRED, JobStatus.CANCELLED}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    """Persistent JSON-backed store for ingest jobs."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else get_jobs_file()
        self._jobs: dict[str, IngestJob] = {}
        changed = self._load()
        if changed:
            self._write_atomic()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        namespace: str = DEFAULT_NAMESPACE,
        source: Literal["user", "backup", "maintenance"] = "user",
        path: str = "",
        collection: str = "",
    ) -> IngestJob:
        now = _now_iso()
        job = IngestJob(
            job_id=str(uuid.uuid4()),
            status=JobStatus.PENDING,
            created_at=now,
            updated_at=now,
            namespace=namespace,
            source=source,
            source_path=path,
            collection=collection,
        )
        self._jobs[job.job_id] = job
        self._write_atomic()
        return job

    def create_reindex(self, namespace: str = DEFAULT_NAMESPACE, target_embedding_model: str | None = None) -> ReindexJob:
        now = _now_iso()
        job = ReindexJob(
            job_id=str(uuid.uuid4()),
            status=JobStatus.PENDING,
            created_at=now,
            updated_at=now,
            namespace=namespace,
            target_embedding_model=target_embedding_model,
        )
        self._jobs[job.job_id] = job
        self._write_atomic()
        return job

    def create_job(self, job: IngestJob) -> IngestJob:
        """Store a pre-constructed job (subclass-aware, used for ReindexJob/DeleteJob)."""
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

    def transition(
        self,
        job_id: str,
        from_statuses: set[JobStatus],
        to_status: JobStatus,
    ) -> IngestJob | None:
        """Atomically update status only if current status is in from_statuses.

        Returns the updated job, or None if the transition was rejected
        (job not found or status not in from_statuses).
        """
        job = self._jobs.get(job_id)
        if job is None or job.status not in from_statuses:
            return None
        return self.update(job_id, status=to_status)

    def list(self) -> list[IngestJob]:
        return list(self._jobs.values())

    def create_export(
        self,
        collection: str,
        output_path: str,
        tmp_path: str,
        namespace: str = DEFAULT_NAMESPACE,
        source: Literal["user", "backup"] = "user",
    ) -> ExportJob:
        """Create an ExportJob with QUEUED status and persist it."""
        now = _now_iso()
        job = ExportJob(
            job_id=str(uuid.uuid4()),
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
            namespace=namespace,
            collection=collection,
            output_path=output_path,
            tmp_path=tmp_path,
            source=source,
        )
        return self.create_job(job)  # type: ignore[return-value]

    def create_import(
        self,
        collection: str,
        archive_path: str,
        force_overwrite: bool,
        ignore_schema_version: bool,
        on_error: str,
        namespace: str = DEFAULT_NAMESPACE,
        source: Literal["user", "backup"] = "user",
    ) -> ImportJob:
        """Create an ImportJob with QUEUED status and persist it."""
        now = _now_iso()
        job = ImportJob(
            job_id=str(uuid.uuid4()),
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
            namespace=namespace,
            collection=collection,
            archive_path=archive_path,
            force_overwrite=force_overwrite,
            ignore_schema_version=ignore_schema_version,
            on_error=on_error,
            source=source,
        )
        return self.create_job(job)  # type: ignore[return-value]

    def create_migration(
        self,
        collection: str,
        kind: MigrationKind,
        backup_confirmed: bool | None,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> MigrationJob:
        """Create a MigrationJob with QUEUED status and persist it."""
        now = _now_iso()
        job = MigrationJob(
            job_id=str(uuid.uuid4()),
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
            namespace=namespace,
            collection=collection,
            kind=kind,
            backup_confirmed=backup_confirmed,
        )
        return self.create_job(job)  # type: ignore[return-value]

    def update_progress(self, job_id: str, processed: int, total: int, phase: str) -> None:
        """Set the progress dict on a job."""
        self.update(job_id, progress={"processed": processed, "total": total, "phase": phase})

    def list_queued_bulk(self) -> list[ExportJob | ImportJob | MigrationJob]:
        """Return QUEUED ExportJob/ImportJob/MigrationJob instances sorted by (source_priority, created_at).

        User-sourced jobs (``source="user"``) sort before backup-sourced jobs
        (``source="backup"``). Within each tier, FIFO is preserved by
        ``created_at`` ascending.
        """
        bulk = [
            job
            for job in self._jobs.values()
            if isinstance(job, (ExportJob, ImportJob, MigrationJob)) and job.status == JobStatus.QUEUED
        ]
        bulk.sort(key=lambda j: (0 if j.source == "user" else 1, j.created_at))
        return bulk  # type: ignore[return-value]

    def count_by_status(self) -> dict[JobStatus, int]:
        """Return a count of jobs for every JobStatus member, zero-filled.

        Safe to call without a lock — this is a synchronous method with no
        ``await`` points; asyncio's single-thread scheduling guarantee prevents
        concurrent coroutine mutation during iteration.

        Note: counts all ``JobStatus`` members including ``CANCELLING``; callers
        that expose ``/status`` should surface only ``PENDING`` and ``RUNNING``.
        ``CANCELLING`` jobs are excluded from the public ``running`` count —
        a cancelling job is in the process of stopping and does not represent
        available capacity.
        """
        counts: Counter[JobStatus] = Counter(j.status for j in self._jobs.values())
        return {s: counts.get(s, 0) for s in JobStatus}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> bool:
        """Load jobs from disk. Returns True if store was modified (crash recovery or eviction)."""
        if not self._path.exists():
            return False
        try:
            raw = json.loads(self._path.read_text())
            modified = False
            for item in raw:
                item["status"] = JobStatus(item["status"])
                # Backward compatibility: pre-D1 jobs lack a "progress" key.
                item.setdefault("progress", None)
                # Backward compatibility: pre-D5 jobs lack new IngestJob base fields.
                item.setdefault("source", "user")
                item.setdefault("source_path", "")
                item.setdefault("collection", "")
                item.setdefault("retry_count", 0)
                job_type = item.pop("job_type", "ingest")
                if job_type == "export":
                    job: IngestJob = ExportJob(**item)
                elif job_type == "import":
                    job = ImportJob(**item)
                elif job_type == "reindex":
                    job = ReindexJob(**item)
                elif job_type == "delete":
                    job = DeleteJob(**item)
                elif job_type == "migration":
                    item["kind"] = MigrationKind(item["kind"])
                    job = MigrationJob(**item)
                else:
                    job = IngestJob(**item)
                if job.status in _CRASH_STATUSES:
                    job = dataclasses.replace(
                        job, status=JobStatus.FAILED, error="process_restart"
                    )
                    modified = True
                self._jobs[job.job_id] = job
            count_before = len(self._jobs)
            self._evict_old()
            if len(self._jobs) < count_before:
                modified = True
            return modified
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.error("JobStore: corrupt jobs file %s — resetting (%s)", self._path, exc)
            self._jobs = {}
            return False

    def _write_atomic(self) -> None:
        self._evict_old()  # evict BEFORE serializing so stale jobs are never written
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for job in self._jobs.values():
            item = dataclasses.asdict(job)
            item["status"] = item["status"].value if hasattr(item["status"], "value") else item["status"]
            if isinstance(job, MigrationJob):
                item["job_type"] = "migration"
            elif isinstance(job, ExportJob):
                item["job_type"] = "export"
            elif isinstance(job, ImportJob):
                item["job_type"] = "import"
            elif isinstance(job, ReindexJob):
                item["job_type"] = "reindex"
            elif isinstance(job, DeleteJob):
                item["job_type"] = "delete"
            else:
                item["job_type"] = "ingest"
            data.append(item)
        atomic_write_json(self._path, data)

    def _evict_old(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_EVICTION_DAYS)
        to_remove = [
            job_id
            for job_id, job in self._jobs.items()
            if job.status in _TERMINAL_STATUSES
            and datetime.fromisoformat(job.updated_at) < cutoff
        ]
        for job_id in to_remove:
            del self._jobs[job_id]

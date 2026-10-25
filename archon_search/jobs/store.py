"""JobStore — persistent, crash-safe job lifecycle store."""
from __future__ import annotations

import dataclasses
import json
import logging
import threading
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from archon_search._durable_io import atomic_write_json, ensure_private_dir
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.model import IngestJob, JobStatus, get_jobs_file
from archon_search.types import (
    CommunityRebuildJob,
    DeleteJob,
    ExportJob,
    ImportJob,
    IngestOptions,
    JobKind,
    MetadataReindexJob,
    MigrationJob,
    MigrationKind,
    ReindexJob,
    SyncJob,
    is_base_ingest,
)

logger = logging.getLogger(__name__)

_CRASH_STATUSES = {JobStatus.RUNNING, JobStatus.CANCELLING}
# QUEUED is intentionally excluded from crash statuses: QUEUED bulk jobs survive
# a server restart and will be re-dispatched by the scheduler on next tick.
_INGEST_JOB_TYPE = "ingest"
_REINDEX_JOB_TYPE = "reindex"
# Job types whose crash marker means the corpus-scan-and-ingest workload the
# startup sync itself performs is what killed the process: a plain ingest (the
# original incident's shape — a client POST /ingest that OOM'd) or a sync (the
# startup sync's own prior invocation, or a manual POST /sync, crashing). This
# is narrower than "every job type that can ever cause a re-ingest" —
# export/import/reindex/delete/migration/community_rebuild jobs run different
# code paths, so a crash there does not by itself mean the ingest workload is
# dangerous. It does NOT mean a startup sync can never touch a collection
# originally populated by one of those job types: sync.py force-reindexes a
# collection whose indexed_chunk_size is unknown (populated outside
# SearchCollectionSync, e.g. via POST /ingest or POST /import) when
# auto_reindex_on_chunk_size_change is set. But if THAT reindex crashes, it
# crashes as a SyncJob (kind="sync"), which IS in this set — so the guard
# still arms on the next boot regardless of how the collection was
# originally populated.
_INGEST_FAMILY_JOB_TYPES = {_INGEST_JOB_TYPE, JobKind.sync.value}
#: Wire-facing ``error`` (served by GET /jobs) of a job loaded ``RUNNING`` / ``CANCELLING``: the previous process died
#: while it ran — except a ``CANCELLING`` job whose cancel landed while it was still ``PENDING``
#: (``IngestJob.cancelled_before_start``), which never ran and loads ``CANCELLED``. For an ingest-family job it arms
#: the crash-loop guard, and ``MaintenanceLoop`` never retries such an ingest — re-running it could re-enter the
#: workload that killed the process.
PROCESS_RESTART_ERROR = "process_restart"
#: Wire-facing ``error`` (served by GET /jobs) of a job loaded still ``PENDING`` (a base ingest or reindex job) or
#: ``QUEUED`` (a community-rebuild, metadata-reindex or sync job): it never started, and nothing would ever run it —
#: its task died with the previous process, or an older server's route died before claiming it (a PENDING reindex
#: would also block DELETE /collections/{name} forever). Nothing ran, so unlike ``process_restart`` it does not arm the
#: crash-loop guard; as a ``FAILED`` path ingest, ``MaintenanceLoop`` may retry an ingest job (never any other kind).
NOT_STARTED_BEFORE_RESTART_DETAIL = "never started; the server restarted before it ran"
#: ``job_type`` values whose job loaded ``PENDING`` is rewritten FAILED with ``NOT_STARTED_BEFORE_RESTART_DETAIL``.
_NOT_STARTED_ON_LOAD_JOB_TYPES = frozenset({_INGEST_JOB_TYPE, _REINDEX_JOB_TYPE})
#: ``job_type`` values whose job loaded ``QUEUED`` is rewritten FAILED with ``NOT_STARTED_BEFORE_RESTART_DETAIL``. An
#: older server created these kinds QUEUED and their route claimed them RUNNING itself; ``JobScheduler`` promotes
#: export, import and migration jobs only (``list_queued_bulk``), so a row left QUEUED by a crash before the claim
#: never runs, and — non-terminal — is never cancelled by DELETE /jobs/{id} nor evicted.
_NEVER_SCHEDULED_ON_LOAD_JOB_TYPES = frozenset({"community_rebuild", JobKind.metadata_reindex.value, JobKind.sync.value})
#: Statuses of a base ingest job a ``MaintenanceLoop`` retry may still act on: FAILED (a retry candidate) and
#: PENDING (a retry not yet claimed RUNNING). ``cancel_ingest_retries`` ends them.
_RETRY_CANDIDATE_STATUSES = frozenset({JobStatus.FAILED, JobStatus.PENDING})
_EVICTION_DAYS = 7
# Only terminal jobs are eligible for eviction; non-terminal jobs (PENDING, QUEUED,
# RUNNING, CANCELLING) are retained regardless of age.
_TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.FAILED, JobStatus.FAILED_EXPIRED, JobStatus.CANCELLED}

#: Statuses in which a job's task may still write its collection's table or meta row. RUNNING: the task has
#: started. CANCELLING: DELETE /jobs/{id} sets it but does NOT cancel the task, which keeps writing until it ends
#: (or, for an import, until its next batch boundary).
_RUNNING_OR_CANCELLING = frozenset({JobStatus.RUNNING, JobStatus.CANCELLING})
#: Job kind (exact type) -> the statuses in which DELETE /collections/{name} refuses to drop the job's collection,
#: because the job may still write the table or meta row without holding the collection lock. Each of these tasks
#: writes outside ``lock_for(name)`` at some point of its run (a metadata reindex holds the lock for its whole store
#: call, but clears its meta pointer after releasing it), and a meta-row write from a read made before a drop
#: (``update_collection_meta`` locks, but upserts by ``merge_insert``) re-inserts the dropped collection's row:
#: - ``IngestJob`` (base ingest only): its dispatch path releases the lock right after marking the job RUNNING and
#:   keeps writing. PENDING is excluded — the lock covers it (see ``JobStore.find_active_collection_job``).
#: - ``ReindexJob``: ``_reindex_task`` writes through ``pipeline.ingest_directory`` (lock per ``ingest_chunks``
#:   call; ``ensure_collection`` re-creates a dropped table and meta row). PENDING too: the task holds no lock
#:   while it loads the embedder before its PENDING -> RUNNING claim. POST /collections/{name}/reindex creates it
#:   PENDING under the lock; one left PENDING by a restart is failed at load (``JobStore._load``).
#: - ``ImportJob``: its task locks per write (``ensure_collection``, ``ingest_chunks``). QUEUED is excluded: no
#:   task runs yet, and an import creates its target collection by design.
#: - ``MigrationJob``: ``apply_rewrite_migration`` locks only during the rewrite, then re-reads and rewrites the meta
#:   row (``schema_version``). QUEUED is excluded, and POST /collections/{name}/migrate needs no lock to create its
#:   RUNNING job: the rewrite opens the table under the lock first, so a dropped collection fails it cleanly before
#:   any meta write.
#: - ``MetadataReindexJob``: created RUNNING (under the lock, by its route) before ``reindex_metadata`` takes the
#:   lock (and holds it for its whole run); once that call returns, the task clears its meta pointer from a read made
#:   outside the lock. A ``dry_run`` one is exempt in every status (``may_write_collection``): it only reads the
#:   table, records no meta pointer, and so has none to clear.
#: - ``CommunityRebuildJob``: created RUNNING (under the lock, by its route); writes the collection's graph tables,
#:   then clears its meta pointer from a read made outside the lock.
COLLECTION_WRITING_JOB_STATUSES: dict[type[IngestJob], frozenset[JobStatus]] = {
    IngestJob: _RUNNING_OR_CANCELLING,
    ReindexJob: _RUNNING_OR_CANCELLING | {JobStatus.PENDING},
    ImportJob: _RUNNING_OR_CANCELLING,
    MigrationJob: _RUNNING_OR_CANCELLING,
    MetadataReindexJob: _RUNNING_OR_CANCELLING,
    CommunityRebuildJob: _RUNNING_OR_CANCELLING,
}
#: Job kinds that never block DELETE /collections/{name}; every kind is in exactly one of this set and
#: ``COLLECTION_WRITING_JOB_STATUSES`` (an unclassified kind fails closed — see ``may_write_collection``):
#: - ``ExportJob``: reads the collection only.
#: - ``DeleteJob``: legacy; nothing creates or runs one any more.
#: - ``SyncJob``: names no collection (``collection`` is always ""), though a RUNNING sync writes (and may drop)
#:   collections: it registers in ``SearchStore.collection_writer`` around each collection's write instead
#:   (``SearchCollectionSync._write_collection``), and consults ``find_active_collection_job`` before it drops one.
NEVER_BLOCKING_JOB_KINDS: frozenset[type[IngestJob]] = frozenset({ExportJob, DeleteJob, SyncJob})


def may_write_collection(job: IngestJob) -> bool:
    """Whether *job*'s kind and status mean it may still write its collection (``COLLECTION_WRITING_JOB_STATUSES``).

    A dry-run ``MetadataReindexJob`` never does (a read-only preview), whatever its status. The kind is matched by
    exact type (as ``is_base_ingest`` does). A kind in neither that table nor
    ``NEVER_BLOCKING_JOB_KINDS`` fails closed: it may write while not terminal, and each such check is logged at
    ERROR so the kind gets classified — DELETE refuses rather than drop a collection it may write.
    """
    kind = type(job)
    if kind in NEVER_BLOCKING_JOB_KINDS or (isinstance(job, MetadataReindexJob) and job.dry_run):
        return False
    statuses = COLLECTION_WRITING_JOB_STATUSES.get(kind)
    if statuses is not None:
        return job.status in statuses
    may_write = job.status not in _TERMINAL_STATUSES
    if may_write:
        logger.error(
            "JobStore: job %s has the unclassified kind %s; treating it as writing collection %r until it is terminal",
            job.job_id, kind.__name__, job.collection,
        )
    return may_write


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ingest_options_from_json(job_id: object, raw: object) -> IngestOptions | None:
    """Job *job_id*'s ``ingest_options`` as loaded from the jobs file (JSON has no tuple); ``None`` when the row has
    none — or an unusable one (not an object, an unknown or missing key, a value of the wrong JSON type: e.g. a
    ``chunk_scopes`` string, which would otherwise split into one-letter scopes), which is logged at WARNING: that job
    alone loses its options, so ``MaintenanceLoop`` never retries it, and the rest of the jobs file loads unharmed.
    Value ranges (TTL bounds, scope lengths) are ``IngestRequest``'s to check when a retry is dispatched.
    """
    if raw is None:
        return None
    try:
        if not isinstance(raw, dict):
            raise TypeError(f"not an object but {type(raw).__name__}")
        scopes = raw.get("chunk_scopes")
        if scopes is not None and not (isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes)):
            raise TypeError("chunk_scopes is not a list of strings")
        options = IngestOptions(**{**raw, "chunk_scopes": tuple(scopes) if scopes is not None else None})
        if not isinstance(options.submitted_path, str) or not isinstance(options.ingested_by, str):
            raise TypeError("submitted_path or ingested_by is not a string")
        ttl = options.chunk_ttl_seconds
        if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int)):
            raise TypeError("chunk_ttl_seconds is not an integer")
        return options
    except TypeError as exc:
        logger.warning("JobStore: job %s has unusable ingest_options (%s); loading it without them", job_id, exc)
        return None


def _flag_from_json(job_id: object, key: str, raw: object) -> bool:
    """Job *job_id*'s boolean marker *key* (``cancel_requested``, ``cancelled_before_start``) as loaded from the jobs
    file; a value that is not a JSON boolean sets nothing — logged at WARNING and loaded False, so a truthy
    ``"false"`` can neither end a retry chain (``cancel_requested``) nor excuse a job that ran from the crash-loop
    guard (``cancelled_before_start``)."""
    if isinstance(raw, bool):
        return raw
    logger.warning("JobStore: job %s has a non-boolean %s (%r); loading it as False", job_id, key, raw)
    return False


class JobStore:
    """Persistent JSON-backed store for ingest jobs."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else get_jobs_file()
        self._lock = threading.Lock()
        self._jobs: dict[str, IngestJob] = {}
        self._crashed_ingest_on_load = False
        changed = self._load()
        if changed:
            self._write_atomic()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def crashed_ingest_on_load(self) -> bool:
        """True when loading this store rewrote an ingest-family job to ``process_restart``.

        The previous process died mid-ingest (OOM, ``kill -9``, machine reset).
        Narrower than the ``modified`` flag ``_load()`` returns, which also
        covers age-eviction. The lifespan's crash-loop guard reads this to
        decide whether re-running the startup sync would re-enter the workload
        that killed the process.
        """
        return self._crashed_ingest_on_load

    def create(
        self,
        namespace: str = DEFAULT_NAMESPACE,
        source: Literal["user", "backup", "maintenance"] = "user",
        path: str = "",
        collection: str = "",
        *,
        ingest_options: IngestOptions | None = None,
        retry_count: int = 0,
    ) -> IngestJob:
        """Create a PENDING base ``IngestJob``. *ingest_options*: the POST /ingest request, kept so a
        ``MaintenanceLoop`` retry re-ingests with it; *retry_count*: which retry attempt the job is (0: none)."""
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
            ingest_options=ingest_options,
            retry_count=retry_count,
        )
        return self.create_job(job)

    def create_reindex(
        self, collection: str, namespace: str = DEFAULT_NAMESPACE, target_embedding_model: str | None = None
    ) -> ReindexJob:
        """Create a PENDING ReindexJob of *collection*; DELETE /collections/{name} refuses while it is active."""
        now = _now_iso()
        job = ReindexJob(
            job_id=str(uuid.uuid4()),
            status=JobStatus.PENDING,
            created_at=now,
            updated_at=now,
            namespace=namespace,
            collection=collection,
            target_embedding_model=target_embedding_model,
        )
        return self.create_job(job)

    def create_job[J: IngestJob](self, job: J) -> J:
        """Store and persist a pre-constructed job (subclass-aware); every ``create*`` method funnels here.

        A failed persist removes the job from memory again and re-raises, so memory never keeps a job whose
        persist raised. ``atomic_write_json`` can raise after its rename (the directory fsync), leaving the
        job in the file, so a best-effort corrective rewrite of memory follows (``_rewrite_after_failed_persist``
        has the limits): if that rewrite fails too, the job reappears on the next load.
        """
        self._jobs[job.job_id] = job
        try:
            self._write_atomic()
        except BaseException:
            self._jobs.pop(job.job_id, None)
            self._rewrite_after_failed_persist()
            raise
        return job

    def update(self, job_id: str, **kwargs: object) -> IngestJob:
        """Apply *kwargs* to the job and persist it.

        A failed persist re-raises but keeps the update in memory, so readers in this process see it at
        once. Every write serializes all in-memory jobs, so the next successful write of any job persists
        it too; if the process restarts before such a write, the file holds the status last saved.
        """
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
        **fields: object,
    ) -> IngestJob | None:
        """Atomically update status only if current status is in from_statuses, with any other job *fields* in the
        same write (e.g. ``cancel_requested``).

        Returns the updated job, or None if the transition was rejected
        (job not found or status not in from_statuses).

        A failed persist restores the job in memory to its old status and re-raises: the transition did
        not happen. ``atomic_write_json`` can raise after its rename (the directory fsync), leaving the new
        status in the file, so a best-effort corrective rewrite of memory follows (``_rewrite_after_failed_persist``
        has the limits). This deliberately differs from ``update()``: an update
        records the outcome of work already done (e.g. a terminal status), and rolling it back would pin
        the job to a stale state such as RUNNING. A transition is a compare-and-set claim — when its write
        fails the claim was never made and nothing has started, so the old status is the truthful state.
        The rollback also restores the job's old ``updated_at``, so the corrective rewrite's age eviction
        can drop a terminal job whose old ``updated_at`` is more than 7 days old.
        """
        job = self._jobs.get(job_id)
        if job is None or job.status not in from_statuses:
            return None
        try:
            return self.update(job_id, status=to_status, **fields)
        except BaseException:
            self._jobs[job_id] = job
            self._rewrite_after_failed_persist()
            raise

    def _rewrite_after_failed_persist(self) -> None:
        """Best-effort: rewrite the file from memory after a rolled-back persist.

        When the rewrite succeeds, the file's content matches memory; durability across a crash is not
        guaranteed: after a failed directory fsync, a later fsync that succeeds does not prove the earlier
        rename reached the disk (``_durable_io.atomic_write_json``: do not retry fsync), so a crash can still
        leave either version. When it fails too, the file keeps what the failed persist left until the next
        successful write of any job. Under a persistent write failure every rolled-back persist (e.g. each
        failed scheduler claim, once per tick) thus costs one extra write attempt and one WARNING line.

        Catches any ``Exception`` from the rewrite and logs it as one WARNING line without a traceback, so
        the caller's re-raised original persist error is not masked and a persistent disk failure does not
        emit a second traceback per failed persist. A ``BaseException`` that is not an ``Exception`` (e.g.
        ``asyncio.CancelledError``, ``KeyboardInterrupt``) propagates and replaces the original error.
        """
        try:
            self._write_atomic()
        except Exception as exc:  # noqa: BLE001 — best-effort rewrite on an error path; must not mask the original persist error
            logger.warning(
                "JobStore: corrective rewrite after a failed persist also failed: %s: %s",
                type(exc).__name__,
                exc,
            )

    def list(self) -> list[IngestJob]:
        return list(self._jobs.values())

    def cancel_ingest_retries(self, collection: str) -> None:
        """Cancel every base ingest job on *collection* a ``MaintenanceLoop`` retry could still run or re-create.

        For DELETE /collections/{name}, under the collection lock, once the table is dropped (or, for a config-only
        collection, its config entry removed — a retry would create the table): each FAILED or PENDING
        base ``IngestJob`` on it becomes CANCELLED, in every namespace — the table and lock are keyed by name alone,
        so any namespace's retry would re-create the dropped table. Under the lock a PENDING one is a retry waiting
        for it, whose PENDING -> RUNNING claim is then refused. Subclass jobs are untouched. All the cancels are
        persisted in one write. Best-effort and all-or-nothing: a failed write is logged at WARNING, every job keeps
        its old status (memory restored, as in ``transition``), and nothing is raised.
        """
        old_jobs = {
            job.job_id: job
            for job in self._jobs.values()
            if is_base_ingest(job) and job.collection == collection and job.status in _RETRY_CANDIDATE_STATUSES
        }
        if not old_jobs:
            return
        now = _now_iso()
        for job_id, job in old_jobs.items():
            self._jobs[job_id] = dataclasses.replace(job, status=JobStatus.CANCELLED, updated_at=now)
        try:
            self._write_atomic()
        except Exception:
            self._jobs.update(old_jobs)
            self._rewrite_after_failed_persist()
            logger.warning(
                "JobStore: could not cancel the ingest jobs of dropped collection %s (%s)",
                collection, ", ".join(old_jobs), exc_info=True,
            )

    def find_active_collection_job(
        self, collection: str, namespace: str, *, excluding_job_id: str | None = None,
    ) -> IngestJob | None:
        """Return a job that may still write *collection* (``may_write_collection``), in ANY namespace, else ``None``.

        A match in the caller's *namespace* is preferred, so the caller sees its own job id when it has one. The job
        *excluding_job_id* is never returned: an import's own job is ``RUNNING`` while its task checks, and must not
        refuse itself.

        Any namespace: the search store keeps one LanceDB table per collection name (``drop_collection`` drops the
        table by name alone) and ``lock_for`` keys the collection lock by name only, so a job submitted from any
        namespace writes into the very table DELETE /collections/{name} drops.

        A base ingest job's PENDING is excluded: the lock covers it. POST /ingest and POST /collections/ hold the
        collection lock while their job is PENDING, and a maintenance retry waits for the lock before its
        PENDING -> RUNNING claim (``routes_jobs._run_maintenance_retry``) — a DELETE that holds the lock cancels
        such a retry instead (``cancel_ingest_retries``).
        """
        active = [
            job for job in self._jobs.values()
            if job.collection == collection and job.job_id != excluding_job_id and may_write_collection(job)
        ]
        return next((job for job in active if job.namespace == namespace), next(iter(active), None))

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
        return self.create_job(job)

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
        return self.create_job(job)

    def create_migration(
        self,
        collection: str,
        kind: MigrationKind,
        backup_confirmed: bool | None,
        namespace: str = DEFAULT_NAMESPACE,
        *,
        status: JobStatus = JobStatus.QUEUED,
    ) -> MigrationJob:
        """Create a MigrationJob with *status* (QUEUED: for the scheduler to run) and persist it."""
        now = _now_iso()
        job = MigrationJob(
            job_id=str(uuid.uuid4()),
            status=status,
            created_at=now,
            updated_at=now,
            namespace=namespace,
            collection=collection,
            kind=kind,
            backup_confirmed=backup_confirmed,
        )
        return self.create_job(job)

    def create_community_rebuild(
        self,
        collection: str,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> CommunityRebuildJob:
        """Create a CommunityRebuildJob directly RUNNING (one write) and persist it.

        Its route runs the task itself and nothing schedules this kind, so a QUEUED one would never run.
        """
        now = _now_iso()
        job = CommunityRebuildJob(
            job_id=str(uuid.uuid4()),
            status=JobStatus.RUNNING,
            created_at=now,
            updated_at=now,
            namespace=namespace,
            collection=collection,
        )
        return self.create_job(job)

    def create_sync(self, namespace: str = DEFAULT_NAMESPACE) -> SyncJob:
        """Create a SyncJob directly RUNNING (one write) and persist it.

        Its caller runs the task itself and nothing schedules this kind, so a QUEUED one would never run.
        """
        now = _now_iso()
        job = SyncJob(
            job_id=str(uuid.uuid4()),
            status=JobStatus.RUNNING,
            created_at=now,
            updated_at=now,
            namespace=namespace,
            collection="",
        )
        return self.create_job(job)

    def create_metadata_reindex(
        self,
        collection: str,
        namespace: str = DEFAULT_NAMESPACE,
        dry_run: bool = False,
    ) -> MetadataReindexJob:
        """Create a MetadataReindexJob directly RUNNING (one write) and persist it. *dry_run*: a read-only preview.

        Its route runs the task itself and nothing schedules this kind, so a QUEUED one would never run.
        """
        now = _now_iso()
        job = MetadataReindexJob(
            job_id=str(uuid.uuid4()),
            status=JobStatus.RUNNING,
            created_at=now,
            updated_at=now,
            namespace=namespace,
            collection=collection,
            dry_run=dry_run,
        )
        return self.create_job(job)

    def update_progress(self, job_id: str, processed: int, total: int, phase: str, **fields: object) -> None:
        """Set the progress dict on a job, plus any other job *fields* in the same atomic write.

        *fields* keeps a job's internal checkpoint state (e.g. ``ImportJob.lines_consumed``) consistent
        with ``progress`` across a crash, without adding keys to the wire-facing dict.
        """
        self.update(job_id, progress={"processed": processed, "total": total, "phase": phase}, **fields)

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
                item["ingest_options"] = _ingest_options_from_json(item.get("job_id"), item.get("ingest_options"))
                for flag in ("cancel_requested", "cancelled_before_start"):
                    item[flag] = _flag_from_json(item.get("job_id"), flag, item.get(flag, False))
                job_type = item.pop("job_type", _INGEST_JOB_TYPE)
                if job_type == "export":
                    job: IngestJob = ExportJob(**item)
                elif job_type == "import":
                    job = ImportJob(**item)
                elif job_type == _REINDEX_JOB_TYPE:
                    job = ReindexJob(**item)
                elif job_type == "delete":
                    job = DeleteJob(**item)
                elif job_type == "migration":
                    item["kind"] = MigrationKind(item["kind"])
                    job = MigrationJob(**item)
                elif job_type == "community_rebuild":
                    job = CommunityRebuildJob(**item)
                elif job_type == JobKind.sync.value:
                    item["kind"] = JobKind(item["kind"])
                    job = SyncJob(**item)
                elif job_type == JobKind.metadata_reindex.value:
                    item["kind"] = JobKind(item["kind"])
                    job = MetadataReindexJob(**item)
                else:
                    job = IngestJob(**item)
                if job.status == JobStatus.CANCELLING and job.cancelled_before_start:
                    # A cancel requested while the job was PENDING: its task never claimed it RUNNING, so nothing ran
                    # and the previous process died before the task could write CANCELLED. Complete the cancel as that
                    # task would have — no crash marker, no guard (a job still PENDING at load arms none either).
                    job = dataclasses.replace(job, status=JobStatus.CANCELLED, updated_at=_now_iso())
                    modified = True
                elif job.status in _CRASH_STATUSES:
                    job = dataclasses.replace(
                        job,
                        status=JobStatus.FAILED,
                        error=PROCESS_RESTART_ERROR,
                        updated_at=_now_iso(),
                    )
                    modified = True
                    if job_type in _INGEST_FAMILY_JOB_TYPES:
                        self._crashed_ingest_on_load = True
                elif (job.status == JobStatus.PENDING and job_type in _NOT_STARTED_ON_LOAD_JOB_TYPES) or (
                    job.status == JobStatus.QUEUED and job_type in _NEVER_SCHEDULED_ON_LOAD_JOB_TYPES
                ):
                    # Every base ingest and reindex job's task is spawned when the job is created (a shutdown cancels
                    # a reindex task while it loads its embedder, still PENDING), so one still PENDING lost its task
                    # with the previous process; a community-rebuild, metadata-reindex or sync job is run by its
                    # route, never the scheduler, so one still QUEUED lost its route; other kinds keep their status.
                    job = dataclasses.replace(
                        job,
                        status=JobStatus.FAILED,
                        error=NOT_STARTED_BEFORE_RESTART_DETAIL,
                        updated_at=_now_iso(),
                    )
                    modified = True
                self._jobs[job.job_id] = job
            count_before = len(self._jobs)
            # Ordering is load-bearing: the rewrite above sets status=FAILED, which
            # IS in _TERMINAL_STATUSES, so an old crash row is itself eligible for
            # eviction below. The crash-loop guard survives only because
            # _crashed_ingest_on_load is latched in the recovery branch above, before eviction runs
            # here — evicting first would silently disable the guard for any row
            # older than _EVICTION_DAYS. Do not reorder.
            self._evict_old()
            if len(self._jobs) < count_before:
                modified = True
            return modified
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.error("JobStore: corrupt jobs file %s — resetting (%s)", self._path, exc)
            self._jobs = {}
            self._crashed_ingest_on_load = False
            return False

    def _write_atomic(self) -> None:
        with self._lock:
            self._evict_old()  # evict BEFORE serializing so stale jobs are never written
            ensure_private_dir(self._path.parent)
            data = []
            for job in self._jobs.values():
                item = dataclasses.asdict(job)
                item["status"] = item["status"].value if hasattr(item["status"], "value") else item["status"]
                # A row without options / a cancel request keeps the shape it had before those fields existed.
                if item["ingest_options"] is None:
                    del item["ingest_options"]
                for flag in ("cancel_requested", "cancelled_before_start"):
                    if not item[flag]:
                        del item[flag]
                if isinstance(job, MigrationJob):
                    item["job_type"] = "migration"
                elif isinstance(job, ExportJob):
                    item["job_type"] = "export"
                elif isinstance(job, ImportJob):
                    item["job_type"] = "import"
                elif isinstance(job, ReindexJob):
                    item["job_type"] = _REINDEX_JOB_TYPE
                elif isinstance(job, DeleteJob):
                    item["job_type"] = "delete"
                elif isinstance(job, CommunityRebuildJob):
                    item["job_type"] = "community_rebuild"
                elif isinstance(job, SyncJob):
                    item["job_type"] = JobKind.sync.value
                elif isinstance(job, MetadataReindexJob):
                    item["job_type"] = JobKind.metadata_reindex.value
                else:
                    item["job_type"] = _INGEST_JOB_TYPE
                data.append(item)
            atomic_write_json(self._path, data)

    def _evict_old(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=_EVICTION_DAYS)
        to_remove = [
            job_id
            for job_id, job in self._jobs.items()
            if job.status in _TERMINAL_STATUSES
            and datetime.fromisoformat(job.updated_at) < cutoff
        ]
        for job_id in to_remove:
            del self._jobs[job_id]

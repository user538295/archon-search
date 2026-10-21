"""Canonical domain types for archon-search."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from archon_search.constants import DEFAULT_NAMESPACE

_VALID_INGEST_SOURCES = frozenset({"user", "backup", "maintenance"})


class JobStatus(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"       # bulk job waiting for a scheduler slot
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    FAILED_EXPIRED = "FAILED_EXPIRED"
    CANCELLED = "CANCELLED"
    CANCELLING = "CANCELLING"


@dataclass(frozen=True)
class IngestOptions:
    """The POST /ingest request a path ingest job was made from, which a ``MaintenanceLoop`` retry re-applies.

    ``submitted_path`` is the caller's raw ``path`` (the job's ``source_path`` holds its resolution), so a retry's
    not-found error echoes what the caller sent; the rest are the request's ``ingested_by`` (``X-Ingested-By``),
    ``chunk_ttl_seconds`` and ``chunk_scopes``.
    """

    submitted_path: str
    ingested_by: str
    chunk_ttl_seconds: int | None = None
    chunk_scopes: tuple[str, ...] | None = None


@dataclass
class IngestJob:
    job_id: str
    status: JobStatus
    created_at: str
    updated_at: str
    result: dict | None = None
    error: str | None = None
    namespace: str = DEFAULT_NAMESPACE
    progress: dict | None = None  # {"processed": int, "total": int, "phase": str}
    source: Literal["user", "backup", "maintenance"] = "user"
    source_path: str = ""   # resolved POST /ingest path, set at create; "" for an inline or POST /collections/ ingest
    collection: str = ""    # target collection name, set when the job is created
    retry_count: int = 0    # the retry attempt a MaintenanceLoop retry job is (1, 2, ...); 0: not a retry
    # Set only on a path ingest made by POST /ingest (and copied to its maintenance retries); persisted in the jobs
    # file only (job_to_dict does not expose it). None: the original request is unknown (a job saved before the
    # options were stored, an inline or POST /collections/ ingest, or any subclass), so it is never retried.
    ingest_options: IngestOptions | None = None
    # True once a DELETE /jobs/{id} asked to cancel the job — a client's cancel, unlike a CANCELLED written when the
    # server shut down; a MaintenanceLoop retry chain ends there. Persisted in the jobs file only (off the wire).
    cancel_requested: bool = False

    def __post_init__(self) -> None:
        if self.source not in _VALID_INGEST_SOURCES:
            raise ValueError(
                f"Invalid source {self.source!r}; must be one of {sorted(_VALID_INGEST_SOURCES)}"
            )


def is_base_ingest(job: IngestJob) -> bool:
    """Whether *job* is a plain ingest job — exactly ``IngestJob`` (POST /ingest, POST /collections/ and the
    ``MaintenanceLoop`` retries), none of its subclasses (reindex, export, sync, ...), which run other work."""
    return type(job) is IngestJob


@dataclass
class ReindexJob(IngestJob):
    target_embedding_model: str | None = None


@dataclass
class DeleteJob(IngestJob):
    deleted_ids: list[str] = field(default_factory=list)


@dataclass
class ExportJob(IngestJob):
    collection: str = ""
    output_path: str = ""   # final .tar.gz path (set on DONE)
    tmp_path: str = ""      # .export-<job_id>.jsonl.tmp path
    source: Literal["user", "backup"] = "user"


@dataclass
class ImportJob(IngestJob):
    collection: str = ""
    archive_path: str = ""
    force_overwrite: bool = False
    ignore_schema_version: bool = False
    on_error: str = "fail"  # "fail" | "skip"
    source: Literal["user", "backup"] = "user"
    # Non-empty archive lines read (imported or skipped): the resume position. Written at each
    # ingesting checkpoint, by the indexing write, and by the collection claim (which resets it
    # on a restart from scratch). Persisted in jobs.json only; job_to_dict does not expose it.
    lines_consumed: int = 0
    # Set by the claim written right before a run's first ingest write to the collection. Rows may be committed
    # before the first checkpoint, so a resume trusts this, not only `progress`.
    collection_claimed: bool = False
    # Set when another import (any namespace, any status) claims the same collection: the rows a previous run of
    # this job wrote may be gone or overwritten, so a resume never continues into the collection. Cleared by this
    # job's own next claim. Both flags are persisted in jobs.json only; job_to_dict does not expose them.
    collection_claim_revoked: bool = False


# UPPER_CASE names follow the convention of JobStatus and IndexingStatus.
# Wire values are snake_case to match the TypeSpec C1 contract and REST API.
class MigrationKind(str, Enum):
    IN_PLACE = "in_place"
    REWRITE = "rewrite"
    EXPORT_REBUILD = "export_rebuild"


@dataclass
class MigrationSpec:
    name: str
    kind: MigrationKind
    description: str
    introduced_at: int


@dataclass
class MigrationJob(IngestJob):
    collection: str = ""
    kind: MigrationKind = MigrationKind.IN_PLACE
    migrations_applied: list[str] = field(default_factory=list)
    backup_confirmed: bool | None = None
    source: Literal["user", "backup"] = "user"


@dataclass
class CommunityRebuildJob(IngestJob):
    collection: str = ""


class JobKind(str, Enum):
    sync = "sync"
    metadata_reindex = "metadata_reindex"


@dataclass
class SyncJob(IngestJob):
    collection: str = ""
    kind: JobKind = JobKind.sync


@dataclass
class MetadataReindexJob(IngestJob):
    collection: str = ""
    kind: JobKind = JobKind.metadata_reindex


@dataclass
class Query:
    text: str
    slots: int | None = None


@dataclass
class RouteResponse:
    pre_context: str | None
    pinned_names: list[str]
    routable_names: list[str]
    decomposer_invoked: bool


@dataclass
class Collection:
    name: str
    path: str
    description: str
    doc_count: int
    chunk_count: int
    status: str
    watching: bool = False


@dataclass
class CollectionDetail(Collection):
    embedding_model: str = ""
    centroid_present: bool = False
    last_indexed: str | None = None


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str
    source_path: str
    collection: str
    indexed_at: str
    file_type: str
    language: str | None
    metadata: dict[str, str] = field(default_factory=dict)
    custom_score: float | None = None
    ingested_by: str = "archon-search-cli"
    updated_at: str = ""

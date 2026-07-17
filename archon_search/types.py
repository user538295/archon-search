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
    source_path: str = ""   # absolute path of the ingested file; set by ingest worker
    collection: str = ""    # target collection name; set by ingest worker
    retry_count: int = 0    # incremented by MaintenanceLoop on each retry attempt

    def __post_init__(self) -> None:
        if self.source not in _VALID_INGEST_SOURCES:
            raise ValueError(
                f"Invalid source {self.source!r}; must be one of {sorted(_VALID_INGEST_SOURCES)}"
            )


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

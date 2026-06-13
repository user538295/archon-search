"""Canonical domain types for archon-search."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from archon_search.constants import DEFAULT_NAMESPACE


class JobStatus(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"       # bulk job waiting for a scheduler slot
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
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


@dataclass
class ImportJob(IngestJob):
    collection: str = ""
    archive_path: str = ""
    force_overwrite: bool = False
    ignore_schema_version: bool = False
    on_error: str = "fail"  # "fail" | "skip"


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

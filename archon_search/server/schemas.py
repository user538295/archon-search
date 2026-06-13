"""Shared Pydantic response models for archon-search REST API.

Pure data models — no business logic.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class CheckStatus(str, Enum):
    """Storage check result for readiness probes."""

    OK = "ok"
    FAIL = "fail"


class ReadinessChecks(BaseModel):
    storage: CheckStatus


class ReadinessResponse(BaseModel):
    """Terse, unauthenticated response body for GET /ready."""

    ready: bool
    checks: ReadinessChecks


class WatcherReport(BaseModel):
    running: bool
    watching: list[str] = []


class JobCounts(BaseModel):
    """Job queue depth snapshot.

    ``running`` counts jobs in RUNNING status only; CANCELLING jobs are
    excluded — a cancelling job is in the process of stopping and does not
    represent available capacity.
    """

    pending: int = Field(ge=0)
    running: int = Field(ge=0)


class ReadinessDetail(BaseModel):
    """Rich readiness sub-object for authenticated GET /status."""

    storage_connected: bool
    embedder_warm: bool
    reranker_warm: bool
    jobs: JobCounts
    collections_indexing: int = Field(ge=0)
    collections_failed: int = Field(ge=0)
    watcher: WatcherReport


class HealthResponse(BaseModel):
    status: str  # "running"
    version: str


class StatusCollectionEntry(BaseModel):
    name: str
    path: str
    doc_count: int = 0
    chunk_count: int = 0
    status: str
    watching: bool
    eta_seconds: float | None = None
    processed_files: int = 0
    total_files: int = 0
    error: str | None = None
    error_count: int = 0
    needs_reindex: bool = False
    warning: str | None = None


class StatusResponse(BaseModel):
    running: bool
    pid: int
    version: str
    collections: list[StatusCollectionEntry]
    readiness: ReadinessDetail | None = None


class IndexingStateCollectionEntry(BaseModel):
    status: str
    processed_files: int = 0
    total_files: int = 0
    error: str | None = None
    error_count: int = 0
    started_at: str | None = None
    completed_at: str | None = None


class IndexingStateResponse(BaseModel):
    collections: dict[str, IndexingStateCollectionEntry]
    last_updated: str | None = None
    trigger: str | None = None


class CollectionSummary(BaseModel):
    name: str
    path: str
    description: str = ""
    doc_count: int = 0
    chunk_count: int = 0
    namespace: str
    status: str
    active_embedding_model: str = ""
    needs_reindex: bool = False


class CollectionDetail(CollectionSummary):
    active_embedding_model: str
    pending_embedding_model: str | None = None
    needs_reindex: bool = False
    reindex_job_id: str | None = None
    centroid_present: bool = False
    last_indexed: str | None = None
    acl_protected_count: int = 0
    acl_open_count: int = 0


class JobResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    updated_at: str
    result: str | None = None
    error: str | None = None
    namespace: str
    progress: dict | None = None
    # D2-1.4 bulk-job subclass fields. All nullable and additive: base
    # IngestJob instances serialize them as None; ExportJob/ImportJob carry
    # the real values.
    source: str | None = None
    collection: str | None = None
    output_path: str | None = None
    archive_path: str | None = None


class JobListResponse(BaseModel):
    items: list[JobResponse]
    next_cursor: str | None
    total: int


class DeleteResponse(BaseModel):
    name: str
    deleted: bool


class ErrorDetail(BaseModel):
    detail: str


class ExcludedCollectionSchema(BaseModel):
    name: str
    reason: str


class PatchCollectionBody(BaseModel):
    embedding_model: str

    @field_validator("embedding_model")
    @classmethod
    def validate_embedding_model_not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("embedding_model field required")
        return v

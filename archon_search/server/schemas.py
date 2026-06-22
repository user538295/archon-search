"""Shared Pydantic response models for archon-search REST API.

Pure data models — no business logic.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CheckStatus(str, Enum):
    """Check result for readiness probes.

    ``PENDING`` and ``WARN`` (D6 C2) are additive values used by the model
    validation check: ``PENDING`` while background validation has not yet
    completed, ``WARN`` when a model loaded via provider fallback (e.g. CPU
    instead of GPU). See contract C2 in
    ``Documentation/Backlog/D6-provider-validation-team-plan.md``.
    """

    OK = "ok"
    FAIL = "fail"
    PENDING = "pending"
    WARN = "warn"


class ReadinessChecks(BaseModel):
    storage: CheckStatus
    # D6 BE-5 — model validation check; PENDING until background validation
    # completes. Default required: routes_ready.py constructs this with only
    # ``storage=`` while validation may still be running.
    models: CheckStatus = CheckStatus.PENDING


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


class CollectionBackupStatus(BaseModel):
    """Per-collection backup state surfaced under StatusResponse.backup (D2 Task 4.2)."""

    collection: str
    last_backup_at: str | None = None
    archive_count: int = 0


class BackupStatusDetail(BaseModel):
    """Scheduled-backup state for the caller's namespace (D2 Task 4.2)."""

    enabled: bool
    interval_hours: int
    last_tick_at: str | None = None
    next_run_at: str | None = None
    collections_excluded: list[str] = []
    collection_status: list[CollectionBackupStatus] = []


class CollectionHealthEntry(BaseModel):
    """Per-collection health snapshot written after each maintenance pass (D5 C1)."""

    collection: str
    fts_optimized_at: str | None = None
    orphans_removed_last_run: int = Field(default=0, ge=0)
    last_retry_at: str | None = None
    last_error: str | None = None
    mutations_since_recompute: int = Field(default=0, ge=0)
    centroid_recompute_threshold: int = Field(default=0, ge=0)
    meta_chunk_count: int = Field(default=0, ge=0)


class MaintenanceStatusDetail(BaseModel):
    """Maintenance loop state for the caller's namespace (D5 C1)."""

    enabled: bool
    interval_hours: int = Field(default=0, ge=0)
    last_run_at: str | None = None
    next_run_at: str | None = None
    collection_health: list[CollectionHealthEntry] = []


class MaintenanceTriggerResponse(BaseModel):
    """Response body for POST /maintenance/trigger (D5 C2)."""

    status: Literal["triggered", "already_triggered"]


class ModelValidationStatus(BaseModel):
    """API-facing mirror of ModelValidationResult, nested under GET /status (D6 C1).

    A null (``None``) boolean field means the corresponding probe has not run /
    completed. ``validated_at`` is ``None`` while validation is pending and a UTC
    timestamp once it has finished. See ``D6-model-validation-status.tsp`` (C1).
    """

    embedder_ok: bool | None = None
    reranker_ok: bool | None = None
    provider_warnings: list[str] = Field(default_factory=list)
    validated_at: datetime | None = None


class StatusResponse(BaseModel):
    running: bool
    pid: int
    version: str
    collections: list[StatusCollectionEntry]
    readiness: ReadinessDetail | None = None
    backup: BackupStatusDetail | None = None
    # D3 BE-15 — schema migration health fields
    store_schema_version: int = 0
    collections_schema_behind: int = Field(default=0, ge=0)
    # D5 BE-3 — maintenance health field (additive, nullable)
    maintenance: MaintenanceStatusDetail | None = None
    # D6 BE-2 — model validation health field (additive, nullable)
    model_validation: ModelValidationStatus | None = None


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
    result: str | dict | None = None
    error: str | None = None
    namespace: str
    progress: dict | None = None
    # D5-BE-7: source, source_path, collection, retry_count are now on the
    # IngestJob base class. source defaults to "user"; collection and
    # source_path default to ""; retry_count defaults to 0.
    source: str = "user"
    source_path: str = ""
    collection: str = ""
    retry_count: int = 0
    output_path: str | None = None
    archive_path: str | None = None
    # D3 MigrationJob fields. Nullable and additive: non-migration jobs get None.
    kind: str | None = None
    migrations_applied: list[str] | None = None
    backup_confirmed: bool | None = None


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


class SkippedItem(BaseModel):
    """One entry in BackupTriggerResponse.skipped — collection + machine-readable reason."""

    collection: str
    reason: str


class BackupTriggerResponse(BaseModel):
    """Response body for POST /backup/trigger (D2 Task 4.1)."""

    queued: list[str]
    skipped: list[SkippedItem]


class PatchCollectionBody(BaseModel):
    embedding_model: str

    @field_validator("embedding_model")
    @classmethod
    def validate_embedding_model_not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("embedding_model field required")
        return v


class MigrationSpecSchema(BaseModel):
    """Serialized representation of a MigrationSpec for REST responses."""

    name: str
    kind: str
    description: str
    introduced_at: int


class MigrationPendingResponse(BaseModel):
    """Response body for GET /collections/{name}/migrations/pending."""

    collection: str
    pending: list[MigrationSpecSchema]
    schema_version: int


class MigrateRequest(BaseModel):
    """Request body for POST /collections/{name}/migrate."""

    backup_confirmed: bool = False
    dry_run: bool = False


class MigrateInPlaceResponse(BaseModel):
    """Response body for POST /collections/{name}/migrate (in-place synchronous path)."""

    migrations_applied: list[str]

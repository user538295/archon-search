"""Shared Pydantic response models for archon-search REST API (FEAT-045 Task 1.4).

Pure data models — no business logic.
"""
from __future__ import annotations

from pydantic import BaseModel


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


class StatusResponse(BaseModel):
    running: bool
    pid: int
    version: str
    collections: list[StatusCollectionEntry]


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


class CollectionDetail(CollectionSummary):
    embedding_model: str
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


class DeleteResponse(BaseModel):
    name: str
    deleted: bool


class ErrorDetail(BaseModel):
    detail: str

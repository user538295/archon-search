"""Tests for shared REST response Pydantic schemas ."""
from __future__ import annotations

from archon_search.jobs.model import IngestJob, JobStatus, job_to_dict
from archon_search.server.schemas import (
    CollectionDetail,
    CollectionSummary,
    HealthResponse,
    IndexingStateResponse,
    JobResponse,
    StatusCollectionEntry,
    StatusResponse,
)


def test_job_response_from_job_to_dict() -> None:
    """JobResponse(**job_to_dict(job)) succeeds for a valid IngestJob."""
    job = IngestJob(
        job_id="abc-123",
        status=JobStatus.DONE,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T01:00:00+00:00",
        result=None,
        error=None,
        namespace="default",
    )
    data = job_to_dict(job)
    response = JobResponse(**data)
    assert response.job_id == "abc-123"
    assert response.status == "DONE"
    assert response.namespace == "default"
    assert response.result is None
    assert response.error is None


def test_collection_detail_inherits_summary_fields() -> None:
    """CollectionDetail includes all CollectionSummary fields."""
    detail = CollectionDetail(
        name="docs",
        path="/some/path",
        description="My docs",
        doc_count=10,
        chunk_count=100,
        namespace="default",
        status="DONE",
        active_embedding_model="all-MiniLM-L6-v2",
        pending_embedding_model=None,
        needs_reindex=False,
        reindex_job_id=None,
        centroid_present=True,
        last_indexed="2026-01-01T00:00:00Z",
        acl_protected_count=2,
        acl_open_count=8,
    )
    # All CollectionSummary fields are present on CollectionDetail
    assert detail.name == "docs"
    assert detail.path == "/some/path"
    assert detail.description == "My docs"
    assert detail.doc_count == 10
    assert detail.chunk_count == 100
    assert detail.namespace == "default"
    assert detail.status == "DONE"
    # CollectionDetail-specific fields
    assert detail.active_embedding_model == "all-MiniLM-L6-v2"
    assert detail.pending_embedding_model is None
    assert detail.needs_reindex is False
    assert detail.reindex_job_id is None
    assert detail.centroid_present is True
    assert detail.last_indexed == "2026-01-01T00:00:00Z"
    assert detail.acl_protected_count == 2
    assert detail.acl_open_count == 8
    # Verify CollectionDetail is a subclass of CollectionSummary
    assert isinstance(detail, CollectionSummary)


def test_indexing_state_response_empty_collections() -> None:
    """IndexingStateResponse(collections={}) is valid."""
    response = IndexingStateResponse(collections={})
    assert response.collections == {}
    assert response.last_updated is None
    assert response.trigger is None


def test_health_response_construction() -> None:
    """HealthResponse(status=..., version=...) succeeds and has correct fields."""
    response = HealthResponse(status="running", version="1.0.0")
    assert response.status == "running"
    assert response.version == "1.0.0"


def test_status_response_with_collection_entries() -> None:
    """StatusResponse with a StatusCollectionEntry succeeds and contains the entry."""
    entry = StatusCollectionEntry(name="col", path="/p", status="DONE", watching=False)
    response = StatusResponse(running=True, pid=123, version="1.0.0", collections=[entry])
    assert response.running is True
    assert response.pid == 123
    assert response.version == "1.0.0"
    assert len(response.collections) == 1
    col = response.collections[0]
    assert col.name == "col"
    assert col.path == "/p"
    assert col.status == "DONE"
    assert col.watching is False
    assert col.doc_count == 0
    assert col.chunk_count == 0

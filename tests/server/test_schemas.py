"""Tests for shared REST response Pydantic schemas ."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from archon_search.jobs.model import IngestJob, JobStatus, job_to_dict
from archon_search.server.schemas import (
    CollectionDetail,
    CollectionHealthEntry,
    CollectionSummary,
    HealthResponse,
    IndexingStateResponse,
    JobResponse,
    MaintenanceStatusDetail,
    MaintenanceTriggerResponse,
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


# --- D5 BE-3: Maintenance schema tests ---


def test_status_response_maintenance_field_optional() -> None:
    """StatusResponse serialises with maintenance=None without error."""
    entry = StatusCollectionEntry(name="col", path="/p", status="DONE", watching=False)
    response = StatusResponse(running=True, pid=1, version="1.0.0", collections=[entry])
    assert response.maintenance is None
    # Serialisation should succeed without maintenance field
    data = response.model_dump()
    assert "maintenance" in data
    assert data["maintenance"] is None


def test_collection_health_entry_all_fields() -> None:
    """All eight fields of CollectionHealthEntry round-trip through Pydantic serialisation."""
    entry = CollectionHealthEntry(
        collection="my-col",
        fts_optimized_at="2026-01-01T00:00:00Z",
        orphans_removed_last_run=5,
        last_retry_at=None,
        last_error="some error",
        mutations_since_recompute=42,
        centroid_recompute_threshold=100,
        meta_chunk_count=200,
    )
    data = entry.model_dump()
    assert data["collection"] == "my-col"
    assert data["fts_optimized_at"] == "2026-01-01T00:00:00Z"
    assert data["orphans_removed_last_run"] == 5
    assert data["last_retry_at"] is None
    assert data["last_error"] == "some error"
    assert data["mutations_since_recompute"] == 42
    assert data["centroid_recompute_threshold"] == 100
    assert data["meta_chunk_count"] == 200

    # Verify all-nullable scenario also works
    entry_nulls = CollectionHealthEntry(
        collection="col2",
        fts_optimized_at=None,
        orphans_removed_last_run=0,
        last_retry_at=None,
        last_error=None,
        mutations_since_recompute=0,
        centroid_recompute_threshold=0,
        meta_chunk_count=0,
    )
    data_nulls = entry_nulls.model_dump()
    assert data_nulls["fts_optimized_at"] is None
    assert data_nulls["last_retry_at"] is None
    assert data_nulls["last_error"] is None

    # C1-T-1: verify all 7 defaults when only collection is supplied
    entry_defaults = CollectionHealthEntry(collection="defaults-only")
    assert entry_defaults.fts_optimized_at is None
    assert entry_defaults.orphans_removed_last_run == 0
    assert entry_defaults.last_retry_at is None
    assert entry_defaults.last_error is None
    assert entry_defaults.mutations_since_recompute == 0
    assert entry_defaults.centroid_recompute_threshold == 0
    assert entry_defaults.meta_chunk_count == 0


def test_collection_health_entry_requires_collection() -> None:
    """CollectionHealthEntry() with no arguments raises ValidationError (collection is required)."""
    with pytest.raises(ValidationError):
        CollectionHealthEntry()  # type: ignore[call-arg]


def test_maintenance_trigger_response_literal() -> None:
    """MaintenanceTriggerResponse status must be 'triggered' or 'already_triggered'."""
    # Both valid values should work
    r1 = MaintenanceTriggerResponse(status="triggered")
    assert r1.status == "triggered"

    r2 = MaintenanceTriggerResponse(status="already_triggered")
    assert r2.status == "already_triggered"

    # Invalid values should raise a ValidationError
    with pytest.raises(ValidationError):
        MaintenanceTriggerResponse(status="unknown_status")  # type: ignore[arg-type]


def test_maintenance_status_detail_all_fields() -> None:
    """MaintenanceStatusDetail round-trips with collection_health list."""
    health_entry = CollectionHealthEntry(
        collection="docs",
        fts_optimized_at=None,
        orphans_removed_last_run=3,
        last_retry_at="2026-06-01T12:00:00Z",
        last_error=None,
        mutations_since_recompute=10,
        centroid_recompute_threshold=50,
        meta_chunk_count=300,
    )
    detail = MaintenanceStatusDetail(
        enabled=True,
        interval_hours=24,
        last_run_at="2026-06-21T08:00:00Z",
        next_run_at="2026-06-22T08:00:00Z",
        collection_health=[health_entry],
    )
    assert detail.enabled is True
    assert detail.interval_hours == 24
    assert detail.last_run_at == "2026-06-21T08:00:00Z"
    assert detail.next_run_at == "2026-06-22T08:00:00Z"
    assert len(detail.collection_health) == 1
    assert detail.collection_health[0].collection == "docs"

    # C1-T-5: verify defaults when only required fields are supplied
    detail_defaults = MaintenanceStatusDetail(enabled=False, interval_hours=0)
    assert detail_defaults.last_run_at is None
    assert detail_defaults.next_run_at is None
    assert detail_defaults.collection_health == []


def test_status_response_with_maintenance_populated() -> None:
    """StatusResponse correctly carries a populated MaintenanceStatusDetail."""
    maintenance = MaintenanceStatusDetail(
        enabled=True,
        interval_hours=12,
        last_run_at=None,
        next_run_at=None,
        collection_health=[],
    )
    response = StatusResponse(
        running=True,
        pid=1,
        version="1.0.0",
        collections=[],
        maintenance=maintenance,
    )
    assert response.maintenance is not None
    assert response.maintenance.enabled is True
    assert response.maintenance.interval_hours == 12
    assert response.maintenance.collection_health == []

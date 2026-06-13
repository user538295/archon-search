"""Tests for Task 1.5 — job_to_dict() and JobResponse include progress field."""
import pytest

from archon_search.jobs.model import job_to_dict
from archon_search.server.schemas import JobResponse
from archon_search.types import IngestJob, JobStatus


def test_job_to_dict_includes_progress_none():
    """job_to_dict for an IngestJob without progress includes key 'progress' with value None."""
    job = IngestJob(
        job_id="j1",
        status=JobStatus.PENDING,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    result = job_to_dict(job)
    assert "progress" in result
    assert result["progress"] is None


def test_job_to_dict_includes_progress_dict():
    """job_to_dict serializes progress dict correctly when set."""
    progress = {"processed": 5, "total": 10, "phase": "reading"}
    job = IngestJob(
        job_id="j2",
        status=JobStatus.RUNNING,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        progress=progress,
    )
    result = job_to_dict(job)
    assert result["progress"] == {"processed": 5, "total": 10, "phase": "reading"}


def test_job_response_progress_optional():
    """JobResponse constructs without progress field; defaults to None."""
    resp = JobResponse(
        job_id="j3",
        status="RUNNING",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        namespace="default",
    )
    assert resp.progress is None


def test_job_response_roundtrip_with_progress():
    """JobResponse JSON serialization roundtrip preserves the progress dict."""
    progress = {"processed": 42, "total": 100, "phase": "reading"}
    resp = JobResponse(
        job_id="j4",
        status="RUNNING",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        namespace="default",
        progress=progress,
    )
    as_json = resp.model_dump_json()
    restored = JobResponse.model_validate_json(as_json)
    assert restored.progress == progress

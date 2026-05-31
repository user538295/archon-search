"""Tests for JobStore type dispatch and ReindexJob.target_embedding_model field."""
import json
import uuid
from datetime import datetime, timezone

import pytest

from archon_search.jobs.store import JobStore
from archon_search.types import DeleteJob, IngestJob, JobStatus, ReindexJob


def _now():
    return datetime.now(timezone.utc).isoformat()


def _base_kwargs():
    return {
        "job_id": str(uuid.uuid4()),
        "status": JobStatus.PENDING,
        "created_at": _now(),
        "updated_at": _now(),
    }


def test_reindex_job_target_embedding_model_defaults_to_none():
    job = ReindexJob(**_base_kwargs())
    assert job.target_embedding_model is None


def test_reindex_job_target_embedding_model_round_trips(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    job = ReindexJob(**_base_kwargs(), target_embedding_model="model-X")
    store.create_job(job)
    retrieved = store.get(job.job_id)
    assert isinstance(retrieved, ReindexJob)
    assert retrieved.target_embedding_model == "model-X"


def test_reindex_job_missing_field_deserializes_to_none(tmp_path):
    # Write JSON without target_embedding_model; loading should set it to None
    path = tmp_path / "jobs.json"
    job_data = {**_base_kwargs()}
    job_data["status"] = job_data["status"].value
    job_data["job_type"] = "reindex"
    path.write_text(json.dumps([job_data]))
    store = JobStore(path)
    job = store.get(job_data["job_id"])
    assert isinstance(job, ReindexJob)
    assert job.target_embedding_model is None


def test_job_store_round_trips_reindex_job_type(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    job = ReindexJob(**_base_kwargs(), target_embedding_model="model-X")
    store.create_job(job)
    retrieved = store.get(job.job_id)
    assert isinstance(retrieved, ReindexJob)
    assert retrieved.target_embedding_model == "model-X"


def test_job_store_round_trips_delete_job_type(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    job = DeleteJob(**_base_kwargs(), deleted_ids=["id1", "id2"])
    store.create_job(job)
    retrieved = store.get(job.job_id)
    assert isinstance(retrieved, DeleteJob)
    assert retrieved.deleted_ids == ["id1", "id2"]


def test_job_store_crash_recovery_preserves_reindex_job_subclass(tmp_path):
    path = tmp_path / "jobs.json"
    store1 = JobStore(path)
    kwargs = {**_base_kwargs(), "status": JobStatus.RUNNING}
    job = ReindexJob(**kwargs, target_embedding_model="model-X")
    store1.create_job(job)
    # Simulate crash: create new store from same file
    store2 = JobStore(path)
    loaded = store2.get(job.job_id)
    assert isinstance(loaded, ReindexJob)
    assert loaded.target_embedding_model == "model-X"
    assert loaded.status == JobStatus.FAILED  # crash recovery applied


def test_job_store_write_includes_job_type_field(tmp_path):
    path = tmp_path / "jobs.json"
    store = JobStore(path)
    job = ReindexJob(**_base_kwargs(), target_embedding_model="model-X")
    store.create_job(job)
    raw = json.loads(path.read_text())
    assert any(item.get("job_type") == "reindex" for item in raw)

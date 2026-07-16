"""Tests for BE-1 — CommunityRebuildJob integration into JobStore.

Covers:
- _write_atomic() / _load() discriminator round-trip (IC-4)
- _write_atomic() tags CommunityRebuildJob distinctly, not folded into "ingest" (Mo7)
- create_community_rebuild() factory
- Crash recovery: RUNNING CommunityRebuildJob → FAILED on load
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.store import JobStore
from archon_search.types import CommunityRebuildJob, JobStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def test_community_rebuild_job_round_trips_through_store(tmp_path: Path) -> None:
    """After write + reload it deserialises as CommunityRebuildJob, not a plain IngestJob (IC-4)."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)

    job = store.create_community_rebuild(collection="my-col", namespace="ns1")

    store2 = JobStore(path=jobs_path)
    reloaded = store2.get(job.job_id)

    assert reloaded is not None
    assert type(reloaded) is CommunityRebuildJob
    assert reloaded.collection == "my-col"
    assert reloaded.namespace == "ns1"


def test_write_atomic_tags_community_rebuild(tmp_path: Path) -> None:
    """A CommunityRebuildJob is tagged 'community_rebuild', not folded into the 'ingest' catch-all (Mo7)."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)

    store.create_community_rebuild(collection="col-a")

    raw = json.loads(jobs_path.read_text())
    assert len(raw) == 1
    assert raw[0]["job_type"] == "community_rebuild"


def test_create_community_rebuild_creates_queued_job(store: JobStore) -> None:
    """The factory creates a QUEUED job carrying collection + namespace."""
    job = store.create_community_rebuild(collection="col1", namespace="tenant-x")

    assert isinstance(job, CommunityRebuildJob)
    assert job.status == JobStatus.QUEUED
    assert job.collection == "col1"
    assert job.namespace == "tenant-x"


def test_create_community_rebuild_defaults_to_default_namespace(store: JobStore) -> None:
    """namespace defaults to DEFAULT_NAMESPACE when omitted."""
    job = store.create_community_rebuild(collection="col1")

    assert job.namespace == DEFAULT_NAMESPACE


def test_community_rebuild_job_crash_recovery_running_to_failed(tmp_path: Path) -> None:
    """CommunityRebuildJob in RUNNING loaded as FAILED with error='process_restart' (C1-I-1)."""
    jobs_path = tmp_path / "jobs.json"
    now = _now()
    jid = str(uuid.uuid4())
    data = [
        {
            "job_id": jid,
            "status": "RUNNING",
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
            "namespace": DEFAULT_NAMESPACE,
            "progress": None,
            "collection": "my-col",
            "source": "user",
            "job_type": "community_rebuild",
        }
    ]
    jobs_path.write_text(json.dumps(data))
    store = JobStore(path=jobs_path)

    job = store.get(jid)
    assert job is not None
    assert type(job) is CommunityRebuildJob
    assert job.status == JobStatus.FAILED
    assert job.error == "process_restart"
    assert job.collection == "my-col"

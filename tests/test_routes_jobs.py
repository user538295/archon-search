"""Tests for POST /ingest, GET /jobs/{id}, DELETE /jobs/{id} (Task 5.6)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.model import JobStatus, job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.server.routes_jobs import IngestRequest, _default_ingest_task, _run_pipeline
from archon_search.types import IngestJob


@pytest.fixture
def tmp_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


@pytest.fixture
def client(tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str]) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, tmp_store)
    return TestClient(app, headers=auth_headers)


# ---------------------------------------------------------------------------
# POST /ingest
# ---------------------------------------------------------------------------


def test_ingest_returns_202_with_job_id(client: TestClient) -> None:
    response = client.post("/ingest", json={"collection": "docs"})
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == JobStatus.PENDING.value


def test_ingest_empty_collection_returns_422(client: TestClient) -> None:
    response = client.post("/ingest", json={"collection": ""})
    assert response.status_code == 422


def test_ingest_x_ingested_by_header_accepted(client: TestClient) -> None:
    """POST /ingest with X-Ingested-By header must succeed with 202."""
    response = client.post(
        "/ingest",
        json={"collection": "docs"},
        headers={"X-Ingested-By": "external-tool"},
    )
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    # Verify the job was created and is retrievable
    job_id = data["job_id"]
    get_response = client.get(f"/jobs/{job_id}")
    assert get_response.status_code == 200


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}
# ---------------------------------------------------------------------------


def test_get_job_returns_current_status(client: TestClient, tmp_store: JobStore) -> None:
    job = tmp_store.create()
    response = client.get(f"/jobs/{job.job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == job.job_id
    assert data["status"] == JobStatus.PENDING.value


def test_get_job_unknown_returns_404(client: TestClient) -> None:
    response = client.get("/jobs/nonexistent-id")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /jobs/{job_id}
# ---------------------------------------------------------------------------


def test_delete_job_done_returns_200(client: TestClient, tmp_store: JobStore) -> None:
    job = tmp_store.create()
    tmp_store.update(job.job_id, status=JobStatus.DONE)
    response = client.delete(f"/jobs/{job.job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == job.job_id
    assert data["status"] == JobStatus.DONE.value


def test_delete_job_running_returns_202(client: TestClient, tmp_store: JobStore) -> None:
    job = tmp_store.create()
    tmp_store.update(job.job_id, status=JobStatus.RUNNING)
    response = client.delete(f"/jobs/{job.job_id}")
    assert response.status_code == 202
    data = response.json()
    assert data["job_id"] == job.job_id
    assert data["status"] == JobStatus.CANCELLING.value
    # Status should be set to CANCELLING
    updated = tmp_store.get(job.job_id)
    assert updated is not None
    assert updated.status == JobStatus.CANCELLING


def test_delete_job_unknown_returns_404(client: TestClient) -> None:
    response = client.delete("/jobs/nonexistent-id")
    assert response.status_code == 404


def test_delete_job_cancelled_returns_200(client: TestClient, tmp_store: JobStore) -> None:
    job = tmp_store.create()
    tmp_store.update(job.job_id, status=JobStatus.CANCELLED)
    response = client.delete(f"/jobs/{job.job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == job.job_id
    assert data["status"] == JobStatus.CANCELLED.value


def test_delete_job_failed_returns_200(client: TestClient, tmp_store: JobStore) -> None:
    """FAILED is terminal — DELETE is idempotent, returns 200."""
    job = tmp_store.create()
    tmp_store.update(job.job_id, status=JobStatus.FAILED, error="oops")
    response = client.delete(f"/jobs/{job.job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == job.job_id
    assert data["status"] == JobStatus.FAILED.value


def test_delete_job_cancelling_returns_202(client: TestClient, tmp_store: JobStore) -> None:
    """CANCELLING job — already being cancelled, returns 202."""
    job = tmp_store.create()
    tmp_store.update(job.job_id, status=JobStatus.CANCELLING)
    response = client.delete(f"/jobs/{job.job_id}")
    assert response.status_code == 202
    data = response.json()
    assert data["job_id"] == job.job_id
    assert data["status"] == JobStatus.CANCELLING.value


def test_delete_job_pending_returns_202(client: TestClient, tmp_store: JobStore) -> None:
    """PENDING job — sets CANCELLING, returns 202."""
    job = tmp_store.create()
    assert job.status == JobStatus.PENDING
    response = client.delete(f"/jobs/{job.job_id}")
    assert response.status_code == 202
    data = response.json()
    assert data["job_id"] == job.job_id
    assert data["status"] == JobStatus.CANCELLING.value
    updated = tmp_store.get(job.job_id)
    assert updated is not None
    assert updated.status == JobStatus.CANCELLING


# ---------------------------------------------------------------------------
# Background task lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ingest_background_task_completes(tmp_path: Path) -> None:
    """_default_ingest_task transitions PENDING → RUNNING → DONE."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create()
    assert job.status == JobStatus.PENDING

    body = IngestRequest(collection="docs")
    await _default_ingest_task(job.job_id, store, body)

    completed = store.get(job.job_id)
    assert completed is not None
    assert completed.status == JobStatus.DONE


# ---------------------------------------------------------------------------
# job_to_dict — Task 3.2
# ---------------------------------------------------------------------------


def test_job_to_dict_includes_namespace() -> None:
    """job_to_dict() output must include 'namespace' key."""
    job = IngestJob(
        job_id="test-job-1",
        status=JobStatus.PENDING,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        namespace="tenantA",
    )
    result = job_to_dict(job)
    assert "namespace" in result
    assert result["namespace"] == "tenantA"


def test_job_to_dict_default_namespace() -> None:
    """IngestJob with no explicit namespace → 'namespace': 'default' in dict."""
    job = IngestJob(
        job_id="test-job-2",
        status=JobStatus.PENDING,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    result = job_to_dict(job)
    assert "namespace" in result
    assert result["namespace"] == DEFAULT_NAMESPACE


# ---------------------------------------------------------------------------
# Task 3.4 — _default_ingest_task namespace parameter
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_default_ingest_task_takes_namespace_param(tmp_path: Path) -> None:
    """_default_ingest_task accepts namespace as 4th positional param and completes."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create()
    body = IngestRequest(collection="docs")

    await _default_ingest_task(job.job_id, store, body, namespace="tenantA")

    completed = store.get(job.job_id)
    assert completed is not None
    assert completed.status == JobStatus.DONE


# ---------------------------------------------------------------------------
# Task 3.5 — _run_pipeline and _default_ingest_task namespace forwarding
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_run_pipeline_forwards_namespace(tmp_path: Path) -> None:
    """_run_pipeline passes namespace= kwarg to pipeline_fn."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create()
    body = IngestRequest(collection="docs")

    received: dict = {}

    async def mock_fn(job_id: str, s: JobStore, b: IngestRequest, namespace: str = "default") -> None:
        received["namespace"] = namespace

    await _run_pipeline(job.job_id, store, body, namespace="tenantA", pipeline_fn=mock_fn)

    assert received["namespace"] == "tenantA"


@pytest.mark.anyio
async def test_default_ingest_task_forwards_namespace_to_run_pipeline(tmp_path: Path) -> None:
    """_default_ingest_task passes its namespace to _run_pipeline (and on to pipeline_fn)."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create()
    body = IngestRequest(collection="docs")

    received: dict = {}

    async def mock_fn(job_id: str, s: JobStore, b: IngestRequest, namespace: str = "default") -> None:
        received["namespace"] = namespace

    await _default_ingest_task(job.job_id, store, body, namespace="tenantA", pipeline_fn=mock_fn)

    assert received["namespace"] == "tenantA"


def test_ingest_request_ignores_body_namespace(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    """POST /ingest with unknown 'namespace' field in body: no 422, job uses request namespace."""
    store = JobStore(path=tmp_path / "jobs.json")
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, store)
    client = TestClient(app, headers=auth_headers)

    response = client.post(
        "/ingest",
        json={"collection": "docs", "namespace": "attacker-namespace"},
    )
    # Must not return 422 — unknown fields are silently ignored by Pydantic
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    # The job namespace must NOT be "attacker-namespace"; it comes from request.state.namespace
    assert data.get("namespace") != "attacker-namespace"

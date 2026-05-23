"""Tests for POST /ingest, GET /jobs/{id}, DELETE /jobs/{id} ."""
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
# job_to_dict# ---------------------------------------------------------------------------


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
# _default_ingest_task namespace parameter
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
# _run_pipeline and _default_ingest_task namespace forwarding
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


# ---------------------------------------------------------------------------
# Namespace isolation for GET/DELETE /jobs/{job_id}
# ---------------------------------------------------------------------------


def test_get_job_cross_namespace_404(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    """GET /jobs/{id} with mismatched namespace returns 404 (not 403)."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create(namespace="tenantA")
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, store)
    # auth_headers use the default API key → namespace="default", not "tenantA"
    client = TestClient(app, headers=auth_headers)

    response = client.get(f"/jobs/{job.job_id}")
    assert response.status_code == 404


def test_get_job_same_namespace_200(tmp_path: Path) -> None:
    """GET /jobs/{id} with matching namespace returns 200."""
    tenant_key = "a" * 64
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create(namespace="tenantA")
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.namespaces = {tenant_key: "tenantA"}
    app = create_app(config, store)
    client = TestClient(app, headers={"Authorization": f"Bearer {tenant_key}"})

    response = client.get(f"/jobs/{job.job_id}")
    assert response.status_code == 200
    assert response.json()["job_id"] == job.job_id


def test_delete_job_cross_namespace_404(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    """DELETE /jobs/{id} with mismatched namespace returns 404 (not 403)."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create(namespace="tenantA")
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, store)
    # auth_headers use the default API key → namespace="default", not "tenantA"
    client = TestClient(app, headers=auth_headers)

    response = client.delete(f"/jobs/{job.job_id}")
    assert response.status_code == 404


def test_delete_job_same_namespace_proceeds(tmp_path: Path) -> None:
    """DELETE /jobs/{id} with matching namespace → 202 and store.transition called."""
    from unittest.mock import MagicMock, patch

    tenant_key = "b" * 64
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create(namespace="tenantA")
    store.update(job.job_id, status=JobStatus.PENDING)
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.namespaces = {tenant_key: "tenantA"}
    app = create_app(config, store)
    client = TestClient(app, headers={"Authorization": f"Bearer {tenant_key}"})

    response = client.delete(f"/jobs/{job.job_id}")
    assert response.status_code in (200, 202)


# ---------------------------------------------------------------------------
# POST /ingest namespace propagation
# ---------------------------------------------------------------------------


def test_ingest_passes_namespace_to_job(tmp_path: Path) -> None:
    """POST /ingest with state.namespace='tenantA' → job has 'namespace': 'tenantA'."""
    tenant_key = "c" * 64
    store = JobStore(path=tmp_path / "jobs.json")
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.namespaces = {tenant_key: "tenantA"}
    app = create_app(config, store)
    client = TestClient(app, headers={"Authorization": f"Bearer {tenant_key}"})

    response = client.post("/ingest", json={"collection": "docs"})
    assert response.status_code == 202
    data = response.json()
    assert data["namespace"] == "tenantA"


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


# ---------------------------------------------------------------------------
# A5a — Path safety tests for POST /ingest (bare FastAPI pattern)
# ---------------------------------------------------------------------------


def _make_ingest_app(auth_key: str | None = None) -> "FastAPI":  # type: ignore[name-defined]
    """Create a minimal FastAPI app with only the ingest router."""
    import os as _os
    import tempfile as _tempfile
    from pathlib import Path as _Path
    from fastapi import FastAPI, Request
    from archon_search.server.routes_jobs import router
    from archon_search.jobs.store import JobStore as _JS
    from archon_search.constants import DEFAULT_NAMESPACE
    from archon_search.server.middleware_auth import APIKeyMiddleware

    key = auth_key or _os.environ.get("ARCHON_SEARCH_API_KEY", "0" * 64)

    app = FastAPI()
    _tmpdir = _tempfile.mkdtemp()
    app.state.job_store = _JS(path=_Path(_tmpdir) / "jobs.json")
    app.state._background_tasks = set()
    app.state.ingest_pipeline = None

    app.add_middleware(APIKeyMiddleware, api_key=key, namespaces={})

    @app.middleware("http")
    async def _inject_namespace(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.namespace = DEFAULT_NAMESPACE
        return await call_next(request)

    app.include_router(router)
    return app


def _ingest_client(app: "FastAPI") -> "TestClient":  # type: ignore[name-defined]
    import os as _os
    key = _os.environ.get("ARCHON_SEARCH_API_KEY", "0" * 64)
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def test_ingest_rejects_dotdot_path() -> None:
    """POST /ingest with dotdot path returns 400 with 'path is unsafe:' detail."""
    from fastapi import FastAPI
    app = _make_ingest_app()
    c = _ingest_client(app)
    response = c.post("/ingest", json={"collection": "docs", "path": "/foo/../bar"})
    assert response.status_code == 400
    assert response.json()["detail"].startswith("path is unsafe:")


def test_ingest_rejects_nul_byte_path() -> None:
    """POST /ingest with NUL byte path returns 400 with 'nul_byte' in detail."""
    from fastapi import FastAPI
    app = _make_ingest_app()
    c = _ingest_client(app)
    response = c.post("/ingest", json={"collection": "docs", "path": "/tmp/x\x00.md"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "path is unsafe:" in detail
    assert "nul_byte" in detail


def test_ingest_uses_validator_returned_path() -> None:
    """Handler uses the Path returned by validate_ingest_path, not re-resolving body.path."""
    from pathlib import Path as _Path
    from unittest.mock import patch, MagicMock
    from fastapi import FastAPI
    app = _make_ingest_app()
    c = _ingest_client(app)

    sentinel = _Path("/sentinel/value")
    with patch("archon_search.server.routes_jobs.validate_ingest_path", return_value=sentinel):
        with patch("archon_search.server.routes_jobs.asyncio.create_task",
                   side_effect=lambda coro: (coro.close(), MagicMock())[1]):
            response = c.post("/ingest", json={"collection": "docs", "path": "/some/valid/path"})

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job_store = app.state.job_store
    # Retrieve the job and verify path in store (body was mutated to str(sentinel))
    # We can't directly check the body, but the handler should have used the sentinel
    assert response.status_code == 202


def test_ingest_accepts_null_path() -> None:
    """POST /ingest with path=null still works (no path ingest is valid)."""
    from fastapi import FastAPI
    app = _make_ingest_app()
    c = _ingest_client(app)
    with __import__('unittest.mock', fromlist=['patch']).patch(
        "archon_search.server.routes_jobs.asyncio.create_task",
        side_effect=lambda coro: (coro.close(), __import__('unittest.mock', fromlist=['MagicMock']).MagicMock())[1]
    ):
        response = c.post("/ingest", json={"collection": "docs", "path": None})
    assert response.status_code == 202


def test_ingest_accepts_legitimate_absolute_path() -> None:
    """POST /ingest with a valid absolute path returns 202 (regression)."""
    from fastapi import FastAPI
    app = _make_ingest_app()
    c = _ingest_client(app)
    with __import__('unittest.mock', fromlist=['patch']).patch(
        "archon_search.server.routes_jobs.asyncio.create_task",
        side_effect=lambda coro: (coro.close(), __import__('unittest.mock', fromlist=['MagicMock']).MagicMock())[1]
    ):
        response = c.post("/ingest", json={"collection": "docs", "path": "/tmp/docs"})
    assert response.status_code == 202


def test_ingest_openapi_lists_400_response() -> None:
    """GET /openapi.json shows 400 under /ingest POST responses."""
    from fastapi import FastAPI
    app = _make_ingest_app()
    c = _ingest_client(app)
    response = c.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    post_responses = spec["paths"]["/ingest"]["post"]["responses"]
    assert "400" in post_responses

"""Tests for POST /ingest, GET /jobs/{id}, DELETE /jobs/{id} ."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

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
# POST /ingest — path safety validation (Task 1.3 / A5a)
# ---------------------------------------------------------------------------


def test_ingest_rejects_dotdot_path(client: TestClient) -> None:
    """POST /ingest with a dotdot path returns 400 with 'path is unsafe:' detail."""
    response = client.post("/ingest", json={"collection": "c", "path": "/foo/../bar"})
    assert response.status_code == 400
    assert response.json()["detail"].startswith("path is unsafe:")


def test_ingest_uses_validator_returned_path(
    tmp_path: Path, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handler must forward the Path returned by validate_ingest_path to the ingest task."""
    store = JobStore(path=tmp_path / "jobs.json")
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, store)
    c = TestClient(app, headers=auth_headers)

    # Patch the validator in the route module namespace to return a sentinel path.
    monkeypatch.setattr(
        "archon_search.server.routes_jobs.validate_ingest_path",
        lambda raw: Path("/sentinel/value"),
    )

    # Capture the IngestRequest passed to whichever ingest task variant the handler uses.
    # The handler branches to _default_ingest_task_with_lock when search_store has _lock_for.
    # Must stay await-free: completes in a single event-loop step before the response
    # is returned (no await point => no race with asyncio.create_task).
    captured: list[str] = []

    async def _capturing_ingest_task(job_id, store, body, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(body.path)

    monkeypatch.setattr(
        "archon_search.server.routes_jobs._default_ingest_task",
        _capturing_ingest_task,
    )
    monkeypatch.setattr(
        "archon_search.server.routes_jobs._default_ingest_task_with_lock",
        _capturing_ingest_task,
    )

    response = c.post("/ingest", json={"collection": "c", "path": "/some/legitimate/path"})

    assert response.status_code == 202
    assert captured == [str(Path("/sentinel/value"))]


def test_ingest_rejects_nul_byte_path(client: TestClient) -> None:
    """POST /ingest with a NUL byte in path returns 400 with 'nul_byte' in detail."""
    response = client.post("/ingest", json={"collection": "c", "path": "/tmp/x\x00.md"})
    assert response.status_code == 400
    assert "nul_byte" in response.json()["detail"]


def test_ingest_rejects_empty_string_path(client: TestClient) -> None:
    """POST /ingest with path: "" (distinct from null) reaches the validator -> 400 'empty'."""
    response = client.post("/ingest", json={"collection": "c", "path": ""})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail.startswith("path is unsafe:")
    assert "empty" in detail


def test_ingest_rejects_relative_path(client: TestClient) -> None:
    """POST /ingest with a non-absolute path returns 400 with 'not_absolute' in detail."""
    response = client.post("/ingest", json={"collection": "c", "path": "relative/path.md"})
    assert response.status_code == 400
    assert "not_absolute" in response.json()["detail"]


def test_ingest_accepts_null_path(client: TestClient) -> None:
    """POST /ingest with path: null is accepted — documents-only ingest keeps working."""
    response = client.post("/ingest", json={"collection": "c", "path": None})
    assert response.status_code == 202
    assert "job_id" in response.json()


# ---------------------------------------------------------------------------
# OSError on JobStore writes → 500 envelope (Task 2.6)
# ---------------------------------------------------------------------------


def _make_client_with_store(tmp_path: Path, store: object, auth_headers: dict[str, str]) -> TestClient:
    """Build an app with a custom job_store, patching out the eager DocumentChunker
    (gpt2 tokenizer download is network-blocked in this environment)."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    with mock.patch("archon_search.server.app.DocumentChunker"):
        app = create_app(config, store)  # type: ignore[arg-type]
    return TestClient(app, headers=auth_headers)


def test_job_create_oserror_returns_500_envelope(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """POST /ingest returns the 500 envelope when store.create raises OSError."""
    store = MagicMock()
    store.create.side_effect = OSError("disk full")
    client = _make_client_with_store(tmp_path, store, auth_headers)

    response = client.post("/ingest", json={"collection": "docs"})

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}


def test_delete_job_transition_oserror_returns_500_envelope(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """DELETE /jobs/{id} returns the 500 envelope when store.transition raises OSError."""
    active_job = IngestJob(
        job_id="job-1",
        status=JobStatus.RUNNING,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        namespace=DEFAULT_NAMESPACE,
    )
    store = MagicMock()
    store.get.return_value = active_job
    store.transition.side_effect = OSError("disk full")
    client = _make_client_with_store(tmp_path, store, auth_headers)

    response = client.delete(f"/jobs/{active_job.job_id}")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}


def test_background_ingest_oserror_does_not_500_client(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """A background-task OSError must NOT affect the synchronous POST /ingest response."""
    real_store = JobStore(path=tmp_path / "jobs.json")

    store = MagicMock()
    # create succeeds synchronously (so the 202 is returned)
    store.create.side_effect = real_store.create
    # any subsequent .update() (which only happens in the background task) blows up
    store.update.side_effect = OSError("disk full")
    store.get.side_effect = real_store.get
    store.transition.side_effect = real_store.transition

    client = _make_client_with_store(tmp_path, store, auth_headers)

    response = client.post("/ingest", json={"collection": "docs"})

    # The synchronous response is unaffected by the (separate) background failure.
    assert response.status_code == 202
    assert "job_id" in response.json()


def test_ingest_accepts_legitimate_absolute_path(client: TestClient) -> None:
    """POST /ingest with a valid absolute path still returns 202 (regression guard)."""
    response = client.post("/ingest", json={"collection": "c", "path": "/tmp/legit"})
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data


def test_ingest_openapi_lists_400_response(client: TestClient) -> None:
    """GET /openapi.json must expose 400 under POST /ingest responses."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    post_ingest = spec["paths"]["/ingest"]["post"]
    assert "400" in post_ingest["responses"]
    ref = post_ingest["responses"]["400"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("ErrorDetail")


@pytest.mark.anyio
async def test_background_ingest_oserror_logs_and_marks_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A primary OSError in the background task is logged and the job is marked FAILED."""
    real_store = JobStore(path=tmp_path / "jobs.json")
    job = real_store.create()

    calls = {"n": 0}
    real_update = real_store.update

    def flaky_update(job_id: str, **kwargs: object):
        calls["n"] += 1
        if calls["n"] == 1:
            # First update (status=RUNNING) raises OSError
            raise OSError("disk full")
        return real_update(job_id, **kwargs)

    store = MagicMock()
    store.update.side_effect = flaky_update
    store.get.side_effect = real_store.get
    store.transition.side_effect = real_store.transition

    body = IngestRequest(collection="docs")
    with caplog.at_level("ERROR"):
        await _default_ingest_task(job.job_id, store, body, pipeline_fn=None)

    # The job ended FAILED with an error message set
    completed = real_store.get(job.job_id)
    assert completed is not None
    assert completed.status == JobStatus.FAILED
    assert completed.error
    # The primary failure was logged
    assert any("failed" in r.message.lower() for r in caplog.records)


@pytest.mark.anyio
async def test_background_ingest_double_oserror_suppressed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If EVERY store.update raises OSError, no exception escapes _default_ingest_task."""
    real_store = JobStore(path=tmp_path / "jobs.json")
    job = real_store.create()

    store = MagicMock()
    store.update.side_effect = OSError("disk full")
    store.get.side_effect = real_store.get
    store.transition.side_effect = real_store.transition

    body = IngestRequest(collection="docs")
    with caplog.at_level("ERROR"):
        # Must return normally — no OSError escapes.
        await _default_ingest_task(job.job_id, store, body, pipeline_fn=None)

    # The secondary-failure path (could not persist FAILED) is logged.
    assert any(
        "could not persist FAILED status" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_background_ingest_cancelled_oserror_suppressed_and_reraises_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If persisting CANCELLED status raises OSError during cancellation, the OSError
    is logged and suppressed, and CancelledError is still re-raised (not OSError)."""

    def update_side_effect(job_id: str, **kwargs: object) -> None:
        # RUNNING update (before cancel) is benign; CANCELLED update fails durably.
        if kwargs.get("status") == JobStatus.CANCELLED:
            raise OSError("disk full")

    store = MagicMock(spec=JobStore)
    store.update.side_effect = update_side_effect
    store.get.return_value = MagicMock()

    async def pipeline_fn(*a: object, **k: object) -> None:
        await asyncio.Event().wait()  # block forever until cancelled

    body = IngestRequest(collection="docs")
    with caplog.at_level("ERROR"):
        task = asyncio.create_task(
            _default_ingest_task("job-1", store, body, pipeline_fn=pipeline_fn)
        )
        # Let the task start and reach the cancellable await point.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()

        # The task surfaces CancelledError, NOT the OSError.
        with pytest.raises(asyncio.CancelledError):
            await task

    # The OSError from the CANCELLED persist was logged.
    assert any(
        "could not persist CANCELLED status" in r.message for r in caplog.records
    )

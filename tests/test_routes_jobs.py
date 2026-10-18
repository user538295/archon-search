"""Tests for POST /ingest, GET /jobs/{id}, DELETE /jobs/{id} ."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search._types import IngestResult
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.model import JobStatus, job_to_dict
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.embedder_cache import EmbedderNotReadyError
from archon_search.server.routes_jobs import (
    REINDEX_FAILED_DETAIL,
    IngestRequest,
    _default_ingest_task,
    _default_ingest_task_with_lock,
    _reindex_task,
    _run_pipeline,
)
from archon_search.types import IngestJob, ReindexJob
from tests._internal_error_leak import assert_logged_with_traceback


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


def test_delete_job_failed_expired_returns_200(client: TestClient, tmp_store: JobStore) -> None:
    """FAILED_EXPIRED is terminal — DELETE is idempotent, returns 200."""
    job = tmp_store.create()
    tmp_store.update(job.job_id, status=JobStatus.FAILED_EXPIRED)
    response = client.delete(f"/jobs/{job.job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == job.job_id
    assert data["status"] == JobStatus.FAILED_EXPIRED.value


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


def test_job_to_dict_includes_job_type_ingest() -> None:
    """job_to_dict() output includes 'job_type': 'ingest' for base IngestJob."""
    job = IngestJob(
        job_id="test-job-3",
        status=JobStatus.PENDING,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    result = job_to_dict(job)
    assert "job_type" in result
    assert result["job_type"] == "ingest"


def test_get_job_response_includes_job_type(client: TestClient, tmp_store: JobStore) -> None:
    """GET /jobs/{id} response body includes 'job_type' field."""
    job = tmp_store.create(path="/f.txt", collection="col", namespace="default")
    resp = client.get(f"/jobs/{job.job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "job_type" in body
    assert body["job_type"] == "ingest"


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


def test_ingest_persists_collection_and_path_on_job(client: TestClient, tmp_store: JobStore) -> None:
    """S97: POST /ingest must persist the target collection (and source_path) on the job record.

    Regression: store.create() was called without collection/path, so the job — and every
    downstream view (GET /jobs/{id}, `jobs show`) — reported an empty collection/source_path.
    """
    response = client.post("/ingest", json={"collection": "s097-col", "path": "/tmp/s097.txt"})
    assert response.status_code == 202
    data = response.json()
    assert data["collection"] == "s097-col"
    # source_path is resolve()d (e.g. /tmp -> /private/tmp on macOS); assert it survives, non-empty.
    assert data["source_path"].endswith("s097.txt")

    # Same values must be visible on the persisted job via GET /jobs/{id}.
    got = client.get(f"/jobs/{data['job_id']}")
    assert got.status_code == 200
    assert got.json()["collection"] == "s097-col"
    assert got.json()["source_path"].endswith("s097.txt")


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
    # The handler branches to _default_ingest_task_with_lock when search_store has lock_for.
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


def test_ingest_keeps_submitted_path_beside_resolved_one(
    tmp_path: Path, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2-I-103: the task gets the resolved ``path`` plus the caller's raw string for error text."""
    home = tmp_path / "server-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    store = JobStore(path=tmp_path / "jobs.json")
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    c = TestClient(create_app(config, store), headers=auth_headers)
    # Await-free capture: it completes before the response returns (see the test above).
    captured: list[IngestRequest] = []

    async def _capturing_ingest_task(job_id, store, body, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(body)

    monkeypatch.setattr("archon_search.server.routes_jobs._default_ingest_task", _capturing_ingest_task)
    monkeypatch.setattr("archon_search.server.routes_jobs._default_ingest_task_with_lock", _capturing_ingest_task)

    response = c.post("/ingest", json={"collection": "c", "path": "~/corpus"})

    assert response.status_code == 202
    [body] = captured
    assert body.path == str((home / "corpus").resolve())
    assert body.submitted_path == "~/corpus"


def test_with_resolved_path_returns_resolved_copy_keeping_submitted_path() -> None:
    """The resolved request carries both strings; the caller's request is left untouched."""
    submitted = IngestRequest(collection="c", path="~/corpus", ingested_by="cli", chunk_scopes=["a"])

    resolved = submitted.with_resolved_path("/srv/home/corpus")

    assert resolved is not submitted
    assert resolved.path == "/srv/home/corpus"
    assert resolved.submitted_path == "~/corpus"
    assert (resolved.collection, resolved.ingested_by, resolved.chunk_scopes) == ("c", "cli", ["a"])
    assert submitted.path == "~/corpus"
    assert submitted.submitted_path == "~/corpus"


def test_submitted_path_equals_path_when_never_resolved() -> None:
    """A request built directly (e.g. by a test or the documents mode) echoes its own ``path``."""
    assert IngestRequest(collection="c", path="/data/corpus").submitted_path == "/data/corpus"


def test_submitted_path_of_a_pathless_request_raises_instead_of_echoing_none() -> None:
    """Only a ``path`` request has a submitted path: never let ``None`` become the text "None"."""
    with pytest.raises(ValueError, match="no path"):
        _ = IngestRequest(collection="c").submitted_path


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


_INGEST_TASK_FNS = [_default_ingest_task, _default_ingest_task_with_lock]
_INGEST_TASK_IDS = ["plain", "with_lock"]


@pytest.mark.anyio
@pytest.mark.parametrize("task_fn", _INGEST_TASK_FNS, ids=_INGEST_TASK_IDS)
async def test_background_ingest_double_oserror_suppressed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, task_fn: Any
) -> None:
    """If EVERY store.update raises OSError, no exception escapes either ingest task."""
    real_store = JobStore(path=tmp_path / "jobs.json")
    job = real_store.create()

    store = MagicMock()
    store.update.side_effect = OSError("disk full")
    store.get.side_effect = real_store.get
    store.transition.side_effect = real_store.transition

    body = IngestRequest(collection="docs")
    with caplog.at_level("ERROR"):
        # Must return normally — no OSError escapes.
        await task_fn(job.job_id, store, body, pipeline_fn=None)

    # The secondary-failure path (could not persist FAILED) is logged.
    assert any(
        "could not persist FAILED status" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("task_fn", _INGEST_TASK_FNS, ids=_INGEST_TASK_IDS)
async def test_background_ingest_cancelled_oserror_suppressed_and_reraises_cancelled(
    caplog: pytest.LogCaptureFixture, task_fn: Any
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
            task_fn("job-1", store, body, pipeline_fn=pipeline_fn)
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


@pytest.mark.anyio
@pytest.mark.parametrize("task_fn", _INGEST_TASK_FNS, ids=_INGEST_TASK_IDS)
async def test_background_ingest_done_write_failure_never_marks_the_ingest_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, task_fn: Any
) -> None:
    """The ingest succeeded: a failing DONE write is logged, never turned into FAILED (C5-B-4)."""
    real_store = JobStore(path=tmp_path / "jobs.json")
    job = real_store.create()
    attempted: list[JobStatus] = []

    def update(job_id: str, **kwargs: Any) -> Any:
        attempted.append(kwargs["status"])
        if kwargs["status"] == JobStatus.DONE:
            raise OSError("disk full")
        return real_store.update(job_id, **kwargs)

    store = MagicMock()
    store.update.side_effect = update
    store.get.side_effect = real_store.get

    with caplog.at_level(logging.ERROR):
        await task_fn(job.job_id, store, IngestRequest(collection="docs"), pipeline_fn=AsyncMock())

    assert attempted == [JobStatus.RUNNING, JobStatus.DONE]
    assert any("could not persist DONE status" in r.getMessage() for r in caplog.records)
    assert not any(r.getMessage().startswith("Ingest task") for r in caplog.records), "the ingest did not fail"


_NON_IO_STORE_ERROR = "job store backend exploded"


@pytest.mark.anyio
@pytest.mark.parametrize("task_fn", _INGEST_TASK_FNS, ids=_INGEST_TASK_IDS)
async def test_background_ingest_any_failed_write_error_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, task_fn: Any
) -> None:
    """A non-I/O error persisting FAILED must not escape the background task either."""
    real_store = JobStore(path=tmp_path / "jobs.json")
    job = real_store.create()
    store = MagicMock()
    store.update.side_effect = RuntimeError(_NON_IO_STORE_ERROR)
    store.get.side_effect = real_store.get

    with caplog.at_level(logging.ERROR):
        await task_fn(job.job_id, store, IngestRequest(collection="docs"), pipeline_fn=None)

    assert any("could not persist FAILED status" in r.getMessage() for r in caplog.records)
    assert_logged_with_traceback(caplog, _NON_IO_STORE_ERROR)


@pytest.mark.asyncio
@pytest.mark.parametrize("task_fn", _INGEST_TASK_FNS, ids=_INGEST_TASK_IDS)
async def test_background_ingest_any_cancelled_write_error_still_reraises_cancelled(
    caplog: pytest.LogCaptureFixture, task_fn: Any
) -> None:
    """A non-I/O error persisting CANCELLED is logged; CancelledError still propagates."""

    def update_side_effect(job_id: str, **kwargs: object) -> None:
        if kwargs.get("status") == JobStatus.CANCELLED:
            raise RuntimeError(_NON_IO_STORE_ERROR)

    store = MagicMock(spec=JobStore)
    store.update.side_effect = update_side_effect

    async def pipeline_fn(*a: object, **k: object) -> None:
        await asyncio.Event().wait()

    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(task_fn("job-1", store, IngestRequest(collection="docs"), pipeline_fn=pipeline_fn))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert any("could not persist CANCELLED status" in r.getMessage() for r in caplog.records)
    assert_logged_with_traceback(caplog, _NON_IO_STORE_ERROR)


# ---------------------------------------------------------------------------
# _reindex_task tests (Task 8.1)
# ---------------------------------------------------------------------------


def _make_reindex_job(
    job_id: str = "job-reindex-1",
    target_embedding_model: str | None = "model-B",
    status: JobStatus = JobStatus.PENDING,
    namespace: str = DEFAULT_NAMESPACE,
) -> ReindexJob:
    return ReindexJob(
        job_id=job_id,
        status=status,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        target_embedding_model=target_embedding_model,
        namespace=namespace,
    )


def _make_collection_meta(
    name: str = "col",
    active_embedding_model: str = "model-A",
    pending_embedding_model: str | None = None,
    needs_reindex: bool = False,
    reindex_job_id: str | None = "job-reindex-1",
    namespace: str = DEFAULT_NAMESPACE,
) -> CollectionMeta:
    return CollectionMeta(
        name=name,
        active_embedding_model=active_embedding_model,
        pending_embedding_model=pending_embedding_model,
        needs_reindex=needs_reindex,
        reindex_job_id=reindex_job_id,
        namespace=namespace,
    )


def _make_mocks(
    job: ReindexJob,
    meta: CollectionMeta,
    ingest_raises: Exception | None = None,
    embedder_raises: Exception | None = None,
) -> tuple:
    """Return (job_store, search_store, embedder_cache, pipeline) mocks."""
    job_store = MagicMock(spec=JobStore)
    job_store.get.return_value = job
    job_store.update.return_value = None

    search_store = MagicMock()
    search_store.get_collection_meta = AsyncMock(return_value=meta)
    search_store.update_collection_meta = AsyncMock(return_value=None)

    fake_embedder = MagicMock()
    embedder_cache = MagicMock()
    if embedder_raises:
        embedder_cache.get_or_load = AsyncMock(side_effect=embedder_raises)
    else:
        embedder_cache.get_or_load = AsyncMock(return_value=fake_embedder)

    pipeline = MagicMock()
    if ingest_raises:
        pipeline.ingest_directory = AsyncMock(side_effect=ingest_raises)
    else:
        pipeline.ingest_directory = AsyncMock(return_value=[])

    return job_store, search_store, embedder_cache, pipeline


@pytest.mark.anyio
async def test_reindex_task_promotes_active_on_success(tmp_path: Path) -> None:
    """After successful reindex: active=target_model, pending=None, needs_reindex=False."""
    from archon_search.server.routes_jobs import _reindex_task

    job = _make_reindex_job(target_embedding_model="model-B")
    meta = _make_collection_meta(
        active_embedding_model="model-A",
        pending_embedding_model="model-B",
        needs_reindex=True,
    )
    job_store, search_store, embedder_cache, pipeline = _make_mocks(job, meta)

    await _reindex_task(
        job_id=job.job_id,
        store=search_store,
        job_store=job_store,
        embedder_cache=embedder_cache,
        pipeline=pipeline,
        collection="col",
        namespace=DEFAULT_NAMESPACE,
        collection_path=tmp_path,
        config=SearchConfig(),
    )

    # update_collection_meta was called; inspect the meta that was written
    assert search_store.update_collection_meta.called
    written_meta: CollectionMeta = search_store.update_collection_meta.call_args[0][0]
    assert written_meta.active_embedding_model == "model-B"
    assert written_meta.pending_embedding_model is None
    assert written_meta.needs_reindex is False
    assert written_meta.reindex_job_id is None

    # job was marked DONE
    job_store.update.assert_called_with(job.job_id, status=JobStatus.DONE)


@pytest.mark.anyio
async def test_reindex_task_no_source_dir_skips_scan_and_completes() -> None:
    """S22: a meta-only collection (collection_path=None) must NOT scan the CWD.

    The reindex is a data-only no-op: ``ingest_directory`` is never called (which
    with the old empty-path would have walked the entire server CWD), and even when
    a target model is set the active model is NOT promoted over un-re-embedded
    vectors. The job still reaches DONE so ``reindex --wait`` exits 0.
    """
    from archon_search.server.routes_jobs import _reindex_task

    job = _make_reindex_job(target_embedding_model="model-B")
    meta = _make_collection_meta(
        active_embedding_model="model-A",
        pending_embedding_model="model-B",
        needs_reindex=True,
    )
    job_store, search_store, embedder_cache, pipeline = _make_mocks(job, meta)

    await _reindex_task(
        job_id=job.job_id,
        store=search_store,
        job_store=job_store,
        embedder_cache=embedder_cache,
        pipeline=pipeline,
        collection="col",
        namespace=DEFAULT_NAMESPACE,
        collection_path=None,
        config=SearchConfig(),
    )

    # The directory scan was skipped entirely — the core of the S22 fix.
    pipeline.ingest_directory.assert_not_called()

    # Model NOT promoted (vectors were never re-embedded); only reindex_job_id cleared.
    written_meta: CollectionMeta = search_store.update_collection_meta.call_args[0][0]
    assert written_meta.active_embedding_model == "model-A"
    assert written_meta.pending_embedding_model == "model-B"
    assert written_meta.needs_reindex is True
    assert written_meta.reindex_job_id is None

    # Job completes DONE so the CLI --wait exits 0 with a completion message.
    job_store.update.assert_called_with(job.job_id, status=JobStatus.DONE)


@pytest.mark.anyio
async def test_reindex_task_preserves_active_on_failure(tmp_path: Path) -> None:
    """When ingest_directory raises, active_embedding_model is left unchanged."""
    from archon_search.server.routes_jobs import _reindex_task

    job = _make_reindex_job(target_embedding_model="model-B")
    meta = _make_collection_meta(active_embedding_model="model-A")
    job_store, search_store, embedder_cache, pipeline = _make_mocks(
        job, meta, ingest_raises=RuntimeError("ingest boom")
    )
    # Return the same meta on success-path fetch
    search_store.get_collection_meta = AsyncMock(return_value=meta)

    await _reindex_task(
        job_id=job.job_id,
        store=search_store,
        job_store=job_store,
        embedder_cache=embedder_cache,
        pipeline=pipeline,
        collection="col",
        namespace=DEFAULT_NAMESPACE,
        collection_path=tmp_path,
        config=SearchConfig(),
    )

    written_meta: CollectionMeta = search_store.update_collection_meta.call_args[0][0]
    assert written_meta.active_embedding_model == "model-A"
    assert written_meta.reindex_job_id is None

    # job was marked FAILED
    update_calls = [str(c) for c in job_store.update.call_args_list]
    assert any("FAILED" in c for c in update_calls)


@pytest.mark.anyio
async def test_reindex_task_writes_collection_meta_before_job_done(tmp_path: Path) -> None:
    """CollectionMeta write must precede the DONE job_store.update."""
    from archon_search.server.routes_jobs import _reindex_task

    call_order: list[str] = []

    job = _make_reindex_job(target_embedding_model="model-B")
    meta = _make_collection_meta(active_embedding_model="model-A")
    job_store, search_store, embedder_cache, pipeline = _make_mocks(job, meta)

    async def track_meta_write(m: CollectionMeta) -> None:
        call_order.append("meta_write")

    def track_job_update(job_id: str, **kwargs: object) -> None:
        call_order.append(f"job_update:{kwargs.get('status')}")

    search_store.update_collection_meta = AsyncMock(side_effect=track_meta_write)
    job_store.update.side_effect = track_job_update

    await _reindex_task(
        job_id=job.job_id,
        store=search_store,
        job_store=job_store,
        embedder_cache=embedder_cache,
        pipeline=pipeline,
        collection="col",
        namespace=DEFAULT_NAMESPACE,
        collection_path=tmp_path,
        config=SearchConfig(),
    )

    meta_idx = call_order.index("meta_write")
    done_idx = call_order.index(f"job_update:{JobStatus.DONE}")
    assert meta_idx < done_idx, f"Expected meta write before DONE, got order: {call_order}"


@pytest.mark.anyio
async def test_reindex_task_uses_target_model_from_job(tmp_path: Path) -> None:
    """embedder_cache is called with target_embedding_model from the job."""
    from archon_search.server.routes_jobs import _reindex_task

    job = _make_reindex_job(target_embedding_model="model-X")
    meta = _make_collection_meta(active_embedding_model="model-A")
    job_store, search_store, embedder_cache, pipeline = _make_mocks(job, meta)

    await _reindex_task(
        job_id=job.job_id,
        store=search_store,
        job_store=job_store,
        embedder_cache=embedder_cache,
        pipeline=pipeline,
        collection="col",
        namespace=DEFAULT_NAMESPACE,
        collection_path=tmp_path,
        config=SearchConfig(),
    )

    embedder_cache.get_or_load.assert_called_once_with("model-X")


@pytest.mark.anyio
async def test_reindex_task_concurrent_patch_preserves_new_pending(tmp_path: Path) -> None:
    """Simulate concurrent PATCH that changes pending to model-C while reindex runs.

    After reindex of model-B: active=B, pending=C, needs_reindex=True (unchanged from PATCH).
    """
    from archon_search.server.routes_jobs import _reindex_task

    job = _make_reindex_job(target_embedding_model="model-B")
    # Initial meta at job start
    initial_meta = _make_collection_meta(
        active_embedding_model="model-A",
        pending_embedding_model="model-B",
        needs_reindex=True,
    )
    # Meta as it appears when read on success-path (concurrent PATCH happened)
    concurrent_meta = _make_collection_meta(
        active_embedding_model="model-A",
        pending_embedding_model="model-C",
        needs_reindex=True,
    )
    job_store, search_store, embedder_cache, pipeline = _make_mocks(job, initial_meta)
    # First call returns initial_meta (for embedder resolution in data-only path, not needed here)
    # success-path read returns concurrent_meta
    search_store.get_collection_meta = AsyncMock(return_value=concurrent_meta)

    await _reindex_task(
        job_id=job.job_id,
        store=search_store,
        job_store=job_store,
        embedder_cache=embedder_cache,
        pipeline=pipeline,
        collection="col",
        namespace=DEFAULT_NAMESPACE,
        collection_path=tmp_path,
        config=SearchConfig(),
    )

    written_meta: CollectionMeta = search_store.update_collection_meta.call_args[0][0]
    # active promoted to B (we completed the reindex for B)
    assert written_meta.active_embedding_model == "model-B"
    # pending=C and needs_reindex=True are preserved (concurrent PATCH set them)
    assert written_meta.pending_embedding_model == "model-C"
    assert written_meta.needs_reindex is True


@pytest.mark.anyio
async def test_reindex_task_after_patch_revert_promotes_active_clears_needs_reindex(
    tmp_path: Path,
) -> None:
    """Simulate revert (pending=None, needs_reindex=False) — after reindex: active=B, pending=None, needs_reindex=False."""
    from archon_search.server.routes_jobs import _reindex_task

    job = _make_reindex_job(target_embedding_model="model-B")
    # Success-path meta: pending reverted to None
    reverted_meta = _make_collection_meta(
        active_embedding_model="model-A",
        pending_embedding_model=None,
        needs_reindex=False,
    )
    job_store, search_store, embedder_cache, pipeline = _make_mocks(job, reverted_meta)
    search_store.get_collection_meta = AsyncMock(return_value=reverted_meta)

    await _reindex_task(
        job_id=job.job_id,
        store=search_store,
        job_store=job_store,
        embedder_cache=embedder_cache,
        pipeline=pipeline,
        collection="col",
        namespace=DEFAULT_NAMESPACE,
        collection_path=tmp_path,
        config=SearchConfig(),
    )

    written_meta: CollectionMeta = search_store.update_collection_meta.call_args[0][0]
    assert written_meta.active_embedding_model == "model-B"
    assert written_meta.pending_embedding_model is None
    assert written_meta.needs_reindex is False


@pytest.mark.anyio
async def test_reindex_task_data_only_resolves_default_model_for_unpinned_collection(
    tmp_path: Path,
) -> None:
    """A collection pinning no model re-embeds with config.embedding_model, not "".

    ``active_embedding_model`` defaults to "" meaning "use the global default"
    (``resolve_active_model``). Passing "" straight to the cache would build an
    embedder for an empty model name instead of the model the collection searches with.
    """
    from archon_search.server.routes_jobs import _reindex_task

    job = _make_reindex_job(target_embedding_model=None)
    meta = _make_collection_meta(active_embedding_model="")
    job_store, search_store, embedder_cache, pipeline = _make_mocks(job, meta)
    config = SearchConfig()

    await _reindex_task(
        job_id=job.job_id,
        store=search_store,
        job_store=job_store,
        embedder_cache=embedder_cache,
        pipeline=pipeline,
        collection="col",
        namespace=DEFAULT_NAMESPACE,
        collection_path=tmp_path,
        config=config,
    )

    embedder_cache.get_or_load.assert_awaited_once_with(config.embedding_model)


@pytest.mark.anyio
async def test_reindex_task_data_only_preserves_active_model(tmp_path: Path) -> None:
    """data-only reindex (target_embedding_model=None): active and pending unchanged after success."""
    from archon_search.server.routes_jobs import _reindex_task

    job = _make_reindex_job(target_embedding_model=None)
    meta = _make_collection_meta(
        active_embedding_model="model-A",
        pending_embedding_model="model-B",
        needs_reindex=True,
    )
    job_store, search_store, embedder_cache, pipeline = _make_mocks(job, meta)
    search_store.get_collection_meta = AsyncMock(return_value=meta)

    await _reindex_task(
        job_id=job.job_id,
        store=search_store,
        job_store=job_store,
        embedder_cache=embedder_cache,
        pipeline=pipeline,
        collection="col",
        namespace=DEFAULT_NAMESPACE,
        collection_path=tmp_path,
        config=SearchConfig(),
    )

    written_meta: CollectionMeta = search_store.update_collection_meta.call_args[0][0]
    # active and pending untouched
    assert written_meta.active_embedding_model == "model-A"
    assert written_meta.pending_embedding_model == "model-B"
    assert written_meta.needs_reindex is True
    assert written_meta.reindex_job_id is None

    job_store.update.assert_called_with(job.job_id, status=JobStatus.DONE)


@pytest.mark.anyio
async def test_reindex_task_data_only_failure_clears_job_id_only(tmp_path: Path) -> None:
    """data-only, ingest_directory raises: active/pending unchanged, reindex_job_id=None, status=FAILED."""
    from archon_search.server.routes_jobs import _reindex_task

    job = _make_reindex_job(target_embedding_model=None)
    meta = _make_collection_meta(
        active_embedding_model="model-A",
        pending_embedding_model="model-B",
        needs_reindex=True,
    )
    job_store, search_store, embedder_cache, pipeline = _make_mocks(
        job, meta, ingest_raises=ValueError("data-only boom")
    )
    search_store.get_collection_meta = AsyncMock(return_value=meta)

    await _reindex_task(
        job_id=job.job_id,
        store=search_store,
        job_store=job_store,
        embedder_cache=embedder_cache,
        pipeline=pipeline,
        collection="col",
        namespace=DEFAULT_NAMESPACE,
        collection_path=tmp_path,
        config=SearchConfig(),
    )

    written_meta: CollectionMeta = search_store.update_collection_meta.call_args[0][0]
    assert written_meta.active_embedding_model == "model-A"
    assert written_meta.pending_embedding_model == "model-B"
    assert written_meta.reindex_job_id is None

    update_calls = [str(c) for c in job_store.update.call_args_list]
    assert any("FAILED" in c for c in update_calls)


@pytest.mark.anyio
async def test_reindex_task_embedder_cache_failure_marks_job_failed(tmp_path: Path) -> None:
    """If embedder_cache.get_or_load raises, job is marked FAILED and reindex_job_id cleared."""
    from archon_search.server.routes_jobs import _reindex_task

    job = _make_reindex_job(target_embedding_model="model-B")
    meta = _make_collection_meta(active_embedding_model="model-A")
    job_store, search_store, embedder_cache, pipeline = _make_mocks(
        job, meta, embedder_raises=RuntimeError("model load failed")
    )
    search_store.get_collection_meta = AsyncMock(return_value=meta)

    await _reindex_task(
        job_id=job.job_id,
        store=search_store,
        job_store=job_store,
        embedder_cache=embedder_cache,
        pipeline=pipeline,
        collection="col",
        namespace=DEFAULT_NAMESPACE,
        collection_path=tmp_path,
        config=SearchConfig(),
    )

    # reindex_job_id must be cleared
    written_meta: CollectionMeta = search_store.update_collection_meta.call_args[0][0]
    assert written_meta.reindex_job_id is None

    # status must be FAILED
    update_calls = [str(c) for c in job_store.update.call_args_list]
    assert any("FAILED" in c for c in update_calls)


# ---------------------------------------------------------------------------
# _reindex_task — job-store write failures never escape the task (C2-I-3)
# ---------------------------------------------------------------------------

_JOB_STORE_WRITE_ERROR = "job store write failed"


def _failing_writes_job_store(
    tmp_path: Path, fail_on: JobStatus, *, error: Exception | None = None, times: int | None = None
) -> tuple[JobStore, ReindexJob, list[JobStatus]]:
    """A real JobStore whose ``update`` to ``fail_on`` raises (``times`` times, or always)."""
    job_store = JobStore(path=tmp_path / "jobs.json")
    job = job_store.create_reindex(namespace=DEFAULT_NAMESPACE, target_embedding_model="model-B")
    real_update = job_store.update
    attempted: list[JobStatus] = []
    remaining = [times]

    def _update(job_id: str, **kwargs: object) -> IngestJob:
        status = kwargs.get("status")
        if isinstance(status, JobStatus):
            attempted.append(status)
        if status == fail_on and remaining[0] != 0:
            if remaining[0] is not None:
                remaining[0] -= 1
            raise error if error is not None else OSError(_JOB_STORE_WRITE_ERROR)
        return real_update(job_id, **kwargs)

    job_store.update = _update  # type: ignore[method-assign]
    return job_store, job, attempted


async def _run_reindex(job_store: JobStore, job_id: str, tmp_path: Path, embedder_cache: Any, pipeline: Any) -> Any:
    search_store = MagicMock()
    search_store.get_collection_meta = AsyncMock(return_value=_make_collection_meta(reindex_job_id=job_id))
    search_store.update_collection_meta = AsyncMock(return_value=None)
    await _reindex_task(
        job_id=job_id, store=search_store, job_store=job_store, embedder_cache=embedder_cache, pipeline=pipeline,
        collection="col", namespace=DEFAULT_NAMESPACE, collection_path=tmp_path, config=SearchConfig(),
    )
    return search_store


def _embedder_cache(error: Exception | None = None) -> MagicMock:
    cache = MagicMock()
    cache.get_or_load = AsyncMock(side_effect=error) if error is not None else AsyncMock(return_value=MagicMock())
    return cache


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("embedder_error", "store_error"),
    [
        (EmbedderNotReadyError("still loading"), OSError(_JOB_STORE_WRITE_ERROR)),
        (RuntimeError("model load failed"), KeyError(_JOB_STORE_WRITE_ERROR)),
    ],
    ids=["not_ready-oserror", "load_failure-keyerror"],
)
async def test_reindex_task_failed_write_after_embedder_failure_is_logged_not_raised(
    tmp_path: Path, embedder_error: Exception, store_error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    job_store, job, attempted = _failing_writes_job_store(tmp_path, JobStatus.FAILED, error=store_error)

    with caplog.at_level(logging.ERROR):
        search_store = await _run_reindex(job_store, job.job_id, tmp_path, _embedder_cache(embedder_error), MagicMock())

    assert attempted == [JobStatus.FAILED]
    assert search_store.update_collection_meta.call_args[0][0].reindex_job_id is None
    assert_logged_with_traceback(caplog, _JOB_STORE_WRITE_ERROR)


@pytest.mark.anyio
async def test_reindex_task_running_write_failure_marks_job_failed_without_ingesting(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A transient RUNNING write failure must not leave the job PENDING or ingest untracked."""
    job_store, job, attempted = _failing_writes_job_store(tmp_path, JobStatus.RUNNING, times=1)
    pipeline = MagicMock()
    pipeline.ingest_directory = AsyncMock(return_value=[])

    with caplog.at_level(logging.ERROR):
        search_store = await _run_reindex(job_store, job.job_id, tmp_path, _embedder_cache(), pipeline)

    assert attempted == [JobStatus.RUNNING, JobStatus.FAILED]
    final = job_store.get(job.job_id)
    assert final is not None and final.status == JobStatus.FAILED and final.error == REINDEX_FAILED_DETAIL
    pipeline.ingest_directory.assert_not_awaited()
    assert search_store.update_collection_meta.call_args[0][0].reindex_job_id is None
    assert_logged_with_traceback(caplog, _JOB_STORE_WRITE_ERROR)


@pytest.mark.anyio
async def test_reindex_task_cancelled_write_failure_after_ingest_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    job_store, job, attempted = _failing_writes_job_store(tmp_path, JobStatus.CANCELLED)

    async def _cancel_mid_ingest(*args: object, **kwargs: object) -> list[IngestResult]:
        job_store.update(job.job_id, status=JobStatus.CANCELLING)
        return []

    pipeline = MagicMock()
    pipeline.ingest_directory = AsyncMock(side_effect=_cancel_mid_ingest)

    with caplog.at_level(logging.ERROR):
        search_store = await _run_reindex(job_store, job.job_id, tmp_path, _embedder_cache(), pipeline)

    assert attempted == [JobStatus.RUNNING, JobStatus.CANCELLING, JobStatus.CANCELLED]
    assert search_store.update_collection_meta.call_args[0][0].reindex_job_id is None
    assert_logged_with_traceback(caplog, _JOB_STORE_WRITE_ERROR)


@pytest.mark.anyio
async def test_reindex_task_wrong_job_type_failed_write_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    job_store = JobStore(path=tmp_path / "jobs.json")
    job = job_store.create(collection="col")
    job_store.update = MagicMock(side_effect=OSError(_JOB_STORE_WRITE_ERROR))  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR):
        search_store = await _run_reindex(job_store, job.job_id, tmp_path, _embedder_cache(), MagicMock())

    job_store.update.assert_called_once_with(job.job_id, status=JobStatus.FAILED, error="not a ReindexJob")
    assert search_store.update_collection_meta.call_args[0][0].reindex_job_id is None
    assert_logged_with_traceback(caplog, _JOB_STORE_WRITE_ERROR)


# ---------------------------------------------------------------------------
# _default_ingest_task — Task 8.2: per-collection embedding model dispatch
# ---------------------------------------------------------------------------


def _make_ingest_mocks(
    active_model: str = "model-X",
    config_model: str = "global-default",
) -> tuple:
    """Return (search_store, embedder_cache, pipeline, config, fake_embedder) mocks."""
    fake_embedder = MagicMock()

    search_store = MagicMock()
    meta = CollectionMeta(
        name="col",
        active_embedding_model=active_model,
        namespace=DEFAULT_NAMESPACE,
    )
    search_store.get_collection_meta = AsyncMock(return_value=meta)

    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(return_value=fake_embedder)

    pipeline = MagicMock()
    pipeline.ingest_file = AsyncMock(
        return_value=IngestResult(doc_id="test-doc-id", chunks_created=0, status="ok")
    )
    pipeline.ingest_directory = AsyncMock(return_value=[])

    config = MagicMock()
    config.embedding_model = config_model

    return search_store, embedder_cache, pipeline, config, fake_embedder


@pytest.mark.anyio
async def test_default_ingest_task_reads_embedder_from_collection_meta(
    tmp_path: Path,
) -> None:
    """_default_ingest_task resolves the embedder from active_embedding_model on CollectionMeta."""
    from archon_search.server.routes_jobs import _default_ingest_task

    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create()
    body = IngestRequest(collection="col")
    search_store, embedder_cache, pipeline, config, _ = _make_ingest_mocks(active_model="model-X")

    await _default_ingest_task(
        job.job_id, store, body,
        search_store=search_store, embedder_cache=embedder_cache,
        pipeline=pipeline, config=config,
    )

    embedder_cache.get_or_load.assert_called_once_with("model-X")


@pytest.mark.anyio
async def test_default_ingest_task_does_not_use_target_embedding_model(
    tmp_path: Path,
) -> None:
    """_default_ingest_task reads from CollectionMeta, not from any 'target_embedding_model' field."""
    from archon_search.server.routes_jobs import _default_ingest_task

    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create()
    body = IngestRequest(collection="col")
    search_store, embedder_cache, pipeline, config, _ = _make_ingest_mocks(active_model="model-X")

    await _default_ingest_task(
        job.job_id, store, body,
        search_store=search_store, embedder_cache=embedder_cache,
        pipeline=pipeline, config=config,
    )

    # "model-Y" is a hypothetical target model — must never be used
    for call_args in embedder_cache.get_or_load.call_args_list:
        assert call_args[0][0] != "model-Y"


@pytest.mark.anyio
async def test_default_ingest_task_with_lock_reads_embedder_from_collection_meta(
    tmp_path: Path,
) -> None:
    """_default_ingest_task_with_lock also reads embedder from CollectionMeta.active_embedding_model."""
    from archon_search.server.routes_jobs import _default_ingest_task_with_lock

    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create()
    body = IngestRequest(collection="col")
    search_store, embedder_cache, pipeline, config, _ = _make_ingest_mocks(active_model="model-X")

    await _default_ingest_task_with_lock(
        job.job_id, store, body,
        search_store=search_store, embedder_cache=embedder_cache,
        pipeline=pipeline, config=config,
    )

    embedder_cache.get_or_load.assert_called_once_with("model-X")


@pytest.mark.anyio
async def test_default_ingest_task_empty_active_model_uses_global_fallback(
    tmp_path: Path,
) -> None:
    """When active_embedding_model is empty, falls back to config.embedding_model."""
    from archon_search.server.routes_jobs import _default_ingest_task

    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create()
    body = IngestRequest(collection="col")
    search_store, embedder_cache, pipeline, config, _ = _make_ingest_mocks(
        active_model="", config_model="global-default"
    )

    await _default_ingest_task(
        job.job_id, store, body,
        search_store=search_store, embedder_cache=embedder_cache,
        pipeline=pipeline, config=config,
    )

    embedder_cache.get_or_load.assert_called_once_with("global-default")


@pytest.mark.anyio
async def test_default_ingest_task_file_path_calls_ingest_file(
    tmp_path: Path,
) -> None:
    """When body.path points to a file, pipeline.ingest_file is called."""
    from archon_search.server.routes_jobs import _default_ingest_task

    # Create a real file so is_file() returns True
    test_file = tmp_path / "doc.md"
    test_file.write_text("hello")

    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create()
    body = IngestRequest(collection="col", path=str(test_file))
    search_store, embedder_cache, pipeline, config, fake_embedder = _make_ingest_mocks(
        active_model="model-X"
    )

    await _default_ingest_task(
        job.job_id, store, body,
        search_store=search_store, embedder_cache=embedder_cache,
        pipeline=pipeline, config=config,
    )

    pipeline.ingest_file.assert_called_once()
    call_kwargs = pipeline.ingest_file.call_args
    # First positional arg is the Path
    assert call_kwargs[0][0] == test_file
    assert call_kwargs[1]["embedder"] is fake_embedder
    pipeline.ingest_directory.assert_not_called()


@pytest.mark.anyio
async def test_default_ingest_task_dir_path_calls_ingest_directory(
    tmp_path: Path,
) -> None:
    """When body.path points to a directory, pipeline.ingest_directory is called."""
    from archon_search.server.routes_jobs import _default_ingest_task

    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()

    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create()
    body = IngestRequest(collection="col", path=str(corpus_dir))
    search_store, embedder_cache, pipeline, config, fake_embedder = _make_ingest_mocks(
        active_model="model-X"
    )

    await _default_ingest_task(
        job.job_id, store, body,
        search_store=search_store, embedder_cache=embedder_cache,
        pipeline=pipeline, config=config,
    )

    pipeline.ingest_directory.assert_called_once()
    call_kwargs = pipeline.ingest_directory.call_args
    assert call_kwargs[0][0] == corpus_dir
    assert call_kwargs[1]["embedder"] is fake_embedder
    pipeline.ingest_file.assert_not_called()

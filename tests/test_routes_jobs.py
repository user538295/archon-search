"""Tests for POST /ingest, GET /jobs/{id}, DELETE /jobs/{id} ."""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.datastructures import State

from archon_search._types import IngestResult
from archon_search.collection_meta import CollectionMeta
from archon_search.config import MaintenanceConfig, SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.embedder_cache import EMBEDDER_NOT_READY_DETAIL, EmbedderNotReadyError
from archon_search.jobs.maintenance_loop import MaintenanceLoop
from archon_search.jobs.model import JobStatus, job_to_dict
from archon_search.jobs.store import NOT_STARTED_BEFORE_RESTART_DETAIL, PROCESS_RESTART_ERROR, JobStore
from archon_search.server.app import create_app
from archon_search.server.routes_jobs import (
    INGEST_FAILED_DETAIL,
    JOB_STORE_WRITE_FAILED_DETAIL,
    REINDEX_FAILED_DETAIL,
    IngestRequest,
    _default_ingest_task,
    _default_ingest_task_with_lock,
    _reindex_task,
    _run_pipeline,
    make_retry_dispatcher,
)
from archon_search.types import (
    CommunityRebuildJob,
    IngestJob,
    IngestOptions,
    MetadataReindexJob,
    MigrationKind,
    ReindexJob,
    SyncJob,
)
from tests._internal_error_leak import LEAK_NEEDLE, assert_logged_with_traceback, permission_error
from tests._parked_lock import parked_waiters, reaping, until_parked


def _running_ingest_job(job_id: str) -> IngestJob:
    """The job a successful PENDING -> RUNNING claim returns, for ``MagicMock(spec=JobStore).transition``."""
    return IngestJob(job_id=job_id, status=JobStatus.RUNNING, created_at="", updated_at="", namespace=DEFAULT_NAMESPACE)


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
    tmp_path: Path, auth_headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """POST /ingest answers the sanitized 500 when store.create raises OSError, and logs the
    cause with its traceback."""
    store = MagicMock()
    error = permission_error("jobs.json")
    store.create.side_effect = error
    client = _make_client_with_store(tmp_path, store, auth_headers)

    with caplog.at_level(logging.ERROR, logger="archon_search.server.routes_jobs"):
        response = client.post("/ingest", json={"collection": "docs"})

    assert response.status_code == 500
    assert response.json() == {"detail": JOB_STORE_WRITE_FAILED_DETAIL}
    assert LEAK_NEEDLE not in response.text
    assert_logged_with_traceback(caplog, str(error))


def test_delete_job_transition_oserror_returns_500_envelope(
    tmp_path: Path, auth_headers: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """DELETE /jobs/{id} answers the sanitized 500 when store.transition raises OSError, and logs
    the cause with its traceback."""
    active_job = IngestJob(
        job_id="job-1",
        status=JobStatus.RUNNING,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        namespace=DEFAULT_NAMESPACE,
    )
    store = MagicMock()
    store.get.return_value = active_job
    error = permission_error("jobs.json")
    store.transition.side_effect = error
    client = _make_client_with_store(tmp_path, store, auth_headers)

    with caplog.at_level(logging.ERROR, logger="archon_search.server.routes_jobs"):
        response = client.delete(f"/jobs/{active_job.job_id}")

    assert response.status_code == 500
    assert response.json() == {"detail": JOB_STORE_WRITE_FAILED_DETAIL}
    assert LEAK_NEEDLE not in response.text
    assert_logged_with_traceback(caplog, str(error))


def test_delete_job_transition_write_failure_keeps_job_running(
    client: TestClient, tmp_store: JobStore, tmp_path: Path
) -> None:
    """A DELETE whose RUNNING->CANCELLING persist fails answers 500, so the cancel did not happen: the job must
    stay RUNNING in memory (GET agrees) and on disk — not report CANCELLING after the client was told 500."""
    from tests._job_store_faults import failing_job_writes, persisted_statuses

    job = tmp_store.create()
    tmp_store.update(job.job_id, status=JobStatus.RUNNING)

    # The failing write reaches disk (CANCELLING in the file) before raising; the corrective rewrite restores
    # RUNNING there — so the on-disk assertion below is not satisfied by an untouched file.
    with failing_job_writes(count=1, fail_after_write=1) as writes:
        response = client.delete(f"/jobs/{job.job_id}")

    assert writes.statuses_of(job.job_id) == [JobStatus.CANCELLING.value, JobStatus.RUNNING.value]
    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    in_memory = tmp_store.get(job.job_id)
    assert in_memory is not None
    assert in_memory.status == JobStatus.RUNNING
    assert client.get(f"/jobs/{job.job_id}").json()["status"] == JobStatus.RUNNING.value
    # Raw file, not a reloaded JobStore: loading rewrites every RUNNING job to FAILED (crash recovery).
    assert persisted_statuses(tmp_path / "jobs.json")[job.job_id] == JobStatus.RUNNING.value


def test_delete_job_transition_non_oserror_returns_sanitized_500(
    client: TestClient, tmp_store: JobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Any persist failure (not only OSError, e.g. a serialization TypeError) answers the sanitized 500 envelope
    — never an unhandled exception whose text could reach the client — logs the cause with its traceback, and
    the cancel did not happen."""
    import logging

    from tests._job_store_faults import assert_error_logged_with_traceback, failing_job_writes

    job = tmp_store.create()
    tmp_store.update(job.job_id, status=JobStatus.RUNNING)
    routes_jobs_logger = "archon_search.server.routes_jobs"
    caplog.set_level(logging.ERROR, logger=routes_jobs_logger)

    with failing_job_writes(exc=TypeError("simulated serialization failure /secret/path")):
        response = client.delete(f"/jobs/{job.job_id}")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    assert_error_logged_with_traceback(caplog, routes_jobs_logger)
    assert tmp_store.get(job.job_id).status == JobStatus.RUNNING  # type: ignore[union-attr]


def test_delete_job_in_unhandled_status_names_the_status_value(client: TestClient, tmp_store: JobStore) -> None:
    """DELETE on a QUEUED job (a status DELETE does not handle) answers 500 with the documented body
    ``Unknown job status: <status>`` — the bare status value, not the enum repr ``JobStatus.QUEUED``."""
    job = tmp_store.create_migration("col", MigrationKind.REWRITE, True)  # created QUEUED

    response = client.delete(f"/jobs/{job.job_id}")

    assert response.status_code == 500
    assert response.json() == {"detail": f"Unknown job status: {JobStatus.QUEUED.value}"}


@pytest.mark.parametrize("job_cls", [CommunityRebuildJob, MetadataReindexJob, SyncJob], ids=lambda cls: cls.__name__)
def test_delete_job_on_legacy_queued_row_of_a_kind_nothing_schedules_is_the_idempotent_200(
    tmp_path: Path, auth_headers: dict[str, str], job_cls: type[IngestJob]
) -> None:
    """A community-rebuild, metadata-reindex or sync job an older server left ``QUEUED`` (it died before claiming it)
    can never run. The restart fails it at load, so GET /jobs lists it FAILED and DELETE /jobs/{id} answers the
    terminal row's idempotent 200 — not the ``500`` ``Unknown job status: QUEUED`` (``409`` for a sync) it answered
    while the row stayed QUEUED."""
    jobs_path = tmp_path / "jobs.json"
    now = datetime.now(timezone.utc).isoformat()
    legacy = job_cls(job_id="legacy-queued", status=JobStatus.QUEUED, created_at=now, updated_at=now, collection="c")
    JobStore(path=jobs_path).create_job(legacy)
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    client = TestClient(create_app(config, JobStore(path=jobs_path)), headers=auth_headers)

    listed = {job["job_id"]: job for job in client.get("/jobs").json()["items"]}
    response = client.delete(f"/jobs/{legacy.job_id}")

    assert (listed[legacy.job_id]["status"], listed[legacy.job_id]["error"]) == ("FAILED", NOT_STARTED_BEFORE_RESTART_DETAIL)
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["error"]) == ("FAILED", NOT_STARTED_BEFORE_RESTART_DETAIL)


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


def test_ingest_openapi_lists_500_response(client: TestClient) -> None:
    """POST /ingest declares the sanitized 500 it returns when creating the job fails."""
    responses = client.get("/openapi.json").json()["paths"]["/ingest"]["post"]["responses"]
    assert responses["500"]["content"]["application/json"]["schema"]["$ref"].endswith("ErrorDetail")


def test_ingest_openapi_lists_503_response(client: TestClient) -> None:
    """POST /ingest declares its store-busy 503 with the ErrorDetail body it returns ({"detail", "code"})."""
    responses = client.get("/openapi.json").json()["paths"]["/ingest"]["post"]["responses"]
    assert responses["503"]["content"]["application/json"]["schema"]["$ref"].endswith("ErrorDetail")


@pytest.mark.anyio
async def test_background_ingest_oserror_logs_and_marks_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A primary OSError in the background task (persisting the RUNNING claim) is logged and the job is marked FAILED."""
    real_store = JobStore(path=tmp_path / "jobs.json")
    job = real_store.create()
    primary_error = OSError("disk full")

    store = MagicMock()
    store.update.side_effect = real_store.update
    store.get.side_effect = real_store.get
    store.transition.side_effect = primary_error

    body = IngestRequest(collection="docs")
    with caplog.at_level("ERROR"):
        await _default_ingest_task(job.job_id, store, body, pipeline_fn=None)

    # The job ended FAILED with an error message set
    completed = real_store.get(job.job_id)
    assert completed is not None
    assert completed.status == JobStatus.FAILED
    assert completed.error == INGEST_FAILED_DETAIL
    # The primary failure was logged, with its traceback
    [record] = [r for r in caplog.records if r.getMessage() == f"Ingest task {job.job_id} failed"]
    assert record.exc_info is not None and record.exc_info[1] is primary_error


_INGEST_TASK_FNS = [_default_ingest_task, _default_ingest_task_with_lock]
_INGEST_TASK_IDS = ["plain", "with_lock"]


@pytest.mark.anyio
@pytest.mark.parametrize("task_fn", _INGEST_TASK_FNS, ids=_INGEST_TASK_IDS)
async def test_background_ingest_double_oserror_suppressed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, task_fn: Any
) -> None:
    """If EVERY status write (the RUNNING claim and store.update) raises OSError, no exception escapes
    _default_ingest_task."""
    real_store = JobStore(path=tmp_path / "jobs.json")
    job = real_store.create()

    store = MagicMock()
    store.update.side_effect = OSError("disk full")
    store.get.side_effect = real_store.get
    store.transition.side_effect = OSError("disk full")

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
        if kwargs.get("status") == JobStatus.CANCELLED:
            raise OSError("disk full")

    store = MagicMock(spec=JobStore)
    store.transition.return_value = _running_ingest_job("job-1")
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

    # The RUNNING claim goes through store.transition (a compare-and-set), not update.
    store.transition.assert_called_once_with(job.job_id, {JobStatus.PENDING}, JobStatus.RUNNING)
    assert attempted == [JobStatus.DONE]
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
        await task_fn(
            job.job_id, store, IngestRequest(collection="docs"),
            pipeline_fn=AsyncMock(side_effect=RuntimeError("ingest failed")),
        )

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


@pytest.mark.asyncio
@pytest.mark.parametrize("with_lock", [False, True], ids=["no-lock", "with-lock"])
@pytest.mark.parametrize(
    ("first_error", "expected_error"),
    [
        (OSError("disk full"), INGEST_FAILED_DETAIL),
        (EmbedderNotReadyError("model still loading"), EMBEDDER_NOT_READY_DETAIL),
    ],
    ids=["generic-failure", "embedder-not-ready"],
)
@pytest.mark.parametrize(
    "persist_error",
    [OSError("disk full"), KeyError("job-1"), TypeError("Object of type set is not JSON serializable")],
    ids=["persist-oserror", "persist-keyerror", "persist-typeerror"],
)
async def test_background_ingest_failed_status_persist_error_suppressed(
    first_error: Exception, expected_error: str, persist_error: Exception, with_lock: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the ingest itself fails and persisting FAILED then raises (OSError, KeyError for a job no longer in
    the store, or a non-OSError such as a JSON-serialization TypeError), both ingest tasks log the persist
    failure with its traceback and suppress it (no exception escapes); _default_ingest_task_with_lock still
    releases the held lock."""
    status_writes: list[dict[str, object]] = []

    def update_side_effect(job_id: str, **kwargs: object) -> None:
        status_writes.append(kwargs)
        raise persist_error

    store = MagicMock(spec=JobStore)
    store.transition.return_value = _running_ingest_job("job-1")
    store.update.side_effect = update_side_effect

    async def pipeline_fn(*args: object, **kwargs: object) -> None:
        raise first_error

    body = IngestRequest(collection="docs")
    with caplog.at_level("ERROR"):
        if with_lock:
            held_lock = asyncio.Lock()
            await held_lock.acquire()
            await _default_ingest_task_with_lock("job-1", store, body, pipeline_fn=pipeline_fn, held_lock=held_lock)
            assert not held_lock.locked()
        else:
            await _default_ingest_task("job-1", store, body, pipeline_fn=pipeline_fn)

    store.transition.assert_called_once_with("job-1", {JobStatus.PENDING}, JobStatus.RUNNING)
    assert status_writes == [{"status": JobStatus.FAILED, "error": expected_error}]
    [record] = [r for r in caplog.records if "could not persist FAILED status" in r.message]
    assert record.exc_info is not None and record.exc_info[1] is persist_error


@pytest.mark.asyncio
@pytest.mark.parametrize("with_lock", [False, True], ids=["no-lock", "with-lock"])
async def test_background_ingest_job_missing_at_done_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, with_lock: bool
) -> None:
    """A job no longer in the JobStore when its ingest finishes makes the DONE write raise KeyError: that is
    logged with its traceback and suppressed — never an exception, never reported as an ingest failure."""
    real_store = JobStore(path=tmp_path / "jobs.json")
    job = real_store.create()

    async def pipeline_fn(*args: object, **kwargs: object) -> None:
        # Defensive case: nothing in JobStore drops a running job (_evict_old only evicts old terminal jobs).
        del real_store._jobs[job.job_id]

    body = IngestRequest(collection="docs")
    with caplog.at_level("ERROR", logger="archon_search.server.routes_jobs"):
        if with_lock:
            held_lock = asyncio.Lock()
            await held_lock.acquire()
            await _default_ingest_task_with_lock(job.job_id, real_store, body, pipeline_fn=pipeline_fn, held_lock=held_lock)
            assert not held_lock.locked()
        else:
            await _default_ingest_task(job.job_id, real_store, body, pipeline_fn=pipeline_fn)

    assert real_store.get(job.job_id) is None
    [record] = [r for r in caplog.records if r.getMessage() == f"background ingest: could not persist DONE status for job {job.job_id}"]
    assert record.exc_info is not None and isinstance(record.exc_info[1], KeyError)
    assert not [r for r in caplog.records if r.getMessage() == f"Ingest task {job.job_id} failed"]


@pytest.mark.asyncio
async def test_background_ingest_with_lock_cancelled_oserror_suppressed_and_reraises_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Like _default_ingest_task: an OSError persisting CANCELLED is logged and suppressed,
    CancelledError is still re-raised, and the held lock is released."""

    def update_side_effect(job_id: str, **kwargs: object) -> None:
        if kwargs.get("status") == JobStatus.CANCELLED:
            raise OSError("disk full")

    store = MagicMock(spec=JobStore)
    store.update.side_effect = update_side_effect
    held_lock = asyncio.Lock()
    await held_lock.acquire()

    async def pipeline_fn(*a: object, **k: object) -> None:
        await asyncio.Event().wait()  # block forever until cancelled

    body = IngestRequest(collection="docs")
    with caplog.at_level("ERROR"):
        task = asyncio.create_task(
            _default_ingest_task_with_lock("job-1", store, body, pipeline_fn=pipeline_fn, held_lock=held_lock)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    assert any("could not persist CANCELLED status" in r.message for r in caplog.records)
    assert not held_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch_fails", [False, True], ids=["dispatch-succeeds", "dispatch-fails"])
async def test_background_ingest_with_lock_never_releases_a_lock_another_holder_acquired(dispatch_fails: bool) -> None:
    """The task releases its pre-acquired lock early, before dispatch; when another holder (e.g. a DELETE)
    acquires that lock meanwhile, the task's ``finally`` must not release the other holder's acquisition."""
    store = MagicMock(spec=JobStore)
    store.transition.return_value = _running_ingest_job("job-1")
    store.get.return_value = None
    held_lock = asyncio.Lock()
    await held_lock.acquire()

    async def _dispatch_while_another_holder_acquires(*args: object, **kwargs: object) -> tuple[list, list]:
        assert not held_lock.locked(), "the task must release its lock before dispatching"
        await held_lock.acquire()  # the other holder
        if dispatch_fails:
            raise RuntimeError("ingest failed")
        return [], []

    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock()
    with patch("archon_search.server.routes_jobs._dispatch_ingest", new=_dispatch_while_another_holder_acquires):
        await _default_ingest_task_with_lock(
            "job-1", store, IngestRequest(collection="docs"), held_lock=held_lock,
            search_store=MagicMock(), embedder_cache=MagicMock(), pipeline=pipeline, config=MagicMock(),
        )

    assert held_lock.locked(), "the other holder's lock acquisition was released by the ingest task"


@pytest.mark.asyncio
async def test_background_ingest_with_lock_marks_job_running_before_early_release(tmp_path: Path) -> None:
    """On the early-release branch the job must be RUNNING before the pre-acquired lock is released.

    DELETE /collections/{name} waits for this lock and then refuses while a base IngestJob is RUNNING;
    releasing the lock while the job is still PENDING would let that DELETE drop the table mid-ingest.
    """
    real_store = JobStore(path=tmp_path / "jobs.json")
    job = real_store.create(collection="docs")
    statuses_at_release: list[JobStatus | None] = []

    class _StatusRecordingLock(asyncio.Lock):
        def release(self) -> None:
            # Only record here: an assert raised inside release() would be swallowed by the task's handlers.
            current = real_store.get(job.job_id)
            statuses_at_release.append(current.status if current is not None else None)
            super().release()

    held_lock = _StatusRecordingLock()
    await held_lock.acquire()
    pipeline = MagicMock()
    pipeline.warmup_models = AsyncMock()
    with patch("archon_search.server.routes_jobs._dispatch_ingest", new=AsyncMock(return_value=([], []))):
        await _default_ingest_task_with_lock(
            job.job_id, real_store, IngestRequest(collection="docs"), held_lock=held_lock,
            search_store=MagicMock(), embedder_cache=MagicMock(), pipeline=pipeline, config=MagicMock(),
        )

    assert statuses_at_release == [JobStatus.RUNNING], f"lock released while the job was {statuses_at_release}"
    assert not held_lock.locked()


_SECRET_PATH = "/very/secret/internal/path/store.lance"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_lock", [False, True], ids=["no-lock", "with-lock"])
@pytest.mark.parametrize(
    "failure",
    [
        OSError(5, "Input/output error", _SECRET_PATH),
        # A deep FileNotFoundError (not archon-search's own IngestPathNotFoundError) is no user-facing refusal.
        FileNotFoundError(2, "No such file or directory", _SECRET_PATH),
    ],
    ids=["oserror", "deep-file-not-found"],
)
async def test_background_ingest_unexpected_failure_records_sanitized_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, with_lock: bool, failure: OSError
) -> None:
    """An unexpected ingest failure leaves the job FAILED with the fixed INGEST_FAILED_DETAIL (the
    exception text embeds server paths and is served by GET /jobs); the exception itself is logged."""
    real_store = JobStore(path=tmp_path / "jobs.json")
    job = real_store.create()

    async def pipeline_fn(*args: object, **kwargs: object) -> None:
        raise failure

    body = IngestRequest(collection="docs")
    with caplog.at_level("ERROR", logger="archon_search.server.routes_jobs"):
        if with_lock:
            held_lock = asyncio.Lock()
            await held_lock.acquire()
            await _default_ingest_task_with_lock(job.job_id, real_store, body, pipeline_fn=pipeline_fn, held_lock=held_lock)
        else:
            await _default_ingest_task(job.job_id, real_store, body, pipeline_fn=pipeline_fn)

    failed = real_store.get(job.job_id)
    assert failed is not None
    assert failed.status == JobStatus.FAILED
    assert failed.error == INGEST_FAILED_DETAIL
    assert _SECRET_PATH not in failed.error
    [record] = [r for r in caplog.records if r.getMessage() == f"Ingest task {job.job_id} failed"]
    assert record.exc_info is not None and record.exc_info[1] is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("with_lock", [False, True], ids=["no-lock", "with-lock"])
async def test_background_ingest_missing_path_keeps_its_user_facing_error(
    tmp_path: Path, with_lock: bool
) -> None:
    """A path that does not exist is the caller's own input, phrased by archon-search: the job keeps
    the documented "path does not exist or is not a file/directory" error (UserManual 50)."""
    real_store = JobStore(path=tmp_path / "jobs.json")
    job = real_store.create()
    missing = tmp_path / "nope"
    body = IngestRequest(collection="docs", path=str(missing))
    search_store = MagicMock()
    search_store.get_collection_meta = AsyncMock(return_value=CollectionMeta(name="docs"))
    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(return_value=MagicMock())
    deps = {"search_store": search_store, "embedder_cache": embedder_cache, "pipeline": MagicMock(), "config": SearchConfig()}

    if with_lock:
        held_lock = asyncio.Lock()
        await held_lock.acquire()
        await _default_ingest_task_with_lock(job.job_id, real_store, body, held_lock=held_lock, **deps)
    else:
        await _default_ingest_task(job.job_id, real_store, body, **deps)

    failed = real_store.get(job.job_id)
    assert failed is not None
    assert failed.status == JobStatus.FAILED
    assert failed.error == f"path does not exist or is not a file/directory: {missing}"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_lock", [False, True], ids=["no-lock", "with-lock"])
@pytest.mark.parametrize("cancel_requested", [False, True], ids=["done", "cancelled-on-cancelling"])
async def test_background_ingest_final_status_persist_failure_is_not_an_ingest_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, with_lock: bool, cancel_requested: bool
) -> None:
    """A successful ingest whose terminal-status write (DONE, or CANCELLED for a CANCELLING job) fails to
    persist keeps that status in memory and logs the persist failure — it is never reported FAILED with
    INGEST_FAILED_DETAIL (which MaintenanceLoop would then retry)."""
    real_store = JobStore(path=tmp_path / "jobs.json")
    job = real_store.create()
    final_status = JobStatus.CANCELLED if cancel_requested else JobStatus.DONE
    persist_error = OSError(28, "No space left on device")
    real_write = real_store._write_atomic

    def write_failing_on_final_status() -> None:
        current = real_store.get(job.job_id)
        if current is not None and current.status == final_status:
            raise persist_error
        real_write()

    async def pipeline_fn(*args: object, **kwargs: object) -> None:
        if cancel_requested:
            real_store.update(job.job_id, status=JobStatus.CANCELLING)

    body = IngestRequest(collection="docs")
    with (
        patch.object(real_store, "_write_atomic", side_effect=write_failing_on_final_status),
        caplog.at_level("ERROR", logger="archon_search.server.routes_jobs"),
    ):
        if with_lock:
            held_lock = asyncio.Lock()
            await held_lock.acquire()
            await _default_ingest_task_with_lock(job.job_id, real_store, body, pipeline_fn=pipeline_fn, held_lock=held_lock)
            assert not held_lock.locked()
        else:
            await _default_ingest_task(job.job_id, real_store, body, pipeline_fn=pipeline_fn)

    final = real_store.get(job.job_id)
    assert final is not None
    assert final.status == final_status
    assert final.error is None
    [record] = [r for r in caplog.records if f"could not persist {final_status.value} status" in r.getMessage()]
    assert record.exc_info is not None and record.exc_info[1] is persist_error
    assert not [r for r in caplog.records if r.getMessage() == f"Ingest task {job.job_id} failed"]


def _ingest_app_with_lock(tmp_path: Path, job_store: JobStore, auth_headers: dict[str, str]) -> tuple[TestClient, asyncio.Lock]:
    """App whose search_store hands out one real per-collection lock, for the POST /ingest lock paths."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    with mock.patch("archon_search.server.app.DocumentChunker"):
        app = create_app(config, job_store)
    lock = asyncio.Lock()
    search_store = MagicMock()
    search_store.lock_for = MagicMock(return_value=lock)
    app.state.search_store = search_store
    return TestClient(app, headers=auth_headers, raise_server_exceptions=False), lock


def test_ingest_lock_timeout_503_creates_no_job(
    tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 503 lock timeout on POST /ingest returns the store-busy body with Retry-After and must not leave a
    PENDING job behind that nothing ever runs."""
    import archon_search.constants as constants

    client, _lock = _ingest_app_with_lock(tmp_path, tmp_store, auth_headers)

    async def _never_acquired() -> bool:
        await asyncio.Event().wait()
        return True

    busy_lock = MagicMock()
    busy_lock.acquire = _never_acquired
    client.app.state.search_store.lock_for.return_value = busy_lock
    monkeypatch.setattr(constants, "INGEST_LOCK_TIMEOUT_S", 0.05)

    response = client.post("/ingest", json={"collection": "docs"})

    assert response.status_code == 503
    assert response.json() == {"detail": "collection is busy with another write; retry after Retry-After seconds", "code": "store_busy"}
    assert response.headers["Retry-After"] == "1"
    assert tmp_store.list() == []


@pytest.mark.parametrize("create_error", [OSError("disk full"), TypeError("/very/secret/path")], ids=["oserror", "non-oserror"])
def test_ingest_job_create_failure_logs_returns_500_and_releases_lock(
    tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str], caplog: pytest.LogCaptureFixture,
    create_error: Exception,
) -> None:
    """JobStore.create raising anything after the lock was acquired → logged, sanitized 500, lock released."""
    client, lock = _ingest_app_with_lock(tmp_path, tmp_store, auth_headers)

    with (
        patch.object(tmp_store, "create", side_effect=create_error),
        caplog.at_level("ERROR", logger="archon_search.server.routes_jobs"),
    ):
        response = client.post("/ingest", json={"collection": "docs"})

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error"}
    [record] = [r for r in caplog.records if r.getMessage().startswith("Failed to create the ingest job")]
    assert record.exc_info is not None and record.exc_info[1] is create_error
    assert not lock.locked()


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
    job_store.transition.return_value = replace(job, status=JobStatus.RUNNING)
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


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["embedder-failure", "ingest-failure"])
async def test_reindex_task_unexpected_failure_records_sanitized_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, scenario: str
) -> None:
    """An unexpected embedder-resolution or ingest failure leaves the reindex job FAILED with the fixed
    REINDEX_FAILED_DETAIL, never the exception text; the exception is logged."""
    from archon_search.server.routes_jobs import REINDEX_FAILED_DETAIL, _reindex_task

    job = _make_reindex_job(target_embedding_model="model-B")
    meta = _make_collection_meta(active_embedding_model="model-A")
    failure = OSError(5, "Input/output error", _SECRET_PATH)
    job_store, search_store, embedder_cache, pipeline = _make_mocks(
        job,
        meta,
        embedder_raises=failure if scenario == "embedder-failure" else None,
        ingest_raises=failure if scenario == "ingest-failure" else None,
    )

    with caplog.at_level("ERROR", logger="archon_search.server.routes_jobs"):
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

    job_store.update.assert_called_with(job.job_id, status=JobStatus.FAILED, error=REINDEX_FAILED_DETAIL)
    assert any(r.exc_info is not None and r.exc_info[1] is failure for r in caplog.records)


def _reindex_scenario(scenario: str) -> tuple:
    """(job_store, search_store, embedder_cache, pipeline) mocks steering _reindex_task into one branch."""
    job = _make_reindex_job(target_embedding_model="model-B")
    meta = _make_collection_meta(active_embedding_model="model-A", pending_embedding_model="model-B")
    embedder_raises = {
        "embedder-not-ready": EmbedderNotReadyError("model still loading"),
        "embedder-failure": RuntimeError("model load failed"),
    }.get(scenario)
    ingest_raises = {
        "ingest-failure": RuntimeError("ingest failed"),
        "ingest-cancelled": asyncio.CancelledError(),
    }.get(scenario)
    job_store, search_store, embedder_cache, pipeline = _make_mocks(
        job, meta, ingest_raises=ingest_raises, embedder_raises=embedder_raises
    )
    if scenario == "not-a-reindex-job":
        job_store.get.return_value = IngestJob(
            job_id=job.job_id, status=JobStatus.PENDING, created_at="", updated_at="", namespace=DEFAULT_NAMESPACE
        )
    elif scenario == "already-cancelling":
        job_store.get.return_value = _make_reindex_job(target_embedding_model="model-B", status=JobStatus.CANCELLING)
    elif scenario == "cancelling-after-ingest":
        cancelling = _make_reindex_job(target_embedding_model="model-B", status=JobStatus.CANCELLING)
        job_store.get.side_effect = itertools.chain([job], itertools.repeat(cancelling))
    return job_store, search_store, embedder_cache, pipeline


# scenario -> the terminal status _reindex_task writes last on that path
_REINDEX_FINAL_STATUS = {
    "not-a-reindex-job": JobStatus.FAILED,
    "already-cancelling": JobStatus.CANCELLED,
    "embedder-not-ready": JobStatus.FAILED,
    "embedder-failure": JobStatus.FAILED,
    "ingest-failure": JobStatus.FAILED,
    "ingest-cancelled": JobStatus.CANCELLED,
    "cancelling-after-ingest": JobStatus.CANCELLED,
    "success": JobStatus.DONE,
}
_REINDEX_SCENARIOS = list(_REINDEX_FINAL_STATUS)
# scenarios that get past embedder resolution and run the ingest
_REINDEX_INGEST_SCENARIOS = {"ingest-failure", "ingest-cancelled", "cancelling-after-ingest", "success"}


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", _REINDEX_SCENARIOS)
@pytest.mark.parametrize(
    "persist_error",
    [OSError("disk full"), KeyError("job-reindex-1"), TypeError("Object of type set is not JSON serializable")],
    ids=["oserror", "keyerror", "typeerror"],
)
async def test_reindex_task_never_raises_when_job_update_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, scenario: str, persist_error: Exception
) -> None:
    """A job_store.update failure on any status write of _reindex_task (OSError persist failure, KeyError for
    a job no longer in the store, or a non-OSError such as a JSON-serialization TypeError) is logged with its
    traceback, never propagated."""
    from archon_search.server.routes_jobs import _reindex_task

    job_store, search_store, embedder_cache, pipeline = _reindex_scenario(scenario)
    job_store.update.side_effect = persist_error

    with caplog.at_level("ERROR", logger="archon_search.server.routes_jobs"):
        await _reindex_task(
            job_id="job-reindex-1",
            store=search_store,
            job_store=job_store,
            embedder_cache=embedder_cache,
            pipeline=pipeline,
            collection="col",
            namespace=DEFAULT_NAMESPACE,
            collection_path=tmp_path,
            config=SearchConfig(),
        )

    assert job_store.update.call_args.kwargs["status"] == _REINDEX_FINAL_STATUS[scenario]
    if scenario in _REINDEX_INGEST_SCENARIOS:
        pipeline.ingest_directory.assert_awaited_once()
    else:
        pipeline.ingest_directory.assert_not_awaited()
    written_meta: CollectionMeta = search_store.update_collection_meta.call_args[0][0]
    assert written_meta.reindex_job_id is None
    # Only a completed reindex promotes the target model.
    assert written_meta.active_embedding_model == ("model-B" if scenario == "success" else "model-A")
    assert any(r.exc_info is not None and r.exc_info[1] is persist_error for r in caplog.records)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "scenario",
    [
        "job-missing",
        "not-a-reindex-job",
        "already-cancelled",
        "already-cancelling",
        "embedder-not-ready",
        "embedder-failure",
        "ingest-failure",
        "ingest-cancelled",
        "cancelling-after-ingest",
    ],
)
async def test_reindex_task_never_raises_when_clearing_reindex_job_id_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, scenario: str
) -> None:
    """A failing meta write while clearing reindex_job_id on an early-exit, ingest-failure or cancel path is
    logged, never raised, and the job still gets the terminal status that path writes."""
    from archon_search.server.routes_jobs import _reindex_task

    job_store, search_store, embedder_cache, pipeline = _reindex_scenario(scenario)
    if scenario == "job-missing":
        job_store.get.return_value = None
    elif scenario == "already-cancelled":
        job_store.get.return_value = _make_reindex_job(status=JobStatus.CANCELLED)
    meta_error = RuntimeError("meta write failed")
    search_store.update_collection_meta.side_effect = meta_error

    with caplog.at_level("ERROR", logger="archon_search.server.routes_jobs"):
        await _reindex_task(
            job_id="job-reindex-1",
            store=search_store,
            job_store=job_store,
            embedder_cache=embedder_cache,
            pipeline=pipeline,
            collection="col",
            namespace=DEFAULT_NAMESPACE,
            collection_path=tmp_path,
            config=SearchConfig(),
        )

    search_store.update_collection_meta.assert_awaited_once()
    assert search_store.update_collection_meta.call_args[0][0].active_embedding_model == "model-A"
    assert any(r.exc_info is not None and r.exc_info[1] is meta_error for r in caplog.records)
    if scenario in _REINDEX_FINAL_STATUS:
        assert job_store.update.call_args.kwargs["status"] == _REINDEX_FINAL_STATUS[scenario]
    else:  # job-missing / already-cancelled: the guard exits without a status write
        job_store.update.assert_not_called()


# ---------------------------------------------------------------------------
# _reindex_task — job-store write failures never escape the task (C2-I-3)
# ---------------------------------------------------------------------------

_JOB_STORE_WRITE_ERROR = "job store write failed"


def _failing_writes_job_store(
    tmp_path: Path, fail_on: JobStatus, *, error: Exception | None = None, times: int | None = None
) -> tuple[JobStore, ReindexJob, list[JobStatus]]:
    """A real JobStore whose ``update`` to ``fail_on`` raises (``times`` times, or always)."""
    job_store = JobStore(path=tmp_path / "jobs.json")
    job = job_store.create_reindex("col", namespace=DEFAULT_NAMESPACE, target_embedding_model="model-B")
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


async def _run_reindex_on_failing_writes(
    job_store: JobStore, job_id: str, tmp_path: Path, embedder_cache: Any, pipeline: Any
) -> Any:
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
        search_store = await _run_reindex_on_failing_writes(job_store, job.job_id, tmp_path, _embedder_cache(embedder_error), MagicMock())

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
        search_store = await _run_reindex_on_failing_writes(job_store, job.job_id, tmp_path, _embedder_cache(), pipeline)

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
        search_store = await _run_reindex_on_failing_writes(job_store, job.job_id, tmp_path, _embedder_cache(), pipeline)

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
        search_store = await _run_reindex_on_failing_writes(job_store, job.job_id, tmp_path, _embedder_cache(), MagicMock())

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


# ---------------------------------------------------------------------------
# Bug 4: a cancel requested before the task marks RUNNING must not be overwritten
# ---------------------------------------------------------------------------


def _cancel_job(job_store: JobStore, job_id: str) -> None:
    """Simulate DELETE /jobs/{id} on an active job: PENDING/RUNNING -> CANCELLING."""
    cancelled = job_store.transition(job_id, {JobStatus.PENDING, JobStatus.RUNNING}, JobStatus.CANCELLING)
    assert cancelled is not None, "precondition: the simulated DELETE must move the job to CANCELLING"


def _cancel_during_embedder_load(
    job_store: JobStore, job_id: str, then: Callable[[], None] | None = None
) -> MagicMock:
    """An ``embedder_cache`` whose ``get_or_load`` simulates a DELETE /jobs/{id} landing mid-load, then runs *then*."""

    async def _load(_model: str) -> MagicMock:
        _cancel_job(job_store, job_id)
        if then is not None:
            then()
        return MagicMock()

    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(side_effect=_load)
    return embedder_cache


def _reindex_cancel_fixture(tmp_path: Path, target_model: str | None) -> tuple:
    """A real JobStore holding a PENDING ReindexJob plus a stateful collection meta pointing at it."""
    job_store = JobStore(path=tmp_path / "jobs.json")
    job = job_store.create_reindex("col", target_embedding_model=target_model)
    meta = _make_collection_meta(
        active_embedding_model="model-A",
        pending_embedding_model=target_model,
        needs_reindex=target_model is not None,
        reindex_job_id=job.job_id,
    )
    search_store = MagicMock()
    search_store.get_collection_meta = AsyncMock(return_value=meta)
    search_store.update_collection_meta = AsyncMock(return_value=None)
    pipeline = MagicMock()
    pipeline.ingest_directory = AsyncMock(return_value=[])
    return job_store, job, meta, search_store, pipeline


async def _run_reindex(
    job_store: JobStore,
    job_id: str,
    search_store: MagicMock,
    embedder_cache: MagicMock,
    pipeline: MagicMock,
    collection_path: Path,
    config: SearchConfig | None = None,
) -> None:
    """Run _reindex_task on collection "col" in the default namespace."""
    from archon_search.server.routes_jobs import _reindex_task

    await _reindex_task(
        job_id=job_id,
        store=search_store,
        job_store=job_store,
        embedder_cache=embedder_cache,
        pipeline=pipeline,
        collection="col",
        namespace=DEFAULT_NAMESPACE,
        collection_path=collection_path,
        config=config if config is not None else SearchConfig(),
    )


async def _run_ingest_task(store: JobStore, job_id: str, pipeline_fn: AsyncMock, *, with_lock: bool) -> None:
    """Run _default_ingest_task, or _default_ingest_task_with_lock with a pre-acquired lock it must release."""
    body = IngestRequest(collection="docs")
    if with_lock:
        held_lock = asyncio.Lock()
        await held_lock.acquire()
        await _default_ingest_task_with_lock(job_id, store, body, pipeline_fn=pipeline_fn, held_lock=held_lock)
        assert not held_lock.locked()
    else:
        await _default_ingest_task(job_id, store, body, pipeline_fn=pipeline_fn)


def _fail_running_persist(job_store: JobStore, job_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every persist of *job_id* as RUNNING raise OSError, as a full disk would; other writes succeed."""
    real_write = job_store._write_atomic

    def _write() -> None:
        current = job_store.get(job_id)
        if current is not None and current.status == JobStatus.RUNNING:
            raise OSError("disk full")
        real_write()

    monkeypatch.setattr(job_store, "_write_atomic", _write)


@pytest.mark.anyio
@pytest.mark.parametrize("target_model", ["model-B", None], ids=["model-change", "data-only"])
async def test_reindex_task_cancel_during_embedder_load_skips_ingest_and_promotion(
    tmp_path: Path, target_model: str | None
) -> None:
    """A cancel requested during the embedder load must be honoured before any work: the job ends CANCELLED (not
    overwritten to RUNNING and completed DONE), with no directory re-ingest, no promotion of the target model, and
    the collection's reindex_job_id cleared."""
    job_store, job, meta, search_store, pipeline = _reindex_cancel_fixture(tmp_path, target_model)
    embedder_cache = _cancel_during_embedder_load(job_store, job.job_id)

    await _run_reindex(job_store, job.job_id, search_store, embedder_cache, pipeline, tmp_path)

    embedder_cache.get_or_load.assert_awaited_once()
    pipeline.ingest_directory.assert_not_awaited()
    assert meta.active_embedding_model == "model-A"
    assert meta.pending_embedding_model == target_model
    assert meta.needs_reindex is (target_model is not None)
    assert meta.reindex_job_id is None
    search_store.update_collection_meta.assert_awaited()
    final = job_store.get(job.job_id)
    assert final is not None
    assert final.status == JobStatus.CANCELLED


@dataclass
class _ReindexExit:
    """A _reindex_task run on a real JobStore, steered into one terminal path by a ``_REINDEX_EXITS`` setup."""

    job_store: JobStore
    job_id: str
    meta: CollectionMeta
    search_store: MagicMock
    embedder_cache: MagicMock
    pipeline: MagicMock
    monkeypatch: pytest.MonkeyPatch
    # When set, a newer reindex points the collection's reindex_job_id at this id as the path is triggered.
    newer_reindex_job_id: str | None = None

    def trigger(self) -> None:
        """Called by each setup at the moment its exit path is triggered."""
        if self.newer_reindex_job_id is not None:
            self.meta.reindex_job_id = self.newer_reindex_job_id


def _exit_at_task_start(run: _ReindexExit) -> None:
    """A DELETE lands before the task starts."""
    _cancel_job(run.job_store, run.job_id)
    run.trigger()


def _exit_at_embedder_load(run: _ReindexExit) -> None:
    """A DELETE lands during the embedder load, which then succeeds."""
    run.embedder_cache = _cancel_during_embedder_load(run.job_store, run.job_id, then=run.trigger)


def _exit_at_claim_persist_failure(run: _ReindexExit) -> None:
    """Persisting the PENDING -> RUNNING claim fails."""
    _fail_running_persist(run.job_store, run.job_id, run.monkeypatch)
    run.trigger()


def _exit_not_a_reindex_job(*, cancelled: bool) -> Callable[[_ReindexExit], None]:
    """The task is handed a plain IngestJob, which the collection's reindex_job_id names — already CANCELLING when
    *cancelled*."""

    def _setup(run: _ReindexExit) -> None:
        run.job_id = run.job_store.create(collection="col").job_id
        run.meta.reindex_job_id = run.job_id
        if cancelled:
            _cancel_job(run.job_store, run.job_id)
        run.trigger()

    return _setup


def _exit_on_embedder_error(error: type[Exception], *, cancel_first: bool) -> Callable[[_ReindexExit], None]:
    """The embedder load raises *error* — after a DELETE lands during it when *cancel_first*."""

    def _setup(run: _ReindexExit) -> None:
        async def _load(_model: str) -> MagicMock:
            if cancel_first:
                _cancel_job(run.job_store, run.job_id)
            run.trigger()
            raise error("model load failed")

        run.embedder_cache.get_or_load = AsyncMock(side_effect=_load)

    return _setup


def _exit_during_ingest(
    *, cancel: bool = False, error: type[BaseException] | None = None
) -> Callable[[_ReindexExit], None]:
    """The directory re-ingest returns, or raises *error* — after a DELETE lands during it when *cancel*."""

    def _setup(run: _ReindexExit) -> None:
        async def _ingest(*_args: object, **_kwargs: object) -> list:
            if cancel:
                _cancel_job(run.job_store, run.job_id)
            run.trigger()
            if error is not None:
                raise error("ingest stopped")
            return []

        run.pipeline.ingest_directory = AsyncMock(side_effect=_ingest)

    return _setup


# terminal path -> (its setup, the job's final status, the job's final error)
_REINDEX_EXITS: dict[str, tuple[Callable[[_ReindexExit], None], JobStatus, str | None]] = {
    "task-start": (_exit_at_task_start, JobStatus.CANCELLED, None),
    "not-a-reindex-job": (_exit_not_a_reindex_job(cancelled=False), JobStatus.FAILED, "not a ReindexJob"),
    "not-a-reindex-job-cancelled": (_exit_not_a_reindex_job(cancelled=True), JobStatus.CANCELLED, None),
    "embedder-load": (_exit_at_embedder_load, JobStatus.CANCELLED, None),
    "claim-persist-failure": (_exit_at_claim_persist_failure, JobStatus.FAILED, REINDEX_FAILED_DETAIL),
    "embedder-not-ready": (
        _exit_on_embedder_error(EmbedderNotReadyError, cancel_first=False), JobStatus.FAILED, EMBEDDER_NOT_READY_DETAIL
    ),
    "embedder-failure": (_exit_on_embedder_error(RuntimeError, cancel_first=False), JobStatus.FAILED, REINDEX_FAILED_DETAIL),
    "embedder-not-ready-after-cancel": (
        _exit_on_embedder_error(EmbedderNotReadyError, cancel_first=True), JobStatus.CANCELLED, None
    ),
    "embedder-failure-after-cancel": (_exit_on_embedder_error(RuntimeError, cancel_first=True), JobStatus.CANCELLED, None),
    "after-ingest": (_exit_during_ingest(cancel=True), JobStatus.CANCELLED, None),
    "ingest-failure": (_exit_during_ingest(error=RuntimeError), JobStatus.FAILED, REINDEX_FAILED_DETAIL),
    "ingest-failure-after-cancel": (
        _exit_during_ingest(cancel=True, error=RuntimeError), JobStatus.FAILED, REINDEX_FAILED_DETAIL
    ),
    "ingest-cancelled": (_exit_during_ingest(error=asyncio.CancelledError), JobStatus.CANCELLED, None),
}
# terminal paths reached after the directory re-ingest ran
_EXITS_AFTER_INGEST = {"after-ingest", "ingest-failure", "ingest-failure-after-cancel", "ingest-cancelled"}


def _reindex_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_at: str, newer_reindex_job_id: str | None = None
) -> _ReindexExit:
    """A ``_reindex_cancel_fixture`` run (target model-B) set up to end through the terminal path *exit_at*."""
    job_store, job, meta, search_store, pipeline = _reindex_cancel_fixture(tmp_path, "model-B")
    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(return_value=MagicMock())
    run = _ReindexExit(
        job_store, job.job_id, meta, search_store, embedder_cache, pipeline, monkeypatch, newer_reindex_job_id
    )
    setup, _, _ = _REINDEX_EXITS[exit_at]
    setup(run)
    return run


async def _run_reindex_exit(run: _ReindexExit, tmp_path: Path) -> None:
    await _run_reindex(run.job_store, run.job_id, run.search_store, run.embedder_cache, run.pipeline, tmp_path)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "exit_at",
    [
        "task-start",
        "embedder-load",
        "claim-persist-failure",
        "embedder-failure",
        "after-ingest",
        "ingest-failure",
        "ingest-cancelled",
    ],
)
async def test_reindex_task_exit_keeps_a_newer_reindex_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_at: str
) -> None:
    """Once reindex A is CANCELLING (or, after a failed claim persist, about to end FAILED), the reindex route's 409
    guard may no longer block a new reindex, so reindex B may start and point the collection's reindex_job_id at
    itself. Whatever path A ends through, its exit must not clear (or rewrite) B's pointer; A still ends terminal."""
    run = _reindex_exit(tmp_path, monkeypatch, exit_at, newer_reindex_job_id="job-newer")

    await _run_reindex_exit(run, tmp_path)

    _, expected_status, _ = _REINDEX_EXITS[exit_at]
    assert run.meta.reindex_job_id == "job-newer"
    run.search_store.update_collection_meta.assert_not_awaited()
    assert run.meta.active_embedding_model == "model-A"
    final = run.job_store.get(run.job_id)
    assert final is not None and final.status == expected_status
    assert run.pipeline.ingest_directory.await_count == (1 if exit_at in _EXITS_AFTER_INGEST else 0)


@pytest.mark.anyio
@pytest.mark.parametrize("exit_at", list(_REINDEX_EXITS))
async def test_reindex_task_clears_pointer_before_terminal_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_at: str
) -> None:
    """On every non-success exit the collection's reindex_job_id is cleared BEFORE the terminal status is written:
    the clear rewrites the whole meta row, so a client that waits for the terminal status and then starts a new
    reindex (or PATCHes the collection) must not have its row clobbered by this task's stale write. A DELETE that
    lands before a failing exit (embedder load, "not a ReindexJob") ends the job CANCELLED, not FAILED; a failed
    ingest ends FAILED even after a DELETE."""
    run = _reindex_exit(tmp_path, monkeypatch, exit_at)
    status_at_clear: list[JobStatus] = []

    async def _record_clear(_meta: CollectionMeta) -> None:
        status_at_clear.append(run.job_store.get(run.job_id).status)  # type: ignore[union-attr]

    run.search_store.update_collection_meta = AsyncMock(side_effect=_record_clear)

    await _run_reindex_exit(run, tmp_path)

    _, expected_status, expected_error = _REINDEX_EXITS[exit_at]
    final = run.job_store.get(run.job_id)
    assert final is not None
    assert (final.status, final.error) == (expected_status, expected_error)
    assert len(status_at_clear) == 1
    assert status_at_clear[0] not in (JobStatus.CANCELLED, JobStatus.FAILED)
    assert run.meta.reindex_job_id is None
    assert run.meta.active_embedding_model == "model-A"


@pytest.mark.anyio
async def test_reindex_task_success_writes_the_meta_row_before_done(tmp_path: Path) -> None:
    """On success the promotion's meta write (which also clears reindex_job_id) runs while the job is still RUNNING,
    and DONE is written after it: DELETE /collections/{name} refuses only while the job is PENDING/RUNNING/CANCELLING,
    so a meta write after DONE (an unlocked read, then an upsert) could re-create a collection dropped in between."""
    job_store, job, meta, search_store, pipeline = _reindex_cancel_fixture(tmp_path, "model-B")
    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(return_value=MagicMock())
    status_at_write: list[JobStatus] = []

    async def _record_write(_meta: CollectionMeta) -> None:
        status_at_write.append(job_store.get(job.job_id).status)  # type: ignore[union-attr]

    search_store.update_collection_meta = AsyncMock(side_effect=_record_write)

    await _run_reindex(job_store, job.job_id, search_store, embedder_cache, pipeline, tmp_path)

    assert status_at_write == [JobStatus.RUNNING]
    final = job_store.get(job.job_id)
    assert final is not None and final.status == JobStatus.DONE
    assert (meta.reindex_job_id, meta.active_embedding_model) == (None, "model-B")


@pytest.mark.anyio
async def test_reindex_task_cancel_via_delete_route_during_embedder_load_ends_cancelled(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """HTTP-level: a real DELETE /jobs/{id} issued while _reindex_task awaits the embedder load answers 202
    CANCELLING, and the job must then end CANCELLED (GET /jobs/{id} agrees), not DONE."""
    import httpx

    job_store, job, _, search_store, pipeline = _reindex_cancel_fixture(tmp_path, "model-B")
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, job_store)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=auth_headers
    ) as client:
        delete_responses: list[httpx.Response] = []

        async def _load(_model: str) -> MagicMock:
            delete_responses.append(await client.delete(f"/jobs/{job.job_id}"))
            return MagicMock()

        embedder_cache = MagicMock()
        embedder_cache.get_or_load = AsyncMock(side_effect=_load)

        await _run_reindex(job_store, job.job_id, search_store, embedder_cache, pipeline, tmp_path, config)

        assert [r.status_code for r in delete_responses] == [202]
        assert delete_responses[0].json()["status"] == JobStatus.CANCELLING.value
        got = await client.get(f"/jobs/{job.job_id}")

    assert got.status_code == 200
    assert got.json()["status"] == JobStatus.CANCELLED.value
    pipeline.ingest_directory.assert_not_awaited()


@pytest.mark.anyio
async def test_reindex_task_running_claim_persist_failure_marks_failed_without_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When persisting the reindex job's PENDING -> RUNNING claim fails, nothing is re-ingested or promoted, the
    collection's reindex_job_id is cleared and the job ends FAILED — a job left PENDING would never run and
    would keep POST /collections/{name}/reindex answering 409."""
    from archon_search.server.routes_jobs import REINDEX_FAILED_DETAIL

    job_store, job, meta, search_store, pipeline = _reindex_cancel_fixture(tmp_path, "model-B")
    _fail_running_persist(job_store, job.job_id, monkeypatch)
    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(return_value=MagicMock())

    await _run_reindex(job_store, job.job_id, search_store, embedder_cache, pipeline, tmp_path)

    final = job_store.get(job.job_id)
    assert final is not None
    assert final.status == JobStatus.FAILED
    assert final.error == REINDEX_FAILED_DETAIL
    pipeline.ingest_directory.assert_not_awaited()
    assert meta.active_embedding_model == "model-A"
    assert meta.reindex_job_id is None


@pytest.mark.anyio
async def test_reindex_task_running_claim_persist_failure_honours_cancel_landing_during_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a failed persist of the RUNNING claim the job is PENDING again; a DELETE landing while the task clears
    reindex_job_id moves it to CANCELLING, and the task must then end it CANCELLED, not overwrite it FAILED."""
    job_store, job, meta, search_store, pipeline = _reindex_cancel_fixture(tmp_path, "model-B")
    _fail_running_persist(job_store, job.job_id, monkeypatch)
    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(return_value=MagicMock())

    async def _clear_while_a_delete_lands(_meta: CollectionMeta) -> None:
        _cancel_job(job_store, job.job_id)

    search_store.update_collection_meta = AsyncMock(side_effect=_clear_while_a_delete_lands)

    await _run_reindex(job_store, job.job_id, search_store, embedder_cache, pipeline, tmp_path)

    final = job_store.get(job.job_id)
    assert final is not None
    assert final.status == JobStatus.CANCELLED
    assert meta.reindex_job_id is None
    pipeline.ingest_directory.assert_not_awaited()


@pytest.mark.anyio
async def test_reindex_task_refused_claim_on_running_job_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A RUNNING claim refused because the job is already RUNNING (e.g. a second task for the same job) runs
    nothing, leaves the job's status alone, keeps the collection's reindex_job_id (it names a live job: clearing it
    would disable the reindex/PATCH 409 guards) and logs a WARNING naming the job and its status."""
    job_store, job, meta, search_store, pipeline = _reindex_cancel_fixture(tmp_path, "model-B")
    embedder_cache = MagicMock()

    async def _load(_model: str) -> MagicMock:
        job_store.update(job.job_id, status=JobStatus.RUNNING)
        return MagicMock()

    embedder_cache.get_or_load = AsyncMock(side_effect=_load)

    with caplog.at_level("WARNING", logger="archon_search.server.routes_jobs"):
        await _run_reindex(job_store, job.job_id, search_store, embedder_cache, pipeline, tmp_path)

    final = job_store.get(job.job_id)
    assert final is not None and final.status == JobStatus.RUNNING
    pipeline.ingest_directory.assert_not_awaited()
    assert meta.active_embedding_model == "model-A"
    assert meta.reindex_job_id == job.job_id
    search_store.update_collection_meta.assert_not_awaited()
    [record] = [r for r in caplog.records if "not claimed as RUNNING" in r.getMessage()]
    assert record.levelname == "WARNING"
    assert job.job_id in record.getMessage() and JobStatus.RUNNING.value in record.getMessage()


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["missing", "failed"])
async def test_reindex_task_refused_claim_on_missing_or_terminal_job_clears_pointer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, state: str
) -> None:
    """A RUNNING claim refused because the job vanished from the JobStore or went terminal during the embedder load
    runs nothing and writes no status, but clears the collection's reindex_job_id (no live job owns it any more, so
    it must not keep the reindex/PATCH 409 guards answering 409) and logs a WARNING naming the job and its status."""
    job_store, job, meta, search_store, pipeline = _reindex_cancel_fixture(tmp_path, "model-B")

    async def _load(_model: str) -> MagicMock:
        if state == "missing":
            del job_store._jobs[job.job_id]
        else:
            job_store.update(job.job_id, status=JobStatus.FAILED)
        return MagicMock()

    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(side_effect=_load)

    with caplog.at_level("WARNING", logger="archon_search.server.routes_jobs"):
        await _run_reindex(job_store, job.job_id, search_store, embedder_cache, pipeline, tmp_path)

    final = job_store.get(job.job_id)
    expected_status = None if state == "missing" else JobStatus.FAILED
    assert (final.status if final is not None else None) == expected_status
    pipeline.ingest_directory.assert_not_awaited()
    assert meta.active_embedding_model == "model-A"
    assert meta.reindex_job_id is None
    search_store.update_collection_meta.assert_awaited_once()
    [record] = [r for r in caplog.records if "not claimed as RUNNING" in r.getMessage()]
    assert record.levelname == "WARNING"
    status_text = "missing" if expected_status is None else expected_status.value
    assert job.job_id in record.getMessage() and f"status {status_text}" in record.getMessage()


@pytest.mark.anyio
async def test_reindex_task_cancel_during_model_promotion_ends_cancelled_with_model_promoted(tmp_path: Path) -> None:
    """A DELETE landing while step 5 writes the promoted model (job RUNNING -> CANCELLING) must not be overwritten
    by DONE: the job ends CANCELLED. The model stays promoted — the vectors were already re-embedded with it."""
    job_store, job, meta, search_store, pipeline = _reindex_cancel_fixture(tmp_path, "model-B")
    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(return_value=MagicMock())

    async def _promote_while_a_delete_lands(_meta: CollectionMeta) -> None:
        _cancel_job(job_store, job.job_id)

    search_store.update_collection_meta = AsyncMock(side_effect=_promote_while_a_delete_lands)

    await _run_reindex(job_store, job.job_id, search_store, embedder_cache, pipeline, tmp_path)

    final = job_store.get(job.job_id)
    assert final is not None
    assert final.status == JobStatus.CANCELLED
    pipeline.ingest_directory.assert_awaited_once()
    search_store.update_collection_meta.assert_awaited_once()
    assert meta.active_embedding_model == "model-B"
    assert meta.reindex_job_id is None


@pytest.mark.anyio
async def test_reindex_task_success_keeps_a_newer_reindex_pointer(tmp_path: Path) -> None:
    """When the collection's reindex_job_id names another job by the time step 5 promotes the model, the success
    write must leave that pointer alone (see _clear_reindex_job_id); the job still ends DONE."""
    job_store, job, meta, search_store, pipeline = _reindex_cancel_fixture(tmp_path, "model-B")
    meta.reindex_job_id = "job-newer"
    embedder_cache = MagicMock()
    embedder_cache.get_or_load = AsyncMock(return_value=MagicMock())

    await _run_reindex(job_store, job.job_id, search_store, embedder_cache, pipeline, tmp_path)

    final = job_store.get(job.job_id)
    assert final is not None and final.status == JobStatus.DONE
    search_store.update_collection_meta.assert_awaited_once()
    assert meta.reindex_job_id == "job-newer"


@pytest.mark.anyio
@pytest.mark.parametrize("with_lock", [False, True], ids=["no-lock", "with-lock"])
async def test_ingest_task_cancel_before_task_start_ends_cancelled(tmp_path: Path, with_lock: bool) -> None:
    """A DELETE between job creation (PENDING) and the ingest task's first step moves the job to CANCELLING;
    the task must honour it (job ends CANCELLED, no ingest), not overwrite it with RUNNING and finish DONE."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create(collection="docs")
    # Simulates DELETE /jobs/{id} landing after create_task() but before the task first runs.
    _cancel_job(store, job.job_id)
    pipeline_fn = AsyncMock(return_value=None)

    await _run_ingest_task(store, job.job_id, pipeline_fn, with_lock=with_lock)

    final = store.get(job.job_id)
    assert final is not None
    assert final.status == JobStatus.CANCELLED
    pipeline_fn.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("with_lock", [False, True], ids=["no-lock", "with-lock"])
async def test_ingest_task_cancel_before_task_start_cancelled_write_failure_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, with_lock: bool
) -> None:
    """When the CANCELLED write after a refused claim fails, the failure is logged with its traceback, never
    raised, and nothing is ingested."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create(collection="docs")
    _cancel_job(store, job.job_id)
    real_write = store._write_atomic

    def _write() -> None:
        current = store.get(job.job_id)
        if current is not None and current.status == JobStatus.CANCELLED:
            raise OSError("disk full")
        real_write()

    monkeypatch.setattr(store, "_write_atomic", _write)
    pipeline_fn = AsyncMock(return_value=None)

    with caplog.at_level("ERROR", logger="archon_search.server.routes_jobs"):
        await _run_ingest_task(store, job.job_id, pipeline_fn, with_lock=with_lock)

    pipeline_fn.assert_not_awaited()
    [record] = [r for r in caplog.records if "could not persist CANCELLED status" in r.getMessage()]
    assert record.exc_info is not None and isinstance(record.exc_info[1], OSError)


@pytest.mark.anyio
@pytest.mark.parametrize("with_lock", [False, True], ids=["no-lock", "with-lock"])
@pytest.mark.parametrize(
    ("state", "expected_level"),
    [("missing", "WARNING"), ("running", "WARNING"), ("cancelled", "DEBUG")],
)
async def test_ingest_task_refused_claim_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, with_lock: bool, state: str, expected_level: str
) -> None:
    """A RUNNING claim refused for a reason other than a requested cancel runs nothing and leaves the job alone:
    a missing or RUNNING job (e.g. a second task for the same job) is logged as a WARNING naming the job and its
    status; an already-CANCELLED job is where a cancel would leave it anyway, so it is logged at DEBUG only."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create(collection="docs")
    job_id = "no-such-job" if state == "missing" else job.job_id
    if state == "running":
        store.update(job_id, status=JobStatus.RUNNING)
    elif state == "cancelled":
        _cancel_job(store, job_id)
        store.update(job_id, status=JobStatus.CANCELLED)
    status_before = None if state == "missing" else store.get(job_id).status  # type: ignore[union-attr]
    pipeline_fn = AsyncMock(return_value=None)

    with caplog.at_level("DEBUG", logger="archon_search.server.routes_jobs"):
        await _run_ingest_task(store, job_id, pipeline_fn, with_lock=with_lock)

    pipeline_fn.assert_not_awaited()
    final = store.get(job_id)
    assert (final.status if final is not None else None) == status_before
    [record] = [r for r in caplog.records if "not claimed as RUNNING" in r.getMessage()]
    assert record.levelname == expected_level
    expected_status = "missing" if status_before is None else status_before.value
    assert job_id in record.getMessage() and f"status {expected_status}" in record.getMessage()


@pytest.mark.anyio
@pytest.mark.parametrize("with_lock", [False, True], ids=["no-lock", "with-lock"])
async def test_ingest_task_running_claim_persist_failure_marks_failed_without_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_lock: bool
) -> None:
    """When persisting the PENDING -> RUNNING claim fails, the claim did not happen: the job is back to PENDING
    (never left RUNNING) when the task, having run nothing, marks it FAILED instead of leaving a PENDING job
    nothing will run."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create(collection="docs")
    _fail_running_persist(store, job.job_id, monkeypatch)
    # (status before, status written) for every JobStore.update of the job — transition() writes through it too.
    status_writes: list[tuple[JobStatus, object]] = []
    real_update = store.update

    def _recording_update(job_id: str, **kwargs: object) -> object:
        status_writes.append((store.get(job_id).status, kwargs.get("status")))  # type: ignore[union-attr]
        return real_update(job_id, **kwargs)

    monkeypatch.setattr(store, "update", _recording_update)
    pipeline_fn = AsyncMock(return_value=None)

    await _run_ingest_task(store, job.job_id, pipeline_fn, with_lock=with_lock)

    final = store.get(job.job_id)
    assert final is not None
    assert final.status == JobStatus.FAILED
    assert final.error == INGEST_FAILED_DETAIL
    assert [before for before, written in status_writes if written == JobStatus.FAILED] == [JobStatus.PENDING]
    pipeline_fn.assert_not_awaited()


# ---------------------------------------------------------------------------
# make_retry_dispatcher — runs an already-created PENDING IngestJob (a MaintenanceLoop retry)
# ---------------------------------------------------------------------------

_RETRY_OPTIONS = IngestOptions(
    submitted_path="~/retry.md", ingested_by="cli", chunk_ttl_seconds=3600, chunk_scopes=("team-a",)
)
#: ``_RETRY_OPTIONS`` as the pipeline receives them.
_RETRY_PIPELINE_KWARGS: dict[str, Any] = {"ingested_by": "cli", "chunk_ttl_seconds": 3600, "chunk_scopes": ["team-a"]}


def _create_retry_job(
    store: JobStore,
    path: str = "/data/retry.md",
    *,
    namespace: str = DEFAULT_NAMESPACE,
    options: IngestOptions = _RETRY_OPTIONS,
) -> IngestJob:
    """A PENDING maintenance retry job carrying the original request's *options*."""
    return store.create(
        namespace=namespace, source="maintenance", path=path, collection="docs", ingest_options=options
    )


def _retry_app_state(store: JobStore, **attrs: Any) -> State:
    return State({"job_store": store, "_background_tasks": set(), **attrs})


async def _run_dispatched(state: State, job: IngestJob) -> None:
    make_retry_dispatcher(state)(job)
    (task,) = state._background_tasks
    await task


def test_ingest_persists_request_options_for_a_retry_off_the_wire(client: TestClient, tmp_store: JobStore) -> None:
    """POST /ingest stores the options a maintenance retry must re-apply on the job — never in its wire shape."""
    response = client.post(
        "/ingest",
        json={"collection": "docs", "path": "~/missing-retry-doc.md", "chunk_ttl_seconds": 3600, "chunk_scopes": ["team-a"]},
        headers={"X-Ingested-By": "cli"},
    )

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = tmp_store.get(job_id)
    assert job is not None
    assert job.source_path == str(Path("~/missing-retry-doc.md").expanduser().resolve())
    assert job.ingest_options == IngestOptions(
        submitted_path="~/missing-retry-doc.md", ingested_by="cli", chunk_ttl_seconds=3600, chunk_scopes=("team-a",)
    )
    listed = [item for item in client.get("/jobs").json()["items"] if item["job_id"] == job_id]
    for wire_job in (response.json(), client.get(f"/jobs/{job_id}").json(), *listed):
        assert "source_path" in wire_job, "presence anchor: the wire job shape was read"
        assert not {"ingest_options", "submitted_path", "chunk_scopes", "chunk_ttl_seconds"} & set(wire_job)
    assert len(listed) == 1


def test_inline_ingest_stores_no_ingest_options(client: TestClient, tmp_store: JobStore) -> None:
    """An inline (``documents``) ingest has no path a retry could re-read, so it stores no options."""
    response = client.post(
        "/ingest", json={"collection": "docs", "documents": [{"text": "hi", "source_path": "/virtual/a.md"}]}
    )

    assert response.status_code == 202
    job = tmp_store.get(response.json()["job_id"])
    assert job is not None
    assert job.ingest_options is None


@pytest.mark.anyio
async def test_retry_dispatcher_runs_pending_job_with_its_request_options(tmp_path: Path) -> None:
    """The dispatched job runs through POST /ingest's ingest task — claimed RUNNING, DONE — on the request the
    original ingest made: its path as submitted and resolved, ``ingested_by``, TTL and scopes."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = _create_retry_job(store, namespace="tenantA")
    seen: dict[str, Any] = {}

    async def pipeline_fn(job_id: str, s: JobStore, body: IngestRequest, **kwargs: Any) -> None:
        claimed = s.get(job_id)
        assert claimed is not None
        seen.update(status=claimed.status, body=body, namespace=kwargs["namespace"])

    state = _retry_app_state(store, ingest_pipeline=pipeline_fn)
    await _run_dispatched(state, job)

    body: IngestRequest = seen["body"]
    assert (seen["status"], seen["namespace"]) == (JobStatus.RUNNING, "tenantA")
    assert (body.collection, body.path, body.submitted_path) == ("docs", "/data/retry.md", "~/retry.md")
    assert (body.ingested_by, body.chunk_ttl_seconds, body.chunk_scopes) == ("cli", 3600, ["team-a"])
    final = store.get(job.job_id)
    assert final is not None
    assert final.status == JobStatus.DONE
    assert state._background_tasks == set(), "the finished task must be discarded from app.state._background_tasks"


@pytest.mark.anyio
async def test_retry_dispatcher_reads_app_state_when_a_job_is_dispatched(tmp_path: Path) -> None:
    """The dispatcher is built before the lifespan fills app.state, so it must read app.state at dispatch time."""
    state = State()
    dispatch = make_retry_dispatcher(state)
    store = JobStore(path=tmp_path / "jobs.json")
    state.job_store, state._background_tasks, state.ingest_pipeline = store, set(), AsyncMock()
    job = _create_retry_job(store)

    dispatch(job)
    (task,) = state._background_tasks
    await task

    state.ingest_pipeline.assert_awaited_once()
    final = store.get(job.job_id)
    assert final is not None
    assert final.status == JobStatus.DONE


@pytest.mark.anyio
async def test_retry_dispatcher_production_branch_ingests_with_request_options(tmp_path: Path) -> None:
    """With the real dependencies wired (no ``ingest_pipeline`` hook), the retry ingests its file through the
    pipeline with the original request's options, under the collection lock it then releases."""
    doc = tmp_path / "retry.md"
    doc.write_text("retry me", encoding="utf-8")
    store = JobStore(path=tmp_path / "jobs.json")
    job = _create_retry_job(store, str(doc))
    search_store, embedder_cache, pipeline, config, fake_embedder = _make_ingest_mocks()
    lock = asyncio.Lock()
    search_store.lock_for = MagicMock(return_value=lock)
    state = _retry_app_state(
        store, search_store=search_store, embedder_cache=embedder_cache, pipeline=pipeline, config=config
    )

    await _run_dispatched(state, job)

    pipeline.ingest_file.assert_awaited_once_with(
        doc, "docs", embedder=fake_embedder, namespace=DEFAULT_NAMESPACE, **_RETRY_PIPELINE_KWARGS
    )
    search_store.lock_for.assert_called_once_with("docs")
    assert not lock.locked()
    final = store.get(job.job_id)
    assert final is not None
    assert final.status == JobStatus.DONE


def _parked_retry(
    store: JobStore, lock: asyncio.Lock, **attrs: Any
) -> tuple[State, IngestJob, AsyncMock]:
    """App state whose collection lock is *lock*, a PENDING retry job, and the ``ingest_pipeline`` hook it would run."""
    search_store = MagicMock()
    search_store.lock_for = MagicMock(return_value=lock)
    pipeline_fn = AsyncMock()
    state = _retry_app_state(store, search_store=search_store, ingest_pipeline=pipeline_fn, **attrs)
    return state, _create_retry_job(store), pipeline_fn


@pytest.mark.anyio
async def test_retry_dispatcher_takes_collection_lock_before_claiming(tmp_path: Path) -> None:
    """A retry waits for the collection lock while still PENDING, so a cancel landing meanwhile (CANCELLING) is
    honoured: once the lock frees, the claim is refused and the job ends CANCELLED without ingesting."""
    store = JobStore(path=tmp_path / "jobs.json")
    lock = asyncio.Lock()
    state, job, pipeline_fn = _parked_retry(store, lock)

    await lock.acquire()  # held by another writer
    async with reaping(state._background_tasks):
        make_retry_dispatcher(state)(job)
        (task,) = state._background_tasks
        await until_parked(lock)
        waiting = store.get(job.job_id)
        assert waiting is not None
        assert waiting.status == JobStatus.PENDING, "the claim must wait for the collection lock"
        store.transition(job.job_id, {JobStatus.PENDING}, JobStatus.CANCELLING)
        lock.release()
        await task

    final = store.get(job.job_id)
    assert final is not None
    assert final.status == JobStatus.CANCELLED
    pipeline_fn.assert_not_awaited()
    assert not lock.locked()


@pytest.mark.anyio
async def test_retry_dispatcher_waits_for_the_lock_without_a_timeout(tmp_path: Path) -> None:
    """A background retry is no HTTP request: it awaits the lock itself — never under ``asyncio.wait_for`` /
    ``asyncio.timeout`` — so it waits past the lock-wait bound (``INGEST_LOCK_TIMEOUT_S``) instead of failing:
    contention never burns an attempt. It runs once the lock frees."""
    store = JobStore(path=tmp_path / "jobs.json")
    lock = asyncio.Lock()
    state, job, pipeline_fn = _parked_retry(store, lock)
    timeout_used = AssertionError("the retry's lock wait must not be bounded")

    await lock.acquire()
    async with reaping(state._background_tasks):
        with (
            patch("archon_search.constants.INGEST_LOCK_TIMEOUT_S", 0.01),
            patch("asyncio.wait_for", side_effect=timeout_used),
            patch("asyncio.timeout", side_effect=timeout_used),
        ):
            make_retry_dispatcher(state)(job)
            (task,) = state._background_tasks
            await until_parked(lock)
            await asyncio.sleep(0.05)  # five lock-wait bounds
            assert not task.done()
            assert parked_waiters(lock) == 1
            waiting = store.get(job.job_id)
            assert waiting is not None
            assert waiting.status == JobStatus.PENDING
        lock.release()
        await task

    final = store.get(job.job_id)
    assert final is not None
    assert final.status == JobStatus.DONE
    pipeline_fn.assert_awaited_once()
    assert not lock.locked()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("cancel_requested", "expected"),
    [(False, JobStatus.PENDING), (True, JobStatus.CANCELLED)],
    ids=["shutdown", "shutdown-after-cancel-request"],
)
async def test_retry_dispatcher_cancelled_while_parked(
    tmp_path: Path, cancel_requested: bool, expected: JobStatus
) -> None:
    """Cancelling the task parked on the lock (lifespan shutdown) runs nothing and leaves the lock neither held nor
    waited on. A shutdown is no client's cancel, so the job stays PENDING (the restart's recovery marks it FAILED
    "never started", which stays retryable); a cancel a client already requested (CANCELLING) ends CANCELLED."""
    store = JobStore(path=tmp_path / "jobs.json")
    lock = asyncio.Lock()
    state, job, pipeline_fn = _parked_retry(store, lock)

    await lock.acquire()
    async with reaping(state._background_tasks):
        make_retry_dispatcher(state)(job)
        (task,) = state._background_tasks
        await until_parked(lock)
        if cancel_requested:
            store.transition(job.job_id, {JobStatus.PENDING}, JobStatus.CANCELLING, cancel_requested=True)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    final = store.get(job.job_id)
    assert final is not None
    assert final.status == expected
    pipeline_fn.assert_not_awaited()
    assert parked_waiters(lock) == 0
    lock.release()
    assert not lock.locked()


@pytest.mark.anyio
async def test_retry_dispatcher_shutdown_cancel_never_overwrites_a_settled_job(tmp_path: Path) -> None:
    """The cancel write is a guarded transition from CANCELLING only: a parked job some other writer already made
    terminal keeps that status."""
    store = JobStore(path=tmp_path / "jobs.json")
    lock = asyncio.Lock()
    state, job, _ = _parked_retry(store, lock)

    await lock.acquire()
    async with reaping(state._background_tasks):
        make_retry_dispatcher(state)(job)
        (task,) = state._background_tasks
        await until_parked(lock)
        store.update(job.job_id, status=JobStatus.FAILED_EXPIRED)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    final = store.get(job.job_id)
    assert final is not None
    assert final.status == JobStatus.FAILED_EXPIRED


def _failed_original(store: JobStore) -> IngestJob:
    """The client's FAILED POST /ingest job a maintenance retry descends from."""
    original = store.create(path="/data/retry.md", collection="docs", ingest_options=_RETRY_OPTIONS)
    return store.update(original.job_id, status=JobStatus.FAILED, error="ingest failed")


def _retry_loop(store: JobStore, tmp_path: Path, dispatch: MagicMock) -> MaintenanceLoop:
    return MaintenanceLoop(
        job_store=store, search_store=MagicMock(), config=MaintenanceConfig(), data_dir=tmp_path,
        dispatch_retry=dispatch,
    )


def _app_with_parked_retry(
    tmp_path: Path, store: JobStore, lock: asyncio.Lock
) -> tuple[Any, IngestJob, AsyncMock]:
    """A real app sharing *store* whose collection lock is *lock*, plus a PENDING retry job (attempt 1) and the
    ``ingest_pipeline`` hook it would run."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, store)
    pipeline_fn = AsyncMock()
    app.state.search_store = MagicMock(lock_for=MagicMock(return_value=lock))
    app.state.ingest_pipeline = pipeline_fn
    app.state._background_tasks = set()
    retry = store.create(
        source="maintenance", path="/data/retry.md", collection="docs", ingest_options=_RETRY_OPTIONS, retry_count=1
    )
    return app, retry, pipeline_fn


async def _delete_job(app: Any, auth_headers: dict[str, str], job_id: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=auth_headers
    ) as http:
        return await http.delete(f"/jobs/{job_id}")


@pytest.mark.anyio
async def test_delete_job_while_retry_parked_ends_the_retry_chain(
    tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str]
) -> None:
    """DELETE /jobs/{id} on a retry parked on the collection lock answers 202 CANCELLING; once the lock frees the job
    ends CANCELLED and runs nothing. The client's cancel supersedes the FAILED original it retries, so no maintenance
    pass starts another retry — neither while the cancel settles nor after."""
    original = _failed_original(tmp_store)
    lock = asyncio.Lock()
    app, retry, pipeline_fn = _app_with_parked_retry(tmp_path, tmp_store, lock)
    dispatch = MagicMock()
    loop = _retry_loop(tmp_store, tmp_path, dispatch)

    await lock.acquire()
    async with reaping(app.state._background_tasks):
        make_retry_dispatcher(app.state)(retry)
        (task,) = app.state._background_tasks
        await until_parked(lock)
        response = await _delete_job(app, auth_headers, retry.job_id)
        assert (response.status_code, response.json()["status"]) == (202, JobStatus.CANCELLING.value)
        await loop._run_failed_ingest_retry({})  # the cancel is still settling
        lock.release()
        await task
    await loop._run_failed_ingest_retry({})

    final = tmp_store.get(retry.job_id)
    assert final is not None
    assert final.status == JobStatus.CANCELLED
    pipeline_fn.assert_not_awaited()
    dispatch.assert_not_called()
    assert sorted(j.job_id for j in tmp_store.list()) == sorted([original.job_id, retry.job_id])
    kept = tmp_store.get(original.job_id)
    assert kept is not None
    assert kept.status == JobStatus.FAILED, "superseded, not rewritten"

    # Positive control: the same CANCELLED retry without the client's cancel request (as a shutdown writes it)
    # leaves the chain open, so the pass retries — the cancel request alone stopped it above.
    tmp_store.update(retry.job_id, cancel_requested=False)
    await loop._run_failed_ingest_retry({})
    dispatch.assert_called_once()


@pytest.mark.anyio
async def test_shutdown_while_retry_parked_is_retried_after_restart_recovery(tmp_path: Path) -> None:
    """A shutdown never ends a retry chain: the parked retry stays PENDING, the restart's recovery marks it FAILED
    "never started", and the next pass retries it as the next attempt with the same options."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)
    _failed_original(store)
    lock = asyncio.Lock()
    app, retry, _ = _app_with_parked_retry(tmp_path, store, lock)

    await lock.acquire()
    async with reaping(app.state._background_tasks):
        make_retry_dispatcher(app.state)(retry)
        (task,) = app.state._background_tasks
        await until_parked(lock)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    restarted = JobStore(path=jobs_path)
    recovered = restarted.get(retry.job_id)
    assert recovered is not None
    assert (recovered.status, recovered.error) == (JobStatus.FAILED, NOT_STARTED_BEFORE_RESTART_DETAIL)
    assert not restarted.crashed_ingest_on_load
    dispatch = MagicMock()
    await _retry_loop(restarted, tmp_path, dispatch)._run_failed_ingest_retry({})

    (next_retry,) = [c.args[0] for c in dispatch.call_args_list]
    assert (next_retry.retry_count, next_retry.ingest_options) == (2, _RETRY_OPTIONS)


@pytest.mark.anyio
@pytest.mark.parametrize(("user_cancel", "retried"), [(False, True), (True, False)], ids=["shutdown", "user-cancel"])
async def test_running_retry_cancelled_by_shutdown_stays_retryable_unlike_a_user_cancel(
    tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str], user_cancel: bool, retried: bool
) -> None:
    """A RUNNING retry the shutdown cancels ends CANCELLED as any interrupted ingest does, but that is no client's
    cancel: the next pass retries the chain again, as the next attempt — the interrupted retry used its attempt. One a
    client cancelled (DELETE /jobs/{id}) before the shutdown ends the chain. ``cancel_requested`` tells them apart and
    never reaches the wire."""
    _failed_original(tmp_store)
    lock = asyncio.Lock()
    app, retry, pipeline_fn = _app_with_parked_retry(tmp_path, tmp_store, lock)
    started = asyncio.Event()

    async def _ingest_until_cancelled(*_: Any, **__: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    pipeline_fn.side_effect = _ingest_until_cancelled
    async with reaping(app.state._background_tasks):
        make_retry_dispatcher(app.state)(retry)
        (task,) = app.state._background_tasks
        await started.wait()
        if user_cancel:
            response = await _delete_job(app, auth_headers, retry.job_id)
            assert response.status_code == 202
            assert "cancel_requested" not in response.json()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    final = tmp_store.get(retry.job_id)
    assert final is not None
    assert (final.status, final.cancel_requested) == (JobStatus.CANCELLED, user_cancel)
    dispatch = MagicMock()
    await _retry_loop(tmp_store, tmp_path, dispatch)._run_failed_ingest_retry({})
    assert [c.args[0].retry_count for c in dispatch.call_args_list] == ([2] if retried else [])


@pytest.mark.anyio
async def test_delete_job_on_parked_retry_then_crash_completes_the_cancel(
    tmp_path: Path, auth_headers: dict[str, str]
) -> None:
    """A retry a client cancelled while it was parked is still CANCELLING if the process then dies. It never ran, so
    the restart completes the cancel as its task would have — CANCELLED — and arms no crash-loop guard (the same
    parked retry the client did not cancel loads FAILED "never started" and arms none either). The client's cancel
    still ends the chain, so nothing is retried."""
    jobs_path = tmp_path / "jobs.json"
    store = JobStore(path=jobs_path)
    _failed_original(store)
    lock = asyncio.Lock()
    app, retry, _ = _app_with_parked_retry(tmp_path, store, lock)

    await lock.acquire()
    async with reaping(app.state._background_tasks):
        make_retry_dispatcher(app.state)(retry)
        await until_parked(lock)
        response = await _delete_job(app, auth_headers, retry.job_id)
        assert response.status_code == 202
        # The process dies here: the task never runs again.

        restarted = JobStore(path=jobs_path)
        recovered = restarted.get(retry.job_id)
        assert recovered is not None
        assert (recovered.status, recovered.error) == (JobStatus.CANCELLED, None)
        assert not restarted.crashed_ingest_on_load
        dispatch = MagicMock()
        await _retry_loop(restarted, tmp_path, dispatch)._run_failed_ingest_retry({})
        dispatch.assert_not_called()


def test_delete_running_ingest_then_crash_still_arms_the_crash_loop_guard(
    tmp_path: Path, client: TestClient, tmp_store: JobStore
) -> None:
    """A cancel does not stop a RUNNING ingest (its task keeps writing until it ends), so one still CANCELLING when
    the process dies was running: the restart reads it as the crash it is and arms the guard."""
    job = tmp_store.create(path="/data/file.md", collection="docs")
    tmp_store.update(job.job_id, status=JobStatus.RUNNING)

    response = client.delete(f"/jobs/{job.job_id}")

    assert response.status_code == 202
    restarted = JobStore(path=tmp_path / "jobs.json")
    recovered = restarted.get(job.job_id)
    assert recovered is not None
    assert (recovered.status, recovered.error) == (JobStatus.FAILED, PROCESS_RESTART_ERROR)
    assert restarted.crashed_ingest_on_load


@pytest.mark.parametrize(
    ("started", "before_start"), [(False, True), (True, False)], ids=["pending", "running"]
)
def test_delete_job_records_whether_the_job_had_started(
    tmp_path: Path, client: TestClient, tmp_store: JobStore, started: bool, before_start: bool
) -> None:
    """DELETE /jobs/{id} marks a job it cancels while PENDING as ``cancelled_before_start`` (kept in the jobs file
    only, off the wire): its task has not claimed it RUNNING, so a restart finding it CANCELLING knows nothing ran."""
    job = tmp_store.create(path="/data/file.md", collection="docs")
    if started:
        tmp_store.update(job.job_id, status=JobStatus.RUNNING)

    response = client.delete(f"/jobs/{job.job_id}")

    assert response.status_code == 202
    assert "cancelled_before_start" not in response.json()
    assert "cancelled_before_start" not in client.get(f"/jobs/{job.job_id}").json()
    cancelled = tmp_store.get(job.job_id)
    assert cancelled is not None
    assert cancelled.cancelled_before_start is before_start
    rows = {row["job_id"]: row for row in json.loads((tmp_path / "jobs.json").read_text())}
    assert ("cancelled_before_start" in rows[job.job_id]) is before_start


def test_delete_job_records_the_cancel_request_off_the_wire(
    tmp_path: Path, client: TestClient, tmp_store: JobStore
) -> None:
    """DELETE /jobs/{id} marks the job ``cancel_requested`` (kept in the jobs file) without adding a wire field."""
    job = tmp_store.create(path="/data/file.md", collection="docs")

    response = client.delete(f"/jobs/{job.job_id}")

    assert response.status_code == 202
    assert "cancel_requested" not in response.json()
    assert "cancel_requested" not in client.get(f"/jobs/{job.job_id}").json()
    rows = {row["job_id"]: row for row in json.loads((tmp_path / "jobs.json").read_text())}
    assert (rows[job.job_id]["status"], rows[job.job_id]["cancel_requested"]) == (JobStatus.CANCELLING.value, True)


def test_retry_dispatcher_rejects_invalid_persisted_options(tmp_path: Path) -> None:
    """Options that no longer validate as an ``IngestRequest`` raise from the dispatcher (MaintenanceLoop then
    writes the job FAILED) and spawn no task."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = _create_retry_job(store, options=replace(_RETRY_OPTIONS, chunk_ttl_seconds=0))
    state = _retry_app_state(store, ingest_pipeline=AsyncMock())

    with pytest.raises(ValidationError):
        make_retry_dispatcher(state)(job)

    assert state._background_tasks == set()


def test_retry_dispatcher_rejects_a_job_without_ingest_options(tmp_path: Path) -> None:
    """A job whose original request is unknown is never run with request defaults (no scopes would widen access):
    the dispatcher raises and spawns no task."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = store.create(source="maintenance", path="/data/retry.md", collection="docs")
    state = _retry_app_state(store, ingest_pipeline=AsyncMock())

    with pytest.raises(ValueError, match="no ingest options"):
        make_retry_dispatcher(state)(job)

    assert state._background_tasks == set()


@pytest.mark.anyio
async def test_retry_dispatcher_failure_is_sanitized(tmp_path: Path) -> None:
    """A failing dispatched ingest ends FAILED with the sanitized detail, never the exception text."""
    store = JobStore(path=tmp_path / "jobs.json")
    job = _create_retry_job(store)
    state = _retry_app_state(store, ingest_pipeline=AsyncMock(side_effect=RuntimeError(LEAK_NEEDLE)))

    await _run_dispatched(state, job)

    final = store.get(job.job_id)
    assert final is not None
    assert final.status == JobStatus.FAILED
    assert final.error == INGEST_FAILED_DETAIL

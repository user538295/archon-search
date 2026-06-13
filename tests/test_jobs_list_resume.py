"""Integration tests for GET /jobs list endpoint (Task 6.1) and POST /jobs/{id}/resume (Task 6.2)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.types import ExportJob, ImportJob, JobStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def _make_mock_search_store() -> MagicMock:
    mock = MagicMock()
    mock.connect = AsyncMock()
    mock.disconnect = AsyncMock()
    mock.migrate_namespace = AsyncMock()
    mock.migrate_description_embedding = AsyncMock()
    mock.migrate_acl = AsyncMock()
    mock.migrate_centroid_sum = AsyncMock()
    mock.migrate_per_collection_model = AsyncMock()
    mock.get_all_collections_meta = AsyncMock(return_value=[])
    mock.get_collection_meta = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def client(tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str]) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store()
    return TestClient(app, headers=auth_headers)


# ---------------------------------------------------------------------------
# GET /jobs — basic
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_list_jobs_empty(client: TestClient) -> None:
    """Empty store returns items=[], next_cursor=null, total=0."""
    response = client.get("/jobs")
    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["next_cursor"] is None
    assert data["total"] == 0


@pytest.mark.integration
def test_list_jobs_default_limit(client: TestClient, tmp_store: JobStore) -> None:
    """60 jobs returns 50 items + a next_cursor; total == 60."""
    for _ in range(60):
        tmp_store.create(namespace=DEFAULT_NAMESPACE)

    response = client.get("/jobs")
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 50
    assert data["next_cursor"] is not None
    assert data["total"] == 60


@pytest.mark.integration
def test_list_jobs_filter_by_status(client: TestClient, tmp_store: JobStore) -> None:
    """?status=RUNNING filters to only RUNNING jobs."""
    j1 = tmp_store.create(namespace=DEFAULT_NAMESPACE)
    j2 = tmp_store.create(namespace=DEFAULT_NAMESPACE)
    tmp_store.update(j1.job_id, status=JobStatus.RUNNING)
    tmp_store.update(j2.job_id, status=JobStatus.DONE)

    response = client.get("/jobs?status=RUNNING")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["status"] == "RUNNING"


@pytest.mark.integration
def test_list_jobs_filter_by_kind(client: TestClient, tmp_store: JobStore) -> None:
    """?kind=export returns only ExportJobs."""
    tmp_store.create(namespace=DEFAULT_NAMESPACE)  # IngestJob
    tmp_store.create_export(
        collection="col",
        output_path="/tmp/out.tar.gz",
        tmp_path="/tmp/out.jsonl.tmp",
        namespace=DEFAULT_NAMESPACE,
    )

    response = client.get("/jobs?kind=export")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1


@pytest.mark.integration
def test_list_jobs_namespace_isolated(
    tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str]
) -> None:
    """Jobs from other namespaces are not visible."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    # Add a second namespace to the config so the middleware accepts it
    config.namespaces = {DEFAULT_NAMESPACE: DEFAULT_NAMESPACE, "other": "other"}
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store()

    # Create one job in the default namespace and one in "other"
    tmp_store.create(namespace=DEFAULT_NAMESPACE)
    tmp_store.create(namespace="other")

    client = TestClient(app, headers=auth_headers)
    response = client.get("/jobs")
    assert response.status_code == 200
    data = response.json()
    # The test API key maps to DEFAULT_NAMESPACE; only its job should appear
    assert data["total"] == 1


@pytest.mark.integration
def test_list_jobs_cursor_pagination(client: TestClient, tmp_store: JobStore) -> None:
    """Cursor advances through the full list without overlap or gaps."""
    for _ in range(5):
        tmp_store.create(namespace=DEFAULT_NAMESPACE)

    # First page: limit=3
    r1 = client.get("/jobs?limit=3")
    assert r1.status_code == 200
    d1 = r1.json()
    assert len(d1["items"]) == 3
    assert d1["next_cursor"] is not None
    assert d1["total"] == 5

    # Second page
    r2 = client.get(f"/jobs?limit=3&cursor={d1['next_cursor']}")
    assert r2.status_code == 200
    d2 = r2.json()
    assert len(d2["items"]) == 2
    assert d2["next_cursor"] is None

    # No overlap between pages
    ids1 = {item["job_id"] for item in d1["items"]}
    ids2 = {item["job_id"] for item in d2["items"]}
    assert ids1.isdisjoint(ids2)
    # Together they cover all 5
    assert len(ids1 | ids2) == 5


@pytest.mark.integration
def test_list_jobs_kind_ingest_excludes_export_import(
    client: TestClient, tmp_store: JobStore
) -> None:
    """?kind=ingest returns only IngestJob (exact type), not ExportJob or ImportJob subclasses."""
    tmp_store.create(namespace=DEFAULT_NAMESPACE)  # IngestJob
    tmp_store.create_export(
        collection="col",
        output_path="/tmp/out.tar.gz",
        tmp_path="/tmp/out.jsonl.tmp",
        namespace=DEFAULT_NAMESPACE,
    )
    tmp_store.create_import(
        collection="col",
        archive_path="/tmp/archive.tar.gz",
        force_overwrite=False,
        ignore_schema_version=False,
        on_error="fail",
        namespace=DEFAULT_NAMESPACE,
    )

    response = client.get("/jobs?kind=ingest")
    assert response.status_code == 200
    data = response.json()
    # Only the plain IngestJob; ExportJob and ImportJob are subclasses and must be excluded
    assert data["total"] == 1


@pytest.mark.integration
def test_get_jobs_source_filter_backup(client: TestClient, tmp_store: JobStore) -> None:
    """?source=backup returns only backup-sourced jobs."""
    tmp_store.create_export(
        collection="u",
        output_path="/tmp/u.tar.gz",
        tmp_path="/tmp/u.jsonl.tmp",
        namespace=DEFAULT_NAMESPACE,
        source="user",
    )
    backup_job = tmp_store.create_export(
        collection="b",
        output_path="/tmp/b.tar.gz",
        tmp_path="/tmp/b.jsonl.tmp",
        namespace=DEFAULT_NAMESPACE,
        source="backup",
    )

    response = client.get("/jobs?source=backup")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["job_id"] == backup_job.job_id


@pytest.mark.integration
def test_get_jobs_source_filter_user(client: TestClient, tmp_store: JobStore) -> None:
    """?source=user returns only user-sourced jobs."""
    user_job = tmp_store.create_export(
        collection="u",
        output_path="/tmp/u.tar.gz",
        tmp_path="/tmp/u.jsonl.tmp",
        namespace=DEFAULT_NAMESPACE,
        source="user",
    )
    tmp_store.create_export(
        collection="b",
        output_path="/tmp/b.tar.gz",
        tmp_path="/tmp/b.jsonl.tmp",
        namespace=DEFAULT_NAMESPACE,
        source="backup",
    )

    response = client.get("/jobs?source=user")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["job_id"] == user_job.job_id


@pytest.mark.integration
def test_get_jobs_source_filter_combined(client: TestClient, tmp_store: JobStore) -> None:
    """?source=user&source=backup returns both."""
    tmp_store.create_export(
        collection="u",
        output_path="/tmp/u.tar.gz",
        tmp_path="/tmp/u.jsonl.tmp",
        namespace=DEFAULT_NAMESPACE,
        source="user",
    )
    tmp_store.create_export(
        collection="b",
        output_path="/tmp/b.tar.gz",
        tmp_path="/tmp/b.jsonl.tmp",
        namespace=DEFAULT_NAMESPACE,
        source="backup",
    )

    response = client.get("/jobs?source=user&source=backup")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2


@pytest.mark.integration
def test_list_jobs_unauthenticated(tmp_path: Path, tmp_store: JobStore) -> None:
    """GET /jobs without a bearer token returns 401."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store()

    client = TestClient(app)  # no auth headers
    response = client.get("/jobs")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/resume — Task 6.2
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_resume_failed_export_job(client: TestClient, tmp_store: JobStore, tmp_path: Path) -> None:
    """A FAILED ExportJob with no progress (no checkpoint) transitions to QUEUED; returns 202."""
    # Create a tmp file so the existence check passes (no progress means no tmp check)
    job = tmp_store.create_export(
        collection="col",
        output_path=str(tmp_path / "out.tar.gz"),
        tmp_path=str(tmp_path / "out.jsonl.tmp"),
        namespace=DEFAULT_NAMESPACE,
    )
    tmp_store.update(job.job_id, status=JobStatus.FAILED)

    response = client.post(f"/jobs/{job.job_id}/resume")
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "QUEUED"
    assert data["job_id"] == job.job_id


@pytest.mark.integration
def test_resume_failed_import_job(client: TestClient, tmp_store: JobStore, tmp_path: Path) -> None:
    """A FAILED ImportJob transitions to QUEUED when archive exists; returns 202."""
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"fake")
    job = tmp_store.create_import(
        collection="col",
        archive_path=str(archive),
        force_overwrite=False,
        ignore_schema_version=False,
        on_error="fail",
        namespace=DEFAULT_NAMESPACE,
    )
    tmp_store.update(job.job_id, status=JobStatus.FAILED)

    response = client.post(f"/jobs/{job.job_id}/resume")
    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "QUEUED"
    assert data["job_id"] == job.job_id


@pytest.mark.integration
def test_resume_non_failed_job(client: TestClient, tmp_store: JobStore, tmp_path: Path) -> None:
    """A RUNNING ExportJob returns 409 with error=job_not_failed."""
    job = tmp_store.create_export(
        collection="col",
        output_path=str(tmp_path / "out.tar.gz"),
        tmp_path=str(tmp_path / "out.jsonl.tmp"),
        namespace=DEFAULT_NAMESPACE,
    )
    tmp_store.update(job.job_id, status=JobStatus.RUNNING)

    response = client.post(f"/jobs/{job.job_id}/resume")
    assert response.status_code == 409
    data = response.json()
    assert data["error"] == "job_not_failed"
    assert data["current_status"] == "RUNNING"


@pytest.mark.integration
def test_resume_missing_archive(client: TestClient, tmp_store: JobStore, tmp_path: Path) -> None:
    """A FAILED ImportJob whose archive is gone returns 422."""
    job = tmp_store.create_import(
        collection="col",
        archive_path=str(tmp_path / "nonexistent.tar.gz"),
        force_overwrite=False,
        ignore_schema_version=False,
        on_error="fail",
        namespace=DEFAULT_NAMESPACE,
    )
    tmp_store.update(job.job_id, status=JobStatus.FAILED)

    response = client.post(f"/jobs/{job.job_id}/resume")
    assert response.status_code == 422
    data = response.json()
    assert data["error"] == "source_not_found"


@pytest.mark.integration
def test_resume_ingest_job_not_resumable(client: TestClient, tmp_store: JobStore) -> None:
    """A FAILED IngestJob returns 409 with error=job_not_resumable."""
    job = tmp_store.create(namespace=DEFAULT_NAMESPACE)
    tmp_store.update(job.job_id, status=JobStatus.FAILED)

    response = client.post(f"/jobs/{job.job_id}/resume")
    assert response.status_code == 409
    data = response.json()
    assert data["error"] == "job_not_resumable"
    assert "only export and import jobs" in data["reason"]


@pytest.mark.integration
def test_resume_wrong_namespace(tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str]) -> None:
    """A job in a different namespace returns 404."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.namespaces = {DEFAULT_NAMESPACE: DEFAULT_NAMESPACE, "other": "other"}
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store()

    # Create job in "other" namespace
    job = tmp_store.create_export(
        collection="col",
        output_path=str(tmp_path / "out.tar.gz"),
        tmp_path=str(tmp_path / "out.jsonl.tmp"),
        namespace="other",
    )
    tmp_store.update(job.job_id, status=JobStatus.FAILED)

    # Request uses DEFAULT_NAMESPACE token — cannot see "other" namespace job
    client = TestClient(app, headers=auth_headers)
    response = client.post(f"/jobs/{job.job_id}/resume")
    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "not_found"

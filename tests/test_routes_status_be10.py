"""Tests for BE-10: failed_expired_ingest_count in GET /status (E0b C2, S13, S14).

Tests:
  - GET /status includes failed_expired_ingest_count=0 when no FAILED_EXPIRED jobs exist
  - GET /status includes failed_expired_ingest_count=N when N FAILED_EXPIRED IngestJobs are seeded
  - failed_expired_ingest_count is namespace-isolated
  - GET /jobs?status=FAILED_EXPIRED returns only FAILED_EXPIRED jobs
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.model import JobStatus
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(
    tmp_db: Path,
    job_store: JobStore,
    *,
    namespace_tokens: dict[str, str] | None = None,
    bearer: str | None = None,
) -> TestClient:
    """Build a TestClient backed by the provided job_store.

    Follows the pattern established in test_routes_status_be8.py.
    ``namespace_tokens`` maps raw tokens to namespace strings (TOML-style).
    When omitted, the default API key is used and the namespace is DEFAULT_NAMESPACE.
    """
    config = SearchConfig()
    config.db_path = str(tmp_db)
    if namespace_tokens:
        config.namespaces = namespace_tokens

    app = create_app(config, job_store)

    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    mock_store.pending_migrations = AsyncMock(return_value=[])
    app.state.search_store = mock_store

    if bearer is None:
        bearer = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {bearer}"})


def _seed_failed_expired_job(
    job_store: JobStore,
    namespace: str = DEFAULT_NAMESPACE,
) -> str:
    """Create an IngestJob in FAILED state then transition it to FAILED_EXPIRED."""
    job = job_store.create(namespace=namespace)
    job_store.update(job.job_id, status=JobStatus.FAILED)
    job_store.update(job.job_id, status=JobStatus.FAILED_EXPIRED)
    return job.job_id


# ---------------------------------------------------------------------------
# Unit test: empty job store → count=0
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_failed_expired_count_zero_when_no_failed_jobs(tmp_path: Path) -> None:
    """GET /status returns failed_expired_ingest_count=0 when no FAILED_EXPIRED jobs exist (BE-10 S14).

    Uses an empty job store (no jobs at all) to confirm the zero baseline.
    """
    tmp_db = tmp_path / "db"
    tmp_db.mkdir()
    job_store = JobStore(path=tmp_db / "jobs.json")

    client = _make_client(tmp_db, job_store)
    response = client.get("/status")

    assert response.status_code == 200
    data = response.json()
    assert "failed_expired_ingest_count" in data
    assert data["failed_expired_ingest_count"] == 0


# ---------------------------------------------------------------------------
# Integration test: seeded jobs → count reflects them
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_failed_expired_count_via_test_client(tmp_path: Path) -> None:
    """GET /status returns failed_expired_ingest_count=2 when 2 FAILED_EXPIRED jobs are seeded (BE-10 S14)."""
    tmp_db = tmp_path / "db"
    tmp_db.mkdir()
    job_store = JobStore(path=tmp_db / "jobs.json")

    _seed_failed_expired_job(job_store, DEFAULT_NAMESPACE)
    _seed_failed_expired_job(job_store, DEFAULT_NAMESPACE)

    client = _make_client(tmp_db, job_store)
    response = client.get("/status")

    assert response.status_code == 200
    data = response.json()
    assert "failed_expired_ingest_count" in data
    assert data["failed_expired_ingest_count"] == 2


@pytest.mark.integration
def test_status_failed_expired_count_does_not_count_other_statuses(tmp_path: Path) -> None:
    """failed_expired_ingest_count does not include FAILED or DONE jobs."""
    tmp_db = tmp_path / "db"
    tmp_db.mkdir()
    job_store = JobStore(path=tmp_db / "jobs.json")

    # 1 FAILED_EXPIRED — must be counted
    _seed_failed_expired_job(job_store, DEFAULT_NAMESPACE)

    # 1 FAILED — must NOT be counted
    failed_job = job_store.create(namespace=DEFAULT_NAMESPACE)
    job_store.update(failed_job.job_id, status=JobStatus.FAILED)

    # 1 DONE — must NOT be counted
    done_job = job_store.create(namespace=DEFAULT_NAMESPACE)
    job_store.update(done_job.job_id, status=JobStatus.DONE)

    client = _make_client(tmp_db, job_store)
    response = client.get("/status")

    assert response.status_code == 200
    data = response.json()
    assert data["failed_expired_ingest_count"] == 1


# ---------------------------------------------------------------------------
# Integration test: subclass exclusion (ExportJob must not be counted)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_failed_expired_count_excludes_export_jobs(tmp_path: Path) -> None:
    """failed_expired_ingest_count does not count ExportJob instances with FAILED_EXPIRED (BE-10 Fix 2).

    Seeds 1 IngestJob with FAILED_EXPIRED and 1 ExportJob with FAILED_EXPIRED in the
    same namespace.  Only the IngestJob must be reflected in the count — ExportJob
    is a subclass of IngestJob and must be excluded by the ``type(j) is IngestJob`` guard.
    """
    tmp_db = tmp_path / "db"
    tmp_db.mkdir()
    job_store = JobStore(path=tmp_db / "jobs.json")

    # 1 base IngestJob → must be counted
    _seed_failed_expired_job(job_store, DEFAULT_NAMESPACE)

    # 1 ExportJob transitioned to FAILED_EXPIRED → must NOT be counted
    export_job = job_store.create_export(
        collection="",
        output_path="",
        tmp_path="",
        namespace=DEFAULT_NAMESPACE,
    )
    job_store.update(export_job.job_id, status=JobStatus.FAILED_EXPIRED)

    client = _make_client(tmp_db, job_store)
    response = client.get("/status")

    assert response.status_code == 200
    data = response.json()
    assert data["failed_expired_ingest_count"] == 1


# ---------------------------------------------------------------------------
# Integration test: namespace isolation
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_failed_expired_count_namespace_isolated(tmp_path: Path) -> None:
    """failed_expired_ingest_count is scoped to the caller's namespace (BE-10 S14).

    Seeds 2 FAILED_EXPIRED jobs in ns-A and 3 in ns-B.
    GET /status as ns-A must return failed_expired_ingest_count=2.
    GET /status as ns-B must return failed_expired_ingest_count=3.
    """
    tmp_db = tmp_path / "db"
    tmp_db.mkdir()
    job_store = JobStore(path=tmp_db / "jobs.json")

    # 2 jobs in ns-A
    _seed_failed_expired_job(job_store, "nsA")
    _seed_failed_expired_job(job_store, "nsA")

    # 3 jobs in ns-B (must not appear in ns-A's status response)
    _seed_failed_expired_job(job_store, "nsB")
    _seed_failed_expired_job(job_store, "nsB")
    _seed_failed_expired_job(job_store, "nsB")

    namespace_tokens = {"token-A": "nsA", "token-B": "nsB"}
    client_a = _make_client(tmp_db, job_store, namespace_tokens=namespace_tokens, bearer="token-A")

    response = client_a.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "failed_expired_ingest_count" in data
    assert data["failed_expired_ingest_count"] == 2

    client_b = _make_client(tmp_db, job_store, namespace_tokens=namespace_tokens, bearer="token-B")
    response_b = client_b.get("/status")
    assert response_b.status_code == 200
    data_b = response_b.json()
    assert "failed_expired_ingest_count" in data_b
    assert data_b["failed_expired_ingest_count"] == 3


# ---------------------------------------------------------------------------
# Integration test: GET /jobs?status=FAILED_EXPIRED (S13)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_jobs_filter_by_failed_expired_status(tmp_path: Path) -> None:
    """GET /jobs?status=FAILED_EXPIRED returns only FAILED_EXPIRED jobs in the namespace (BE-10 S13).

    Seeds 1 FAILED_EXPIRED, 1 DONE, and 1 FAILED job; asserts only the FAILED_EXPIRED job
    appears in the filtered response — confirming FAILED is not matched by a naive prefix match.
    """
    tmp_db = tmp_path / "db"
    tmp_db.mkdir()
    job_store = JobStore(path=tmp_db / "jobs.json")

    expired_id = _seed_failed_expired_job(job_store, DEFAULT_NAMESPACE)

    done_job = job_store.create(namespace=DEFAULT_NAMESPACE)
    job_store.update(done_job.job_id, status=JobStatus.DONE)

    failed_job = job_store.create(namespace=DEFAULT_NAMESPACE)
    job_store.update(failed_job.job_id, status=JobStatus.FAILED)

    client = _make_client(tmp_db, job_store)
    response = client.get("/jobs?status=FAILED_EXPIRED")

    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    items = data["items"]
    assert len(items) == 1
    assert items[0]["job_id"] == expired_id
    assert items[0]["status"] == "FAILED_EXPIRED"

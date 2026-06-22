"""Integration tests for POST /backup/trigger endpoint — D2 Task 4.1."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search._types import CollectionInfo
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


def _make_mock_search_store(collections: list[CollectionInfo] | None = None) -> MagicMock:
    mock = MagicMock()
    mock.connect = AsyncMock()
    mock.disconnect = AsyncMock()
    mock._run_startup_migrations = AsyncMock()
    mock.get_all_collections_meta = AsyncMock(return_value=[])
    mock.get_collection_meta = AsyncMock(return_value=None)
    mock.list_collections = AsyncMock(return_value=collections or [])
    return mock


@pytest.fixture
def tmp_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def _make_client(
    tmp_path: Path,
    tmp_store: JobStore,
    auth_headers: dict[str, str],
    collections: list[CollectionInfo],
    exclude: list[str] | None = None,
) -> TestClient:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.backup.output_dir = str(tmp_path / "backups")
    cfg.backup.exclude = exclude or []
    # Enable backup loop so dedup/exclusion machinery exists on app.state
    cfg.backup.interval_hours = 1
    app = create_app(cfg, tmp_store)
    app.state.search_store = _make_mock_search_store(collections=collections)
    return TestClient(app, headers=auth_headers)


@pytest.mark.integration
def test_trigger_backup_returns_202_with_job_ids(
    tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str]
) -> None:
    """Non-excluded collection in caller's namespace → 202 with queued non-empty."""
    collections = [CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE)]
    client = _make_client(tmp_path, tmp_store, auth_headers, collections)
    with client:
        response = client.post("/backup/trigger")
    assert response.status_code == 202
    body = response.json()
    assert "queued" in body
    assert "skipped" in body
    assert len(body["queued"]) == 1
    assert body["skipped"] == []


@pytest.mark.integration
def test_trigger_backup_excluded_collection_skipped(
    tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str]
) -> None:
    collections = [CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE)]
    client = _make_client(tmp_path, tmp_store, auth_headers, collections, exclude=["docs"])
    with client:
        response = client.post("/backup/trigger")
    assert response.status_code == 202
    body = response.json()
    assert body["queued"] == []
    assert len(body["skipped"]) == 1
    assert body["skipped"][0] == {"collection": "docs", "reason": "excluded"}


@pytest.mark.integration
def test_trigger_backup_already_active_skipped(
    tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str]
) -> None:
    collections = [CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE)]
    client = _make_client(tmp_path, tmp_store, auth_headers, collections)
    with client:
        # Pre-populate in_flight tracker
        client.app.state.backup_loop.track("preexisting-job", DEFAULT_NAMESPACE, "docs")
        response = client.post("/backup/trigger")
    assert response.status_code == 202
    body = response.json()
    assert body["queued"] == []
    assert body["skipped"] == [{"collection": "docs", "reason": "already_active"}]


@pytest.mark.integration
def test_trigger_backup_already_queued_skipped(
    tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str]
) -> None:
    collections = [CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE)]
    client = _make_client(tmp_path, tmp_store, auth_headers, collections)
    with client:
        # Pre-queue a backup-sourced export job for (ns, docs), without tracking it in_flight.
        tmp_store.create_export(
            "docs",
            str(tmp_path / "backups" / DEFAULT_NAMESPACE / "docs.backup.preexisting.tar.gz"),
            str(tmp_path / "backups" / DEFAULT_NAMESPACE / "docs.backup.preexisting.tmp"),
            namespace=DEFAULT_NAMESPACE,
            source="backup",
        )
        response = client.post("/backup/trigger")
    assert response.status_code == 202
    body = response.json()
    assert body["queued"] == []
    assert body["skipped"] == [{"collection": "docs", "reason": "already_queued"}]


@pytest.mark.integration
def test_trigger_backup_namespace_scoped(
    tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str]
) -> None:
    """Only collections in caller's namespace are enqueued; other-ns collections ignored."""
    collections = [
        CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE),
        CollectionInfo(name="other", doc_count=1, chunk_count=1, namespace="tenant-a"),
    ]
    client = _make_client(tmp_path, tmp_store, auth_headers, collections)
    with client:
        response = client.post("/backup/trigger")
    assert response.status_code == 202
    body = response.json()
    assert len(body["queued"]) == 1
    # Verify the queued job is for the default namespace
    job_id = body["queued"][0]
    job = tmp_store.get(job_id)
    assert job is not None
    assert job.namespace == DEFAULT_NAMESPACE
    assert job.collection == "docs"


@pytest.mark.integration
def test_trigger_backup_unauthenticated_returns_401(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.backup.output_dir = str(tmp_path / "backups")
    cfg.backup.interval_hours = 1
    app = create_app(cfg, tmp_store)
    app.state.search_store = _make_mock_search_store(collections=[])
    no_auth = TestClient(app)
    response = no_auth.post("/backup/trigger")
    assert response.status_code == 401


@pytest.mark.integration
def test_trigger_backup_tracks_job_in_backup_loop(
    tmp_path: Path, tmp_store: JobStore, auth_headers: dict[str, str]
) -> None:
    collections = [CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE)]
    client = _make_client(tmp_path, tmp_store, auth_headers, collections)
    with client:
        response = client.post("/backup/trigger")
        assert response.status_code == 202
        backup_loop = client.app.state.backup_loop
        assert backup_loop.is_collection_in_flight(DEFAULT_NAMESPACE, "docs") is True

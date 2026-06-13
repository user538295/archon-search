"""Integration tests for POST /collections/{name}/export endpoint (Task 4.2)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.store import JobStore
from archon_search.paths import get_data_dir
from archon_search.server.app import create_app
from archon_search.types import JobStatus


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def _make_mock_search_store(collection_meta: CollectionMeta | None = None) -> MagicMock:
    """Return a fully-mocked SearchStore that satisfies the lifespan startup."""
    mock = MagicMock()
    mock.connect = AsyncMock()
    mock.disconnect = AsyncMock()
    mock.migrate_namespace = AsyncMock()
    mock.migrate_description_embedding = AsyncMock()
    mock.migrate_acl = AsyncMock()
    mock.migrate_centroid_sum = AsyncMock()
    mock.migrate_per_collection_model = AsyncMock()
    mock.get_all_collections_meta = AsyncMock(return_value=[])
    mock.get_collection_meta = AsyncMock(return_value=collection_meta)
    return mock


@pytest.fixture
def meta() -> CollectionMeta:
    return CollectionMeta(
        name="my-collection",
        namespace=DEFAULT_NAMESPACE,
        active_embedding_model="BAAI/bge-small-en-v1.5",
        description="Test collection",
    )


@pytest.fixture
def client(
    tmp_path: Path,
    tmp_store: JobStore,
    auth_headers: dict[str, str],
    meta: CollectionMeta,
) -> TestClient:
    """Client with a mocked SearchStore that reports 'my-collection' exists."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store(meta)
    return TestClient(app, headers=auth_headers)


@pytest.fixture
def client_no_collection(
    tmp_path: Path,
    tmp_store: JobStore,
    auth_headers: dict[str, str],
) -> TestClient:
    """Client with a mocked SearchStore that reports no collection exists."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store(collection_meta=None)
    return TestClient(app, headers=auth_headers)


# ---------------------------------------------------------------------------
# POST /collections/{name}/export
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_post_export_returns_202_and_job_id(client: TestClient) -> None:
    """Valid collection with valid output_path returns 202 with a job_id."""
    output_dir = str(get_data_dir() / "exports")
    response = client.post(
        "/collections/my-collection/export",
        json={"output_path": output_dir},
    )
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == JobStatus.QUEUED.value


@pytest.mark.integration
def test_post_export_default_output_path(client: TestClient) -> None:
    """Empty output_path defaults to get_data_dir() / 'exports' and returns 202."""
    response = client.post(
        "/collections/my-collection/export",
        json={},
    )
    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == JobStatus.QUEUED.value


@pytest.mark.integration
def test_post_export_collection_not_found(client_no_collection: TestClient) -> None:
    """Unknown collection returns 404."""
    output_dir = str(get_data_dir() / "exports")
    response = client_no_collection.post(
        "/collections/unknown-collection/export",
        json={"output_path": output_dir},
    )
    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "not_found"


@pytest.mark.integration
def test_post_export_path_outside_allowed(client: TestClient) -> None:
    """Path outside get_data_dir() returns 400 with error='path_unsafe'."""
    response = client.post(
        "/collections/my-collection/export",
        json={"output_path": "/tmp/evil-export-dir"},
    )
    assert response.status_code == 400
    data = response.json()
    assert data["error"] == "path_unsafe"


@pytest.mark.integration
def test_post_export_unauthenticated(tmp_path: Path, tmp_store: JobStore) -> None:
    """Request without Bearer token returns 401."""
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    app = create_app(config, tmp_store)
    app.state.search_store = _make_mock_search_store()
    # No auth headers
    no_auth_client = TestClient(app)
    output_dir = str(get_data_dir() / "exports")
    response = no_auth_client.post(
        "/collections/my-collection/export",
        json={"output_path": output_dir},
    )
    assert response.status_code == 401

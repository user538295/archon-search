"""Tests for GET/POST/DELETE /collections/* endpoints (Task 5.7)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.model import JobStatus
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.sync import path_to_collection_name


@pytest.fixture
def tmp_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


@pytest.fixture
def config(tmp_path: Path) -> SearchConfig:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    return cfg


@pytest.fixture
def client(config: SearchConfig, tmp_store: JobStore, auth_headers: dict[str, str]) -> TestClient:
    app = create_app(config, tmp_store)
    return TestClient(app, headers=auth_headers)


# ---------------------------------------------------------------------------
# GET /collections/
# ---------------------------------------------------------------------------


def test_list_collections_empty(client: TestClient) -> None:
    """Empty config returns empty list."""
    response = client.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    assert data == []


def test_list_collections_shows_configured(tmp_path: Path, tmp_store: JobStore) -> None:
    """Collections in config appear in the list with correct name."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get("/collections/")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    entry = data[0]
    assert "name" in entry
    assert "path" in entry
    assert "description" in entry
    assert "doc_count" in entry
    assert "chunk_count" in entry
    assert "status" in entry

    # Fix 8: assert name matches path_to_collection_name
    expected_name = path_to_collection_name(str(src))
    assert entry["name"] == expected_name


# ---------------------------------------------------------------------------
# POST /collections/
# ---------------------------------------------------------------------------


def test_add_collection_persists_and_starts_ingest(
    tmp_path: Path, config: SearchConfig, tmp_store: JobStore
) -> None:
    """POST /collections/ persists the path and returns an IngestJob (202)."""
    src = tmp_path / "myproject"
    src.mkdir()
    app = create_app(config, tmp_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post("/collections/", json={"path": str(src)})

    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == JobStatus.PENDING.value

    # Config was updated
    updated_config: SearchConfig = app.state.config
    assert str(src.resolve()) in updated_config.collections

    # Fix 10: job exists in store
    assert tmp_store.get(data["job_id"]) is not None


def test_add_duplicate_collection_returns_409(
    tmp_path: Path, config: SearchConfig, tmp_store: JobStore
) -> None:
    """POST /collections/ twice with same path returns 409 on second call."""
    src = tmp_path / "myproject"
    src.mkdir()
    app = create_app(config, tmp_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        first = c.post("/collections/", json={"path": str(src)})
        second = c.post("/collections/", json={"path": str(src)})

    assert first.status_code == 202
    assert second.status_code == 409
    assert "already registered" in second.json()["detail"]


def test_add_collection_missing_path_returns_422(client: TestClient) -> None:
    """POST /collections/ without path is a validation error."""
    response = client.post("/collections/", json={})
    assert response.status_code == 422


def test_add_collection_already_pinned_returns_409(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/ with a path already in pinned_collections returns 409."""
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.pinned_collections = [str(pinned)]
    app = create_app(cfg, tmp_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.post("/collections/", json={"path": str(pinned)})
    assert response.status_code == 409
    assert "already registered" in response.json()["detail"]


# ---------------------------------------------------------------------------
# DELETE /collections/{name}
# ---------------------------------------------------------------------------


def test_remove_collection_deletes_config_and_data(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """DELETE /collections/{name} removes path from config and drops LanceDB data."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)

    # Fix 9: wire a mock search_store and assert drop_collection was called
    mock_search_store = MagicMock()
    mock_search_store.drop_collection = AsyncMock()
    app.state.search_store = mock_search_store

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    name = path_to_collection_name(str(src))

    response = c.delete(f"/collections/{name}")

    assert response.status_code == 200
    # Path removed from config
    updated_config: SearchConfig = app.state.config
    assert str(src) not in updated_config.collections

    # LanceDB drop was called
    mock_search_store.drop_collection.assert_called_once_with(name)


def test_remove_unknown_collection_returns_404(client: TestClient) -> None:
    """DELETE /collections/{name} for unknown name returns 404."""
    response = client.delete("/collections/nonexistent-collection")
    assert response.status_code == 404


def test_remove_pinned_only_collection_rejected(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """DELETE rejects a path that is in pinned_collections but NOT in collections."""
    src = tmp_path / "pinned-only"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.pinned_collections = [str(src)]
    # NOT in cfg.collections
    app = create_app(cfg, tmp_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    name = path_to_collection_name(str(src))

    response = c.delete(f"/collections/{name}")
    assert response.status_code in (400, 409)
    data = response.json()
    assert "detail" in data


# ---------------------------------------------------------------------------
# GET /collections/{name}
# ---------------------------------------------------------------------------


def test_get_collection_info(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """GET /collections/{name} returns CollectionDetail fields."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    name = path_to_collection_name(str(src))

    response = c.get(f"/collections/{name}")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == name
    assert "path" in data
    assert "embedding_model" in data
    assert "centroid_present" in data
    assert "last_indexed" in data

    # Fix 8: assert embedding_model value matches config
    assert data["embedding_model"] == cfg.embedding_model


def test_get_collection_info_unknown_returns_404(client: TestClient) -> None:
    """GET /collections/{name} for unknown name returns 404."""
    response = client.get("/collections/nonexistent-collection")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /collections/{name}/reindex
# ---------------------------------------------------------------------------


def test_reindex_returns_ingest_job(
    tmp_path: Path, tmp_store: JobStore
) -> None:
    """POST /collections/{name}/reindex starts a job and returns IngestJob (202)."""
    src = tmp_path / "docs"
    src.mkdir()
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.collections = [str(src)]
    app = create_app(cfg, tmp_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    name = path_to_collection_name(str(src))

    with patch("archon_search.server.routes_collections.asyncio.create_task",
               side_effect=lambda coro: (coro.close(), MagicMock())[1]):
        response = c.post(f"/collections/{name}/reindex")

    assert response.status_code == 202
    data = response.json()
    assert "job_id" in data
    assert data["status"] == JobStatus.PENDING.value


def test_reindex_unknown_collection_returns_404(client: TestClient) -> None:
    """POST /collections/{name}/reindex for unknown collection returns 404."""
    response = client.post("/collections/nonexistent-collection/reindex")
    assert response.status_code == 404

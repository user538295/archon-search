"""Tests for GET /indexing-state endpoint (Task 5.4)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus, IndexingStateStore
from archon_search.server.app import create_app


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "search"
    db.mkdir()
    return db


def _make_mock_store(meta_rows: list[CollectionMeta]) -> MagicMock:
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=meta_rows)
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    return mock_store


def _make_client(tmp_db: Path, *, mock_store: MagicMock | None = None) -> TestClient:
    from archon_search.constants import DEFAULT_NAMESPACE

    config = SearchConfig()
    config.db_path = str(tmp_db)
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)
    # Always inject a mock store to avoid requiring a real DB connection.
    # Default: no collections (safe fallback for tests that check empty state).
    app.state.search_store = mock_store or _make_mock_store([])
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def _make_client_with_state(tmp_db: Path, state: IndexingState, *, mock_store: MagicMock | None = None) -> TestClient:
    from archon_search.constants import DEFAULT_NAMESPACE

    store_obj = IndexingStateStore(tmp_db)
    store_obj.write(state)
    # Default mock: all state collections belong to the default namespace so
    # existing tests (which send no API key → default namespace) still pass.
    if mock_store is None:
        mock_store = _make_mock_store(
            [CollectionMeta(name=n, namespace=DEFAULT_NAMESPACE) for n in state.collections]
        )
    return _make_client(tmp_db, mock_store=mock_store)


def test_indexing_state_empty_when_no_file(tmp_db: Path) -> None:
    """GET /indexing-state returns {} when no state file exists."""
    c = _make_client(tmp_db)
    response = c.get("/indexing-state")
    assert response.status_code == 200
    assert response.json() == {}


def test_indexing_state_returns_collections(tmp_db: Path) -> None:
    """GET /indexing-state includes both collections from state with correct values."""
    state = IndexingState(
        collections={
            "docs": CollectionProgress(status=IndexingStatus.DONE, total_files=5, processed_files=5),
            "notes": CollectionProgress(status=IndexingStatus.PENDING, total_files=5),
        }
    )
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/indexing-state")
    assert response.status_code == 200
    data = response.json()
    assert "collections" in data
    assert "docs" in data["collections"]
    assert "notes" in data["collections"]
    assert data["collections"]["docs"]["status"] == "done"
    assert data["collections"]["notes"]["total_files"] == 5


def test_indexing_state_exact_shared_shape(tmp_db: Path) -> None:
    """Response includes top-level last_updated, trigger, and collections keys."""
    state = IndexingState(trigger="install")
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/indexing-state")
    assert response.status_code == 200
    data = response.json()
    assert "last_updated" in data
    assert "trigger" in data
    assert "collections" in data
    assert data["trigger"] == "install"
    assert isinstance(data["last_updated"], str)
    assert isinstance(data["collections"], dict)


def test_indexing_state_fields_present(tmp_db: Path) -> None:
    """Per-collection entries include status, processed_files, total_files, error, error_count."""
    state = IndexingState(
        collections={
            "repo": CollectionProgress(
                status=IndexingStatus.FAILED,
                total_files=10,
                processed_files=3,
                error="disk full",
                error_count=2,
            )
        }
    )
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/indexing-state")
    assert response.status_code == 200
    col = response.json()["collections"]["repo"]
    assert "status" in col
    assert col["status"] == "failed"
    assert "processed_files" in col
    assert "total_files" in col
    assert "error" in col
    assert "error_count" in col
    assert col["processed_files"] == 3
    assert col["total_files"] == 10
    assert col["error"] == "disk full"
    assert col["error_count"] == 2


def test_indexing_state_status_values_match_persisted_schema(tmp_db: Path) -> None:
    """Status strings match the persisted schema values: pending, in_progress, done, failed."""
    valid_statuses = {"pending", "in_progress", "done", "failed"}
    state = IndexingState(
        collections={
            "a": CollectionProgress(status=IndexingStatus.PENDING),
            "b": CollectionProgress(status=IndexingStatus.IN_PROGRESS),
            "c": CollectionProgress(status=IndexingStatus.DONE),
            "d": CollectionProgress(status=IndexingStatus.FAILED),
        }
    )
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/indexing-state")
    assert response.status_code == 200
    collections = response.json()["collections"]
    for name, col in collections.items():
        assert col["status"] in valid_statuses, f"Collection {name!r} has invalid status {col['status']!r}"
    assert collections["a"]["status"] == "pending"
    assert collections["b"]["status"] == "in_progress"
    assert collections["c"]["status"] == "done"
    assert collections["d"]["status"] == "failed"


def test_indexing_state_corrupt_file(tmp_db: Path) -> None:
    """GET /indexing-state returns {} when the state file contains garbage bytes."""
    store = IndexingStateStore(tmp_db)
    store._state_file.parent.mkdir(parents=True, exist_ok=True)
    store._state_file.write_text("not valid json {{{{ garbage !!!!", encoding="utf-8")
    c = _make_client(tmp_db)
    response = c.get("/indexing-state")
    assert response.status_code == 200
    assert response.json() == {}


def test_indexing_state_no_internal_fields_in_response(tmp_db: Path) -> None:
    """Response must not include processed_paths, file_mtimes, or file_hashes."""
    state = IndexingState(
        collections={
            "repo": CollectionProgress(
                status=IndexingStatus.DONE,
                total_files=2,
                processed_files=2,
                processed_paths=["/a/b.txt", "/a/c.txt"],
                file_mtimes={"/a/b.txt": 1234567.0, "/a/c.txt": 1234568.0},
                file_hashes={"/a/b.txt": "abc", "/a/c.txt": "def"},
            )
        }
    )
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/indexing-state")
    assert response.status_code == 200
    col = response.json()["collections"]["repo"]
    assert "processed_paths" not in col
    assert "file_mtimes" not in col
    assert "file_hashes" not in col


def test_indexing_state_empty_collections(tmp_db: Path) -> None:
    """GET /indexing-state with empty collections returns top-level fields, not just {}."""
    state = IndexingState(collections={}, trigger="manual")
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/indexing-state")
    assert response.status_code == 200
    data = response.json()
    assert "last_updated" in data
    assert "collections" in data
    assert data["collections"] == {}


def test_indexing_state_filters_by_namespace(tmp_db: Path) -> None:
    """GET /indexing-state returns only collections belonging to the caller's namespace."""
    state = IndexingState(
        collections={
            "tenant_a_col": CollectionProgress(status=IndexingStatus.DONE, total_files=3, processed_files=3),
            "tenant_b_col": CollectionProgress(status=IndexingStatus.PENDING, total_files=5),
        }
    )
    mock_store = _make_mock_store([
        CollectionMeta(name="tenant_a_col", namespace="tenantA"),
        CollectionMeta(name="tenant_b_col", namespace="tenantB"),
    ])
    store = IndexingStateStore(tmp_db)
    store.write(state)
    config = SearchConfig()
    config.db_path = str(tmp_db)
    config.namespaces = {"key-a": "tenantA", "key-b": "tenantB"}
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)
    app.state.search_store = mock_store
    c = TestClient(app, headers={"Authorization": "Bearer key-a"})
    response = c.get("/indexing-state")
    assert response.status_code == 200
    data = response.json()
    assert "collections" in data
    collections = data["collections"]
    assert isinstance(collections, dict)
    assert "tenant_a_col" in collections
    assert "tenant_b_col" not in collections


def test_indexing_state_empty_for_unknown_namespace(tmp_db: Path) -> None:
    """GET /indexing-state returns empty collections dict when no collections match namespace."""
    state = IndexingState(
        collections={
            "some_col": CollectionProgress(status=IndexingStatus.DONE, total_files=1, processed_files=1),
        }
    )
    mock_store = _make_mock_store([
        CollectionMeta(name="some_col", namespace="tenantX"),
    ])
    store = IndexingStateStore(tmp_db)
    store.write(state)
    config = SearchConfig()
    config.db_path = str(tmp_db)
    config.namespaces = {"key-y": "tenantY"}
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)
    app.state.search_store = mock_store
    c = TestClient(app, headers={"Authorization": "Bearer key-y"})
    response = c.get("/indexing-state")
    assert response.status_code == 200
    data = response.json()
    assert "collections" in data
    assert isinstance(data["collections"], dict)
    assert data["collections"] == {}

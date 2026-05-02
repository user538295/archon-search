"""Tests for GET /indexing-state endpoint (Task 5.4)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.progress import CollectionProgress, IndexingState, IndexingStatus, IndexingStateStore
from archon_search.server.app import create_app


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "search"
    db.mkdir()
    return db


def _make_client(tmp_db: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_db)
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)
    return TestClient(app)


def _make_client_with_state(tmp_db: Path, state: IndexingState) -> TestClient:
    store = IndexingStateStore(tmp_db)
    store.write(state)
    return _make_client(tmp_db)


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

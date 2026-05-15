"""Tests for GET /status endpoint (Task 5.3a)."""
from __future__ import annotations

import os
from datetime import UTC, datetime
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


def _make_client_with_state(tmp_db: Path, state: IndexingState) -> TestClient:
    store = IndexingStateStore(tmp_db)
    store.write(state)
    config = SearchConfig()
    config.db_path = str(tmp_db)
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def test_status_returns_running_and_collections(tmp_db: Path) -> None:
    state = IndexingState(
        collections={
            "docs": CollectionProgress(
                status=IndexingStatus.DONE,
                total_files=10,
                processed_files=10,
            )
        }
    )
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    # service fields
    assert "running" in data
    assert "pid" in data
    assert "version" in data
    assert data["running"] is True
    assert isinstance(data["pid"], int)
    assert isinstance(data["version"], str)
    # collections
    assert "collections" in data
    assert isinstance(data["collections"], list)
    assert len(data["collections"]) == 1
    col = data["collections"][0]
    assert col["name"] == "docs"
    assert col["status"] == "done"
    assert col["doc_count"] == 0
    assert col["chunk_count"] == 0
    assert "watching" in col


def test_status_includes_eta_when_progress_known(tmp_db: Path) -> None:
    started = datetime.now(UTC).isoformat()
    state = IndexingState(
        collections={
            "big-repo": CollectionProgress(
                status=IndexingStatus.IN_PROGRESS,
                total_files=100,
                processed_files=50,
                started_at=started,
            )
        }
    )
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    col = data["collections"][0]
    assert col["name"] == "big-repo"
    assert col["status"] == "in_progress"
    assert "eta_seconds" in col
    assert col["eta_seconds"] is not None
    assert isinstance(col["eta_seconds"], int)


def test_status_includes_watching_flag(tmp_db: Path) -> None:
    state = IndexingState(
        collections={
            "watched": CollectionProgress(status=IndexingStatus.DONE)
        }
    )
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    col = data["collections"][0]
    assert "watching" in col
    assert isinstance(col["watching"], bool)


def test_status_includes_progress_and_error_fields(tmp_db: Path) -> None:
    state = IndexingState(
        collections={
            "failing": CollectionProgress(
                status=IndexingStatus.FAILED,
                total_files=20,
                processed_files=5,
                error="disk full",
                error_count=3,
            )
        }
    )
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    col = data["collections"][0]
    assert col["processed_files"] == 5
    assert col["total_files"] == 20
    assert col["error"] == "disk full"
    assert col["error_count"] == 3


def test_status_no_state_file(tmp_db: Path) -> None:
    """GET /status returns 200 with empty collections when no state file exists."""
    config = SearchConfig()
    config.db_path = str(tmp_db)
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert data["collections"] == []


def test_status_empty_collections(tmp_db: Path) -> None:
    """GET /status returns 200 with empty collections when state has no collections."""
    state = IndexingState(collections={})
    c = _make_client_with_state(tmp_db, state)
    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert data["collections"] == []


def test_status_config_paths_converted_to_names(tmp_db: Path) -> None:
    """Config collections/pinned_collections paths are converted to collection names via path_to_collection_name()."""
    from archon_search.sync import path_to_collection_name

    config = SearchConfig()
    config.db_path = str(tmp_db)
    config.collections = ["/some/path/my-docs"]
    config.pinned_collections = ["/notes"]
    job_store = JobStore(path=tmp_db / "jobs.json")
    app = create_app(config, job_store)
    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    c = TestClient(app, headers={"Authorization": f"Bearer {key}"})

    response = c.get("/status")
    assert response.status_code == 200
    data = response.json()

    expected_collection_name = path_to_collection_name("/some/path/my-docs")
    expected_pinned_name = path_to_collection_name("/notes")

    names = [col["name"] for col in data["collections"]]
    assert expected_collection_name in names
    assert expected_pinned_name in names

    # Both are not in state → status must be "not_yet_indexed"
    for col in data["collections"]:
        if col["name"] in (expected_collection_name, expected_pinned_name):
            assert col["status"] == "not_yet_indexed"

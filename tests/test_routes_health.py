"""Tests for GET /health endpoint (Task 5.3)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    return TestClient(app)


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_health_has_status_running(client: TestClient) -> None:
    response = client.get("/health")
    assert response.json()["status"] == "running"


def test_health_has_version(client: TestClient) -> None:
    response = client.get("/health")
    data = response.json()
    assert "version" in data
    assert isinstance(data["version"], str)
    assert len(data["version"]) > 0

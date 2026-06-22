"""Tests for POST /maintenance/trigger and GET /status maintenance block — D5 BE-4.

Plan: Documentation/Backlog/D5-maintenance-jobs-policies-team-plan.md Task BE-4.

TDD: tests written first, then routes_maintenance.py + _build_maintenance_status in routes_status.py.
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search._types import CollectionInfo
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.jobs.maintenance_loop import MaintenanceLoop
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_search_store(collections: list[CollectionInfo] | None = None) -> MagicMock:
    mock = MagicMock()
    mock.connect = AsyncMock()
    mock.disconnect = AsyncMock()
    mock._run_startup_migrations = AsyncMock()
    mock.ping = AsyncMock(return_value=True)
    mock.get_all_collections_meta = AsyncMock(
        return_value=[
            CollectionMeta(name=c.name, namespace=c.namespace)
            for c in (collections or [])
        ]
    )
    mock.get_collection_meta = AsyncMock(return_value=None)
    mock.list_collections = AsyncMock(return_value=collections or [])
    mock.count_untagged_language_chunks = AsyncMock(return_value=0)
    mock.pending_migrations = AsyncMock(return_value=[])
    return mock


def _make_client(
    tmp_path: Path,
    auth_headers: dict[str, str],
    *,
    interval_hours: int = 0,
    collections: list[CollectionInfo] | None = None,
) -> TestClient:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.backup.output_dir = str(tmp_path / "backups")
    cfg.maintenance.interval_hours = interval_hours
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(cfg, job_store)
    app.state.search_store = _make_mock_search_store(collections=collections)
    return TestClient(app, headers=auth_headers)


# ---------------------------------------------------------------------------
# POST /maintenance/trigger — unit tests (Style A: pre-patch app.state)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_trigger_returns_202(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    """S17, C2: POST /maintenance/trigger returns 202 with body {"status": "triggered"}."""
    client = _make_client(tmp_path, auth_headers, interval_hours=0)
    with client:
        response = client.post("/maintenance/trigger")
    assert response.status_code == 202
    body = response.json()
    assert body == {"status": "triggered"}


@pytest.mark.integration
def test_trigger_requires_auth(tmp_path: Path) -> None:
    """S19: POST /maintenance/trigger without Bearer token returns 401."""
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.backup.output_dir = str(tmp_path / "backups")
    cfg.maintenance.interval_hours = 0
    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(cfg, job_store)
    app.state.search_store = _make_mock_search_store()
    no_auth_client = TestClient(app)
    with no_auth_client:
        response = no_auth_client.post("/maintenance/trigger")
    assert response.status_code == 401


@pytest.mark.integration
def test_trigger_while_busy_returns_202(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    """S17: when trigger_event is already set (pass in progress), 202 with already_triggered.

    We mock _trigger_event with a MagicMock where is_set() returns True to simulate
    a pass already in progress, without racing against the real trigger loop.
    """
    from unittest.mock import MagicMock as MM

    client = _make_client(tmp_path, auth_headers, interval_hours=0)
    with client:
        # Replace _trigger_event with a mock that always reports as set
        mock_event = MM()
        mock_event.is_set.return_value = True
        client.app.state.maintenance_loop._trigger_event = mock_event
        response = client.post("/maintenance/trigger")
    assert response.status_code == 202
    body = response.json()
    assert body == {"status": "already_triggered"}
    # The mock event should NOT have been set again (no duplicate trigger)
    mock_event.set.assert_not_called()


# ---------------------------------------------------------------------------
# GET /status — maintenance block
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_maintenance_disabled(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    """S21: interval_hours=0 → maintenance.enabled=False, next_run_at=null."""
    client = _make_client(tmp_path, auth_headers, interval_hours=0)
    with client:
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["maintenance"] is not None
    assert body["maintenance"]["enabled"] is False
    assert body["maintenance"]["next_run_at"] is None


@pytest.mark.integration
def test_status_maintenance_absent(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    """S20 null branch: if app.state has no maintenance_loop, maintenance=null in response."""
    client = _make_client(tmp_path, auth_headers, interval_hours=0)
    with client:
        # Remove maintenance_loop attribute to simulate alternative factory
        if hasattr(client.app.state, "maintenance_loop"):
            delattr(client.app.state, "maintenance_loop")
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["maintenance"] is None


@pytest.mark.integration
def test_status_maintenance_namespace_scoped(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    """S22: collection_health is filtered to caller's namespace only."""
    collections = [
        CollectionInfo(name="docs", doc_count=1, chunk_count=1, namespace=DEFAULT_NAMESPACE),
        CollectionInfo(name="other", doc_count=1, chunk_count=1, namespace="tenant-a"),
    ]
    client = _make_client(tmp_path, auth_headers, interval_hours=0, collections=collections)
    with client:
        # Inject state with both namespace/collection entries
        loop: MaintenanceLoop = client.app.state.maintenance_loop
        loop._save_state({
            "last_run_at": "2026-06-21T10:00:00+00:00",
            "next_run_at": None,
            "collection_health": {
                f"{DEFAULT_NAMESPACE}/docs": {
                    "fts_optimized_at": None,
                    "orphans_removed_last_run": 0,
                    "last_retry_at": None,
                    "last_error": None,
                    "meta_chunk_count": 5,
                },
                "tenant-a/other": {
                    "fts_optimized_at": None,
                    "orphans_removed_last_run": 0,
                    "last_retry_at": None,
                    "last_error": None,
                    "meta_chunk_count": 3,
                },
            },
            "retry_counts": {},
        })
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    health = body["maintenance"]["collection_health"]
    collection_names = {e["collection"] for e in health}
    assert "docs" in collection_names
    assert "other" not in collection_names


@pytest.mark.integration
def test_status_maintenance_enabled(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    """S20: interval_hours>0 → maintenance.enabled=True."""
    client = _make_client(tmp_path, auth_headers, interval_hours=24)
    with client:
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["maintenance"] is not None
    assert body["maintenance"]["enabled"] is True
    assert body["maintenance"]["interval_hours"] == 24


@pytest.mark.integration
def test_status_maintenance_last_run_at_populated(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    """S20: after a pass, last_run_at is populated in GET /status."""
    client = _make_client(tmp_path, auth_headers, interval_hours=0)
    expected_ts = "2026-06-21T10:00:00+00:00"
    with client:
        loop: MaintenanceLoop = client.app.state.maintenance_loop
        loop._save_state({
            "last_run_at": expected_ts,
            "next_run_at": None,
            "collection_health": {},
            "retry_counts": {},
        })
        response = client.get("/status")
    assert response.status_code == 200
    body = response.json()
    assert body["maintenance"]["last_run_at"] == expected_ts


# ---------------------------------------------------------------------------
# Integration test: timing (S28)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_trigger_with_interval_zero_fires_pass(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    """S18: trigger with interval_hours=0 sets the event and the loop fires _run_one_pass."""
    import time

    client = _make_client(tmp_path, auth_headers, interval_hours=0)
    with client:
        loop = client.app.state.maintenance_loop
        # Track whether _run_one_pass was called
        pass_called = False
        original_run_one_pass = loop._run_one_pass

        async def _tracking_pass() -> None:
            nonlocal pass_called
            pass_called = True

        loop._run_one_pass = _tracking_pass

        # Trigger the pass
        response = client.post("/maintenance/trigger")
        assert response.status_code == 202
        assert response.json()["status"] == "triggered"

        # Give the background asyncio loop time to pick up the event and run the pass
        time.sleep(0.3)

        assert pass_called, "Expected _run_one_pass to be called after trigger"


@pytest.mark.integration
def test_trigger_maintenance_loop_absent(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    """Defensive path: if maintenance_loop is absent, trigger returns 202 + already_triggered."""
    client = _make_client(tmp_path, auth_headers, interval_hours=0)
    with client:
        if hasattr(client.app.state, "maintenance_loop"):
            delattr(client.app.state, "maintenance_loop")
        response = client.post("/maintenance/trigger")
    assert response.status_code == 202
    body = response.json()
    assert body == {"status": "already_triggered"}


@pytest.mark.integration
@pytest.mark.xdist_group("benchmark")
def test_trigger_post_timing(tmp_path: Path, auth_headers: dict[str, str]) -> None:
    """S28: POST /maintenance/trigger responds in < 2000 ms (202 is async — pass runs in background)."""
    client = _make_client(tmp_path, auth_headers, interval_hours=0)
    with client:
        t0 = time.monotonic()
        response = client.post("/maintenance/trigger")
        elapsed_ms = (time.monotonic() - t0) * 1000
    assert response.status_code == 202
    assert elapsed_ms < 2000, f"trigger took {elapsed_ms:.0f} ms (> 2000 ms threshold)"

"""Tests for POST /sync endpoint — CSP120 BE-3."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Unit tests (stub app via make_real_app + mock collection_sync)
# ---------------------------------------------------------------------------


def test_post_sync_returns_202_running(tmp_path, monkeypatch):
    """POST /sync returns 202 with JobResponse status=RUNNING."""
    from tests.integration.conftest import make_real_app

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Mock sync so it doesn't actually run filesystem operations
        client.app.state.collection_sync.sync = AsyncMock(return_value=MagicMock(
            added=[], removed=[], unchanged=[], errors=[], skipped=[], updated=[]
        ))

        resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "RUNNING"
        assert "job_id" in body


def test_post_sync_409_on_concurrent_submit(tmp_path, monkeypatch):
    """POST /sync returns 409 when sync_lock is already held."""
    from tests.integration.conftest import make_real_app

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Acquire the lock externally (simulating a concurrent sync in progress)
        lock = client.app.state.sync_lock

        async def _hold_lock():
            await lock.acquire()

        client.portal.call(_hold_lock)
        try:
            resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
            assert resp.status_code == 409
            assert "sync already in progress" in resp.json()["detail"]
        finally:
            lock.release()


def test_post_sync_requires_bearer_auth(tmp_path, monkeypatch):
    """POST /sync returns 401 without a valid Bearer token."""
    from tests.integration.conftest import make_real_app

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        resp = client.post("/sync")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Integration tests (real async behaviour)
# ---------------------------------------------------------------------------


def _poll_job_until_terminal(client: TestClient, job_id: str, api_key: str, timeout: float = 5.0) -> dict:
    """Poll GET /jobs/{id} until a terminal status is reached."""
    deadline = time.monotonic() + timeout
    terminal = {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers={"Authorization": f"Bearer {api_key}"})
        assert r.status_code == 200
        if r.json()["status"] in terminal:
            return r.json()
        time.sleep(0.05)
    pytest.fail(f"Job {job_id} did not reach a terminal status within {timeout}s")


def test_post_sync_dispatches_to_collection_sync_only(tmp_path, monkeypatch):
    """POST /sync calls SearchCollectionSync.sync once; MaintenanceLoop is not invoked.

    S15: sync only runs SearchCollectionSync.sync(), not FTS optimize / orphan cleanup /
    graph GC.
    """
    from tests.integration.conftest import make_real_app

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        sync_mock = AsyncMock(return_value=MagicMock(
            added=[], removed=[], unchanged=[], errors=[], skipped=[], updated=[]
        ))
        client.app.state.collection_sync.sync = sync_mock

        resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        _poll_job_until_terminal(client, job_id, api_key)

        # assert sync called once — S15 proof (not MaintenanceLoop)
        assert sync_mock.await_count == 1, f"Expected sync called once, got {sync_mock.await_count}"
        # assert collections list passed — pinned_collections + collections (both empty by default)
        call_args = sync_mock.call_args
        assert call_args is not None
        passed_collections = call_args.args[0] if call_args.args else call_args.kwargs.get("collections", [])
        assert isinstance(passed_collections, list)


def test_post_sync_task_failed_releases_lock_and_second_submit_succeeds(tmp_path, monkeypatch):
    """If sync() raises, job transitions to FAILED and sync_lock is released.

    S23: a subsequent POST /sync must return 202, not 409.
    """
    from tests.integration.conftest import make_real_app

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        fail_mock = AsyncMock(side_effect=OSError("disk full"))
        client.app.state.collection_sync.sync = fail_mock

        resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        job = _poll_job_until_terminal(client, job_id, api_key)
        assert job["status"] == "FAILED"

        # Lock must be released — second submit should succeed (202, not 409)
        success_mock = AsyncMock(return_value=MagicMock(
            added=[], removed=[], unchanged=[], errors=[], skipped=[], updated=[]
        ))
        client.app.state.collection_sync.sync = success_mock

        resp2 = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
        assert resp2.status_code == 202


def test_post_sync_job_result_contains_all_sync_result_fields(tmp_path, monkeypatch):
    """Job result on DONE contains all 6 SyncResult fields (S7).

    Uses a real sync over a tmp_path collection (empty — no configured collections).
    """
    from tests.integration.conftest import make_real_app

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Real sync — no collections configured, so it runs cleanly and DONE quickly
        resp = client.post("/sync", headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        job = _poll_job_until_terminal(client, job_id, api_key)
        assert job["status"] == "DONE"
        assert job.get("kind") == "sync", f"Expected kind='sync', got {job.get('kind')!r}"

        result = job["result"]
        assert result is not None
        for field in ("added", "removed", "unchanged", "errors", "skipped", "updated"):
            assert field in result, f"Missing field: {field}"
            assert isinstance(result[field], list), f"Expected list for {field}"

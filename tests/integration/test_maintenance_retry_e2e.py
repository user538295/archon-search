"""T-4 — e2e: force FAILED IngestJob, POST trigger, verify new job with source='maintenance' in GET /jobs.

Plan: Documentation/Backlog/D5-maintenance-jobs-policies-team-plan.md Task T-4

Verifies:
- S13: FAILED IngestJob is re-enqueued with source="maintenance" after a maintenance pass.
- The re-enqueued job is run by the app the lifespan wires (create_app → MaintenanceLoop → dispatcher), with the
  original request's chunk_scopes, and a later pass does not re-enqueue a path whose retry succeeded.

Flow:
1. Start real app with maintenance_enabled=True.
2. Get a FAILED IngestJob into the job store (inserted directly, or a real POST /ingest that fails).
3. POST /maintenance/trigger — fires a pass asynchronously.
4. Poll until a job with source="maintenance" appears (and, for the run tests, reaches DONE).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.types import IngestJob, IngestOptions, JobStatus
from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("benchmark")]

# Poll constants — 30s matches the project convention for scheduler-timing-sensitive tests.
_POLL_TIMEOUT_S: float = 30.0
_POLL_INTERVAL_S: float = 0.1


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _wait_until[T](probe: Callable[[], T], timeout_s: float = _POLL_TIMEOUT_S) -> T:
    """Call *probe* until it returns a truthy value or *timeout_s* passes; return its last value (no sleep after)."""
    deadline = time.monotonic() + timeout_s
    while not (value := probe()) and time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_S)
    return value


def _maintenance_jobs(job_store: Any) -> list[IngestJob]:
    return [j for j in job_store.list() if j.source == "maintenance"]


def _job_with_status(job_store: Any, job_id: str, status: JobStatus) -> IngestJob | None:
    job = job_store.get(job_id)
    return job if job is not None and job.status == status else None


def _last_maintenance_run(client: TestClient, headers: dict[str, str]) -> str | None:
    resp = client.get("/status", headers=headers)
    assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
    return resp.json()["maintenance"]["last_run_at"]


def _run_maintenance_pass(client: TestClient, headers: dict[str, str]) -> None:
    """Trigger a maintenance pass and wait until it has finished (``last_run_at`` moved)."""
    before = _last_maintenance_run(client, headers)
    trigger = client.post("/maintenance/trigger", headers=headers)
    assert trigger.status_code == 202, f"trigger failed: {trigger.status_code} {trigger.text}"
    assert trigger.json().get("status") == "triggered"

    def finished_run() -> str | None:
        run = _last_maintenance_run(client, headers)
        return run if run != before else None

    assert _wait_until(finished_run), f"the maintenance pass did not finish within {_POLL_TIMEOUT_S}s"


def _fail_ingest_then_fix(
    client: TestClient, headers: dict[str, str], col: str, doc: Path, **request: Any
) -> IngestJob:
    """A real POST /ingest of a not-yet-existing file fails through the production route; then the file is written."""
    resp = client.post("/ingest", json={"collection": col, "path": str(doc), **request}, headers=headers)
    assert resp.status_code == 202, f"ingest POST failed: {resp.status_code} {resp.text}"
    job_store = client.app.state.job_store
    failed = _wait_until(lambda: _job_with_status(job_store, resp.json()["job_id"], JobStatus.FAILED))
    assert failed is not None, f"precondition: the first ingest must fail, got {job_store.get(resp.json()['job_id'])}"
    doc.write_text("# Retry\nThe maintenance retry must ingest this quokka document.\n", encoding="utf-8")
    return failed


def _wait_for_done_retry(job_store: Any) -> IngestJob:
    (retry,) = _wait_until(lambda: _maintenance_jobs(job_store)) or [None]
    assert retry is not None, "precondition: the maintenance pass must re-enqueue the FAILED ingest"
    done = _wait_until(lambda: _job_with_status(job_store, retry.job_id, JobStatus.DONE))
    final = job_store.get(retry.job_id)
    assert done is not None, (
        f"maintenance retry job {retry.job_id} was never executed: status {final.status.value} "
        f"after {_POLL_TIMEOUT_S}s (error={final.error!r})"
    )
    return done


def test_failed_ingest_retry_creates_new_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-4 e2e: directly insert FAILED IngestJob; POST trigger; poll GET /jobs until a job
    with source='maintenance' appears.

    Completes: S13 (FAILED job re-enqueued with source='maintenance').
    """
    with make_real_app(tmp_path, monkeypatch, maintenance_enabled=True) as (client, _cfg, api_key):
        headers = _auth(api_key)
        job_store = client.app.state.job_store
        failed_file = str(tmp_path / "test_document.txt")

        # Write a real file so the retry logic doesn't filter on file existence
        # (maintenance retry only checks age and retry_count, not file existence).
        Path(failed_file).write_text("test content", encoding="utf-8")

        job = job_store.create(
            namespace="default", source="user", path=failed_file, collection="test-col",
            ingest_options=IngestOptions(submitted_path=failed_file, ingested_by="http"),
        )
        job_store.update(job.job_id, status=JobStatus.FAILED, error="test failure")

        trigger_resp = client.post("/maintenance/trigger", headers=headers)
        assert trigger_resp.status_code == 202, f"expected 202, got {trigger_resp.status_code}: {trigger_resp.text}"
        assert trigger_resp.json().get("status") == "triggered"

        def maintenance_items() -> list[dict[str, Any]]:
            jobs_resp = client.get("/jobs", headers=headers)
            assert jobs_resp.status_code == 200, f"GET /jobs failed: {jobs_resp.status_code} {jobs_resp.text}"
            return [j for j in jobs_resp.json().get("items", []) if j.get("source") == "maintenance"]

        items = _wait_until(maintenance_items)
        assert items, f"No job with source='maintenance' appeared in GET /jobs within {_POLL_TIMEOUT_S}s."
        new_job = items[0]
        assert new_job.get("source_path") == failed_file
        assert new_job.get("collection") == "test-col"
        assert new_job.get("namespace") == "default"
        assert new_job.get("retry_count") == 1, f"GET /jobs must report the retry attempt: {new_job}"
        # A started retry runs to completion — awaited here, so no task outlives the app's lifespan.
        done = _wait_until(lambda: _job_with_status(job_store, new_job["job_id"], JobStatus.DONE))
        assert done is not None, f"retry job never reached DONE: {job_store.get(new_job['job_id'])}"


def test_failed_ingest_retry_job_is_executed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real POST /ingest fails; the maintenance retry it earns must actually run the ingest — and a later pass
    must not retry the path again once that retry succeeded.

    Regression: ``MaintenanceLoop._run_failed_ingest_retry`` re-enqueued the FAILED ingest via
    ``JobStore.create(source="maintenance")`` — a PENDING ``IngestJob`` that no task, scheduler, or
    worker ever claimed, so the retry stayed PENDING forever and the file was never ingested.
    """
    col = "retry-col"
    with make_real_app(tmp_path, monkeypatch, maintenance_enabled=True) as (client, _cfg, api_key):
        headers = _auth(api_key)
        job_store = client.app.state.job_store
        _fail_ingest_then_fix(client, headers, col, tmp_path / "retry_target.md")

        _run_maintenance_pass(client, headers)
        _wait_for_done_retry(job_store)

        # ...and its ingest work really ran: the file's chunks are in the collection.
        info = client.get(f"/collections/{col}", headers=headers)
        assert info.status_code == 200, f"collection missing after retry: {info.status_code} {info.text}"
        assert info.json()["chunk_count"] > 0, f"retry wrote no chunks: {info.json()}"

        # The original FAILED job is superseded by the DONE retry: the next pass re-enqueues nothing.
        _run_maintenance_pass(client, headers)
        assert len(_maintenance_jobs(job_store)) == 1, [
            (j.job_id, j.status.value) for j in _maintenance_jobs(job_store)
        ]


def test_failed_ingest_retry_keeps_the_requests_chunk_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scoped ingest that fails and is retried ends with chunks carrying the original scopes — an unscoped retry
    would make them match every scope filter (ACL widening)."""
    col = "scoped-retry-col"
    with make_real_app(tmp_path, monkeypatch, maintenance_enabled=True) as (client, _cfg, api_key):
        headers = _auth(api_key)
        _fail_ingest_then_fix(client, headers, col, tmp_path / "scoped.md", chunk_scopes=["team-a"])

        _run_maintenance_pass(client, headers)
        _wait_for_done_retry(client.app.state.job_store)

        docs = client.get(f"/collections/{col}/documents", headers=headers)
        assert docs.status_code == 200, f"documents listing failed: {docs.status_code} {docs.text}"
        items = docs.json()["items"]
        assert items, f"retry wrote no document: {docs.json()}"
        assert [item["scopes"] for item in items] == [["team-a"]]

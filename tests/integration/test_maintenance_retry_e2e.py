"""T-4 — e2e: force FAILED IngestJob, POST trigger, verify new job with source='maintenance' in GET /jobs.

Plan: Documentation/Backlog/D5-maintenance-jobs-policies-team-plan.md Task T-4

Verifies:
- S13: FAILED IngestJob is re-enqueued with source="maintenance" after a maintenance pass.

Flow:
1. Start real app with maintenance_enabled=True.
2. Directly insert a FAILED IngestJob into the job store.
3. POST /maintenance/trigger — fires a pass asynchronously.
4. Poll GET /jobs until a job with source="maintenance" appears.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from archon_search.types import IngestJob, JobStatus
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

# Poll constants
_POLL_TIMEOUT_S: float = 15.0
_POLL_INTERVAL_S: float = 0.1


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def test_failed_ingest_retry_creates_new_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-4 e2e: directly insert FAILED IngestJob; POST trigger; poll GET /jobs until a job
    with source='maintenance' appears.

    Completes: S13 (FAILED job re-enqueued with source='maintenance').
    """
    with make_real_app(tmp_path, monkeypatch, maintenance_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Step 2: directly insert a FAILED IngestJob into the job store.
        job_store = client.app.state.job_store
        failed_file = str(tmp_path / "test_document.txt")

        # Write a real file so the retry logic doesn't filter on file existence
        # (maintenance retry only checks age and retry_count, not file existence).
        Path(failed_file).write_text("test content", encoding="utf-8")

        # Create the job and mark it FAILED
        job = job_store.create(
            namespace="default",
            source="user",
            path=failed_file,
            collection="test-col",
        )
        job_store.update(job.job_id, status=JobStatus.FAILED, error="test failure")

        # Step 3: POST /maintenance/trigger
        trigger_resp = client.post("/maintenance/trigger", headers=_auth(api_key))
        assert trigger_resp.status_code == 202, (
            f"expected 202, got {trigger_resp.status_code}: {trigger_resp.text}"
        )
        assert trigger_resp.json().get("status") == "triggered"

        # Step 4: Poll GET /jobs until a job with source="maintenance" appears
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        maintenance_job_found = False
        last_jobs_body: dict = {}
        while time.monotonic() < deadline:
            jobs_resp = client.get("/jobs", headers=_auth(api_key))
            assert jobs_resp.status_code == 200, (
                f"GET /jobs failed: {jobs_resp.status_code} {jobs_resp.text}"
            )
            jobs_body = jobs_resp.json()
            last_jobs_body = jobs_body
            items = jobs_body.get("items", [])
            maintenance_jobs = [j for j in items if j.get("source") == "maintenance"]
            if maintenance_jobs:
                maintenance_job_found = True
                # Verify the new job has the correct fields
                new_job = maintenance_jobs[0]
                assert new_job.get("source_path") == failed_file, (
                    f"expected source_path={failed_file!r}; got: {new_job.get('source_path')}"
                )
                assert new_job.get("collection") == "test-col", (
                    f"expected collection='test-col'; got: {new_job.get('collection')}"
                )
                assert new_job.get("namespace") == "default", (
                    f"expected namespace='default'; got: {new_job.get('namespace')}"
                )
                break
            time.sleep(_POLL_INTERVAL_S)

        assert maintenance_job_found, (
            f"No job with source='maintenance' appeared in GET /jobs within {_POLL_TIMEOUT_S}s. "
            f"Items: {[j.get('source') for j in last_jobs_body.get('items', [])]}"
        )

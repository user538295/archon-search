"""Task 2.2 — Backup trigger and on-disk verification.

Verifies that POST /backup/trigger enqueues real export jobs that reach DONE and
produce ``.tar.gz`` archives on disk, and that GET /status reflects the completion
via ``backup.collection_status[*].last_backup_at``.

Key timing note: The BackupLoop completion loop polls in-flight jobs every
``_BACKUP_COMPLETION_POLL_SECONDS`` (default 60 s). Tests monkeypatch this to
0.1 s *before* calling ``make_real_app(backup_enabled=True)``; if applied after,
the loop may already be sleeping the full 60 s before the patch takes effect.

Also: ``make_real_app(backup_enabled=True)`` sets ``config.backup.interval_hours = 1``
and ``config.backup.output_dir = str(tmp_path / 'backups')``.

Run with:
    uv run pytest tests/integration/test_backup_dispatch_e2e.py -v --no-cov
"""
from __future__ import annotations

import json
import tarfile
import time
from pathlib import Path

import pytest

import archon_search.jobs.backup_loop as _backup_loop_module
import archon_search.jobs.scheduler as _scheduler_module
from archon_search.types import ExportJob, JobStatus
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_POLL_TIMEOUT_S: float = 30.0
_POLL_INTERVAL_S: float = 0.1

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _poll_all_backup_jobs_done(
    job_store,
    backup_job_ids: list[str],
    *,
    timeout_s: float = _POLL_TIMEOUT_S,
) -> None:
    """Poll job_store until every job in ``backup_job_ids`` reaches a terminal state.

    Uses direct job_store access (not GET /jobs/{id}) to avoid the Pydantic
    schema mismatch on ``result: str | None`` vs the dataclass ``dict | None``
    (the REST endpoint returns 500 for completed bulk jobs with dict results).

    Calls ``pytest.fail`` if any job has not reached terminal state within
    ``timeout_s`` seconds.
    """
    terminal = {JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        statuses = {}
        for jid in backup_job_ids:
            job = job_store.get(jid)
            statuses[jid] = job.status if job is not None else None

        if all(s in terminal for s in statuses.values()):
            # Assert all are DONE, not just terminal.
            failed = [jid for jid, s in statuses.items() if s != JobStatus.DONE]
            if failed:
                details = {jid: job_store.get(jid) for jid in failed}
                job_errors = ", ".join(
                    f"{jid}: {getattr(details[jid], 'error', None)}" for jid in failed
                )
                pytest.fail(f"backup jobs did not reach DONE: {job_errors}")
            return
        time.sleep(_POLL_INTERVAL_S)

    current = {jid: job_store.get(jid) for jid in backup_job_ids}
    status_summary = ", ".join(
        f"{jid}={getattr(j, 'status', None)}" for jid, j in current.items()
    )
    pytest.fail(
        f"backup jobs did not reach terminal state within {timeout_s}s; "
        f"statuses: {status_summary}"
    )


# ---------------------------------------------------------------------------
# Test 1 — Backup trigger: queued jobs eventually complete with archive on disk
# ---------------------------------------------------------------------------


def test_backup_trigger_queued_jobs_eventually_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /backup/trigger against real app with real SearchStore and real dispatch_fn.

    Flow:
    1. Monkeypatch _BACKUP_COMPLETION_POLL_SECONDS = 0.1 *before* make_real_app.
    2. Build real app with backup_enabled=True (interval_hours=1, output_dir set).
    3. Ingest a file so the collection exists.
    4. POST /backup/trigger — expect 202 with at least one queued job_id.
    5. Poll job_store until all queued job_ids reach DONE.
    6. Assert .tar.gz files appear under the configured output_dir.
    """
    # Must be patched before make_real_app so the completion loop starts with 0.1s.
    monkeypatch.setattr(_backup_loop_module, "_BACKUP_COMPLETION_POLL_SECONDS", 0.1)
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    col = "test-backup-trigger-done"
    doc = tmp_path / "backup_trigger_test.md"
    doc.write_text("# Backup trigger test\n\nContent for backup dispatch test.\n" * 4)

    with make_real_app(tmp_path, monkeypatch, backup_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        job_store = client.app.state.job_store

        # Ingest so the collection and meta row exist.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Trigger manual backup for the caller's namespace.
        trigger_resp = client.post("/backup/trigger", headers=_auth(api_key))
        assert trigger_resp.status_code == 202, (
            f"POST /backup/trigger expected 202, got {trigger_resp.status_code}: "
            f"{trigger_resp.text}"
        )
        body = trigger_resp.json()
        queued_ids: list[str] = body.get("queued", [])
        assert queued_ids, (
            f"expected at least one queued backup job; trigger response: {body}"
        )

        # Poll until all backup jobs reach DONE.
        _poll_all_backup_jobs_done(job_store, queued_ids)

        # Assert .tar.gz archive files exist under the configured output_dir.
        output_dir = Path(cfg.backup.output_dir)
        all_archives = list(output_dir.rglob("*.tar.gz"))
        assert all_archives, (
            f"expected .tar.gz archives under {output_dir}; directory contents: "
            f"{list(output_dir.rglob('*')) if output_dir.exists() else 'dir does not exist'}"
        )

        # Validate each archive is openable and contains a well-formed manifest.
        for archive_path in all_archives:
            assert archive_path.stat().st_size > 0, (
                f"archive {archive_path} exists but is empty (0 bytes)"
            )
            with tarfile.open(archive_path, "r:gz") as tf:
                names = tf.getnames()
                assert "manifest.json" in names, (
                    f"archive {archive_path} missing manifest.json; members: {names}"
                )
                manifest_file = tf.extractfile(tf.getmember("manifest.json"))
                assert manifest_file is not None
                manifest = json.loads(manifest_file.read().decode("utf-8"))
            assert isinstance(manifest, dict), (
                f"manifest.json in {archive_path} is not a JSON object"
            )
            assert manifest.get("collection") == col, (
                f"manifest 'collection' mismatch: expected {col!r}, got {manifest.get('collection')!r}"
            )
            assert "schema_version" in manifest, (
                f"manifest.json missing 'schema_version'"
            )

        # Confirm each queued job is DONE with an output_path on disk.
        for job_id in queued_ids:
            job = job_store.get(job_id)
            assert job is not None, f"job {job_id!r} missing from job_store after completion"
            assert job.status == JobStatus.DONE, (
                f"job {job_id!r} expected DONE, got {job.status}; error: {getattr(job, 'error', None)}"
            )
            if isinstance(job, ExportJob):
                assert job.output_path, (
                    f"ExportJob {job_id!r} has empty output_path after DONE"
                )
                assert Path(job.output_path).exists(), (
                    f"ExportJob {job_id!r} output_path {job.output_path!r} does not exist on disk"
                )


# ---------------------------------------------------------------------------
# Test 2 — POST /backup/trigger → GET /status reflects completion
# ---------------------------------------------------------------------------


def test_backup_trigger_post_status_reflects_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /backup/trigger, wait for completion, assert GET /status backup state updated.

    The completion loop updates ``.backup-state.json`` when a job reaches DONE;
    GET /status reads that file and surfaces ``backup.collection_status[i].last_backup_at``.

    Note: testing ``archon-search backup now`` via CliRunner is architecturally
    impossible — the CLI uses httpx to call a real TCP server, but TestClient is
    ASGI-transport only. HTTP endpoint testing is the correct approach here.

    Flow:
    1. Monkeypatch _BACKUP_COMPLETION_POLL_SECONDS = 0.1 *before* make_real_app.
    2. Build real app with backup_enabled=True.
    3. Ingest a file so the collection exists.
    4. POST /backup/trigger — collect queued job_ids.
    5. Poll job_store until all jobs reach DONE.
    6. Poll GET /status (max 15s) until backup.collection_status[0].last_backup_at is non-null.
    7. Assert GET /status 200, backup.enabled == True, collection_status non-empty,
       and at least one collection has a non-null last_backup_at.
    """
    # Must be patched before make_real_app so the completion loop starts with 0.1s.
    monkeypatch.setattr(_backup_loop_module, "_BACKUP_COMPLETION_POLL_SECONDS", 0.1)
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    col = "test-backup-status-reflect"
    doc = tmp_path / "backup_status_test.md"
    doc.write_text("# Backup status test\n\nContent for backup status reflection test.\n" * 4)

    with make_real_app(tmp_path, monkeypatch, backup_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        job_store = client.app.state.job_store

        # Ingest so the collection and meta row exist.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Trigger manual backup.
        trigger_resp = client.post("/backup/trigger", headers=_auth(api_key))
        assert trigger_resp.status_code == 202, (
            f"POST /backup/trigger expected 202, got {trigger_resp.status_code}: "
            f"{trigger_resp.text}"
        )
        queued_ids: list[str] = trigger_resp.json().get("queued", [])
        assert queued_ids, "expected at least one queued backup job"

        # Wait for all backup jobs to complete (job_store poll — avoids REST 500 on dict result).
        _poll_all_backup_jobs_done(job_store, queued_ids)

        # Poll GET /status until backup.collection_status carries last_backup_at.
        # The completion loop writes .backup-state.json on DONE; status reads it.
        deadline = time.monotonic() + 15.0
        last_backup_at: str | None = None
        while time.monotonic() < deadline:
            status_resp = client.get("/status", headers=_auth(api_key))
            assert status_resp.status_code == 200, (
                f"GET /status expected 200, got {status_resp.status_code}: {status_resp.text}"
            )
            status_body = status_resp.json()
            backup_detail = status_body.get("backup")
            if backup_detail is not None:
                collection_statuses = backup_detail.get("collection_status", [])
                for col_status in collection_statuses:
                    if col_status.get("last_backup_at") is not None:
                        last_backup_at = col_status["last_backup_at"]
                        break
            if last_backup_at is not None:
                break
            time.sleep(_POLL_INTERVAL_S)

        # Final assertions on the status response.
        status_resp = client.get("/status", headers=_auth(api_key))
        assert status_resp.status_code == 200
        status_body = status_resp.json()

        backup_detail = status_body.get("backup")
        assert backup_detail is not None, (
            "GET /status response missing 'backup' field; backup_enabled=True was set"
        )
        assert backup_detail.get("enabled") is True, (
            f"expected backup.enabled == True; got: {backup_detail.get('enabled')}"
        )
        collection_statuses = backup_detail.get("collection_status", [])
        assert collection_statuses, (
            f"expected non-empty backup.collection_status after trigger; "
            f"backup_detail: {backup_detail}"
        )
        assert last_backup_at is not None, (
            f"expected at least one collection with non-null last_backup_at within 15s; "
            f"final collection_status: {collection_statuses}"
        )

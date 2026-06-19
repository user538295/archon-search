"""Integration tests for POST /collections/{name}/migrate rewrite async path (D3 BE-12).

Tests that a MigrationJob is created, dispatched via the scheduler, and reaches
DONE with correct result.  Uses make_real_app + a synthetic REWRITE MigrationSpec
that has a matching no-op transform method on SearchStore.

Polling uses job_store directly (not GET /jobs/{id}) to avoid the Pydantic
schema mismatch between JobResponse.result: str | None and the dict stored in
the job dataclass (same pattern as test_dispatch_scheduler_e2e.py).
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import archon_search.jobs.scheduler as _scheduler_module
from archon_search.types import JobStatus, MigrationJob
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_TERMINAL = {JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _poll_job_store(job_store, job_id: str, *, timeout_s: float = 30.0):
    """Poll job_store until job reaches terminal state. Returns final job."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = job_store.get(job_id)
        if job is None:
            pytest.fail(f"job {job_id!r} not found in job_store")
        if job.status in _TERMINAL:
            return job
        time.sleep(0.05)
    pytest.fail(f"job {job_id!r} did not reach terminal state within {timeout_s}s")


def test_migration_job_dispatched_and_reaches_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /migrate with a synthetic REWRITE spec creates a MigrationJob that reaches DONE.

    We patch pending_migrations to return a synthetic REWRITE spec and patch
    apply_rewrite_migration to succeed immediately (no-op transform, 0 chunks).
    The test verifies:
      - POST /migrate returns 202 with job_id
      - job transitions QUEUED → RUNNING (route) → DONE (task) — no scheduler involvement
      - job.result contains migrated_chunks
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    from archon_search.types import MigrationKind, MigrationSpec
    from archon_search.store import SearchStore

    dummy_spec = MigrationSpec(
        name="dummy_rewrite",
        kind=MigrationKind.REWRITE,
        description="no-op rewrite for testing",
        introduced_at=999,
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        job_store = client.app.state.job_store
        search_store = client.app.state.search_store

        # Register a collection.
        col_path = tmp_path / "test_docs"
        col_path.mkdir()
        from archon_search.sync import path_to_collection_name
        col_name = path_to_collection_name(str(col_path))

        resp = client.post(
            "/collections/",
            json={"path": str(col_path)},
            headers=_auth(api_key),
        )
        assert resp.status_code == 202, f"add_collection failed: {resp.status_code} {resp.text}"

        # Patch pending_migrations to return the dummy REWRITE spec.
        # Patch apply_rewrite_migration to succeed with 0 chunks.
        original_pending = search_store.pending_migrations
        original_apply_rewrite = search_store.apply_rewrite_migration

        async def _fake_pending_migrations(collection, namespace):
            return [dummy_spec]

        async def _fake_apply_rewrite(collection, namespace, spec, progress_cb=None):
            return 0  # 0 chunks migrated

        with patch.object(search_store, "pending_migrations", _fake_pending_migrations), \
             patch.object(search_store, "apply_rewrite_migration", _fake_apply_rewrite):

            # POST migrate with backup_confirmed=True.
            resp = client.post(
                f"/collections/{col_name}/migrate",
                json={"backup_confirmed": True},
                headers=_auth(api_key),
            )
            assert resp.status_code == 202, f"POST /migrate expected 202, got {resp.status_code}: {resp.text}"
            job_id = resp.json()["job_id"]
            assert resp.json()["status"] == "RUNNING"

            # Poll until the job reaches a terminal state.
            final_job = _poll_job_store(job_store, job_id, timeout_s=15.0)

        assert final_job.status == JobStatus.DONE, (
            f"migration job ended with {final_job.status}: error={final_job.error!r}"
        )
        assert isinstance(final_job.result, dict), (
            f"expected job.result to be a dict, got: {final_job.result!r}"
        )
        assert "migrated_chunks" in final_job.result, (
            f"expected 'migrated_chunks' in job.result; got: {final_job.result}"
        )
        assert final_job.result["migrated_chunks"] == 0


def test_migration_job_progress_written_every_100_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MigrationJob progress is written at 100-chunk intervals.

    We simulate 150 chunks with a 100-chunk batch interval.  The progress_cb
    should be called at processed=100 and processed=150 (final batch).
    We verify that the job's progress dict is non-None after DONE.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    from archon_search.types import MigrationKind, MigrationSpec

    dummy_spec = MigrationSpec(
        name="dummy_rewrite_150",
        kind=MigrationKind.REWRITE,
        description="150-chunk rewrite for progress test",
        introduced_at=999,
    )
    progress_calls: list[tuple[int, int, str]] = []

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        job_store = client.app.state.job_store
        search_store = client.app.state.search_store

        col_path = tmp_path / "progress_docs"
        col_path.mkdir()
        from archon_search.sync import path_to_collection_name
        col_name = path_to_collection_name(str(col_path))

        resp = client.post(
            "/collections/",
            json={"path": str(col_path)},
            headers=_auth(api_key),
        )
        assert resp.status_code == 202

        async def _fake_pending_migrations(collection, namespace):
            return [dummy_spec]

        async def _fake_apply_rewrite(collection, namespace, spec, progress_cb=None):
            # Simulate 150 chunks: 2 batches (100 + 50).
            INTERVAL = 100
            TOTAL = 150
            processed = 0
            for batch_end in range(INTERVAL, TOTAL + INTERVAL, INTERVAL):
                batch_size = min(INTERVAL, TOTAL - processed)
                processed += batch_size
                if progress_cb is not None:
                    progress_calls.append((processed, TOTAL, "rewrite"))
                    progress_cb(processed, TOTAL, "rewrite")
                if processed >= TOTAL:
                    break
            return TOTAL

        with patch.object(search_store, "pending_migrations", _fake_pending_migrations), \
             patch.object(search_store, "apply_rewrite_migration", _fake_apply_rewrite):

            resp = client.post(
                f"/collections/{col_name}/migrate",
                json={"backup_confirmed": True},
                headers=_auth(api_key),
            )
            assert resp.status_code == 202
            job_id = resp.json()["job_id"]

            final_job = _poll_job_store(job_store, job_id, timeout_s=15.0)

    assert final_job.status == JobStatus.DONE, (
        f"migration job ended with {final_job.status}: error={final_job.error!r}"
    )
    # progress_cb was called during the rewrite phase
    assert len(progress_calls) == 2, f"expected exactly 2 progress_cb calls for 150 chunks, got {len(progress_calls)}: {progress_calls}"
    # The final progress update should be stored in the job (last update_progress call)
    # or the job.result should contain migrated_chunks
    assert final_job.result is not None
    assert final_job.result.get("migrated_chunks") == 150
    # update_progress stores {"processed": N, "total": N, "phase": str} in job.progress;
    # the last call was processed=150 (the final batch).
    assert final_job.progress is not None, "expected job.progress to be set after progress_cb calls"
    assert final_job.progress.get("processed") == 150, (
        f"expected job.progress['processed'] == 150, got: {final_job.progress}"
    )


def test_migration_job_dispatched_via_scheduler_reaches_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scheduler dispatch path: MigrationJob created directly in QUEUED state, scheduler picks it up.

    This exercises the _real_dispatch closure in app.py for MigrationJob with
    spec=None — the scheduler path used for crash-resume. The route is NOT used
    to create the job; instead we inject a QUEUED MigrationJob directly into
    job_store and let the scheduler's _tick() promote it and call _real_dispatch.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    from archon_search.types import MigrationKind, MigrationSpec

    dummy_spec = MigrationSpec(
        name="scheduler_path_rewrite",
        kind=MigrationKind.REWRITE,
        description="scheduler-dispatch test no-op rewrite",
        introduced_at=999,
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        job_store = client.app.state.job_store
        search_store = client.app.state.search_store

        # Register a collection so the job has a valid collection name.
        col_path = tmp_path / "scheduler_docs"
        col_path.mkdir()
        from archon_search.sync import path_to_collection_name
        col_name = path_to_collection_name(str(col_path))

        resp = client.post(
            "/collections/",
            json={"path": str(col_path)},
            headers=_auth(api_key),
        )
        assert resp.status_code == 202, f"add_collection failed: {resp.status_code} {resp.text}"

        # Patch pending_migrations and apply_rewrite_migration on the real search store.
        async def _fake_pending_migrations(collection, namespace):
            return [dummy_spec]

        async def _fake_apply_rewrite(collection, namespace, spec, progress_cb=None):
            return 7  # 7 chunks migrated

        with patch.object(search_store, "pending_migrations", _fake_pending_migrations), \
             patch.object(search_store, "apply_rewrite_migration", _fake_apply_rewrite):

            # Create the MigrationJob DIRECTLY in QUEUED state (bypassing the route).
            # The scheduler's _tick() will pick it up, transition to RUNNING,
            # and call _real_dispatch(job) — which invokes _migration_task(spec=None).
            queued_job = job_store.create_migration(
                collection=col_name,
                kind=MigrationKind.REWRITE,
                backup_confirmed=True,
                namespace="default",
            )
            assert queued_job.status == JobStatus.QUEUED

            # Poll until terminal — the scheduler tick is 0.1s so this should be fast.
            final_job = _poll_job_store(job_store, queued_job.job_id, timeout_s=15.0)

    assert final_job.status == JobStatus.DONE, (
        f"scheduler-dispatched migration job ended with {final_job.status}: error={final_job.error!r}"
    )
    assert isinstance(final_job.result, dict), f"expected dict result, got: {final_job.result!r}"
    assert final_job.result.get("migrated_chunks") == 7


def test_migration_job_resume_from_failed_state_reaches_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAILED MigrationJob resumes via POST /jobs/{id}/resume and reaches DONE.

    Note: _migration_task always restarts from scratch by calling apply_rewrite_migration
    from zero — it does NOT resume from the checkpoint offset. The checkpoint stored in
    job.progress is preserved for observability only; the rewrite is idempotent so
    restarting from scratch is safe.

    Flow:
    1. Create a QUEUED MigrationJob directly in job_store (bypassing route).
    2. Force it to FAILED with a progress checkpoint (simulating a crash).
    3. POST /jobs/{id}/resume → 202, job back in QUEUED.
    4. Scheduler picks up the job and dispatches it; job reaches DONE.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    from archon_search.types import MigrationKind, MigrationSpec

    dummy_spec = MigrationSpec(
        name="resume_checkpoint_rewrite",
        kind=MigrationKind.REWRITE,
        description="checkpoint-resume test no-op rewrite",
        introduced_at=999,
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        job_store = client.app.state.job_store
        search_store = client.app.state.search_store

        # Register a collection.
        col_path = tmp_path / "resume_docs"
        col_path.mkdir()
        from archon_search.sync import path_to_collection_name
        col_name = path_to_collection_name(str(col_path))

        resp = client.post(
            "/collections/",
            json={"path": str(col_path)},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 202, f"add_collection failed: {resp.text}"

        async def _fake_pending_migrations(collection, namespace):
            return [dummy_spec]

        async def _fake_apply_rewrite(collection, namespace, spec, progress_cb=None):
            return 5  # 5 chunks migrated

        with patch.object(search_store, "pending_migrations", _fake_pending_migrations), \
             patch.object(search_store, "apply_rewrite_migration", _fake_apply_rewrite):

            # 1. Create QUEUED MigrationJob directly in the store.
            queued_job = job_store.create_migration(
                collection=col_name,
                kind=MigrationKind.REWRITE,
                backup_confirmed=True,
                namespace="default",
            )

            # 2. Force FAILED with a checkpoint (simulating a mid-run crash).
            # The checkpoint (processed=50) is preserved for observability; resume
            # will restart from scratch (not from offset 50) because apply_rewrite_migration
            # is idempotent.
            job_store.update(
                queued_job.job_id,
                status=JobStatus.FAILED,
                error="process_restart",
                progress={"processed": 50, "total": 100, "phase": "rewriting"},
            )
            crashed = job_store.get(queued_job.job_id)
            assert crashed is not None
            assert crashed.status == JobStatus.FAILED
            assert crashed.progress is not None

            # 3. Resume: POST /jobs/{id}/resume → 202, job transitions FAILED → QUEUED.
            resp = client.post(
                f"/jobs/{queued_job.job_id}/resume",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            assert resp.status_code == 202, f"resume expected 202, got {resp.status_code}: {resp.text}"
            assert resp.json()["status"] == "QUEUED"

            # 4. Scheduler picks up the QUEUED job and runs it to DONE.
            final_job = _poll_job_store(job_store, queued_job.job_id, timeout_s=15.0)

    assert final_job.status == JobStatus.DONE, (
        f"resumed migration job ended with {final_job.status}: error={final_job.error!r}"
    )
    assert isinstance(final_job.result, dict), f"expected dict result, got: {final_job.result!r}"
    assert "migrated_chunks" in final_job.result

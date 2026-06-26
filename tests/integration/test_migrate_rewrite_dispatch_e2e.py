"""Integration tests for POST /collections/{name}/migrate rewrite async path (D3 BE-12).

Tests that a MigrationJob is created, dispatched via the scheduler, and reaches
DONE with correct result.  Uses make_real_app + a synthetic REWRITE MigrationSpec
that has a matching no-op transform method on SearchStore.

Polling uses job_store directly (not GET /jobs/{id}) to avoid the Pydantic
schema mismatch between JobResponse.result: str | None and the dict stored in
the job dataclass (same pattern as test_dispatch_scheduler_e2e.py).
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import archon_search.constants as _constants
import archon_search.jobs.scheduler as _scheduler_module
from archon_search.types import JobStatus, MigrationJob
from tests.integration.conftest import make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("benchmark")]

_TERMINAL = {JobStatus.DONE, JobStatus.FAILED, JobStatus.FAILED_EXPIRED, JobStatus.CANCELLED}


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


# ---------------------------------------------------------------------------
# T-2 tests: full rewrite lifecycle, concurrent 503, empty collection
# ---------------------------------------------------------------------------


def test_rewrite_migration_full_lifecycle_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full rewrite lifecycle: POST /migrate → 202 → poll to DONE → assert result → pending empty.

    Covers S7, S8, S14 (partial — zero chunks is covered by the dedicated empty test).

    Flow:
    1. Register a collection.
    2. Patch ``pending_migrations`` with a side_effect list: first call returns
       ``[dummy_spec]`` (used by the route to classify the rewrite), second call
       returns ``[]`` (used by GET /migrations/pending after the job is DONE).
    3. Patch ``apply_rewrite_migration`` to return 5 (simulating 5 chunks migrated).
    4. POST /migrate → assert 202 + job_id.
    5. Poll job_store until DONE; assert ``result["migrated_chunks"] == 5``.
    6. GET /collections/{name}/migrations/pending → assert ``pending == []``.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    from archon_search.types import MigrationKind, MigrationSpec
    from archon_search.sync import path_to_collection_name

    dummy_spec = MigrationSpec(
        name="lifecycle_rewrite",
        kind=MigrationKind.REWRITE,
        description="lifecycle test rewrite spec",
        introduced_at=999,
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        job_store = client.app.state.job_store
        search_store = client.app.state.search_store
        headers = _auth(api_key)

        col_path = tmp_path / "lifecycle_docs"
        col_path.mkdir()
        col_name = path_to_collection_name(str(col_path))

        resp = client.post(
            "/collections/",
            json={"path": str(col_path)},
            headers=headers,
        )
        assert resp.status_code == 202, f"add_collection failed: {resp.status_code} {resp.text}"

        # Wait for the ingest job to reach terminal state so DB state is stable.
        reg_job_id = resp.json()["job_id"]
        _poll_job_store(job_store, reg_job_id, timeout_s=15.0)

        # Side-effect list: first call from route returns the spec; second call from
        # GET /migrations/pending returns [] (simulating that the migration was applied).
        pending_calls: list[list] = [[dummy_spec], []]

        async def _fake_pending_side_effect(collection, namespace):
            return pending_calls.pop(0) if pending_calls else []

        async def _fake_apply_rewrite(collection, namespace, spec, progress_cb=None):
            return 5  # 5 chunks migrated

        with patch.object(search_store, "pending_migrations", _fake_pending_side_effect), \
             patch.object(search_store, "apply_rewrite_migration", _fake_apply_rewrite):

            resp = client.post(
                f"/collections/{col_name}/migrate",
                json={"backup_confirmed": True},
                headers=headers,
            )
            assert resp.status_code == 202, (
                f"POST /migrate expected 202, got {resp.status_code}: {resp.text}"
            )
            job_id = resp.json()["job_id"]
            assert resp.json()["status"] == "RUNNING"

            final_job = _poll_job_store(job_store, job_id, timeout_s=15.0)

            assert final_job.status == JobStatus.DONE, (
                f"migration job ended with {final_job.status}: error={final_job.error!r}"
            )
            assert isinstance(final_job.result, dict), (
                f"expected dict result, got: {final_job.result!r}"
            )
            assert final_job.result["migrated_chunks"] == 5
            assert final_job.migrations_applied == ["lifecycle_rewrite"], (
                f"expected migrations_applied=['lifecycle_rewrite'], got: {final_job.migrations_applied!r}"
            )

            # After the migration task completes, pending_migrations returns [].
            # GET /migrations/pending should reflect an empty list.
            pending_resp = client.get(
                f"/collections/{col_name}/migrations/pending",
                headers=headers,
            )
            assert pending_resp.status_code == 200, (
                f"GET /migrations/pending expected 200, got {pending_resp.status_code}: {pending_resp.text}"
            )
            assert pending_resp.json()["pending"] == [], (
                f"expected empty pending list after migration, got: {pending_resp.json()['pending']}"
            )


def test_concurrent_ingest_503_during_rewrite_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a MigrationJob holds the per-collection lock, a concurrent ingest returns 503.

    Covers S10.

    The fake ``apply_rewrite_migration`` acquires the same per-collection lock
    that ``POST /ingest`` tries to acquire via ``acquire_collection_lock_or_503``.
    A ``threading.Event`` pair coordinates between the main test thread and the
    asyncio background task running in the TestClient's event loop thread:

    1. POST /migrate → 202; the background task is scheduled.
    2. Main thread waits for ``lock_held_event`` (set when task acquires the lock).
    3. Main thread lowers ``INGEST_LOCK_TIMEOUT_S`` to a tiny value and issues
       POST /ingest → expects 503.
    4. Main thread sets ``allow_release_event``; the background task releases the
       lock and returns.
    5. Poll migration job to DONE.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)
    # Lower the lock-acquisition timeout so the ingest 503s quickly.
    monkeypatch.setattr(_constants, "INGEST_LOCK_TIMEOUT_S", 0.05)

    from archon_search.types import MigrationKind, MigrationSpec
    from archon_search.sync import path_to_collection_name

    dummy_spec = MigrationSpec(
        name="concurrent_rewrite",
        kind=MigrationKind.REWRITE,
        description="concurrent 503 test rewrite spec",
        introduced_at=999,
    )

    lock_held_event = threading.Event()
    allow_release_event = threading.Event()

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        job_store = client.app.state.job_store
        search_store = client.app.state.search_store
        headers = _auth(api_key)

        col_path = tmp_path / "concurrent_docs"
        col_path.mkdir()
        col_name = path_to_collection_name(str(col_path))

        resp = client.post(
            "/collections/",
            json={"path": str(col_path)},
            headers=headers,
        )
        assert resp.status_code == 202, f"add_collection failed: {resp.status_code} {resp.text}"

        # Wait for registration ingest to finish before patching pending_migrations.
        reg_job_id = resp.json()["job_id"]
        _poll_job_store(job_store, reg_job_id, timeout_s=15.0)

        async def _holding_rewrite(collection, namespace, spec, progress_cb=None):
            """Acquire the lock, signal the test thread, block until released."""
            lock = search_store.lock_for(collection)
            await lock.acquire()
            lock_held_event.set()  # signal: lock is now held
            # Wait in a thread pool so the event loop can process other requests.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, allow_release_event.wait)
            lock.release()
            return 0

        async def _fake_pending_migrations(collection, namespace):
            return [dummy_spec]

        with patch.object(search_store, "pending_migrations", _fake_pending_migrations), \
             patch.object(search_store, "apply_rewrite_migration", _holding_rewrite):

            # Trigger the rewrite migration; background task is scheduled.
            resp = client.post(
                f"/collections/{col_name}/migrate",
                json={"backup_confirmed": True},
                headers=headers,
            )
            assert resp.status_code == 202, (
                f"POST /migrate expected 202, got {resp.status_code}: {resp.text}"
            )
            job_id = resp.json()["job_id"]

            # Wait until the background task acquires the lock.
            acquired = lock_held_event.wait(timeout=10.0)
            assert acquired, "background task did not acquire the lock within 10s"

            # While the migration holds the lock, ingest must get 503.
            try:
                ingest_resp = client.post(
                    "/ingest",
                    json={"collection": col_name, "path": str(col_path)},
                    headers=headers,
                )
                assert ingest_resp.status_code == 503, (
                    f"expected 503 while rewrite holds lock, got {ingest_resp.status_code}: {ingest_resp.text}"
                )
                assert ingest_resp.json().get("error") == "store_busy", (
                    f"expected error='store_busy' in 503 response, got: {ingest_resp.json()!r}"
                )
                assert "Retry-After" in ingest_resp.headers, (
                    "expected Retry-After header in 503 response"
                )

                # Verify that a DIFFERENT collection is NOT blocked (per-collection lock isolation).
                other_col_path = tmp_path / "other_docs"
                other_col_path.mkdir()
                other_col_name = path_to_collection_name(str(other_col_path))
                other_resp = client.post(
                    "/collections/",
                    json={"path": str(other_col_path)},
                    headers=headers,
                )
                assert other_resp.status_code == 202, (
                    f"expected 202 for other collection while rewrite holds lock, got {other_resp.status_code}: {other_resp.text}"
                )
            finally:
                # Release the lock; let the migration complete.
                allow_release_event.set()

            final_job = _poll_job_store(job_store, job_id, timeout_s=15.0)

    assert final_job.status == JobStatus.DONE, (
        f"migration job ended with {final_job.status}: error={final_job.error!r}"
    )
    assert final_job.result["migrated_chunks"] == 0
    assert final_job.migrations_applied == ["concurrent_rewrite"], (
        f"expected migrations_applied=['concurrent_rewrite'], got: {final_job.migrations_applied!r}"
    )


def test_list_jobs_kind_migration_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /jobs?kind=migration is accepted by the HTTP layer and returns a valid response.

    Verifies:
      - The filter is not rejected (no 422/400).
      - After a successful rewrite migration, total >= 1 and all returned items are
        MigrationJobs (kind field present on the JobResponse).
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    from archon_search.types import MigrationKind, MigrationSpec
    from archon_search.sync import path_to_collection_name

    dummy_spec = MigrationSpec(
        name="filter_test_rewrite",
        kind=MigrationKind.REWRITE,
        description="kind-filter HTTP test rewrite spec",
        introduced_at=999,
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        search_store = client.app.state.search_store
        headers = _auth(api_key)

        col_path = tmp_path / "filter_docs"
        col_path.mkdir()
        col_name = path_to_collection_name(str(col_path))

        resp = client.post(
            "/collections/",
            json={"path": str(col_path)},
            headers=headers,
        )
        assert resp.status_code == 202, f"add_collection failed: {resp.status_code} {resp.text}"

        # Before any migration, the filter must still return 200 with total >= 0.
        pre_resp = client.get("/jobs?kind=migration", headers=headers)
        assert pre_resp.status_code == 200, (
            f"GET /jobs?kind=migration expected 200, got {pre_resp.status_code}: {pre_resp.text}"
        )
        pre_body = pre_resp.json()
        assert "total" in pre_body, f"missing 'total' in response: {pre_body!r}"
        assert pre_body["total"] >= 0

        async def _fake_pending_migrations(collection, namespace):
            return [dummy_spec]

        async def _fake_apply_rewrite(collection, namespace, spec, progress_cb=None):
            return 0

        with patch.object(search_store, "pending_migrations", _fake_pending_migrations), \
             patch.object(search_store, "apply_rewrite_migration", _fake_apply_rewrite):

            resp = client.post(
                f"/collections/{col_name}/migrate",
                json={"backup_confirmed": True},
                headers=headers,
            )
            assert resp.status_code == 202, (
                f"POST /migrate expected 202, got {resp.status_code}: {resp.text}"
            )
            job_id = resp.json()["job_id"]

            # Wait for the migration job to reach terminal state.
            job_store = client.app.state.job_store
            _poll_job_store(job_store, job_id, timeout_s=15.0)

        # After migration completes, GET /jobs?kind=migration must return total >= 1.
        post_resp = client.get("/jobs?kind=migration", headers=headers)
        assert post_resp.status_code == 200, (
            f"GET /jobs?kind=migration expected 200, got {post_resp.status_code}: {post_resp.text}"
        )
        post_body = post_resp.json()
        assert post_body["total"] >= 1, (
            f"expected total >= 1 after rewrite migration, got: {post_body['total']}"
        )
        # All returned items must have the migration-specific fields (kind is not null).
        for item in post_body["items"]:
            assert item.get("kind") is not None, (
                f"expected non-null 'kind' on migration job item: {item!r}"
            )


def test_empty_collection_rewrite_completes_immediately_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero-chunk collection: POST /migrate reaches DONE with migrated_chunks == 0.

    Covers S14.

    Registers a collection with no ingested documents, then triggers a rewrite
    migration.  ``apply_rewrite_migration`` is patched to return 0 because a
    freshly-registered collection has no LanceDB table yet (the table is only
    created on first ingest).  The test verifies that the route → job dispatch
    path handles zero chunks correctly end-to-end.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    from archon_search.types import MigrationKind, MigrationSpec
    from archon_search.sync import path_to_collection_name

    dummy_spec = MigrationSpec(
        name="empty_rewrite",
        kind=MigrationKind.REWRITE,
        description="zero-chunk rewrite spec",
        introduced_at=999,
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        job_store = client.app.state.job_store
        search_store = client.app.state.search_store
        headers = _auth(api_key)

        col_path = tmp_path / "empty_docs"
        col_path.mkdir()
        col_name = path_to_collection_name(str(col_path))

        resp = client.post(
            "/collections/",
            json={"path": str(col_path)},
            headers=headers,
        )
        assert resp.status_code == 202, f"add_collection failed: {resp.status_code} {resp.text}"

        # Wait for registration ingest to finish (no documents in col_path, so fast).
        reg_job_id = resp.json()["job_id"]
        _poll_job_store(job_store, reg_job_id, timeout_s=15.0)

        async def _fake_pending_migrations(collection, namespace):
            return [dummy_spec]

        async def _fake_apply_rewrite_zero(collection, namespace, spec, progress_cb=None):
            return 0  # zero chunks — matches an empty collection's behaviour

        with patch.object(search_store, "pending_migrations", _fake_pending_migrations), \
             patch.object(search_store, "apply_rewrite_migration", _fake_apply_rewrite_zero):

            resp = client.post(
                f"/collections/{col_name}/migrate",
                json={"backup_confirmed": True},
                headers=headers,
            )
            assert resp.status_code == 202, (
                f"POST /migrate expected 202, got {resp.status_code}: {resp.text}"
            )
            job_id = resp.json()["job_id"]
            assert resp.json()["status"] == "RUNNING"

            final_job = _poll_job_store(job_store, job_id, timeout_s=15.0)

    assert final_job.status == JobStatus.DONE, (
        f"empty-collection migration ended with {final_job.status}: error={final_job.error!r}"
    )
    assert isinstance(final_job.result, dict), (
        f"expected dict result, got: {final_job.result!r}"
    )
    assert final_job.result["migrated_chunks"] == 0, (
        f"expected 0 migrated_chunks for empty collection, got: {final_job.result}"
    )
    assert final_job.migrations_applied == ["empty_rewrite"], (
        f"expected migrations_applied=['empty_rewrite'], got: {final_job.migrations_applied!r}"
    )

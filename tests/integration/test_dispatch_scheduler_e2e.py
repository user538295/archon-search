"""Task 2.1 — Export/import job dispatch integration.

Verifies that ExportJob and ImportJob reach terminal state via the real
``JobScheduler`` dispatch path (no mocking of dispatch_fn).  Also verifies
user-sourced jobs are dispatched before backup-sourced jobs, and that unsafe
tar members are rejected at import time.

Run with:
    uv run pytest tests/integration/test_dispatch_scheduler_e2e.py -v --no-cov

Implementation note — polling via job_store rather than GET /jobs/{id}:
``JobResponse.result`` is typed as ``str | None`` in the Pydantic schema
(schemas.py:152) while the underlying ``IngestJob.result`` dataclass field
is ``dict | None`` (types.py:27).  When an export/import job stores a dict
result (e.g. ``{"archive_path": "..."}``) the ``GET /jobs/{id}`` route calls
``JobResponse(**job_to_dict(job))`` which fails Pydantic validation with a
type-string error, making the endpoint return 500 for completed bulk jobs.

The tests therefore poll ``app.state.job_store.get(job_id)`` (synchronous,
no HTTP) for status transitions, and read dict fields directly from the
dataclass.  The 202 response from POST /export and POST /import is still
asserted via HTTP; only the polling uses the in-process job_store.
"""
from __future__ import annotations

import io
import json
import tarfile
import time
from pathlib import Path

import pytest

import archon_search.jobs.scheduler as _scheduler_module
from archon_search.types import ImportJob, IngestJob, JobStatus
from tests.integration.conftest import ingest_file_via_path, make_real_app, search

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TERMINAL = {JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _poll_job_store(
    job_store,
    job_id: str,
    *,
    timeout_s: float = 30.0,
) -> IngestJob:
    """Poll ``job_store.get(job_id)`` until the job reaches a terminal status.

    Returns the final job dataclass instance.  Calls ``pytest.fail`` on timeout
    or if the job is not found.

    Uses direct job_store access (not GET /jobs/{id}) because the REST endpoint
    fails with Pydantic validation errors when ``result`` is a dict — a known
    schema mismatch in ``JobResponse.result: str | None`` vs the dataclass
    ``IngestJob.result: dict | None``.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = job_store.get(job_id)
        if job is None:
            pytest.fail(f"job {job_id!r} not found in job_store")
        if job.status in _TERMINAL:
            return job
        time.sleep(0.1)
    pytest.fail(f"job {job_id!r} did not reach terminal state within {timeout_s}s")


# ---------------------------------------------------------------------------
# Test 1 — Export job reaches DONE with archive on disk
# ---------------------------------------------------------------------------


def test_export_job_reaches_done_with_archive_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /collections/{name}/export against real app with real dispatch_fn.

    Flow:
    1. Ingest a file into a real collection.
    2. POST /collections/{name}/export (no output_path — defaults to data_dir/exports/).
    3. Poll job_store until DONE.
    4. Assert .tar.gz exists at the archive_path recorded in job.result.
    5. Open the archive and assert manifest.json is valid JSON with the correct
       'collection' key.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    doc = tmp_path / "export_test.md"
    doc.write_text("# Export integration\n\nContent for export dispatch test.\n" * 4)

    col = "test-export-dispatch"

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        job_store = client.app.state.job_store

        # Ingest a document so the collection and meta row exist.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Trigger export — no output_path, defaults to data_dir/exports/.
        export_resp = client.post(
            f"/collections/{col}/export",
            json={},
            headers=_auth(api_key),
        )
        assert export_resp.status_code == 202, (
            f"POST /collections/{col}/export expected 202, "
            f"got {export_resp.status_code}: {export_resp.text}"
        )
        job_id = export_resp.json()["job_id"]

        # Poll until terminal via job_store (avoids Pydantic schema mismatch on result).
        final_job = _poll_job_store(job_store, job_id)
        assert final_job.status == JobStatus.DONE, (
            f"export job ended with {final_job.status}: error={final_job.error!r}"
        )

        # Archive path is in the result dict.
        assert isinstance(final_job.result, dict), (
            f"expected job.result to be a dict, got: {final_job.result!r}"
        )
        archive_path_str = final_job.result.get("archive_path")
        assert archive_path_str, (
            f"expected 'archive_path' in job.result; got: {final_job.result}"
        )
        archive_path = Path(archive_path_str)
        assert archive_path.exists(), (
            f"expected archive file at {archive_path}; file does not exist"
        )
        assert archive_path.name.endswith(".tar.gz"), (
            f"expected a .tar.gz file, got: {archive_path}"
        )

        # Open archive and validate manifest.
        with tarfile.open(archive_path, "r:gz") as tf:
            member = tf.getmember("manifest.json")
            with tf.extractfile(member) as f:
                assert f is not None, "manifest.json is not extractable"
                manifest = json.loads(f.read().decode("utf-8"))

        assert isinstance(manifest, dict), "manifest must be a JSON object"
        assert manifest.get("collection") == col, (
            f"manifest 'collection' field mismatch: expected {col!r}, "
            f"got {manifest.get('collection')!r}"
        )
        assert "schema_version" in manifest, "manifest missing 'schema_version'"
        assert "doc_count" in manifest, "manifest missing 'doc_count'"


# ---------------------------------------------------------------------------
# Test 2 — Import job reaches DONE and restores searchable collection
# ---------------------------------------------------------------------------


def test_import_job_reaches_done_and_restores_searchable_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Export a real collection, import into a new name, assert search results match.

    Flow:
    1. Ingest a document into collection A.
    2. POST /collections/A/export.  Poll until DONE; record archive_path.
    3. POST /collections/B/import with archive_path.  Poll until DONE.
    4. POST /search against collection B.  Assert non-empty results.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    doc = tmp_path / "import_test.md"
    doc.write_text(
        "# Import integration\n\nContent for export-import round-trip test.\n" * 4
    )

    col_a = "test-import-source"
    col_b = "test-import-dest"

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        job_store = client.app.state.job_store

        # Step 1 — ingest.
        ingest_file_via_path(client, col_a, str(doc), api_key=api_key)

        # Step 2 — export.
        export_resp = client.post(
            f"/collections/{col_a}/export",
            json={},
            headers=_auth(api_key),
        )
        assert export_resp.status_code == 202, (
            f"export POST expected 202, got {export_resp.status_code}: {export_resp.text}"
        )
        export_job_id = export_resp.json()["job_id"]
        export_job = _poll_job_store(job_store, export_job_id)
        assert export_job.status == JobStatus.DONE, (
            f"export job did not reach DONE: {export_job.error!r}"
        )
        archive_path = export_job.result["archive_path"]

        # Step 3 — import into col_b.
        import_resp = client.post(
            f"/collections/{col_b}/import",
            json={"path": archive_path},
            headers=_auth(api_key),
        )
        assert import_resp.status_code == 202, (
            f"import POST expected 202, got {import_resp.status_code}: {import_resp.text}"
        )
        import_job_id = import_resp.json()["job_id"]
        import_job = _poll_job_store(job_store, import_job_id)
        assert import_job.status == JobStatus.DONE, (
            f"import job did not reach DONE: {import_job.error!r}"
        )

        # Step 4 — search imported collection.
        results = search(client, col_b, "import integration content", api_key=api_key)
        assert results, (
            f"expected non-empty search results from imported collection {col_b!r}"
        )


# ---------------------------------------------------------------------------
# Test 3 — User job dispatched before backup-source job
# ---------------------------------------------------------------------------


def test_user_job_precedes_backup_source_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User-sourced export reaches RUNNING before backup-sourced export.

    Strategy:
    1. Reduce scheduler tick to 0.1s for fast dispatch.
    2. Use make_real_app to get a TestClient with a real scheduler.
    3. Freeze dispatch by setting scheduler._max_concurrent = 0.
    4. Enqueue backup job first, then user job (via job_store directly).
    5. Unfreeze (set _max_concurrent = 1).
    6. Poll job_store until both jobs have left QUEUED.
    7. Assert the user job transitioned out of QUEUED at or before the backup job.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    col = "test-priority-col"
    doc = tmp_path / "priority_test.md"
    doc.write_text("# Priority test\n\nContent for priority ordering test.\n" * 4)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Ingest so the collection + meta row exist.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        scheduler = client.app.state.scheduler
        job_store = client.app.state.job_store

        # Freeze dispatch — no slots available.
        monkeypatch.setattr(scheduler, "_max_concurrent", 0)

        # Enqueue backup job first (lower priority), then user job (higher priority).
        exports_dir = tmp_path / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)

        backup_job = job_store.create_export(
            collection=col,
            output_path=str(exports_dir / "backup-export.tar.gz"),
            tmp_path=str(exports_dir / ".backup-export.jsonl.tmp"),
            source="backup",
        )
        user_job = job_store.create_export(
            collection=col,
            output_path=str(exports_dir / "user-export.tar.gz"),
            tmp_path=str(exports_dir / ".user-export.jsonl.tmp"),
            source="user",
        )

        # Unfreeze — allow 1 concurrent job.
        monkeypatch.setattr(scheduler, "_max_concurrent", 1)

        # Poll until the user job leaves QUEUED; record transition timestamps.
        user_left_queued_at: float | None = None
        backup_left_queued_at: float | None = None
        deadline = time.monotonic() + 15.0

        while time.monotonic() < deadline:
            now = time.monotonic()
            user_j = job_store.get(user_job.job_id)
            backup_j = job_store.get(backup_job.job_id)

            if user_j is not None and user_j.status != JobStatus.QUEUED and user_left_queued_at is None:
                user_left_queued_at = now
            if backup_j is not None and backup_j.status != JobStatus.QUEUED and backup_left_queued_at is None:
                backup_left_queued_at = now

            # Exit once both have left QUEUED.
            if user_left_queued_at is not None and backup_left_queued_at is not None:
                break

            time.sleep(0.1)

        assert user_left_queued_at is not None, (
            f"user job {user_job.job_id!r} never left QUEUED within 15s"
        )
        assert backup_left_queued_at is not None, (
            f"backup job {backup_job.job_id!r} never left QUEUED within 15s"
        )
        assert user_left_queued_at <= backup_left_queued_at, (
            f"user job should leave QUEUED before (or simultaneously with) backup job; "
            f"user_left_queued_at={user_left_queued_at:.3f}, "
            f"backup_left_queued_at={backup_left_queued_at:.3f}"
        )


# ---------------------------------------------------------------------------
# Test 4 — Import with unsafe tar member returns 422
# ---------------------------------------------------------------------------


def _make_unsafe_archive(archive_path: Path) -> None:
    """Build a .tar.gz with a zip-slip path component (../../evil.txt)."""
    manifest = {
        "schema_version": 1,
        "collection": "col",
        "exported_at": "2025-01-01T00:00:00+00:00",
        "doc_count": 0,
        "active_embedding_model": "BAAI/bge-small-en-v1.5",
    }
    manifest_bytes = json.dumps(manifest).encode()
    docs_bytes = b""

    with tarfile.open(archive_path, "w:gz") as tf:
        # Add manifest.json (valid)
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        tf.addfile(info, io.BytesIO(manifest_bytes))

        # Add documents.jsonl (valid)
        info2 = tarfile.TarInfo(name="documents.jsonl")
        info2.size = len(docs_bytes)
        tf.addfile(info2, io.BytesIO(docs_bytes))

        # Add the unsafe zip-slip member — this must trigger 422.
        evil = tarfile.TarInfo(name="../../evil.txt")
        evil_bytes = b"evil content"
        evil.size = len(evil_bytes)
        tf.addfile(evil, io.BytesIO(evil_bytes))


def test_post_import_unsafe_tar_member_returns_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /collections/{name}/import with a zip-slip archive member returns 422.

    The import route runs validate_archive_members() before enqueueing any job.
    An archive with a '../../evil.txt' member must be rejected with 422 and no
    file must be written outside the tmp directory.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    # Build the unsafe archive inside tmp_path so path-safety check passes.
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    unsafe_archive = exports_dir / "unsafe.tar.gz"
    _make_unsafe_archive(unsafe_archive)

    # The evil.txt target (if zip-slip succeeded) would be at tmp_path.parent / "evil.txt".
    evil_target = tmp_path.parent / "evil.txt"
    assert not evil_target.exists(), "evil.txt must not exist before the test"

    col = "test-unsafe-import"

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        job_store = client.app.state.job_store

        import_resp = client.post(
            f"/collections/{col}/import",
            json={"path": str(unsafe_archive)},
            headers=_auth(api_key),
        )
        assert import_resp.status_code == 422, (
            f"expected 422 for unsafe archive, "
            f"got {import_resp.status_code}: {import_resp.text}"
        )

        # Confirm no evil file written outside the sandbox.
        assert not evil_target.exists(), (
            "evil.txt was written outside tmp_path — zip-slip guard failed"
        )

        # Confirm no import job was created for this collection.
        all_jobs = job_store.list()
        import_jobs_for_col = [
            j for j in all_jobs
            if isinstance(j, ImportJob) and j.collection == col
        ]
        assert not import_jobs_for_col, (
            f"expected no import job created for rejected import; "
            f"got: {import_jobs_for_col}"
        )

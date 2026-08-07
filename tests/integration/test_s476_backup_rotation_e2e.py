"""S476 — Backup keep-rotation fires synchronously after each successful backup.

Two scenarios:
  - S476-only_keep_archives_remain: exactly ``keep`` archives remain after
    a backup when more than ``keep`` exist.
  - S476-the_oldest_archives_are_the_ones_deleted: the survivors are the
    newest by filename-timestamp; the oldest are deleted.

Both bugs share the same root cause: ``_rotate`` was only triggered from
``_completion_loop`` (60-second poll), so a test that checks immediately
after the job reaches DONE saw 4 archives instead of 2.

Run with:
    uv run pytest tests/integration/test_s476_backup_rotation_e2e.py -v --no-cov
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import archon_search.jobs.scheduler as _scheduler_module
from archon_search.jobs.model import JobStatus
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_POLL_TIMEOUT_S: float = 30.0
_POLL_INTERVAL_S: float = 0.1
_ROTATION_WAIT_S: float = 5.0

COL = "s476_keep_docs"


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _poll_job_done(job_store, job_id: str, *, timeout_s: float = _POLL_TIMEOUT_S) -> None:
    terminal = {JobStatus.DONE, JobStatus.FAILED, JobStatus.FAILED_EXPIRED, JobStatus.CANCELLED}
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = job_store.get(job_id)
        if job is not None and job.status in terminal:
            if job.status != JobStatus.DONE:
                pytest.fail(
                    f"backup job {job_id!r} reached {job.status}, not DONE; "
                    f"error: {getattr(job, 'error', None)}"
                )
            return
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"backup job {job_id!r} did not reach DONE within {timeout_s}s")


def _seeded_archives(ns_dir: Path, col: str) -> tuple[Path, Path, Path]:
    """Create three old backup archives, oldest to newest, and return them."""
    ns_dir.mkdir(parents=True, exist_ok=True)
    a1 = ns_dir / f"{col}.backup.20260101T000000Z.tar.gz"
    a2 = ns_dir / f"{col}.backup.20260102T000000Z.tar.gz"
    a3 = ns_dir / f"{col}.backup.20260103T000000Z.tar.gz"
    for p in (a1, a2, a3):
        p.write_bytes(b"seeded")
    return a1, a2, a3


def test_s476_only_keep_archives_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a backup with keep=2 and 3 pre-existing archives, exactly 2 survive.

    S476-only_keep_archives_remain: assert len(remaining) == keep.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    doc = tmp_path / "s476_doc.md"
    doc.write_text("# S476 rotation test\n\n" + "content " * 50)

    toml = f"""
[backup]
interval_hours = 24
keep = 2
output_dir = "{tmp_path / 'backups'}"
"""

    with make_real_app(tmp_path, monkeypatch, backup_enabled=False, toml_content=toml) as (
        client,
        cfg,
        api_key,
    ):
        # Seed pre-existing archives under the namespace directory.
        ns_dir = Path(cfg.backup.output_dir) / "default"
        a1, a2, a3 = _seeded_archives(ns_dir, COL)

        # Also write a non-matching manual-export file that must survive.
        manual = ns_dir / f"{COL}-manual-export.tar.gz"
        manual.write_bytes(b"manual")

        job_store = client.app.state.job_store

        # Ingest so the collection exists.
        ingest_file_via_path(client, COL, str(doc), api_key=api_key)

        # Trigger backup.
        resp = client.post("/backup/trigger", headers=_auth(api_key))
        assert resp.status_code == 202, f"POST /backup/trigger: {resp.status_code} {resp.text}"
        queued = resp.json().get("queued", [])
        assert queued, f"expected at least one queued job; got: {resp.json()}"
        job_id = queued[0]["job_id"]

        # Wait for the backup job to reach DONE.
        _poll_job_done(job_store, job_id)

        # Rotation must happen as part of backup completion (not on next 60s poll).
        # Give a short window for the event loop to process the rotation callback.
        deadline = time.monotonic() + _ROTATION_WAIT_S
        remaining = []
        while time.monotonic() < deadline:
            remaining = sorted(ns_dir.glob(f"{COL}.backup.*.tar.gz"))
            if len(remaining) <= 2:
                break
            time.sleep(_POLL_INTERVAL_S)

        assert len(remaining) == 2, (
            f"expected exactly keep=2 archives matching {COL}.backup.*.tar.gz after backup; "
            f"remaining={[p.name for p in remaining]}"
        )

        # Non-matching file must still exist.
        assert manual.exists(), (
            "manual export archive must not be touched by rotation"
        )


def test_s476_oldest_archives_are_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two survivors are the newest by filename timestamp.

    S476-the_oldest_archives_are_the_ones_deleted: the oldest two must be gone;
    the newest pre-existing archive and the new backup must survive.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    doc = tmp_path / "s476_doc.md"
    doc.write_text("# S476 rotation test\n\n" + "content " * 50)

    toml = f"""
[backup]
interval_hours = 24
keep = 2
output_dir = "{tmp_path / 'backups'}"
"""

    with make_real_app(tmp_path, monkeypatch, backup_enabled=False, toml_content=toml) as (
        client,
        cfg,
        api_key,
    ):
        ns_dir = Path(cfg.backup.output_dir) / "default"
        a1, a2, a3 = _seeded_archives(ns_dir, COL)

        job_store = client.app.state.job_store

        ingest_file_via_path(client, COL, str(doc), api_key=api_key)

        resp = client.post("/backup/trigger", headers=_auth(api_key))
        assert resp.status_code == 202
        queued = resp.json().get("queued", [])
        assert queued
        job_id = queued[0]["job_id"]

        _poll_job_done(job_store, job_id)

        # Wait for rotation to complete.
        deadline = time.monotonic() + _ROTATION_WAIT_S
        while time.monotonic() < deadline:
            remaining = sorted(ns_dir.glob(f"{COL}.backup.*.tar.gz"))
            if len(remaining) <= 2:
                break
            time.sleep(_POLL_INTERVAL_S)

        remaining = sorted(ns_dir.glob(f"{COL}.backup.*.tar.gz"))

        # The two oldest seeded archives must be gone (line 52 of 40_backup_restore…).
        oldest_survivors = [p for p in (a1, a2) if p.exists()]
        assert not oldest_survivors, (
            f"oldest archives must be deleted after rotation; "
            f"still present: {[p.name for p in oldest_survivors]}; "
            f"remaining={[p.name for p in remaining]}"
        )

        # a3 (20260103) and the new archive from the backup must survive.
        assert a3.exists(), (
            f"newest pre-existing archive {a3.name} must survive rotation"
        )
        assert len(remaining) == 2, (
            f"expected exactly 2 archives; remaining={[p.name for p in remaining]}"
        )

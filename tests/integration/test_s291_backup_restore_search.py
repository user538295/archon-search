"""S291 — backup → wipe → restore → search, with an off-tree ``[backup].output_dir``.

Bug: ``[backup].output_dir`` accepts any absolute path (the operator runbook
``Documentation/OperatorGuide/40_backup_restore_disaster_recovery.md`` documents
``output_dir = "/mnt/nfs/archon-search/backups"`` and then tells operators to
restore with
``archon-search import <collection> /mnt/nfs/archon-search/backups/default/<archive>``),
but ``POST /collections/{name}/import`` validates the archive path against
``[get_data_dir()]`` only (``archon_search/server/routes_export.py``). So every
archive the product itself wrote to an off-tree backup root is rejected with
``400 {"error": "path_unsafe", "reason": "outside_allowed_dirs"}`` and the
collection can never be restored — the post-restore search returns an empty
result set instead of the pre-backup ``doc_id`` set.

Run with:
    uv run pytest tests/integration/test_s291_backup_restore_search.py --no-cov
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import archon_search.jobs.backup_loop as _backup_loop_module
import archon_search.jobs.scheduler as _scheduler_module
from archon_search.types import JobStatus
from tests.integration.conftest import ingest_file_via_path, make_real_app, search

pytestmark = pytest.mark.integration

_POLL_TIMEOUT_S: float = 30.0
_POLL_INTERVAL_S: float = 0.1
_COLLECTION = "s291-offsite-restore"
_QUERY = "hybrid retrieval reranker"


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _poll_job_done(job_store, job_id: str, *, what: str) -> None:
    """Poll ``job_store`` until ``job_id`` is terminal; fail unless it is DONE."""
    terminal = {
        JobStatus.DONE,
        JobStatus.FAILED,
        JobStatus.FAILED_EXPIRED,
        JobStatus.CANCELLED,
    }
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        job = job_store.get(job_id)
        if job is not None and job.status in terminal:
            if job.status != JobStatus.DONE:
                pytest.fail(
                    f"{what} job {job_id} ended in {job.status}: "
                    f"{getattr(job, 'error', None)}"
                )
            return
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"{what} job {job_id} did not reach a terminal status in {_POLL_TIMEOUT_S}s")


def test_s291_offsite_backup_archive_can_be_restored_and_searched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archive written to an off-tree ``[backup].output_dir`` must be importable.

    1. Ingest a document and record the pre-backup ``doc_id`` set from /search.
    2. ``POST /backup/trigger`` writes a ``.tar.gz`` under an off-tree backup root.
    3. Simulate the data-dir wipe by dropping the collection.
    4. Restore with ``POST /collections/{name}/import`` pointing at that archive.
    5. The same search must return the same ``doc_id`` set.
    """
    monkeypatch.setattr(_backup_loop_module, "_BACKUP_COMPLETION_POLL_SECONDS", 0.1)
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    # Off-tree backup root: a sibling of the data dir, exactly like the runbook's
    # /mnt/nfs/... example. Deliberately NOT under tmp_path (= the data dir).
    offsite_root = tmp_path.parent / f"{tmp_path.name}-s291-offsite-backups"
    offsite_root.mkdir(parents=True, exist_ok=True)

    doc = tmp_path / "s291_doc.md"
    doc.write_text(
        "# Hybrid retrieval\n\n"
        "Archon Search combines dense vector search with full-text search and "
        "refines the ranking with a cross-encoder reranker.\n" * 3,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, backup_enabled=True) as (client, cfg, api_key):
        job_store = client.app.state.job_store
        # Off-host archive root, as documented for [backup].output_dir.
        cfg.backup.output_dir = str(offsite_root)

        ingest_file_via_path(client, _COLLECTION, str(doc), api_key=api_key)

        baseline = search(client, _COLLECTION, _QUERY, api_key=api_key)
        assert baseline, "pre-backup search returned no results — fixture is broken"
        baseline_ids = sorted({r["doc_id"] for r in baseline})

        # --- backup ---
        trigger = client.post("/backup/trigger", headers=_auth(api_key))
        assert trigger.status_code == 202, f"{trigger.status_code}: {trigger.text}"
        queued = trigger.json()["queued"]
        assert queued, f"no backup job queued: {trigger.json()}"
        _poll_job_done(job_store, queued[0]["job_id"], what="backup")

        archives = list(offsite_root.rglob("*.tar.gz"))
        assert len(archives) == 1, f"expected one archive under {offsite_root}, got {archives}"
        archive = archives[0]

        # --- wipe (the index is gone; only the off-host archive survives) ---
        drop = client.delete(f"/collections/{_COLLECTION}", headers=_auth(api_key))
        assert drop.status_code == 200, f"{drop.status_code}: {drop.text}"

        # --- restore ---
        restore = client.post(
            f"/collections/{_COLLECTION}/import",
            json={"path": str(archive)},
            headers=_auth(api_key),
        )
        assert restore.status_code == 202, (
            "S291: restoring the product's own backup archive was rejected — "
            f"POST /collections/{_COLLECTION}/import returned "
            f"{restore.status_code}: {restore.text} (archive={archive})"
        )
        _poll_job_done(job_store, restore.json()["job_id"], what="import")

        # --- search again ---
        restored = search(client, _COLLECTION, _QUERY, api_key=api_key)
        assert sorted({r["doc_id"] for r in restored}) == baseline_ids, (
            "post-restore doc_id set differs from the pre-backup set: "
            f"{sorted({r['doc_id'] for r in restored})} != {baseline_ids}"
        )

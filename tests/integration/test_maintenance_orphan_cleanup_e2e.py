"""T-3 — e2e: ingest doc, delete source file, POST trigger, verify orphans_removed_last_run > 0.

Plan: Documentation/Backlog/D5-maintenance-jobs-policies-team-plan.md Task T-3

Verifies:
- S8: orphaned chunks (file-path source_path with Path.exists() == False) are removed via
  delete_by_source_path; orphans_removed_last_run count is written to collection_health;
  all chunks for the deleted source path are gone from the store.

Flow:
1. Start real app with maintenance_enabled=True (interval_hours=1), orphan_cleanup=True.
2. Write a real text file and ingest it via POST /ingest (establishes chunks in the store).
3. Delete the source file so it becomes an orphan.
4. POST /maintenance/trigger → assert 202 + {"status": "triggered"}.
5. Poll GET /status until collection_health entry for the collection has
   orphans_removed_last_run > 0 (max 15 s).
6. Assert orphans_removed_last_run == 1 (exactly one unique source_path deleted).
7. Assert last_run_at is non-null, last_error is None, and search returns no results.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app, search

pytestmark = pytest.mark.integration

_POLL_TIMEOUT_S: float = 15.0
_POLL_INTERVAL_S: float = 0.1


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def test_orphan_cleanup_removes_deleted_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-3 e2e: ingest doc, delete source file, POST trigger, verify orphans_removed_last_run == 1.

    Flow:
    1. Start real app with maintenance_enabled=True (interval_hours=1).
    2. Write a small text file and ingest it via ingest_file_via_path.
       The pipeline calls rebuild_fts_index() after ingest, creating chunks in the store.
    3. Delete the source file so chunks become orphans.
    4. POST /maintenance/trigger — assert 202 + {"status": "triggered"}.
    5. Poll GET /status until collection_health entry for the collection has
       orphans_removed_last_run > 0 (max 15 s).
    6. Assert orphans_removed_last_run == 1 (one unique source_path deleted).
    7. Assert last_run_at is non-null, last_error is None, and search returns no results
       (confirming chunks are actually gone from the store, not just the counter).

    Completes: S8 (orphaned chunks removed; orphans_removed_last_run counter written;
    all chunks for the deleted source path are gone from the store).
    """
    col = "maint-orphan-e2e"

    # Step 2: Write a small text document for ingestion.
    doc = tmp_path / "orphan_e2e_doc.txt"
    doc.write_text(
        "Orphan cleanup e2e test document. " * 30
        + "This text is used to verify that orphaned chunks are removed "
        + "by the MaintenanceLoop._run_orphan_cleanup policy when the source file is deleted."
    )

    with make_real_app(tmp_path, monkeypatch, maintenance_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Step 2: Ingest the document so chunks exist in the store.
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Step 3: Delete the source file to make chunks orphans.
        doc.unlink()
        assert not doc.exists(), "source file must be deleted before triggering maintenance"

        # Step 4: POST /maintenance/trigger
        trigger_resp = client.post("/maintenance/trigger", headers=_auth(api_key))
        assert trigger_resp.status_code == 202, (
            f"expected 202, got {trigger_resp.status_code}: {trigger_resp.text}"
        )
        assert trigger_resp.json().get("status") == "triggered", (
            f"expected status='triggered', got: {trigger_resp.json()}"
        )

        # Step 5: Poll GET /status until orphans_removed_last_run > 0
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        maintenance_block = None
        orphans_removed = None
        last_error_in_entry: str | None = "NOT_SET"  # sentinel to detect absence

        while time.monotonic() < deadline:
            status_resp = client.get("/status", headers=_auth(api_key))
            assert status_resp.status_code == 200, (
                f"GET /status failed: {status_resp.status_code} {status_resp.text}"
            )
            status_body = status_resp.json()
            maintenance_block = status_body.get("maintenance")
            if maintenance_block is not None:
                health = maintenance_block.get("collection_health", [])
                for entry in health:
                    if entry.get("collection") == col:
                        count = entry.get("orphans_removed_last_run", 0)
                        if count > 0:
                            orphans_removed = count
                            last_error_in_entry = entry.get("last_error")
                            break
            if orphans_removed is not None:
                break
            time.sleep(_POLL_INTERVAL_S)
        else:
            pytest.fail(
                f"orphans_removed_last_run did not become > 0 within {_POLL_TIMEOUT_S}s; "
                f"last maintenance block: {maintenance_block}"
            )

        # Step 6: Assert orphans_removed_last_run == 1 (exactly one unique source_path)
        assert orphans_removed == 1, (
            f"expected orphans_removed_last_run == 1 (one unique source_path), got {orphans_removed}"
        )

        # Step 7a: Assert last_run_at is non-null (pass completed and state was written)
        assert maintenance_block is not None
        assert maintenance_block["last_run_at"] is not None, (
            "maintenance.last_run_at must be non-null after a completed pass"
        )

        # Step 7b: Assert no error occurred during cleanup
        assert last_error_in_entry is None, (
            f"expected last_error=None after successful cleanup, got: {last_error_in_entry!r}"
        )

        # Step 7c: Verify chunks are actually gone — search returns no results
        results = search(client, col, "orphan cleanup e2e test document", api_key=api_key)
        assert results == [], (
            f"expected empty search results after orphan cleanup, got {len(results)} results: {results}"
        )

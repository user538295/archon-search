"""T-2 — e2e: POST /maintenance/trigger → fts_optimized_at non-null in GET /status.

Plan: Documentation/Backlog/D5-maintenance-jobs-policies-team-plan.md Task T-2

Verifies:
- S5: FTS optimize runs during a maintenance pass; fts_optimized_at appears
  in GET /status maintenance.collection_health after a pass completes.

Flow:
1. Start real app with maintenance_enabled=True (interval_hours=1).
2. Create a small text file and ingest it via POST /ingest (establishes FTS index via rebuild_fts_index).
3. POST /maintenance/trigger → assert 202.
4. Poll GET /status until collection_health[0].fts_optimized_at is non-null (max 15 s).
5. Assert fts_optimized_at is ISO-8601 parseable.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import archon_search.jobs.scheduler as _scheduler_module
import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_POLL_TIMEOUT_S: float = 15.0
_POLL_INTERVAL_S: float = 0.1


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def test_fts_optimized_at_appears_in_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-2 e2e: ingest doc, POST trigger, poll until fts_optimized_at is non-null.

    Flow:
    1. Start real app with maintenance_enabled=True (interval_hours=1).
    2. Write a small text file and ingest it via POST /ingest.
       The pipeline calls rebuild_fts_index() after ingest, creating the FTS index.
    3. POST /maintenance/trigger — assert 202.
    4. Poll GET /status until collection_health[0].fts_optimized_at is non-null (max 15 s).
    5. Assert fts_optimized_at is ISO-8601 parseable.

    Completes: S5 (FTS optimize runs; fts_optimized_at appears in health state).
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    col = "maint-fts-test"

    # Write a small text document for ingestion.
    doc = tmp_path / "fts_test_doc.txt"
    doc.write_text(
        "Maintenance loop FTS optimize test document. " * 20
        + "This text is used to verify that the FTS index is built during ingest "
        + "and subsequently optimized by the MaintenanceLoop._run_fts_optimize policy."
    )

    with make_real_app(tmp_path, monkeypatch, maintenance_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Step 2: ingest the document (creates FTS index via rebuild_fts_index)
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Step 3: POST /maintenance/trigger
        trigger_resp = client.post("/maintenance/trigger", headers=_auth(api_key))
        assert trigger_resp.status_code == 202, (
            f"expected 202, got {trigger_resp.status_code}: {trigger_resp.text}"
        )

        # Step 4: Poll GET /status until fts_optimized_at is non-null
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        maintenance_block = None
        fts_optimized_at = None

        while time.monotonic() < deadline:
            status_resp = client.get("/status", headers=_auth(api_key))
            assert status_resp.status_code == 200, (
                f"GET /status failed: {status_resp.status_code} {status_resp.text}"
            )
            status_body = status_resp.json()
            maintenance_block = status_body.get("maintenance")
            if maintenance_block is not None:
                health = maintenance_block.get("collection_health", [])
                # Find the health entry for our collection
                for entry in health:
                    if entry.get("collection") == col:
                        fts_optimized_at = entry.get("fts_optimized_at")
                        if fts_optimized_at is not None:
                            break
            if fts_optimized_at is not None:
                break
            time.sleep(_POLL_INTERVAL_S)
        else:
            pytest.fail(
                f"fts_optimized_at did not become non-null within {_POLL_TIMEOUT_S}s; "
                f"last maintenance block: {maintenance_block}"
            )

        # Step 5: assert fts_optimized_at is ISO-8601 parseable
        assert fts_optimized_at is not None, "fts_optimized_at must be non-null"
        datetime.fromisoformat(fts_optimized_at)

"""T-1 — e2e: POST /maintenance/trigger → 202; GET /status shows maintenance.last_run_at non-null.

Plan: Documentation/Backlog/D5-maintenance-jobs-policies-team-plan.md Task T-1

Verifies:
- S17: POST /maintenance/trigger returns 202 with {"status": "triggered"}
- S17: POST /maintenance/trigger returns 202 with {"status": "already_triggered"} when busy
- S20: GET /status reflects maintenance.enabled=True and last_run_at non-null after pass
- S20: next_run_at is non-null when interval_hours=1
- S20: last_run_at is ISO-8601 parseable

The test uses make_real_app(maintenance_enabled=True) which sets
config.maintenance.interval_hours = 1 so that maintenance.enabled=True
is visible in GET /status. The pass is triggered immediately via
POST /maintenance/trigger — no waiting for the real 1-hour interval.
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import archon_search.jobs.scheduler as _scheduler_module
import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

# Poll constants
_POLL_TIMEOUT_S: float = 10.0
_POLL_INTERVAL_S: float = 0.1


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def test_maintenance_trigger_and_status_reflect_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-1 e2e: POST trigger fires a pass; GET /status reflects maintenance.last_run_at non-null.

    Flow:
    1. Start real app with maintenance_enabled=True (interval_hours=1).
    2. POST /maintenance/trigger — assert 202 + {"status": "triggered"}.
    3. Poll GET /status until maintenance.last_run_at is non-null (max 10 s).
    4. Assert maintenance.enabled=True, collection_health==[], next_run_at non-null,
       and last_run_at is ISO-8601.

    Completes: S17 (202 + async pass), S20 (GET /status maintenance block).
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    with make_real_app(tmp_path, monkeypatch, maintenance_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Step 2: POST /maintenance/trigger
        trigger_resp = client.post("/maintenance/trigger", headers=_auth(api_key))
        assert trigger_resp.status_code == 202, (
            f"expected 202, got {trigger_resp.status_code}: {trigger_resp.text}"
        )
        body = trigger_resp.json()
        assert body.get("status") == "triggered", (
            f"expected status='triggered', got: {body}"
        )

        # Step 3: Poll GET /status until maintenance.last_run_at is non-null
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        maintenance_block = None
        while time.monotonic() < deadline:
            status_resp = client.get("/status", headers=_auth(api_key))
            assert status_resp.status_code == 200, (
                f"GET /status failed: {status_resp.status_code} {status_resp.text}"
            )
            status_body = status_resp.json()
            maintenance_block = status_body.get("maintenance")
            if (
                maintenance_block is not None
                and maintenance_block.get("last_run_at") is not None
            ):
                break
            time.sleep(_POLL_INTERVAL_S)
        else:
            pytest.fail(
                f"maintenance.last_run_at did not become non-null within {_POLL_TIMEOUT_S}s; "
                f"last maintenance block: {maintenance_block}"
            )

        # Step 4: assert enabled, collection_health, last_run_at, and next_run_at
        assert maintenance_block is not None, "maintenance block must not be None"
        assert maintenance_block["enabled"] is True, (
            f"expected maintenance.enabled=True with interval_hours=1; got: {maintenance_block['enabled']}"
        )
        assert maintenance_block["collection_health"] == [], (
            f"expected empty collection_health with no collections; got: {maintenance_block['collection_health']}"
        )
        assert maintenance_block["last_run_at"] is not None, (
            "maintenance.last_run_at must be non-null after a completed pass"
        )
        # S20: last_run_at must be ISO-8601 parseable
        datetime.fromisoformat(maintenance_block["last_run_at"])
        # S20: next_run_at must be non-null when interval_hours=1
        assert maintenance_block["next_run_at"] is not None, (
            "maintenance.next_run_at must be non-null when interval_hours=1"
        )


def test_maintenance_trigger_already_triggered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S17 e2e: POST trigger when event is already set returns 202 + {"status": "already_triggered"}.

    Uses MagicMock to set _trigger_event.is_set() = True without racing the real loop,
    mirroring the pattern from test_trigger_while_busy_returns_202 in test_routes_maintenance.py.
    """
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", 0.1)

    with make_real_app(tmp_path, monkeypatch, maintenance_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        loop = client.app.state.maintenance_loop
        mock_event = MagicMock()
        mock_event.is_set.return_value = True
        loop._trigger_event = mock_event

        resp = client.post("/maintenance/trigger", headers=_auth(api_key))
        assert resp.status_code == 202, (
            f"expected 202, got {resp.status_code}: {resp.text}"
        )
        assert resp.json() == {"status": "already_triggered"}, (
            f"expected already_triggered body; got: {resp.json()}"
        )
        mock_event.set.assert_not_called()

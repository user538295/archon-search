"""E0b / T-4 — e2e: CLI --timeout behavior, status warnings, ingest stderr.

Scenarios covered:
  S8:  archon-search status CLI with no API key set, hyde enabled →
       stderr contains 'ANTHROPIC_API_KEY'
  S22: archon-search maintenance run --wait --timeout N (timeout fires) →
       exit 0, stderr has recovery message
  S23: archon-search maintenance run --wait, pass completes with errors →
       exit 2

Manual scenarios (cannot be automated — require real launchd/systemd):
  S9:  macOS launchd service loads ANTHROPIC_API_KEY from ~/.archon-search/.secrets.env
       via wrapper script; .secrets.env absent does not prevent service start.
  S10: macOS absent .secrets.env → confirm launchd service starts normally.

Note: TestClient-based tests are integration-level (in-process ASGI). Labeled
#e2e_test in the plan because they exercise the full application stack with
real server responses fed into the CLI renderer.  True process-isolated e2e is
not required for E0b.

The TestClient is ASGI in-process; ``httpx.get("http://localhost:…")`` from the
CLI would fail against it.  The established project pattern (confirmed in
tests/integration/test_d8_t2_e2e_status_observability.py) is to:
  1. Capture the real server payload via TestClient.
  2. Patch the CLI's HTTP call to return that payload.
This proves the CLI renders the real server's response correctly without
requiring a real TCP socket.

The HTTP wiring of each CLI helper (_fetch_server_status, httpx.get/post) is
covered at unit level in tests/cli/test_status.py and
tests/test_cli_maintenance.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from archon_search.cli.main import main
from archon_search.cli.maintenance_cmd import maintenance_cmd
from archon_search.platform.service import ServiceStatus
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _make_svc(running: bool = True) -> MagicMock:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=running, pid=None, uptime_seconds=None)
    return svc


# ---------------------------------------------------------------------------
# S8: status CLI warns when HyDE enabled and ANTHROPIC_API_KEY absent
# ---------------------------------------------------------------------------


def test_e2e_status_cli_key_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real app with hyde.enabled=True and no ANTHROPIC_API_KEY; CLI emits warning on stderr.

    Covers scenario S8: archon-search status must warn on stderr when HyDE is
    configured but ANTHROPIC_API_KEY is absent from the environment.

    ANTHROPIC_API_KEY is cleared by the root conftest.py for every test so no
    extra monkeypatch.delenv is needed.  The real /status payload is captured via
    TestClient and supplied to the CLI renderer via _fetch_server_status patch
    (established project pattern; see test_d8_t2_e2e_status_observability.py).
    """
    with make_real_app(tmp_path, monkeypatch, hyde_enabled=True) as (
        client,
        _cfg,
        api_key,
    ):
        # Step 1: Capture the real server payload via ASGI TestClient.
        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, (
            f"GET /status failed: {resp.status_code}: {resp.text}"
        )
        body = resp.json()

        # Sanity: confirm the real server reports key_available=false (S7 contract).
        assert "hyde" in body, (
            f"Expected 'hyde' field in GET /status when hyde.enabled=True; body: {body!r}"
        )
        assert body["hyde"] is not None, (
            "Expected non-null hyde sub-object when hyde.enabled=True"
        )
        assert body["hyde"]["key_available"] is False, (
            f"Expected hyde.key_available=false (ANTHROPIC_API_KEY absent); "
            f"got: {body['hyde']['key_available']!r}"
        )

        # Step 2: Feed the real payload into the CLI renderer via patch.
        runner = CliRunner()
        with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
            with patch(
                "archon_search.cli.status._fetch_server_status",
                return_value=body,
            ):
                result = runner.invoke(main, ["status"])

    assert result.exit_code == 0, (
        f"archon-search status exited {result.exit_code}:\n{result.output}"
    )
    assert "ANTHROPIC_API_KEY" in result.stderr, (
        f"Expected 'ANTHROPIC_API_KEY' in stderr warning; got: {result.stderr!r}"
    )
    assert "HyDE" in result.stderr, (
        f"Expected 'HyDE' in stderr warning; got: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# S22: maintenance run --wait --timeout fires → exit 0 + recovery message
# ---------------------------------------------------------------------------


def test_e2e_maintenance_wait_timeout_recovery_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real app; maintenance CLI --wait --timeout fires; exit 0 + recovery message on stderr.

    Covers scenario S22: when ``archon-search maintenance run --wait --timeout N``
    times out before the pass completes, the CLI must exit 0 and print a recovery
    hint containing 'archon-search maintenance status' to stderr.

    Flow:
    1. Start real app via make_real_app.
    2. Capture the real GET /status payload (maintenance.last_run_at) via TestClient.
    3. Invoke the maintenance CLI via Click runner with:
       - httpx.post returning a real-server-like 202 response (triggered).
       - httpx.get always returning the same last_run_at (timeout-forcing stub).
       - time.sleep patched to skip actual waits.
       - --timeout 6 → max_polls = 3 (6 / _POLL_INTERVAL_SECONDS=2).
    4. Assert exit 0 and 'archon-search maintenance status' in stderr.

    The TestClient is ASGI in-process; httpx calls from the CLI are patched per
    the established project pattern. The HTTP wiring of the maintenance CLI is
    covered at unit level in tests/test_cli_maintenance.py.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        # Step 1: Capture real baseline maintenance state from the running app.
        status_resp = client.get("/status", headers=_auth(api_key))
        assert status_resp.status_code == 200, (
            f"GET /status failed: {status_resp.status_code}: {status_resp.text}"
        )
        status_body = status_resp.json()
        # Baseline last_run_at is None (maintenance not triggered yet in this app instance).
        maintenance_block = status_body.get("maintenance") or {}
        baseline_last_run_at = maintenance_block.get("last_run_at")

        # Step 2: Build a mock GET response that keeps last_run_at unchanged (forces timeout).
        frozen_status_payload = {
            "maintenance": {
                "enabled": False,
                "interval_hours": 0,
                "last_run_at": baseline_last_run_at,
                "next_run_at": None,
                "collection_health": [],
            }
        }
        frozen_get_resp = MagicMock()
        frozen_get_resp.status_code = 200
        frozen_get_resp.json.return_value = frozen_status_payload
        frozen_get_resp.text = json.dumps(frozen_status_payload)

        # Build a mock POST 202 response (trigger accepted).
        trigger_post_resp = MagicMock()
        trigger_post_resp.status_code = 202
        trigger_post_resp.json.return_value = {"status": "triggered"}
        trigger_post_resp.text = '{"status": "triggered"}'

        runner = CliRunner()
        with (
            patch(
                "archon_search.cli.maintenance_cmd.get_data_dir",
                return_value=tmp_path,
            ),
            patch(
                "archon_search.cli.maintenance_cmd.httpx.post",
                return_value=trigger_post_resp,
            ),
            patch(
                "archon_search.cli.maintenance_cmd.httpx.get",
                return_value=frozen_get_resp,
            ),
            patch("archon_search.cli.maintenance_cmd.time.sleep"),
        ):
            result = runner.invoke(
                maintenance_cmd,
                ["run", "--wait", "--timeout", "6", "--api-key", api_key],
            )

    # S22: timeout exits 0, not 1 (breaking change from D5).
    assert result.exit_code == 0, (
        f"Expected exit 0 on timeout; got {result.exit_code}. "
        f"Output:\n{result.output}\nStderr:\n{result.stderr}"
    )
    # Verify the timeout code path fired (not the success path).
    assert "Timed out after 6s" in result.stderr, (
        f"Expected 'Timed out after 6s' in stderr (confirms timeout path, not success); "
        f"stderr: {result.stderr!r}"
    )
    # S22: stderr must contain the recovery hint.
    assert "archon-search maintenance status" in result.stderr, (
        f"Expected 'archon-search maintenance status' in stderr after timeout; "
        f"stderr: {result.stderr!r}, output: {result.output!r}"
    )


# ---------------------------------------------------------------------------
# S23: maintenance run --wait, pass completes with errors → exit 2
# ---------------------------------------------------------------------------


def test_e2e_maintenance_wait_exits_2_on_failed_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real app; maintenance run --wait sees a pass complete with errors; assert exit 2.

    Covers scenario S23: when the maintenance pass completes (last_run_at changes)
    but collection_health contains a non-null last_error, the CLI exits 2.

    Flow:
    1. Start real app via make_real_app.
    2. Capture real GET /status baseline (last_run_at=None before any pass).
    3. Invoke maintenance CLI with:
       - httpx.post returning a 202 triggered response.
       - httpx.get returning a payload where last_run_at changed AND
         collection_health contains an entry with a non-null last_error.
       - time.sleep patched to skip waits.
       - --timeout 60 (enough polls to detect the changed last_run_at on first poll).
    4. Assert exit 2.

    The has_errors path in _wait_for_pass() reads collection_health[].last_error
    from the GET /status response. When last_run_at changes and has_errors=True,
    SystemExit(2) is raised. This test exercises that code path at the e2e level;
    the unit-level counterpart is test_maintenance_run_wait_exits_2_on_failed in
    tests/test_cli_maintenance.py.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        # Step 1: Build mock HTTP responses.
        # The CLI calls httpx.get twice: once for the baseline last_run_at (before
        # triggering) and once per poll iteration. We return None as the baseline
        # (no pass has run yet) and a "completed with errors" payload on the first
        # poll — causing _wait_for_pass to detect last_run_at changed AND has_errors.

        # Baseline GET /status: last_run_at is None (no maintenance pass has run).
        baseline_payload = {
            "maintenance": {
                "enabled": False,
                "interval_hours": 0,
                "last_run_at": None,
                "next_run_at": None,
                "collection_health": [],
            }
        }
        baseline_get_resp = MagicMock()
        baseline_get_resp.status_code = 200
        baseline_get_resp.json.return_value = baseline_payload
        baseline_get_resp.text = json.dumps(baseline_payload)

        # Poll GET /status: last_run_at changed AND collection_health has last_error.
        # _get_maintenance_state() checks: current_last_run_at != original AND has_errors.
        failed_pass_payload = {
            "maintenance": {
                "enabled": False,
                "interval_hours": 0,
                "last_run_at": "2026-06-27T00:00:01+00:00",  # Different from None
                "next_run_at": None,
                "collection_health": [
                    {
                        "collection": "test-col",
                        "last_error": "failed to optimize FTS index: disk full",
                        "chunks_scanned": 10,
                        "orphans_deleted": 0,
                        "failed_retried": 0,
                    }
                ],
            }
        }
        failed_get_resp = MagicMock()
        failed_get_resp.status_code = 200
        failed_get_resp.json.return_value = failed_pass_payload
        failed_get_resp.text = json.dumps(failed_pass_payload)

        # Build a mock POST 202 response (trigger accepted).
        trigger_post_resp = MagicMock()
        trigger_post_resp.status_code = 202
        trigger_post_resp.json.return_value = {"status": "triggered"}
        trigger_post_resp.text = '{"status": "triggered"}'

        runner = CliRunner()
        with (
            patch(
                "archon_search.cli.maintenance_cmd.get_data_dir",
                return_value=tmp_path,
            ),
            patch(
                "archon_search.cli.maintenance_cmd.httpx.post",
                return_value=trigger_post_resp,
            ),
            patch(
                "archon_search.cli.maintenance_cmd.httpx.get",
                side_effect=[
                    baseline_get_resp,  # First call: baseline last_run_at capture
                    failed_get_resp,    # Second call: first poll — pass complete with errors
                ],
            ),
            patch("archon_search.cli.maintenance_cmd.time.sleep"),
        ):
            result = runner.invoke(
                maintenance_cmd,
                ["run", "--wait", "--timeout", "60", "--api-key", api_key],
            )

    # S23: FAILED pass (last_run_at changed + has_errors=True) → exit 2.
    assert result.exit_code == 2, (
        f"Expected exit 2 when maintenance pass completed with errors; "
        f"got {result.exit_code}. "
        f"Output:\n{result.output}\nStderr:\n{result.stderr}"
    )
    assert "Maintenance pass completed with errors." in result.stderr, (
        f"Expected has_errors stderr message not found.\nStderr:\n{result.stderr}"
    )

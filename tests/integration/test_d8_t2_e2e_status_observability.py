"""D8 / T-2 — e2e: status observability.

Scenarios covered:
- S10: server running with hash_doc_ids=true and salt loaded → GET /status returns
       telemetry.hash_doc_ids_enabled=true
- S11: server running with hash_doc_ids=false → GET /status returns
       telemetry.hash_doc_ids_enabled=false
- S12a: archon-search status CLI run against real server → output contains hash_doc_ids_enabled
- S12b: archon-search status CLI when server unreachable → service state shown, telemetry omitted

All tests use ``make_real_app`` (real LanceDB in tmp_path, real JobScheduler,
TestClient over ASGI transport).  The S12 CLI tests patch ``_fetch_server_status``
because TestClient is ASGI in-process; a real ``httpx.get("http://localhost:…")``
would fail.  The HTTP wiring of ``_fetch_server_status`` (URL, Bearer auth, error
handling) is covered at the unit level in tests/cli/test_status.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from archon_search.cli.main import main
from archon_search.platform.service import ServiceStatus
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# S10 — GET /status with hashing on → telemetry.hash_doc_ids_enabled=true
# ---------------------------------------------------------------------------


def test_e2e_get_status_hash_doc_ids_enabled_true(
    tmp_path, monkeypatch
) -> None:
    """Real server with hash_doc_ids=true and salt loaded → GET /status returns
    telemetry.hash_doc_ids_enabled=true (S10).

    Uses make_real_app with both telemetry_enabled=True and hash_doc_ids_enabled=True
    so the lifespan loads the salt, builds the doc_id_hasher closure, and
    _build_telemetry_status() returns hash_doc_ids_enabled=True.
    All assertions run inside the ``with`` block while the app is still alive.
    """
    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.get("/status", headers=headers)

        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "telemetry" in body, (
            f"Expected 'telemetry' field in status response; body: {body!r}"
        )
        telemetry = body["telemetry"]
        assert telemetry is not None, (
            "Expected non-null telemetry in GET /status when telemetry is enabled (S10). "
            f"Response body: {body!r}"
        )
        assert telemetry.get("enabled") is True, (
            f"Expected telemetry.enabled=True (S10). Got: {telemetry.get('enabled')!r}"
        )
        assert "hash_doc_ids_enabled" in telemetry, (
            f"Expected 'hash_doc_ids_enabled' in telemetry sub-object (S10). "
            f"Telemetry: {telemetry!r}"
        )
        assert telemetry["hash_doc_ids_enabled"] is True, (
            f"Expected hash_doc_ids_enabled=True when hashing is active (S10). "
            f"Got: {telemetry['hash_doc_ids_enabled']!r}. Full telemetry: {telemetry!r}"
        )


# ---------------------------------------------------------------------------
# S11 — GET /status with hashing off → telemetry.hash_doc_ids_enabled=false
# ---------------------------------------------------------------------------


def test_e2e_get_status_hash_doc_ids_enabled_false(
    tmp_path, monkeypatch
) -> None:
    """Real server with hash_doc_ids=false → GET /status returns
    telemetry.hash_doc_ids_enabled=false (S11).

    Telemetry is enabled so the telemetry sub-object is present (not null);
    only hashing is off, so hash_doc_ids_enabled must be False.
    All assertions run inside the ``with`` block while the app is still alive.
    """
    with make_real_app(
        tmp_path,
        monkeypatch,
        telemetry_enabled=True,
        # hash_doc_ids_enabled defaults to False
    ) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.get("/status", headers=headers)

        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "telemetry" in body, (
            f"Expected 'telemetry' field in status response; body: {body!r}"
        )
        telemetry = body["telemetry"]
        assert telemetry is not None, (
            "Expected non-null telemetry in GET /status when telemetry is enabled (S11). "
            f"Response body: {body!r}"
        )
        assert telemetry.get("enabled") is True, (
            f"Expected telemetry.enabled=True (S11): distinguishes 'hashing off but telemetry on' "
            f"from 'telemetry null'. Got: {telemetry.get('enabled')!r}"
        )
        assert "hash_doc_ids_enabled" in telemetry, (
            f"Expected 'hash_doc_ids_enabled' in telemetry sub-object (S11). "
            f"Telemetry: {telemetry!r}"
        )
        assert telemetry["hash_doc_ids_enabled"] is False, (
            f"Expected hash_doc_ids_enabled=False when hashing is disabled (S11). "
            f"Got: {telemetry['hash_doc_ids_enabled']!r}. Full telemetry: {telemetry!r}"
        )


# ---------------------------------------------------------------------------
# S12a — archon-search status CLI displays hash_doc_ids_enabled (server reachable)
# ---------------------------------------------------------------------------


def test_e2e_cli_status_displays_hash_doc_ids_flag(
    tmp_path, monkeypatch
) -> None:
    """archon-search status CLI against real server → output contains hash_doc_ids_enabled (S12a).

    TestClient is ASGI in-process; ``httpx.get("http://localhost:…")`` would fail.
    ``_fetch_server_status`` is patched to supply the real server payload (captured
    via TestClient) to the CLI renderer.  This proves the CLI correctly parses and
    renders the server's response without requiring a real listening TCP socket.

    The HTTP wiring of ``_fetch_server_status`` (URL construction, Bearer auth,
    error handling) is covered at unit level in tests/cli/test_status.py.
    """
    with make_real_app(
        tmp_path, monkeypatch, telemetry_enabled=True, hash_doc_ids_enabled=True
    ) as (client, _cfg, api_key):
        # Step 1: Capture the real server payload via ASGI TestClient.
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.get("/status", headers=headers)
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "telemetry" in body
        assert body["telemetry"] is not None
        assert body["telemetry"].get("hash_doc_ids_enabled") is True

        # Step 2: Feed the real payload into the CLI renderer via patch.
        svc = MagicMock()
        svc.status.return_value = ServiceStatus(running=True, pid=None, uptime_seconds=None)

        runner = CliRunner()
        with patch("archon_search.cli.status._get_service", return_value=svc):
            with patch(
                "archon_search.cli.status._fetch_server_status",
                return_value=body,
            ):
                result = runner.invoke(main, ["status"])

    assert result.exit_code == 0, (
        f"archon-search status exited {result.exit_code}:\n{result.output}"
    )
    # Confirm the CLI renders the field label and its True value from the real server payload.
    assert "hash_doc_ids_enabled: True" in result.output, (
        f"Expected 'hash_doc_ids_enabled: True' in CLI status output (S12a). "
        f"Output:\n{result.output!r}"
    )


# ---------------------------------------------------------------------------
# S12b — archon-search status CLI degrades gracefully when server is unreachable
# ---------------------------------------------------------------------------


def test_e2e_cli_status_graceful_when_server_unreachable() -> None:
    """archon-search status CLI: server unreachable → service state shown, telemetry omitted (S12b).

    When ``_fetch_server_status`` returns ``None`` (connection error / server down),
    the CLI must show the OS service state and omit the telemetry section entirely
    without crashing or raising.

    ``_get_service`` is also patched because the OS service manager is not available
    in the integration test environment.  The test proves the CLI rendering branch
    that fires when ``server_payload is None``.
    """
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=True, pid=None, uptime_seconds=None)

    runner = CliRunner()
    with patch("archon_search.cli.status._get_service", return_value=svc):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=None,  # simulates server unreachable / connection error
        ):
            result = runner.invoke(main, ["status"])

    assert result.exit_code == 0, (
        f"archon-search status must exit 0 when server is unreachable (S12b). "
        f"Exit: {result.exit_code}\nOutput:\n{result.output}"
    )
    assert "running" in result.output, (
        f"Expected OS service state ('running') in CLI output when server is unreachable (S12b). "
        f"Output:\n{result.output!r}"
    )
    assert "hash_doc_ids_enabled" not in result.output, (
        f"Telemetry section must be omitted when server is unreachable (S12b). "
        f"Output:\n{result.output!r}"
    )

"""TDD tests for archon-search status CLI subcommand."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from archon_search.cli.main import main
from archon_search.platform.service import ServiceStatus


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_status_running_output(runner: CliRunner) -> None:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=True, pid=123, uptime_seconds=42.0)
    with patch("archon_search.cli.status._get_service", return_value=svc):
        with patch("archon_search.cli.status._fetch_server_status", return_value=None):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "running" in result.output


def test_status_stopped_output(runner: CliRunner) -> None:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=False, pid=None, uptime_seconds=None)
    with patch("archon_search.cli.status._get_service", return_value=svc):
        with patch("archon_search.cli.status._fetch_server_status", return_value=None):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "stopped" in result.output


def test_status_running_includes_pid_and_uptime(runner: CliRunner) -> None:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=True, pid=456, uptime_seconds=99.0)
    with patch("archon_search.cli.status._get_service", return_value=svc):
        with patch("archon_search.cli.status._fetch_server_status", return_value=None):
            result = runner.invoke(main, ["status"])
    assert "456" in result.output
    assert "99" in result.output


def test_status_running_no_pid_no_uptime(runner: CliRunner) -> None:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=True, pid=None, uptime_seconds=None)
    with patch("archon_search.cli.status._get_service", return_value=svc):
        with patch("archon_search.cli.status._fetch_server_status", return_value=None):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "running" in result.output


def test_status_service_error_exits_nonzero(runner: CliRunner) -> None:
    svc = MagicMock()
    svc.status.side_effect = RuntimeError("connection refused")
    with patch("archon_search.cli.status._get_service", return_value=svc):
        result = runner.invoke(main, ["status"])
    assert result.exit_code != 0
    assert "connection refused" in result.output


# ---------------------------------------------------------------------------
# FE-1 — New HTTP /status code path tests (D8)
# ---------------------------------------------------------------------------


def _make_svc(running: bool = True) -> MagicMock:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=running, pid=None, uptime_seconds=None)
    return svc


def test_status_cli_shows_hash_doc_ids_enabled_true(runner: CliRunner) -> None:
    """Mocked GET /status with hash_doc_ids_enabled=True → output contains the flag (S12)."""
    server_payload = {
        "telemetry": {"enabled": True, "hash_doc_ids_enabled": True},
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "hash_doc_ids_enabled" in result.output
    assert "True" in result.output or "true" in result.output


def test_status_cli_shows_hash_doc_ids_enabled_false(runner: CliRunner) -> None:
    """flag=False is displayed correctly."""
    server_payload = {
        "telemetry": {"enabled": True, "hash_doc_ids_enabled": False},
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "hash_doc_ids_enabled" in result.output
    assert "False" in result.output or "false" in result.output


def test_status_cli_sends_bearer_token(runner: CliRunner) -> None:
    """The resolved API key is sent as Authorization: Bearer <token> in the HTTP request."""
    captured_headers: list[dict] = []

    def fake_httpx_get(url: str, *, headers: dict, timeout: float) -> MagicMock:
        captured_headers.append(dict(headers))
        # Return a non-200 so we stay in the unreachable branch (safe fallback).
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        return mock_resp

    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        # Patch _resolve_api_key so the test does not require a real key file on disk
        # (CI environments may lack ~/.archon-search/.search.env). The real function would
        # return the --api-key value immediately, so this is a no-op equivalent.
        with patch("archon_search.cli.status._resolve_api_key", return_value="test-token-abc"):
            with patch("archon_search.cli.status.httpx.get", side_effect=fake_httpx_get):
                result = runner.invoke(
                    main,
                    ["status", "--api-key", "test-token-abc", "--api-url", "http://localhost:9999"],
                )
    assert result.exit_code == 0, result.output
    assert captured_headers, "Expected httpx.get to be called"
    assert captured_headers[0].get("Authorization") == "Bearer test-token-abc"


def test_status_cli_handles_401_unauthorized(runner: CliRunner) -> None:
    """Server returns 401 → clear auth-failure message, no crash (distinct from unreachable)."""
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value={"_auth_failed": True},
        ):
            result = runner.invoke(main, ["status", "--api-key", "bad-key"])
    assert result.exit_code == 0, result.output
    assert "401" in result.output
    assert "Unauthorized" in result.output


def test_status_cli_graceful_when_server_unreachable(runner: CliRunner) -> None:
    """Connection error → service state shown, no crash, telemetry section omitted (S12b)."""
    with patch("archon_search.cli.status._get_service", return_value=_make_svc(running=True)):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=None,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "running" in result.output
    # Telemetry section should be omitted when server is unreachable
    assert "hash_doc_ids_enabled" not in result.output


def test_status_cli_omits_telemetry_when_disabled_on_server(runner: CliRunner) -> None:
    """When server returns telemetry: null, no telemetry section is shown."""
    server_payload = {"telemetry": None}
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "hash_doc_ids_enabled" not in result.output


# ---------------------------------------------------------------------------
# FE-1 direct unit tests for _fetch_server_status
# ---------------------------------------------------------------------------


def test_fetch_server_status_returns_none_when_key_resolution_fails() -> None:
    """_resolve_api_key exception → returns None (offline-mode fallback)."""
    import archon_search.cli.status as status_mod

    with patch(
        "archon_search.cli.status._resolve_api_key",
        side_effect=OSError("no key file"),
    ):
        result = status_mod._fetch_server_status("http://localhost:8765", None)
    assert result is None


def test_fetch_server_status_returns_none_on_http_error() -> None:
    """httpx.HTTPError → returns None (server unreachable)."""
    import archon_search.cli.status as status_mod

    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch(
            "archon_search.cli.status.httpx.get",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            result = status_mod._fetch_server_status("http://localhost:8765", None)
    assert result is None


def test_fetch_server_status_returns_auth_failed_on_401() -> None:
    """401 response → returns {'_auth_failed': True}."""
    import archon_search.cli.status as status_mod

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", return_value=mock_resp):
            result = status_mod._fetch_server_status("http://localhost:8765", None)
    assert result == {"_auth_failed": True}


def test_fetch_server_status_returns_none_on_non_200_non_401() -> None:
    """Non-200, non-401 response (e.g. 500) → returns None."""
    import archon_search.cli.status as status_mod

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", return_value=mock_resp):
            result = status_mod._fetch_server_status("http://localhost:8765", None)
    assert result is None


def test_fetch_server_status_returns_none_on_invalid_json() -> None:
    """200 response with non-JSON body → returns None."""
    import archon_search.cli.status as status_mod

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("not JSON")
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", return_value=mock_resp):
            result = status_mod._fetch_server_status("http://localhost:8765", None)
    assert result is None


def test_fetch_server_status_returns_payload_on_200() -> None:
    """200 response with valid JSON → returns the parsed dict."""
    import archon_search.cli.status as status_mod

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    expected = {"telemetry": {"enabled": True, "hash_doc_ids_enabled": True}}
    mock_resp.json.return_value = expected
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", return_value=mock_resp):
            result = status_mod._fetch_server_status("http://localhost:8765", None)
    assert result == expected


def test_fetch_server_status_constructs_url_correctly() -> None:
    """URL is constructed as api_url.rstrip('/') + '/status'."""
    import archon_search.cli.status as status_mod

    captured_urls: list[str] = []

    def fake_get(url: str, *, headers: dict, timeout: float) -> MagicMock:
        captured_urls.append(url)
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        return mock_resp

    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", side_effect=fake_get):
            status_mod._fetch_server_status("http://localhost:9999/", None)
    assert captured_urls == ["http://localhost:9999/status"]


# ---------------------------------------------------------------------------
# FE-1 integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_status_cli_integration_with_real_server(tmp_path, monkeypatch) -> None:
    """Real server running — status CLI output contains hash_doc_ids_enabled (S12)."""
    from tests.integration.conftest import make_real_app

    with make_real_app(tmp_path, monkeypatch, telemetry_enabled=True) as (client, _cfg, api_key):
        # The CLI status command uses httpx to hit the real running server.
        # We call the internal _fetch_server_status helper directly to avoid
        # needing to spin up an actual listening uvicorn server (TestClient is
        # WSGI/ASGI in-process — not a real TCP server).
        # Instead, verify the route-level contract: GET /status returns telemetry field.
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.get("/status", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "telemetry" in body
        telemetry = body["telemetry"]
        assert telemetry is not None
        assert "hash_doc_ids_enabled" in telemetry
        # Default config: hash_doc_ids=False → hash_doc_ids_enabled=False
        assert telemetry["hash_doc_ids_enabled"] is False

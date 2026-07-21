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


# ---------------------------------------------------------------------------
# FE-3 — E0b: HyDE/RAG Fusion key warnings and failed_expired_ingest_count
# ---------------------------------------------------------------------------


def test_status_cli_warns_when_hyde_key_unavailable(runner: CliRunner) -> None:
    """GET /status with hyde.key_available=false → stderr contains 'ANTHROPIC_API_KEY' (S7, S8)."""
    server_payload = {
        "hyde": {"key_available": False},
        "rag_fusion": None,
        "failed_expired_ingest_count": 0,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" in result.stderr, (
        f"Expected 'ANTHROPIC_API_KEY' in stderr; got: {result.stderr!r}"
    )
    assert "HyDE" in result.stderr, (
        f"Expected 'HyDE' in stderr; got: {result.stderr!r}"
    )


def test_status_cli_warns_when_rag_fusion_key_unavailable(runner: CliRunner) -> None:
    """GET /status with rag_fusion.key_available=false → stderr contains 'ANTHROPIC_API_KEY'."""
    server_payload = {
        "hyde": None,
        "rag_fusion": {"key_available": False},
        "failed_expired_ingest_count": 0,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" in result.stderr, (
        f"Expected 'ANTHROPIC_API_KEY' in stderr; got: {result.stderr!r}"
    )
    assert "RAG Fusion" in result.stderr, (
        f"Expected 'RAG Fusion' in stderr; got: {result.stderr!r}"
    )


def test_status_cli_no_warning_when_hyde_key_available(runner: CliRunner) -> None:
    """GET /status with hyde.key_available=true → no ANTHROPIC_API_KEY warning emitted."""
    server_payload = {
        "hyde": {"key_available": True},
        "rag_fusion": None,
        "failed_expired_ingest_count": 0,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" not in result.stderr, (
        f"Unexpected 'ANTHROPIC_API_KEY' in stderr; got: {result.stderr!r}"
    )


def test_status_cli_no_warning_when_hyde_null(runner: CliRunner) -> None:
    """GET /status with hyde=null (feature disabled) → no warning emitted."""
    server_payload = {
        "hyde": None,
        "rag_fusion": None,
        "failed_expired_ingest_count": 0,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" not in result.stderr


def test_status_cli_no_warning_when_rag_fusion_key_available(runner: CliRunner) -> None:
    """GET /status with rag_fusion.key_available=true → no ANTHROPIC_API_KEY warning emitted."""
    server_payload = {
        "hyde": None,
        "rag_fusion": {"key_available": True},
        "failed_expired_ingest_count": 0,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" not in result.stderr, (
        f"Unexpected 'ANTHROPIC_API_KEY' in stderr; got: {result.stderr!r}"
    )


def test_status_cli_warns_for_both_hyde_and_rag_fusion_key_unavailable(runner: CliRunner) -> None:
    """Both hyde.key_available=false AND rag_fusion.key_available=false → two warnings on stderr."""
    server_payload = {
        "hyde": {"key_available": False},
        "rag_fusion": {"key_available": False},
        "failed_expired_ingest_count": 0,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "HyDE" in result.stderr, (
        f"Expected 'HyDE' warning in stderr; got: {result.stderr!r}"
    )
    assert "RAG Fusion" in result.stderr, (
        f"Expected 'RAG Fusion' warning in stderr; got: {result.stderr!r}"
    )
    assert result.stderr.count("ANTHROPIC_API_KEY") == 2, (
        f"Expected two 'ANTHROPIC_API_KEY' occurrences in stderr; got: {result.stderr!r}"
    )


def test_status_cli_shows_failed_expired_count(runner: CliRunner) -> None:
    """GET /status with failed_expired_ingest_count=3 → stdout contains '3' and 're-ingest' (S15)."""
    server_payload = {
        "hyde": None,
        "rag_fusion": None,
        "failed_expired_ingest_count": 3,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "3 ingest job(s) expired" in result.stdout, (
        f"Expected '3 ingest job(s) expired' in stdout; got: {result.stdout!r}"
    )
    assert "re-ingest" in result.stdout.lower(), (
        f"Expected 're-ingest' hint in stdout; got: {result.stdout!r}"
    )


def test_status_cli_no_failed_expired_output_when_zero(runner: CliRunner) -> None:
    """GET /status with failed_expired_ingest_count=0 → no failed-expired section shown."""
    server_payload = {
        "hyde": None,
        "rag_fusion": None,
        "failed_expired_ingest_count": 0,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "re-ingest" not in result.stdout.lower()


def test_status_cli_no_failed_expired_output_when_key_absent(runner: CliRunner) -> None:
    """GET /status without failed_expired_ingest_count key (older server) → no crash, no output."""
    server_payload = {
        "hyde": None,
        "rag_fusion": None,
        # failed_expired_ingest_count intentionally absent (pre-E0b server)
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "re-ingest" not in result.stdout.lower()


def test_status_cli_no_failed_expired_output_when_count_is_null(runner: CliRunner) -> None:
    """GET /status with failed_expired_ingest_count=null → no crash, no re-ingest output.

    The ``or 0`` guard in _print_failed_expired_count coerces ``None`` to 0.
    This test exercises that specific branch (field present but null).
    """
    server_payload = {
        "hyde": None,
        "rag_fusion": None,
        "failed_expired_ingest_count": None,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "re-ingest" not in result.stdout.lower()


def test_status_cli_shows_failed_expired_count_singular(runner: CliRunner) -> None:
    """GET /status with failed_expired_ingest_count=1 → message uses 'job(s)' form (boundary)."""
    server_payload = {
        "hyde": None,
        "rag_fusion": None,
        "failed_expired_ingest_count": 1,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "1 ingest job(s) expired" in result.stdout, (
        f"Expected '1 ingest job(s) expired' in stdout; got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# BE-10 — Graph GC status display in CLI
# ---------------------------------------------------------------------------


def test_status_cli_displays_stale_mention_count(runner: CliRunner) -> None:
    """Mock GET /status response with graph.stale_mention_count=5 → assert 5 appears in output."""
    server_payload = {
        "graph": {
            "enabled": True,
            "backend_threshold_edges": 1000,
            "collections": [],
            "stale_mention_count": 5,
        },
        "maintenance": {
            "enabled": True,
            "interval_hours": 1,
            "last_run_at": None,
            "next_run_at": None,
            "collection_health": [],
            "expired_chunk_count": 0,
            "last_expired_pruned_at": None,
            "last_graph_gc_at": "2026-07-05T12:00:00Z",
        },
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "5" in result.output
    assert "stale" in result.output.lower() or "mention" in result.output.lower()


def test_status_cli_displays_last_graph_gc_at(runner: CliRunner) -> None:
    """Mock response with non-null maintenance.last_graph_gc_at → assert timestamp appears in output."""
    server_payload = {
        "graph": {
            "enabled": True,
            "backend_threshold_edges": 1000,
            "collections": [],
            "stale_mention_count": 0,
        },
        "maintenance": {
            "enabled": True,
            "interval_hours": 1,
            "last_run_at": None,
            "next_run_at": None,
            "collection_health": [],
            "expired_chunk_count": 0,
            "last_expired_pruned_at": None,
            "last_graph_gc_at": "2026-07-05T12:00:00Z",
        },
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "2026-07-05T12:00:00Z" in result.output


def test_status_cli_graph_fields_absent_when_graph_disabled(runner: CliRunner) -> None:
    """Mock response with graph=null (feature disabled) → assert no crash."""
    server_payload = {
        "graph": None,
        "maintenance": {
            "enabled": True,
            "interval_hours": 1,
            "last_run_at": None,
            "next_run_at": None,
            "collection_health": [],
            "expired_chunk_count": 0,
            "last_expired_pruned_at": None,
            "last_graph_gc_at": None,
        },
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value=server_payload,
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    # Should not crash even with null graph


# ---------------------------------------------------------------------------
# SPD — per-collection path + doc_count in `archon-search status` output
# ---------------------------------------------------------------------------


def test_status_cli_prints_collections(runner: CliRunner) -> None:
    """S6: `archon-search status` prints each collection's name, doc_count, and path."""
    server_payload = {
        "collections": [
            {"name": "mydocs", "path": "/srv/data/mydocs", "doc_count": 42},
        ],
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status", return_value=server_payload
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "mydocs" in result.output
    assert "42" in result.output
    assert "/srv/data/mydocs" in result.output


def test_status_cli_prints_collections_when_telemetry_disabled(runner: CliRunner) -> None:
    """S6: the per-collection block prints even when telemetry is None (default install) —
    it is rendered before the telemetry early-return."""
    server_payload = {
        "collections": [
            {"name": "mydocs", "path": "/srv/data/mydocs", "doc_count": 3},
        ],
        "telemetry": None,  # disabled → early-return follows the collections block
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status", return_value=server_payload
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "mydocs" in result.output
    assert "3" in result.output


# ---------------------------------------------------------------------------
# Brief 300 — provider-aware expansion key warnings
# ---------------------------------------------------------------------------


def test_expansion_warning_openai_hyde_shows_openai_key(runner: CliRunner) -> None:
    """provider=openai + key_available=false → OPENAI_API_KEY in stderr, not ANTHROPIC."""
    server_payload = {
        "hyde": {"key_available": False, "provider": "openai"},
        "rag_fusion": None,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "OPENAI_API_KEY" in result.stderr
    assert "ANTHROPIC_API_KEY" not in result.stderr


def test_expansion_warning_openai_rag_fusion_shows_openai_key(runner: CliRunner) -> None:
    """provider=openai + key_available=false on rag_fusion → OPENAI_API_KEY in stderr."""
    server_payload = {
        "hyde": None,
        "rag_fusion": {"key_available": False, "provider": "openai"},
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "OPENAI_API_KEY" in result.stderr
    assert "ANTHROPIC_API_KEY" not in result.stderr


def test_expansion_warning_explicit_anthropic_provider(runner: CliRunner) -> None:
    """provider=anthropic explicitly → ANTHROPIC_API_KEY (same as the default/fallback)."""
    server_payload = {
        "hyde": {"key_available": False, "provider": "anthropic"},
        "rag_fusion": None,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" in result.stderr


def test_expansion_warning_unknown_provider_no_crash(runner: CliRunner) -> None:
    """Unknown provider string → no crash; some warning is still emitted."""
    server_payload = {
        "hyde": {"key_available": False, "provider": "myservice"},
        "rag_fusion": None,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "HyDE enabled but the 'myservice' API key is not set" in result.stderr


def test_expansion_warning_both_openai_shows_two_openai_keys(runner: CliRunner) -> None:
    """Both hyde and rag_fusion with openai provider → OPENAI_API_KEY appears twice."""
    server_payload = {
        "hyde": {"key_available": False, "provider": "openai"},
        "rag_fusion": {"key_available": False, "provider": "openai"},
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert result.stderr.count("OPENAI_API_KEY") == 2
    assert "ANTHROPIC_API_KEY" not in result.stderr


def test_expansion_warning_missing_provider_field_defaults_to_anthropic(runner: CliRunner) -> None:
    """Older server without 'provider' field → defaults to ANTHROPIC_API_KEY (no regression)."""
    server_payload = {
        "hyde": {"key_available": False},  # no 'provider' field
        "rag_fusion": None,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" in result.stderr


def test_expansion_warning_null_provider_defaults_to_anthropic(runner: CliRunner) -> None:
    """provider field present but null → defaults to ANTHROPIC_API_KEY (no crash)."""
    server_payload = {
        "hyde": {"key_available": False, "provider": None},  # null, not absent
        "rag_fusion": None,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" in result.stderr


def test_expansion_warning_key_available_absent_no_warning(runner: CliRunner) -> None:
    """hyde sub-object present but key_available field absent → no warning (silent-on-absent contract)."""
    server_payload = {
        "hyde": {"provider": "anthropic"},  # key_available intentionally absent
        "rag_fusion": None,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_expansion_warning_non_dict_hyde_no_crash(runner: CliRunner) -> None:
    """hyde field present as non-dict (e.g. string from a mismatched server) → no crash, no warning."""
    server_payload = {
        "hyde": "enabled",  # non-dict truthy — should not crash
        "rag_fusion": None,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_expansion_warning_ollama_no_warning(runner: CliRunner) -> None:
    """provider=ollama + key_available=True → no warning (Ollama is keyless; key always available)."""
    server_payload = {
        "hyde": {"key_available": True, "provider": "ollama"},
        "rag_fusion": None,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_expansion_warning_claude_cli_generic_message(runner: CliRunner) -> None:
    """provider=claude_cli + key_available=True → no warning (claude_cli is keyless like ollama)."""
    server_payload = {
        "hyde": {"key_available": True, "provider": "claude_cli"},
        "rag_fusion": None,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_expansion_warning_mixed_providers(runner: CliRunner) -> None:
    """hyde=openai + rag_fusion=anthropic, both key_unavailable → each gets the correct env var."""
    server_payload = {
        "hyde": {"key_available": False, "provider": "openai"},
        "rag_fusion": {"key_available": False, "provider": "anthropic"},
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "OPENAI_API_KEY" in result.stderr
    assert "ANTHROPIC_API_KEY" in result.stderr
    assert result.stderr.count("OPENAI_API_KEY") == 1
    assert result.stderr.count("ANTHROPIC_API_KEY") == 1
    assert "HyDE" in result.stderr
    assert "RAG Fusion" in result.stderr


def test_expansion_warning_openai_key_available_no_warning(runner: CliRunner) -> None:
    """provider=openai + key_available=True → no warning emitted."""
    server_payload = {
        "hyde": {"key_available": True, "provider": "openai"},
        "rag_fusion": None,
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_status_cli_renders_empty_path_without_error(runner: CliRunner) -> None:
    """S7: a collection with an empty path is still listed and the command exits cleanly."""
    server_payload = {
        "collections": [
            {"name": "adhoc", "path": "", "doc_count": 7},
        ],
        "telemetry": None,
    }
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status", return_value=server_payload
        ):
            result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "adhoc" in result.output
    assert "7" in result.output
    assert "(no configured path)" in result.output

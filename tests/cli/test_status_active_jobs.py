"""TDD tests for brief-310: active job queue summary in `archon-search status`."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

import archon_search.cli.status as status_mod
from archon_search.cli.main import main
from archon_search.platform.service import ServiceStatus


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _make_svc(running: bool = True) -> MagicMock:
    svc = MagicMock()
    svc.status.return_value = ServiceStatus(running=running, pid=None, uptime_seconds=None)
    return svc


def _mock_jobs_resp(items: list[dict], total: int | None = None) -> MagicMock:
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"items": items, "total": total if total is not None else len(items)}
    return mock


# ---------------------------------------------------------------------------
# Unit tests for _fetch_active_job_counts
# ---------------------------------------------------------------------------


def test_fetch_active_job_counts_returns_none_on_key_resolution_failure() -> None:
    with patch("archon_search.cli.status._resolve_api_key", side_effect=OSError("no key")):
        result = status_mod._fetch_active_job_counts("http://localhost:8765", None)
    assert result is None


def test_fetch_active_job_counts_returns_none_on_http_error() -> None:
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch(
            "archon_search.cli.status.httpx.get",
            side_effect=httpx.ConnectError("refused"),
        ):
            result = status_mod._fetch_active_job_counts("http://localhost:8765", None)
    assert result is None


def test_fetch_active_job_counts_returns_none_on_non_200() -> None:
    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {"total": 0}
    err_resp = MagicMock()
    err_resp.status_code = 503
    # First call (RUNNING) returns 200, second (PENDING) returns 503 → None
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", side_effect=[ok_resp, err_resp]):
            result = status_mod._fetch_active_job_counts("http://localhost:8765", None)
    assert result is None


def test_fetch_active_job_counts_returns_none_on_non_200_first_call() -> None:
    err_resp = MagicMock()
    err_resp.status_code = 503
    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {"total": 0}
    # First call (RUNNING) returns 503 → None immediately
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", side_effect=[err_resp, ok_resp]):
            result = status_mod._fetch_active_job_counts("http://localhost:8765", None)
    assert result is None


def test_fetch_active_job_counts_returns_none_on_invalid_json() -> None:
    r_resp = MagicMock()
    r_resp.status_code = 200
    r_resp.json.side_effect = ValueError("not JSON")
    p_resp = MagicMock()
    p_resp.status_code = 200
    p_resp.json.return_value = {"total": 0}
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", side_effect=[r_resp, p_resp]):
            result = status_mod._fetch_active_job_counts("http://localhost:8765", None)
    assert result is None


def test_fetch_active_job_counts_returns_none_on_attribute_error() -> None:
    r_resp = MagicMock()
    r_resp.status_code = 200
    r_resp.json.return_value = None  # None.get() → AttributeError
    p_resp = MagicMock()
    p_resp.status_code = 200
    p_resp.json.return_value = {"total": 0}
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", side_effect=[r_resp, p_resp]):
            result = status_mod._fetch_active_job_counts("http://localhost:8765", None)
    assert result is None


def test_fetch_active_job_counts_returns_zero_zero_when_no_active_jobs() -> None:
    r_resp = _mock_jobs_resp([], total=0)
    p_resp = _mock_jobs_resp([], total=0)
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", side_effect=[r_resp, p_resp]):
            result = status_mod._fetch_active_job_counts("http://localhost:8765", None)
    assert result == (0, 0)


def test_fetch_active_job_counts_counts_running_and_pending_separately() -> None:
    r_resp = _mock_jobs_resp([], total=2)
    p_resp = _mock_jobs_resp([], total=1)
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", side_effect=[r_resp, p_resp]):
            result = status_mod._fetch_active_job_counts("http://localhost:8765", None)
    assert result == (2, 1)


def test_fetch_active_job_counts_sends_correct_url_and_params() -> None:
    captured: list[dict] = []

    def fake_get(url: str, *, params, headers, timeout):  # type: ignore[no-untyped-def]
        captured.append({"url": url, "params": list(params)})
        return _mock_jobs_resp([], total=0)

    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", side_effect=fake_get):
            status_mod._fetch_active_job_counts("http://localhost:9999/", None)

    assert len(captured) == 2
    assert captured[0]["url"] == "http://localhost:9999/jobs"
    assert captured[1]["url"] == "http://localhost:9999/jobs"
    assert ("status", "RUNNING") in captured[0]["params"]
    assert ("limit", 1) in captured[0]["params"]
    assert ("status", "PENDING") in captured[1]["params"]
    assert ("limit", 1) in captured[1]["params"]


def test_fetch_active_job_counts_sends_bearer_token() -> None:
    captured_headers: list[dict] = []

    def fake_get(url: str, *, params, headers, timeout):  # type: ignore[no-untyped-def]
        captured_headers.append(dict(headers))
        return _mock_jobs_resp([], total=0)

    with patch("archon_search.cli.status._resolve_api_key", return_value="my-secret"):
        with patch("archon_search.cli.status.httpx.get", side_effect=fake_get):
            status_mod._fetch_active_job_counts("http://localhost:8765", None)

    assert len(captured_headers) == 2
    assert captured_headers[0]["Authorization"] == "Bearer my-secret"
    assert captured_headers[1]["Authorization"] == "Bearer my-secret"


def test_fetch_active_job_counts_uses_total_not_items_count() -> None:
    """total field is used even when items list is empty (e.g. limit=1 returns 1 item but total=150)."""
    r_resp = _mock_jobs_resp([], total=150)
    p_resp = _mock_jobs_resp([], total=75)
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", side_effect=[r_resp, p_resp]):
            result = status_mod._fetch_active_job_counts("http://localhost:8765", None)
    assert result == (150, 75)


def test_fetch_active_job_counts_total_above_page_cap_returns_accurate_count() -> None:
    """total > 200 (server page cap) returns the real total, not the page size."""
    r_resp = _mock_jobs_resp([], total=500)
    p_resp = _mock_jobs_resp([], total=300)
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", side_effect=[r_resp, p_resp]):
            result = status_mod._fetch_active_job_counts("http://localhost:8765", None)
    assert result == (500, 300)


def test_fetch_active_job_counts_returns_none_on_non_int_total() -> None:
    """A non-numeric total (e.g. list) must not crash — returns None instead."""
    r_resp = MagicMock()
    r_resp.status_code = 200
    r_resp.json.return_value = {"total": [1, 2]}  # list total — int([1,2]) → TypeError
    p_resp = _mock_jobs_resp([], total=0)
    with patch("archon_search.cli.status._resolve_api_key", return_value="key"):
        with patch("archon_search.cli.status.httpx.get", side_effect=[r_resp, p_resp]):
            result = status_mod._fetch_active_job_counts("http://localhost:8765", None)
    assert result is None


# ---------------------------------------------------------------------------
# Unit tests for _print_active_jobs
# ---------------------------------------------------------------------------


def test_print_active_jobs_no_output_when_zero(runner: CliRunner) -> None:
    with patch("archon_search.cli.status._fetch_active_job_counts", return_value=(0, 0)):
        with runner.isolated_filesystem():
            from click.testing import CliRunner as _CR
            from click import command, pass_context

            @command()
            def _cmd():  # type: ignore[no-untyped-def]
                status_mod._print_active_jobs("http://localhost:8765", None)

            result = _CR().invoke(_cmd)
    assert "Jobs:" not in result.output


def test_print_active_jobs_no_output_when_fetch_returns_none(runner: CliRunner) -> None:
    with patch("archon_search.cli.status._fetch_active_job_counts", return_value=None):
        from click import command
        from click.testing import CliRunner as _CR

        @command()
        def _cmd():  # type: ignore[no-untyped-def]
            status_mod._print_active_jobs("http://localhost:8765", None)

        result = _CR().invoke(_cmd)
    assert "Jobs:" not in result.output


def test_print_active_jobs_shows_running_and_pending(runner: CliRunner) -> None:
    with patch("archon_search.cli.status._fetch_active_job_counts", return_value=(3, 7)):
        from click import command
        from click.testing import CliRunner as _CR

        @command()
        def _cmd():  # type: ignore[no-untyped-def]
            status_mod._print_active_jobs("http://localhost:8765", None)

        result = _CR().invoke(_cmd)
    assert "Jobs: 3 running, 7 queued" in result.output


def test_print_active_jobs_caps_display_at_50_plus(runner: CliRunner) -> None:
    with patch("archon_search.cli.status._fetch_active_job_counts", return_value=(51, 55)):
        from click import command
        from click.testing import CliRunner as _CR

        @command()
        def _cmd():  # type: ignore[no-untyped-def]
            status_mod._print_active_jobs("http://localhost:8765", None)

        result = _CR().invoke(_cmd)
    assert "50+" in result.output


def test_print_active_jobs_includes_jobs_list_hint() -> None:
    with patch("archon_search.cli.status._fetch_active_job_counts", return_value=(1, 0)):
        from click import command
        from click.testing import CliRunner as _CR

        @command()
        def _cmd():  # type: ignore[no-untyped-def]
            status_mod._print_active_jobs("http://localhost:8765", None)

        result = _CR().invoke(_cmd)
    assert "archon-search jobs list" in result.output


# ---------------------------------------------------------------------------
# CLI integration tests — `archon-search status` output
# ---------------------------------------------------------------------------


def test_status_cli_shows_jobs_line_when_active_jobs_exist(runner: CliRunner) -> None:
    """Active jobs: status command prints Jobs summary line."""
    server_payload: dict = {"telemetry": None}
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            with patch(
                "archon_search.cli.status._fetch_active_job_counts", return_value=(2, 5)
            ):
                result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "Jobs: 2 running, 5 queued" in result.output
    assert "archon-search jobs list" in result.output


def test_status_cli_omits_jobs_line_when_no_active_jobs(runner: CliRunner) -> None:
    """Zero active jobs: jobs line is absent (no clutter in idle case)."""
    server_payload: dict = {"telemetry": None}
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            with patch(
                "archon_search.cli.status._fetch_active_job_counts", return_value=(0, 0)
            ):
                result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "Jobs:" not in result.output


def test_status_cli_omits_jobs_line_when_server_unreachable(runner: CliRunner) -> None:
    """Server unreachable (server_payload=None): no jobs line, no crash."""
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=None):
            # _fetch_active_job_counts must NOT be called when server_payload is None
            with patch(
                "archon_search.cli.status._fetch_active_job_counts"
            ) as mock_fetch:
                result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "Jobs:" not in result.output
    mock_fetch.assert_not_called()


def test_status_cli_omits_jobs_line_when_auth_failed(runner: CliRunner) -> None:
    """401 response: auth failure message shown, jobs line absent."""
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch(
            "archon_search.cli.status._fetch_server_status",
            return_value={"_auth_failed": True},
        ):
            with patch(
                "archon_search.cli.status._fetch_active_job_counts"
            ) as mock_fetch:
                result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "Jobs:" not in result.output
    mock_fetch.assert_not_called()


def test_status_cli_jobs_line_only_running(runner: CliRunner) -> None:
    """Only running jobs (no pending): 'N running, 0 queued'."""
    server_payload: dict = {"telemetry": None}
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            with patch(
                "archon_search.cli.status._fetch_active_job_counts", return_value=(3, 0)
            ):
                result = runner.invoke(main, ["status"])
    assert "Jobs: 3 running, 0 queued" in result.output


def test_status_cli_jobs_line_only_pending(runner: CliRunner) -> None:
    """Only pending jobs (nothing running): '0 running, M queued'."""
    server_payload: dict = {"telemetry": None}
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            with patch(
                "archon_search.cli.status._fetch_active_job_counts", return_value=(0, 4)
            ):
                result = runner.invoke(main, ["status"])
    assert "Jobs: 0 running, 4 queued" in result.output


def test_status_cli_jobs_line_caps_large_counts(runner: CliRunner) -> None:
    """Counts > 50 are capped at '50+' in the display."""
    server_payload: dict = {"telemetry": None}
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            with patch(
                "archon_search.cli.status._fetch_active_job_counts", return_value=(100, 200)
            ):
                result = runner.invoke(main, ["status"])
    assert "Jobs: 50+ running, 50+ queued" in result.output


def test_status_cli_jobs_line_exactly_50_not_capped(runner: CliRunner) -> None:
    """Counts of exactly 50 are not capped (cap is >50)."""
    server_payload: dict = {"telemetry": None}
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            with patch(
                "archon_search.cli.status._fetch_active_job_counts", return_value=(50, 50)
            ):
                result = runner.invoke(main, ["status"])
    assert "Jobs: 50 running, 50 queued" in result.output
    assert "50+" not in result.output


def test_status_cli_jobs_error_is_silent(runner: CliRunner) -> None:
    """If _fetch_active_job_counts errors (returns None), no crash and no Jobs: line."""
    server_payload: dict = {"telemetry": None}
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            with patch(
                "archon_search.cli.status._fetch_active_job_counts", return_value=None
            ):
                result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "Jobs:" not in result.output


def test_status_cli_jobs_line_caps_only_running_not_pending(runner: CliRunner) -> None:
    """Asymmetric counts: only running exceeds cap — pending shows exact number."""
    server_payload: dict = {"telemetry": None}
    with patch("archon_search.cli.status._get_service", return_value=_make_svc()):
        with patch("archon_search.cli.status._fetch_server_status", return_value=server_payload):
            with patch(
                "archon_search.cli.status._fetch_active_job_counts", return_value=(51, 3)
            ):
                result = runner.invoke(main, ["status"])
    assert "Jobs: 50+ running, 3 queued" in result.output

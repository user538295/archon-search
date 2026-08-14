"""Unit tests for the shared ``_poll_job`` helper in ``archon_search.cli._helpers``.

Covers:
- test_poll_job_exits_0_on_done: DONE status → returns job dict without raising
- test_poll_job_exits_1_on_failed_and_cancelled: FAILED/CANCELLED/FAILED_EXPIRED → exit 1
- test_poll_job_failed_prints_error_to_stderr: FAILED → error text echoed to stderr
- test_poll_job_prints_progress_each_interval: progress field printed each poll when present
- test_poll_job_keyboard_interrupt_exits_0: KeyboardInterrupt → prints message, returns {} (falsy)
- test_poll_migration_job_does_not_print_complete_on_interrupt: migration wrapper guards KBI
- test_poll_job_connect_error_exits_1: httpx.ConnectError → exit 1
- test_poll_job_non200_exits_1: non-200 response → exit 1
- test_poll_job_missing_status_exits_1: 200 with no status field → exit 1 (not infinite hang)
- test_poll_job_connect_error_server_alive_shows_starting_up: connect failure while the service
  process IS alive (warmup, port not bound yet) → "starting up" hint, not "not running"
- test_poll_job_connect_error_server_dead_shows_not_running: connect failure while the service
  process is NOT alive → the existing "not running" message
- test_poll_job_connect_error_unsupported_platform_falls_back_to_not_running: _get_service()
  raising (unsupported platform) → "not running" fallback, exit 1, no traceback
- test_poll_job_connect_error_status_probe_failure_falls_back_to_not_running: status() raising
  (launchctl/systemctl unavailable) → "not running" fallback, exit 1
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from archon_search.cli._helpers import _poll_job
from archon_search.platform.service import ServiceStatus


def _job_response(status: str, progress: dict | None = None, error: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    body: dict = {"job_id": "job-123", "status": status}
    if progress is not None:
        body["progress"] = progress
    if error is not None:
        body["error"] = error
    resp.json.return_value = body
    resp.text = str(body)
    return resp


def _service_mock(running: bool) -> MagicMock:
    """A SearchServiceLifecycle double whose status() reports the given running state."""
    service = MagicMock()
    service.status.return_value = ServiceStatus(
        running=running,
        pid=4242 if running else None,
        uptime_seconds=3.0 if running else None,
    )
    return service


# ---------------------------------------------------------------------------
# test_poll_job_exits_0_on_done
# ---------------------------------------------------------------------------


def test_poll_job_exits_0_on_done() -> None:
    """When the job reaches DONE, _poll_job returns the job dict without raising SystemExit."""
    done_resp = _job_response("DONE")

    with (
        patch("archon_search.cli._helpers.httpx.get", return_value=done_resp),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        # Should return the job dict, not raise SystemExit
        result = _poll_job("job-123", "http://localhost:8765", {"Authorization": "Bearer test-key"})

    assert result["status"] == "DONE"
    assert result["job_id"] == "job-123"


# ---------------------------------------------------------------------------
# test_poll_job_exits_1_on_failed_and_cancelled
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal_status", ["FAILED", "CANCELLED", "FAILED_EXPIRED"])
def test_poll_job_exits_1_on_failed_and_cancelled(terminal_status: str) -> None:
    """FAILED, CANCELLED, FAILED_EXPIRED all cause _poll_job to exit 1."""
    fail_resp = _job_response(terminal_status, error="something went wrong")

    with (
        patch("archon_search.cli._helpers.httpx.get", return_value=fail_resp),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _poll_job("job-123", "http://localhost:8765", {"Authorization": "Bearer test-key"})

    assert exc_info.value.code == 1, (
        f"Expected exit code 1 for status {terminal_status}, got {exc_info.value.code}"
    )


# ---------------------------------------------------------------------------
# test_poll_job_failed_prints_error_to_stderr
# ---------------------------------------------------------------------------


def test_poll_job_failed_prints_error_to_stderr(capsys) -> None:
    """On FAILED, the job error field is printed to stderr before exiting 1."""
    fail_resp = _job_response("FAILED", error="embedding model not loaded")

    with (
        patch("archon_search.cli._helpers.httpx.get", return_value=fail_resp),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _poll_job("job-123", "http://localhost:8765", {"Authorization": "Bearer test-key"})

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "embedding model not loaded" in captured.err, (
        f"Expected error text in stderr, got: {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# test_poll_job_prints_progress_each_interval
# ---------------------------------------------------------------------------


def test_poll_job_prints_progress_each_interval(capsys) -> None:
    """When the job response has a non-null progress field, it is printed each poll."""
    running_resp = _job_response(
        "RUNNING",
        progress={"phase": "indexing", "processed": 10, "total": 100},
    )
    done_resp = _job_response("DONE")

    with (
        patch(
            "archon_search.cli._helpers.httpx.get",
            side_effect=[running_resp, running_resp, done_resp],
        ),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = _poll_job("job-123", "http://localhost:8765", {"Authorization": "Bearer test-key"})

    assert result["status"] == "DONE"
    captured = capsys.readouterr()
    # The exact format is "{phase}: {processed}/{total}"
    assert "indexing: 10/100" in captured.out, (
        f"Expected 'indexing: 10/100' in output, got: {captured.out!r}"
    )
    # Progress printed twice (two RUNNING polls)
    assert captured.out.count("indexing: 10/100") == 2, (
        f"Expected progress printed twice (once per RUNNING poll), got: {captured.out!r}"
    )


# ---------------------------------------------------------------------------
# test_poll_job_keyboard_interrupt_exits_0
# ---------------------------------------------------------------------------


def test_poll_job_keyboard_interrupt_exits_0(capsys) -> None:
    """KeyboardInterrupt during polling prints the 'job continues' message and returns {} (falsy)."""

    def _raise_keyboard_interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with (
        patch("archon_search.cli._helpers.httpx.get", side_effect=_raise_keyboard_interrupt),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        # Should return {} (falsy), not raise SystemExit
        result = _poll_job("job-123", "http://localhost:8765", {"Authorization": "Bearer test-key"})

    assert not result, f"Expected falsy return on KeyboardInterrupt, got: {result!r}"

    captured = capsys.readouterr()
    assert "Polling stopped" in captured.out, (
        f"Expected 'Polling stopped' message, got: {captured.out!r}"
    )
    assert "job continues on server" in captured.out, (
        f"Expected 'job continues on server' message, got: {captured.out!r}"
    )


# ---------------------------------------------------------------------------
# test_poll_migration_job_does_not_print_complete_on_interrupt
# ---------------------------------------------------------------------------


def test_poll_migration_job_does_not_print_complete_on_interrupt(capsys) -> None:
    """After KeyboardInterrupt, _poll_migration_job must NOT print 'Migration complete.'"""
    from archon_search.cli.collection import _poll_migration_job

    def _raise_keyboard_interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with (
        patch("archon_search.cli._helpers.httpx.get", side_effect=_raise_keyboard_interrupt),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        _poll_migration_job("job-123", "http://localhost:8765", {"Authorization": "Bearer test-key"})

    captured = capsys.readouterr()
    assert "Migration complete" not in captured.out, (
        f"Expected NO 'Migration complete' on interrupt, got: {captured.out!r}"
    )
    assert "Polling stopped" in captured.out


# ---------------------------------------------------------------------------
# test_poll_job_connect_error_exits_1
# ---------------------------------------------------------------------------


def test_poll_job_connect_error_exits_1(capsys) -> None:
    """httpx.ConnectError during polling → exit 1 with error message."""
    with (
        patch(
            "archon_search.cli._helpers.httpx.get",
            side_effect=httpx.ConnectError("Connection refused"),
        ),
        patch("archon_search.cli._helpers.time.sleep"),
        # Dead server: without this the real service probe runs and the assertion below
        # would flip depending on whether the dev machine's launchd service is up.
        patch(
            "archon_search.cli._helpers._get_service",
            return_value=_service_mock(running=False),
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _poll_job("job-123", "http://localhost:8765", {"Authorization": "Bearer test-key"})

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "not running" in captured.err.lower() or "start it first" in captured.err.lower()


# ---------------------------------------------------------------------------
# test_poll_job_non200_exits_1
# ---------------------------------------------------------------------------


def test_poll_job_non200_exits_1(capsys) -> None:
    """Non-200 poll response → exit 1 with status code in error message."""
    error_resp = MagicMock()
    error_resp.status_code = 500
    error_resp.text = "Internal Server Error"

    with (
        patch("archon_search.cli._helpers.httpx.get", return_value=error_resp),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _poll_job("job-123", "http://localhost:8765", {"Authorization": "Bearer test-key"})

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "500" in captured.err


# ---------------------------------------------------------------------------
# test_poll_job_connect_error_server_alive_shows_starting_up
# ---------------------------------------------------------------------------


def test_poll_job_connect_error_server_alive_shows_starting_up(capsys) -> None:
    """Connect failure while the service process IS alive → 'starting up', not 'not running'.

    The port is unbound during warmup (model loading), so a ConnectError does not mean the
    server is dead. launchd/systemd still reports a live PID, and the message must say so.
    """
    with (
        patch(
            "archon_search.cli._helpers.httpx.get",
            side_effect=httpx.ConnectError("Connection refused"),
        ),
        patch("archon_search.cli._helpers.time.sleep"),
        patch(
            "archon_search.cli._helpers._get_service",
            return_value=_service_mock(running=True),
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _poll_job("job-123", "http://localhost:8765", {"Authorization": "Bearer test-key"})

    assert exc_info.value.code == 1, f"Expected exit code 1, got {exc_info.value.code}"

    captured = capsys.readouterr()
    assert "starting up" in captured.err, (
        f"Expected a 'starting up' hint when the service process is alive, got: {captured.err!r}"
    )
    assert "not running" not in captured.err, (
        f"Must NOT claim the server is not running while its PID is alive, got: {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# test_poll_job_connect_error_server_dead_shows_not_running
# ---------------------------------------------------------------------------


def test_poll_job_connect_error_server_dead_shows_not_running(capsys) -> None:
    """Connect failure while the service process is NOT alive → the existing 'not running' text."""
    with (
        patch(
            "archon_search.cli._helpers.httpx.get",
            side_effect=httpx.ConnectError("Connection refused"),
        ),
        patch("archon_search.cli._helpers.time.sleep"),
        patch(
            "archon_search.cli._helpers._get_service",
            return_value=_service_mock(running=False),
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _poll_job("job-123", "http://localhost:8765", {"Authorization": "Bearer test-key"})

    assert exc_info.value.code == 1, f"Expected exit code 1, got {exc_info.value.code}"

    captured = capsys.readouterr()
    assert "not running" in captured.err, (
        f"Expected the 'not running' message for a dead server, got: {captured.err!r}"
    )
    assert "starting up" not in captured.err, (
        f"Must NOT claim the server is starting up when no PID is alive, got: {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# test_poll_job_connect_error_unsupported_platform_falls_back_to_not_running
# ---------------------------------------------------------------------------


def test_poll_job_connect_error_unsupported_platform_falls_back_to_not_running(capsys) -> None:
    """_get_service() raising (unsupported platform) → 'not running', never a traceback.

    _get_service raises NotImplementedError on any platform that is neither darwin, linux,
    nor win32. The probe is best-effort: it must degrade to the pre-existing message and
    still exit 1, not crash the CLI on the error path.
    """
    with (
        patch(
            "archon_search.cli._helpers.httpx.get",
            side_effect=httpx.ConnectError("Connection refused"),
        ),
        patch("archon_search.cli._helpers.time.sleep"),
        patch(
            "archon_search.cli._helpers._get_service",
            side_effect=NotImplementedError("Unsupported platform: sunos5"),
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _poll_job("job-123", "http://localhost:8765", {"Authorization": "Bearer test-key"})

    assert exc_info.value.code == 1, f"Expected exit code 1, got {exc_info.value.code}"

    captured = capsys.readouterr()
    assert "not running" in captured.err, (
        f"Expected the 'not running' fallback when the service probe is unavailable, "
        f"got: {captured.err!r}"
    )
    assert "starting up" not in captured.err, (
        f"Must NOT claim the server is starting up when the probe failed, got: {captured.err!r}"
    )
    assert "sunos5" not in captured.err, (
        f"The swallowed probe exception must not leak into user output, got: {captured.err!r}"
    )


# ---------------------------------------------------------------------------
# test_poll_job_connect_error_status_probe_failure_falls_back_to_not_running
# ---------------------------------------------------------------------------


def test_poll_job_connect_error_status_probe_failure_falls_back_to_not_running(capsys) -> None:
    """service.status() raising (e.g. launchctl/systemctl missing) → 'not running', exit 1."""
    service = MagicMock()
    service.status.side_effect = OSError("launchctl not found")

    with (
        patch(
            "archon_search.cli._helpers.httpx.get",
            side_effect=httpx.ConnectError("Connection refused"),
        ),
        patch("archon_search.cli._helpers.time.sleep"),
        patch("archon_search.cli._helpers._get_service", return_value=service),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _poll_job("job-123", "http://localhost:8765", {"Authorization": "Bearer test-key"})

    assert exc_info.value.code == 1, f"Expected exit code 1, got {exc_info.value.code}"
    assert service.status.called, "the service probe must actually be attempted"

    captured = capsys.readouterr()
    assert "not running" in captured.err, (
        f"Expected the 'not running' fallback when status() raises, got: {captured.err!r}"
    )
    assert "launchctl not found" not in captured.err, (
        f"The swallowed probe exception must not leak into user output, got: {captured.err!r}"
    )


def test_poll_job_missing_status_exits_1(capsys) -> None:
    """200 response with no 'status' field → exit 1 (not an infinite hang)."""
    missing_status_resp = MagicMock()
    missing_status_resp.status_code = 200
    missing_status_resp.json.return_value = {"job_id": "job-123"}  # no 'status'

    with (
        patch("archon_search.cli._helpers.httpx.get", return_value=missing_status_resp),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _poll_job("job-123", "http://localhost:8765", {"Authorization": "Bearer test-key"})

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "missing" in captured.err

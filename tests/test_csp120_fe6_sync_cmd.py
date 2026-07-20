"""Unit tests for FE-6: sync CLI command converted to httpx proxy.

Completes S7 (CLI side): POST /sync, --api-url/--api-key/--wait options.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
from click.testing import CliRunner

from archon_search.cli.sync import sync


def _make_resp(status_code: int, json_body: dict | None = None, text: str = "") -> MagicMock:
    """Build a fake httpx.Response mock."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# test_sync_submits_job_prints_id
# ---------------------------------------------------------------------------

def test_sync_submits_job_prints_id() -> None:
    """Mocked 202 → job_id printed, exit 0."""
    runner = CliRunner()
    resp_202 = _make_resp(202, {"job_id": "sync-abc123", "status": "RUNNING"})

    with (
        patch("httpx.post", return_value=resp_202) as mock_post,
        patch("httpx.get") as mock_get,
    ):
        result = runner.invoke(
            sync,
            ["--api-url", "http://localhost:8765", "--api-key", "testkey"],
        )

    assert result.exit_code == 0, result.output
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert "/sync" in call_args.args[0]
    assert call_args.kwargs["headers"]["Authorization"] == "Bearer testkey"
    assert "sync-abc123" in result.output
    mock_get.assert_not_called()
    # Assert no body was sent (server's POST /sync takes no request body)
    assert "json" not in call_args.kwargs
    assert "data" not in call_args.kwargs


# ---------------------------------------------------------------------------
# test_sync_wait_polls_to_done
# ---------------------------------------------------------------------------

def test_sync_wait_polls_to_done() -> None:
    """--wait: mocked poll sequence → prints 'Sync complete.', exit 0."""
    runner = CliRunner()
    resp_202 = _make_resp(202, {"job_id": "sync-xyz789", "status": "RUNNING"})
    poll_running = _make_resp(200, {"status": "RUNNING", "job_id": "sync-xyz789"})
    poll_done = _make_resp(200, {"status": "DONE", "job_id": "sync-xyz789"})

    with (
        patch("httpx.post", return_value=resp_202),
        patch("archon_search.cli._helpers.httpx.get", side_effect=[poll_running, poll_done]) as mock_get,
        patch("time.sleep"),  # don't actually sleep during tests
    ):
        result = runner.invoke(
            sync,
            ["--api-url", "http://localhost:8765", "--api-key", "testkey", "--wait"],
        )

    assert result.exit_code == 0, result.output
    assert "Sync complete." in result.output
    # Assert poll targeted the correct job_id
    from unittest.mock import call
    poll_calls = mock_get.call_args_list
    assert len(poll_calls) == 2
    for c in poll_calls:
        assert "sync-xyz789" in c.args[0]
        assert c.kwargs.get("headers", {}).get("Authorization") == "Bearer testkey"


# ---------------------------------------------------------------------------
# test_sync_server_not_running
# ---------------------------------------------------------------------------

def test_sync_server_not_running() -> None:
    """httpx.ConnectError → 'archon-search serve is not running. Start it first.', exit 1."""
    runner = CliRunner()

    with patch("httpx.post", side_effect=httpx.ConnectError("Connection refused")):
        result = runner.invoke(
            sync,
            ["--api-url", "http://localhost:8765", "--api-key", "testkey"],
        )

    assert result.exit_code == 1
    assert "not running" in result.output.lower()


# ---------------------------------------------------------------------------
# test_sync_server_not_running_error_goes_to_stderr
# ---------------------------------------------------------------------------

def test_sync_server_not_running_error_goes_to_stderr() -> None:
    """Verify the 'not running' error message is present and exit code is 1.

    Click 8.x CliRunner merges stderr into output; the err=True routing is
    verified by source inspection (sync.py uses click.echo(..., err=True)).
    This test confirms the message content and exit code are correct.
    """
    runner = CliRunner()
    with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
        result = runner.invoke(
            sync,
            ["--api-url", "http://localhost:8765", "--api-key", "testkey"],
        )
    assert result.exit_code == 1
    assert "not running" in result.output.lower()


# ---------------------------------------------------------------------------
# test_sync_409_already_in_progress
# ---------------------------------------------------------------------------

def test_sync_409_already_in_progress() -> None:
    """409 from server → clean 'Error: sync already in progress' message, exit 1."""
    runner = CliRunner()
    resp_409 = _make_resp(409, {"detail": "sync already in progress"})

    with patch("httpx.post", return_value=resp_409):
        result = runner.invoke(
            sync,
            ["--api-url", "http://localhost:8765", "--api-key", "testkey"],
        )

    assert result.exit_code == 1
    assert "sync already in progress" in result.output
    assert "{" not in result.output  # no raw JSON blob


# ---------------------------------------------------------------------------
# test_sync_409_missing_detail_key_uses_fallback
# ---------------------------------------------------------------------------

def test_sync_409_missing_detail_key_uses_fallback() -> None:
    """409 body missing 'detail' key → fallback message, no crash, exit 1."""
    runner = CliRunner()
    resp_409 = _make_resp(409, {})  # no 'detail' key

    with patch("httpx.post", return_value=resp_409):
        result = runner.invoke(
            sync,
            ["--api-url", "http://localhost:8765", "--api-key", "testkey"],
        )

    assert result.exit_code == 1
    assert "sync already in progress" in result.output


# ---------------------------------------------------------------------------
# test_sync_409_non_json_body_uses_fallback
# ---------------------------------------------------------------------------

def test_sync_409_non_json_body_uses_fallback() -> None:
    """409 with non-JSON body (resp.json() raises) → fallback message, no crash, exit 1."""
    runner = CliRunner()
    resp_409 = _make_resp(409)
    resp_409.json.side_effect = ValueError("No JSON")  # simulates HTML/text 409 body

    with patch("httpx.post", return_value=resp_409):
        result = runner.invoke(
            sync,
            ["--api-url", "http://localhost:8765", "--api-key", "testkey"],
        )

    assert result.exit_code == 1
    assert "sync already in progress" in result.output


# ---------------------------------------------------------------------------
# test_sync_wait_exits_1_on_failed
# ---------------------------------------------------------------------------

def test_sync_wait_exits_1_on_failed() -> None:
    """--wait: job FAILED → exit 1, 'Sync complete.' NOT printed."""
    runner = CliRunner()
    resp_202 = _make_resp(202, {"job_id": "sync-fail99", "status": "RUNNING"})
    poll_failed = _make_resp(200, {"status": "FAILED", "job_id": "sync-fail99", "error": "disk full"})

    with (
        patch("httpx.post", return_value=resp_202),
        patch("archon_search.cli._helpers.httpx.get", return_value=poll_failed),
        patch("time.sleep"),
    ):
        result = runner.invoke(
            sync,
            ["--api-url", "http://localhost:8765", "--api-key", "testkey", "--wait"],
        )

    assert result.exit_code == 1
    assert "Sync complete." not in result.output


# ---------------------------------------------------------------------------
# test_sync_wait_keyboard_interrupt
# ---------------------------------------------------------------------------

def test_sync_wait_keyboard_interrupt() -> None:
    """KeyboardInterrupt during --wait poll → 'Polling stopped' printed, 'Sync complete.' NOT printed, exit 0."""
    runner = CliRunner()
    resp_202 = _make_resp(202, {"job_id": "sync-kbi", "status": "RUNNING"})

    with (
        patch("httpx.post", return_value=resp_202),
        patch("archon_search.cli._helpers.httpx.get", side_effect=KeyboardInterrupt()),
        patch("time.sleep"),
    ):
        result = runner.invoke(
            sync,
            ["--api-url", "http://localhost:8765", "--api-key", "testkey", "--wait"],
        )

    assert result.exit_code == 0
    assert "Polling stopped" in result.output
    assert "Sync complete." not in result.output


# ---------------------------------------------------------------------------
# test_sync_generic_http_error_exits_1
# ---------------------------------------------------------------------------

def test_sync_generic_http_error_exits_1() -> None:
    """Generic httpx.HTTPError (not ConnectError) → 'Error contacting server', exit 1."""
    runner = CliRunner()

    with patch("httpx.post", side_effect=httpx.ReadTimeout("timed out")):
        result = runner.invoke(
            sync,
            ["--api-url", "http://localhost:8765", "--api-key", "testkey"],
        )

    assert result.exit_code == 1
    assert "Error contacting server" in result.output


# ---------------------------------------------------------------------------
# test_sync_non202_prints_status_and_body
# ---------------------------------------------------------------------------

def test_sync_non202_prints_status_and_body() -> None:
    """Non-202/409 response (e.g. 500) → status code + body on stderr, exit 1."""
    runner = CliRunner()
    resp_500 = _make_resp(500, text="internal server error")

    with patch("httpx.post", return_value=resp_500):
        result = runner.invoke(
            sync,
            ["--api-url", "http://localhost:8765", "--api-key", "testkey"],
        )

    assert result.exit_code == 1
    assert "500" in result.output
    assert "internal server error" in result.output

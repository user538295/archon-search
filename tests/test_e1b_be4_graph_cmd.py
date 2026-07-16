"""Unit tests for ``archon-search graph build-communities`` CLI — GBC110 BE-8.

Converted from an in-process command to an HTTP proxy against
``POST /graph/{collection}/rebuild-communities`` (BE-2). Tests:

- test_cli_prints_job_id_without_wait: without --wait, prints job_id and exits 0
- test_cli_wait_polls_until_done_exit_0: --wait polls to DONE and exits 0 (S3)
- test_cli_connect_error_prints_server_not_running: httpx.ConnectError -> message + non-zero (S4)
- test_cli_wait_recognises_all_terminal_statuses: FAILED/CANCELLED/FAILED_EXPIRED all terminal,
  exit non-zero, never hang (S13)
- test_cli_non_202_initial_response_prints_error_and_exits_nonzero: non-202 initial POST
  response -> status code + response text in output, non-zero exit
- test_cli_422_graph_disabled_surfaces_response_body_detail: a 422 initial POST response
  (graph.enabled=false) -> the server's detail string from the response body is echoed,
  non-zero exit (acceptance criterion: CLI surfaces the 422 detail from the response body)
- test_cli_wait_mid_poll_error_exits_nonzero: httpx.HTTPError during --wait polling ->
  error message + non-zero exit
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from archon_search.cli.graph_cmd import graph_cmd


def _response(status_code: int, json_body: dict, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = text or str(json_body)
    return resp


def test_cli_prints_job_id_without_wait() -> None:
    """Without --wait, the CLI posts to the rebuild endpoint, prints job_id, exits 0."""
    runner = CliRunner()

    post_resp = _response(202, {"job_id": "job-123", "status": "RUNNING"})

    with patch("archon_search.cli.graph_cmd.httpx.post", return_value=post_resp) as mock_post:
        result = runner.invoke(
            graph_cmd,
            ["build-communities", "my-collection", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, f"Unexpected exit code: {result.exit_code}\n{result.output}"
    assert "job-123" in result.output, f"Expected job_id in output: {result.output!r}"
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert call_args.args[0].endswith("/graph/my-collection/rebuild-communities")
    assert call_args.kwargs["headers"]["Authorization"] == "Bearer test-key"


def test_cli_wait_polls_until_done_exit_0() -> None:
    """--wait polls GET /jobs/{id} until DONE and exits 0 (S3 unit portion)."""
    runner = CliRunner()

    post_resp = _response(202, {"job_id": "job-abc", "status": "RUNNING"})
    running_resp = _response(200, {"job_id": "job-abc", "status": "RUNNING"})
    done_resp = _response(
        200, {"job_id": "job-abc", "status": "DONE", "result": {"communities_built": 2}}
    )

    with (
        patch("archon_search.cli.graph_cmd.httpx.post", return_value=post_resp),
        patch(
            "archon_search.cli.graph_cmd.httpx.get",
            side_effect=[running_resp, done_resp],
        ),
        patch("archon_search.cli.graph_cmd.time.sleep"),
    ):
        result = runner.invoke(
            graph_cmd,
            ["build-communities", "my-collection", "--wait", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, f"Unexpected exit code: {result.exit_code}\n{result.output}"
    assert "job-abc" in result.output
    assert "Community rebuild complete: 2 communities built." in result.output


def test_cli_connect_error_prints_server_not_running() -> None:
    """A mocked httpx.ConnectError yields exactly the server-not-running message, non-zero exit (S4)."""
    runner = CliRunner()

    with patch(
        "archon_search.cli.graph_cmd.httpx.post",
        side_effect=httpx.ConnectError("connection refused"),
    ):
        result = runner.invoke(
            graph_cmd,
            ["build-communities", "my-collection", "--api-key", "test-key"],
        )

    assert result.exit_code != 0, f"Expected non-zero exit, got {result.exit_code}"
    assert "Server is not running. Start it first with: archon-search start" in result.output, (
        f"Expected exact server-not-running message in output: {result.output!r}"
    )


@pytest.mark.parametrize("terminal_status", ["FAILED", "CANCELLED", "FAILED_EXPIRED"])
def test_cli_wait_recognises_all_terminal_statuses(terminal_status: str) -> None:
    """FAILED/CANCELLED/FAILED_EXPIRED are all terminal, exit non-zero, never hang (S13)."""
    runner = CliRunner()

    post_resp = _response(202, {"job_id": "job-xyz", "status": "RUNNING"})
    terminal_resp = _response(
        200, {"job_id": "job-xyz", "status": terminal_status, "error": "boom"}
    )

    with (
        patch("archon_search.cli.graph_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.graph_cmd.httpx.get", return_value=terminal_resp) as mock_get,
        patch("archon_search.cli.graph_cmd.time.sleep") as mock_sleep,
    ):
        result = runner.invoke(
            graph_cmd,
            ["build-communities", "my-collection", "--wait", "--api-key", "test-key"],
        )

    assert result.exit_code != 0, (
        f"Expected non-zero exit for terminal status {terminal_status!r}, got {result.exit_code}"
    )
    # Never hangs: exactly one poll call, no sleep in between (loop exits immediately on terminal).
    mock_get.assert_called_once()
    mock_sleep.assert_not_called()


def test_cli_non_202_initial_response_prints_error_and_exits_nonzero() -> None:
    """A non-202 initial POST response prints the status code + response text and exits non-zero."""
    runner = CliRunner()

    post_resp = _response(404, {"detail": "Collection 'my-collection' not found"})

    with patch("archon_search.cli.graph_cmd.httpx.post", return_value=post_resp):
        result = runner.invoke(
            graph_cmd,
            ["build-communities", "my-collection", "--api-key", "test-key"],
        )

    assert result.exit_code != 0, f"Expected non-zero exit, got {result.exit_code}"
    assert "404" in result.output
    assert "not found" in result.output.lower()


def test_cli_422_graph_disabled_surfaces_response_body_detail() -> None:
    """A 422 (graph.enabled=false) response's detail string is echoed from the response body.

    Acceptance criterion: "the CLI surfaces the server's 422 detail string from the
    response body" — this exercises the same non-202 echo branch as the 404 test above,
    but with the actual verbatim detail routes_graph.py's guards return.
    """
    runner = CliRunner()
    detail = "graph inspection requires [graph] enabled=true in server config"
    post_resp = _response(422, {"detail": detail})

    with patch("archon_search.cli.graph_cmd.httpx.post", return_value=post_resp):
        result = runner.invoke(
            graph_cmd,
            ["build-communities", "my-collection", "--api-key", "test-key"],
        )

    assert result.exit_code != 0, f"Expected non-zero exit, got {result.exit_code}"
    assert "422" in result.output
    assert detail in result.output, f"Expected server detail in output: {result.output!r}"


def test_cli_wait_mid_poll_error_exits_nonzero() -> None:
    """An httpx.HTTPError raised mid-poll during --wait exits non-zero with an error message."""
    runner = CliRunner()

    post_resp = _response(202, {"job_id": "job-poll-err", "status": "RUNNING"})

    with (
        patch("archon_search.cli.graph_cmd.httpx.post", return_value=post_resp),
        patch(
            "archon_search.cli.graph_cmd.httpx.get",
            side_effect=httpx.HTTPError("boom"),
        ),
        patch("archon_search.cli.graph_cmd.time.sleep"),
    ):
        result = runner.invoke(
            graph_cmd,
            ["build-communities", "my-collection", "--wait", "--api-key", "test-key"],
        )

    assert result.exit_code != 0, f"Expected non-zero exit, got {result.exit_code}"
    assert "error polling job" in result.output.lower()

"""Unit tests for ``archon-search collection reindex`` CLI proxy (FE-2).

Tests:
- test_reindex_submits_job_prints_id: mocked 202 → job_id printed, no polling, exit 0
- test_reindex_wait_polls_to_done: --wait mocked poll sequence → completion marker, exit 0
- test_reindex_server_not_running: ConnectError → "archon-search serve is not running. Start it first.", exit 1
- test_reindex_non202_prints_status_and_body: 409/503 → status + body on stderr, exit 1
- test_reindex_wait_exits_1_on_failed: --wait + FAILED → exit 1, no completion marker
- test_reindex_wait_keyboard_interrupt_no_false_complete: Ctrl-C → "Polling stopped", no false completion
- test_reindex_generic_http_error_exits_1: non-ConnectError HTTPError → "Error contacting server", exit 1
- test_reindex_404_collection_not_found: 404 → "collection not found", exit 1
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from archon_search.cli.collection import collection


def _mock_response(status_code: int, body: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.text = text or str(body or "")
    return resp


def _job_response(status: str, progress: dict | None = None) -> MagicMock:
    body: dict = {"job_id": "job-abc-123", "status": status}
    if progress:
        body["progress"] = progress
    return _mock_response(200, body)


# ---------------------------------------------------------------------------
# test_reindex_submits_job_prints_id
# ---------------------------------------------------------------------------


def test_reindex_submits_job_prints_id() -> None:
    """202 response → job_id printed to stdout, exit 0; no polling without --wait."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"job_id": "job-reindex-001", "status": "RUNNING"})

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp) as mock_post,
        patch("archon_search.cli._helpers.httpx.get") as mock_get,
    ):
        result = runner.invoke(
            collection,
            ["reindex", "mycol", "--api-url", "http://localhost:8765", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, result.output
    assert "job-reindex-001" in result.output
    assert "Reindex complete" not in result.output
    mock_post.assert_called_once()
    call_url = mock_post.call_args[0][0]
    assert "/collections/mycol/reindex" in call_url
    _, call_kwargs = mock_post.call_args
    assert call_kwargs.get("headers", {}).get("Authorization") == "Bearer test-key"
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# test_reindex_wait_polls_to_done
# ---------------------------------------------------------------------------


def test_reindex_wait_polls_to_done() -> None:
    """--wait causes polling until DONE; completion marker printed, exit 0."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"job_id": "job-reindex-002", "status": "RUNNING"})

    get_job_sequence = [
        _job_response("RUNNING", progress={"phase": "ingest", "processed": 5, "total": 10}),
        _job_response("DONE"),
    ]

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp),
        patch("archon_search.cli._helpers.httpx.get", side_effect=get_job_sequence),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = runner.invoke(
            collection,
            ["reindex", "mycol", "--wait", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, result.output
    # Job id is printed before polling
    assert "job-reindex-002" in result.output
    # Completion marker is printed after polling finishes
    assert "Reindex complete" in result.output
    # Progress printed during poll
    assert "5/10" in result.output


# ---------------------------------------------------------------------------
# test_reindex_server_not_running
# ---------------------------------------------------------------------------


def test_reindex_server_not_running() -> None:
    """ConnectError → 'archon-search serve is not running. Start it first.', exit 1."""
    runner = CliRunner()

    with patch(
        "archon_search.cli.collection.httpx.post",
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        result = runner.invoke(
            collection,
            ["reindex", "mycol", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "archon-search serve is not running" in result.output
    assert "Start it first" in result.output


# ---------------------------------------------------------------------------
# test_reindex_non202_prints_status_and_body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [409, 503])
def test_reindex_non202_prints_status_and_body(status_code: int) -> None:
    """Non-202 response (409/503) → status code + body on stderr, exit 1."""
    runner = CliRunner()
    post_resp = _mock_response(status_code, text="server busy")
    post_resp.text = "server busy"

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp):
        result = runner.invoke(
            collection,
            ["reindex", "mycol", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    combined = result.output
    assert str(status_code) in combined
    assert "server busy" in combined


# ---------------------------------------------------------------------------
# test_reindex_wait_exits_1_on_failed
# ---------------------------------------------------------------------------


def test_reindex_wait_exits_1_on_failed() -> None:
    """--wait exits 1 when the job reaches FAILED status; completion marker NOT printed."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"job_id": "job-reindex-fail", "status": "RUNNING"})
    get_job_sequence = [
        _job_response("RUNNING"),
        _job_response("FAILED"),
    ]
    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp),
        patch("archon_search.cli._helpers.httpx.get", side_effect=get_job_sequence),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = runner.invoke(
            collection,
            ["reindex", "mycol", "--wait", "--api-key", "test-key"],
        )
    assert result.exit_code == 1
    assert "Reindex complete" not in result.output


# ---------------------------------------------------------------------------
# test_reindex_wait_keyboard_interrupt_no_false_complete
# ---------------------------------------------------------------------------


def test_reindex_wait_keyboard_interrupt_no_false_complete() -> None:
    """Ctrl-C during --wait polling: 'Polling stopped' printed, 'Reindex complete' NOT printed, exit 0."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"job_id": "job-reindex-kbi", "status": "RUNNING"})

    def _raise_kbi(*args, **kwargs):
        raise KeyboardInterrupt

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp),
        patch("archon_search.cli._helpers.httpx.get", side_effect=_raise_kbi),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = runner.invoke(
            collection,
            ["reindex", "mycol", "--wait", "--api-key", "test-key"],
        )
    assert result.exit_code == 0
    assert "Polling stopped" in result.output
    assert "Reindex complete" not in result.output


# ---------------------------------------------------------------------------
# test_reindex_generic_http_error_exits_1
# ---------------------------------------------------------------------------


def test_reindex_generic_http_error_exits_1() -> None:
    """Non-ConnectError httpx.HTTPError → 'Error contacting server', exit 1."""
    runner = CliRunner()
    with patch(
        "archon_search.cli.collection.httpx.post",
        side_effect=httpx.ReadTimeout("timed out"),
    ):
        result = runner.invoke(
            collection,
            ["reindex", "mycol", "--api-key", "test-key"],
        )
    assert result.exit_code == 1
    assert "Error contacting server" in result.output


# ---------------------------------------------------------------------------
# test_reindex_404_collection_not_found
# ---------------------------------------------------------------------------


def test_reindex_404_collection_not_found() -> None:
    """404 response → 'collection not found' message, exit 1."""
    runner = CliRunner()
    post_resp = _mock_response(404, {"detail": "Collection 'mycol' not found"})

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp):
        result = runner.invoke(
            collection,
            ["reindex", "mycol", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "mycol" in result.output
    assert "not found" in result.output.lower()

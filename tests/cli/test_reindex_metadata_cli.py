"""Unit tests for ``archon-search collection reindex-metadata`` CLI proxy (FE-7).

Tests:
- test_reindex_metadata_submits_job_prints_id: mocked 202 → job_id printed, exit 0
- test_reindex_metadata_forwards_dry_run_flag_in_body: --dry-run → body "dry_run": true
- test_reindex_metadata_forwards_normalize_timestamps_in_body: --no-normalize-timestamps → body false
- test_reindex_metadata_default_body_sends_normalize_true: no flags → normalize_timestamps true by default
- test_reindex_metadata_wait_polls_to_done: --wait mocked poll → completion marker + counts, exit 0
- test_reindex_metadata_wait_keyboard_interrupt_no_false_complete: Ctrl-C → "Polling stopped", exit 0
- test_reindex_metadata_server_not_running: ConnectError → "archon-search serve is not running", exit 1
- test_reindex_metadata_404_collection_not_found: 404 → clean "not found" message, exit 1
- test_reindex_metadata_409_already_in_progress: 409 → detail message extracted, exit 1
- test_reindex_metadata_non202_generic_prints_status_and_body: 503 → status + body, exit 1
- test_reindex_metadata_generic_http_error_exits_1: non-ConnectError HTTPError → "Error contacting", exit 1
- test_reindex_metadata_wait_exits_1_on_failed: FAILED job → exit 1, no completion marker
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


def _job_response(status: str, progress: dict | None = None, result: dict | None = None) -> MagicMock:
    body: dict = {"job_id": "job-rm-123", "status": status}
    if progress:
        body["progress"] = progress
    if result:
        body["result"] = result
    return _mock_response(200, body)


# ---------------------------------------------------------------------------
# test_reindex_metadata_submits_job_prints_id
# ---------------------------------------------------------------------------


def test_reindex_metadata_submits_job_prints_id() -> None:
    """202 response → job_id printed to stdout, exit 0; no polling without --wait."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"job_id": "job-rm-001", "status": "RUNNING"})

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp) as mock_post,
        patch("archon_search.cli._helpers.httpx.get") as mock_get,
    ):
        result = runner.invoke(
            collection,
            ["reindex-metadata", "my-col", "--api-url", "http://localhost:8765", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, result.output
    assert "job-rm-001" in result.output
    assert "Reindex-metadata complete" not in result.output
    mock_post.assert_called_once()
    call_url = mock_post.call_args[0][0]
    assert call_url == "http://localhost:8765/collections/my-col/reindex-metadata"
    _, call_kwargs = mock_post.call_args
    assert call_kwargs.get("headers", {}).get("Authorization") == "Bearer test-key"
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# test_reindex_metadata_forwards_dry_run_flag_in_body
# ---------------------------------------------------------------------------


def test_reindex_metadata_forwards_dry_run_flag_in_body() -> None:
    """--dry-run present → request body contains "dry_run": true."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"job_id": "job-rm-002", "status": "RUNNING"})

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp) as mock_post:
        result = runner.invoke(
            collection,
            ["reindex-metadata", "my-col", "--dry-run", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, result.output
    _, call_kwargs = mock_post.call_args
    body = call_kwargs.get("json", {})
    assert body.get("dry_run") is True
    assert body.get("normalize_timestamps") is True  # default unchanged


# ---------------------------------------------------------------------------
# test_reindex_metadata_forwards_normalize_timestamps_in_body
# ---------------------------------------------------------------------------


def test_reindex_metadata_forwards_normalize_timestamps_in_body() -> None:
    """--no-normalize-timestamps → request body contains "normalize_timestamps": false."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"job_id": "job-rm-003", "status": "RUNNING"})

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp) as mock_post:
        result = runner.invoke(
            collection,
            ["reindex-metadata", "my-col", "--no-normalize-timestamps", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, result.output
    _, call_kwargs = mock_post.call_args
    body = call_kwargs.get("json", {})
    assert body.get("normalize_timestamps") is False


# ---------------------------------------------------------------------------
# test_reindex_metadata_default_body_sends_normalize_true
# ---------------------------------------------------------------------------


def test_reindex_metadata_default_body_sends_normalize_true() -> None:
    """No flags → body sends normalize_timestamps=True (the default) and dry_run=False."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"job_id": "job-rm-default", "status": "RUNNING"})

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp) as mock_post:
        result = runner.invoke(
            collection,
            ["reindex-metadata", "my-col", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, result.output
    _, call_kwargs = mock_post.call_args
    body = call_kwargs.get("json", {})
    assert body.get("normalize_timestamps") is True
    assert body.get("dry_run") is False


# ---------------------------------------------------------------------------
# test_reindex_metadata_wait_polls_to_done
# ---------------------------------------------------------------------------


def test_reindex_metadata_wait_polls_to_done() -> None:
    """--wait causes polling until DONE; completion marker with counts printed, exit 0."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"job_id": "job-rm-004", "status": "RUNNING"})

    done_result = {"processed": 100, "updated": 50, "skipped": 10, "ts_normalized": 30, "warnings": []}
    get_job_sequence = [
        _job_response("RUNNING", progress={"phase": "reindex", "processed": 3, "total": 10}),
        _job_response("DONE", result=done_result),
    ]

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp),
        patch("archon_search.cli._helpers.httpx.get", side_effect=get_job_sequence) as mock_get,
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = runner.invoke(
            collection,
            ["reindex-metadata", "my-col", "--wait", "--api-url", "http://localhost:8765", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, result.output
    assert "job-rm-004" in result.output
    assert "Reindex-metadata complete" in result.output
    assert "processed=100" in result.output
    assert "updated=50" in result.output
    assert "ts_normalized=30" in result.output
    assert "3/10" in result.output
    # Verify polling URL and auth header (C1-I-1: poll goes to correct endpoint with correct token)
    assert mock_get.call_count == 2
    first_get_url = mock_get.call_args_list[0][0][0]
    assert first_get_url == "http://localhost:8765/jobs/job-rm-004"
    _, first_get_kwargs = mock_get.call_args_list[0]
    assert first_get_kwargs.get("headers", {}).get("Authorization") == "Bearer test-key"


# ---------------------------------------------------------------------------
# test_reindex_metadata_wait_keyboard_interrupt_no_false_complete
# ---------------------------------------------------------------------------


def test_reindex_metadata_wait_keyboard_interrupt_no_false_complete() -> None:
    """Ctrl-C during --wait polling: 'Polling stopped' printed, no completion, exit 0."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"job_id": "job-rm-kbi", "status": "RUNNING"})

    def _raise_kbi(*args, **kwargs):
        raise KeyboardInterrupt

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp),
        patch("archon_search.cli._helpers.httpx.get", side_effect=_raise_kbi),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = runner.invoke(
            collection,
            ["reindex-metadata", "my-col", "--wait", "--api-key", "test-key"],
        )

    assert result.exit_code == 0
    assert "Polling stopped" in result.output
    assert "Reindex-metadata complete" not in result.output


# ---------------------------------------------------------------------------
# test_reindex_metadata_server_not_running
# ---------------------------------------------------------------------------


def test_reindex_metadata_server_not_running() -> None:
    """ConnectError → 'archon-search serve is not running. Start it first.', exit 1."""
    runner = CliRunner()

    with patch(
        "archon_search.cli.collection.httpx.post",
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        result = runner.invoke(
            collection,
            ["reindex-metadata", "my-col", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "archon-search serve is not running" in result.output
    assert "Start it first" in result.output


# ---------------------------------------------------------------------------
# test_reindex_metadata_404_collection_not_found
# ---------------------------------------------------------------------------


def test_reindex_metadata_404_collection_not_found() -> None:
    """404 response → clean 'collection not found' message (not raw JSON), exit 1."""
    runner = CliRunner()
    post_resp = _mock_response(404, {"detail": "Collection 'my-col' not found"})

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp):
        result = runner.invoke(
            collection,
            ["reindex-metadata", "my-col", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "my-col" in result.output
    assert "not found" in result.output.lower()
    # Must NOT dump raw JSON body
    assert "{" not in result.output


# ---------------------------------------------------------------------------
# test_reindex_metadata_409_already_in_progress
# ---------------------------------------------------------------------------


def test_reindex_metadata_409_already_in_progress() -> None:
    """409 response → detail extracted and printed cleanly (not raw JSON), exit 1."""
    runner = CliRunner()
    post_resp = _mock_response(
        409,
        {"detail": "metadata reindex already in progress for this collection"},
    )

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp):
        result = runner.invoke(
            collection,
            ["reindex-metadata", "my-col", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "already in progress" in result.output
    # Must NOT dump raw JSON body
    assert "{" not in result.output


# ---------------------------------------------------------------------------
# test_reindex_metadata_non202_generic_prints_status_and_body
# ---------------------------------------------------------------------------


def test_reindex_metadata_non202_generic_prints_status_and_body() -> None:
    """503 (or other unexpected non-202) → status code + body on stderr, exit 1."""
    runner = CliRunner()
    post_resp = _mock_response(503, text="service unavailable")
    post_resp.text = "service unavailable"

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp):
        result = runner.invoke(
            collection,
            ["reindex-metadata", "my-col", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "503" in result.output
    assert "service unavailable" in result.output


# ---------------------------------------------------------------------------
# test_reindex_metadata_generic_http_error_exits_1
# ---------------------------------------------------------------------------


def test_reindex_metadata_generic_http_error_exits_1() -> None:
    """Non-ConnectError httpx.HTTPError → 'Error contacting server', exit 1."""
    runner = CliRunner()
    with patch(
        "archon_search.cli.collection.httpx.post",
        side_effect=httpx.ReadTimeout("timed out"),
    ):
        result = runner.invoke(
            collection,
            ["reindex-metadata", "my-col", "--api-key", "test-key"],
        )
    assert result.exit_code == 1
    assert "Error contacting server" in result.output


# ---------------------------------------------------------------------------
# test_reindex_metadata_wait_exits_1_on_failed
# ---------------------------------------------------------------------------


def test_reindex_metadata_wait_exits_1_on_failed() -> None:
    """--wait exits 1 when the job reaches FAILED status; completion marker NOT printed."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"job_id": "job-rm-fail", "status": "RUNNING"})
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
            ["reindex-metadata", "my-col", "--wait", "--api-key", "test-key"],
        )
    assert result.exit_code == 1
    assert "Reindex-metadata complete" not in result.output


# ---------------------------------------------------------------------------
# test_reindex_metadata_wait_prints_warnings (C1-I-4: warnings echo loop coverage)
# ---------------------------------------------------------------------------


def test_reindex_metadata_wait_prints_warnings() -> None:
    """--wait with warnings in job result: 'warnings:' header and each warning line printed."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"job_id": "job-rm-warn", "status": "RUNNING"})
    done_result = {
        "processed": 5,
        "updated": 3,
        "skipped": 0,
        "ts_normalized": 2,
        "warnings": ["file X missing", "source Y inaccessible"],
    }
    get_job_sequence = [_job_response("DONE", result=done_result)]

    with (
        patch("archon_search.cli.collection.httpx.post", return_value=post_resp),
        patch("archon_search.cli._helpers.httpx.get", side_effect=get_job_sequence),
        patch("archon_search.cli._helpers.time.sleep"),
    ):
        result = runner.invoke(
            collection,
            ["reindex-metadata", "my-col", "--wait", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, result.output
    assert "Reindex-metadata complete" in result.output
    assert "warnings:" in result.output
    assert "  - file X missing" in result.output
    assert "  - source Y inaccessible" in result.output


# ---------------------------------------------------------------------------
# test_reindex_metadata_409_fallback_when_json_parse_fails (C1-I-2: except branch)
# ---------------------------------------------------------------------------


def test_reindex_metadata_409_fallback_when_json_parse_fails() -> None:
    """409 where resp.json() raises → fallback 'metadata reindex already in progress', exit 1."""
    runner = CliRunner()
    post_resp = _mock_response(409)
    post_resp.json.side_effect = ValueError("not json")

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp):
        result = runner.invoke(
            collection,
            ["reindex-metadata", "my-col", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "already in progress" in result.output

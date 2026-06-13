"""Tests for CLI export subcommand (Task 8.1)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from archon_search.cli.export_cmd import export_cmd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job_response(job_id: str = "job-123", status: str = "QUEUED", **extra) -> dict:
    return {
        "job_id": job_id,
        "status": status,
        "created_at": "2026-01-01T00:00:00.000000Z",
        "updated_at": "2026-01-01T00:00:00.000000Z",
        "namespace": "default",
        "result": None,
        "error": None,
        "progress": None,
        **extra,
    }


def _mock_post_response(status_code: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    return resp


def _mock_get_response(status_code: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    return resp


# ---------------------------------------------------------------------------
# Unit: test_export_cmd_prints_job_id
# ---------------------------------------------------------------------------


def test_export_cmd_prints_job_id() -> None:
    """Mock HTTP 202 response; command prints job_id and exits 0."""
    runner = CliRunner()
    post_resp = _mock_post_response(202, _job_response("job-abc", "QUEUED"))

    with patch("archon_search.cli.export_cmd.httpx.post", return_value=post_resp) as mock_post:
        result = runner.invoke(export_cmd, ["my-collection", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "job-abc" in result.output
    mock_post.assert_called_once()


# ---------------------------------------------------------------------------
# Unit: test_export_cmd_wait_prints_progress
# ---------------------------------------------------------------------------


def test_export_cmd_wait_prints_progress() -> None:
    """--wait polls GET /jobs/{job_id} and prints progress output."""
    runner = CliRunner()
    job_id = "job-wait-1"
    post_resp = _mock_post_response(202, _job_response(job_id, "QUEUED"))

    poll_running = _job_response(
        job_id,
        "RUNNING",
        progress={"processed": 50, "total": 200, "phase": "writing"},
    )
    poll_done = _job_response(
        job_id,
        "DONE",
        result={"archive_path": "/data/exports/col.tar.gz"},
    )

    get_responses = [
        _mock_get_response(200, poll_running),
        _mock_get_response(200, poll_done),
    ]

    with (
        patch("archon_search.cli.export_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.export_cmd.httpx.get", side_effect=get_responses),
        patch("archon_search.cli.export_cmd.time.sleep"),  # skip actual sleep
    ):
        result = runner.invoke(
            export_cmd, ["my-collection", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 0, result.output
    assert "writing" in result.output
    assert "50" in result.output
    assert "200" in result.output
    assert "/data/exports/col.tar.gz" in result.output


# ---------------------------------------------------------------------------
# Unit: test_export_cmd_wait_exits_1_on_failed
# ---------------------------------------------------------------------------


def test_export_cmd_wait_exits_1_on_failed() -> None:
    """--wait exits with code 1 when job transitions to FAILED."""
    runner = CliRunner()
    job_id = "job-fail-1"
    post_resp = _mock_post_response(202, _job_response(job_id, "QUEUED"))
    poll_failed = _job_response(job_id, "FAILED", error="something went wrong")
    get_resp = _mock_get_response(200, poll_failed)

    with (
        patch("archon_search.cli.export_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.export_cmd.httpx.get", return_value=get_resp),
        patch("archon_search.cli.export_cmd.time.sleep"),
    ):
        result = runner.invoke(
            export_cmd, ["my-collection", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 1
    assert "something went wrong" in result.output or "FAILED" in result.output


# ---------------------------------------------------------------------------
# Unit: test_export_cmd_collection_not_found
# ---------------------------------------------------------------------------


def test_export_cmd_collection_not_found() -> None:
    """Mock 404 from server; command exits 1 with an error message."""
    runner = CliRunner()
    post_resp = _mock_post_response(404, {"detail": "collection not found"})

    with patch("archon_search.cli.export_cmd.httpx.post", return_value=post_resp):
        result = runner.invoke(export_cmd, ["unknown-col", "--api-key", "deadbeef"])

    assert result.exit_code == 1
    assert "404" in result.output or "not found" in result.output.lower()

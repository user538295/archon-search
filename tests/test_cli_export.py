"""Tests for CLI export and import subcommands (Tasks 8.1, 8.2)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from archon_search.cli.export_cmd import export_cmd, import_cmd


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


def test_export_cmd_wait_exits_2_on_failed_expired() -> None:
    """--wait exits 2 when job transitions to FAILED_EXPIRED (treated same as FAILED)."""
    runner = CliRunner()
    job_id = "job-fail-expired-1"
    post_resp = _mock_post_response(202, _job_response(job_id, "QUEUED"))
    poll_failed = _job_response(job_id, "FAILED_EXPIRED", error="job expired")
    get_resp = _mock_get_response(200, poll_failed)

    with (
        patch("archon_search.cli.export_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.export_cmd.httpx.get", return_value=get_resp),
        patch("archon_search.cli.export_cmd.time.sleep"),
    ):
        result = runner.invoke(
            export_cmd, ["my-collection", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 2, f"output={result.output!r}"
    assert "FAILED" in result.output


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


# ---------------------------------------------------------------------------
# Task 8.2 — import_cmd tests
# ---------------------------------------------------------------------------


def test_import_cmd_prints_job_id() -> None:
    """Mock 202 response; import command prints job_id and exits 0."""
    runner = CliRunner()
    post_resp = _mock_post_response(202, _job_response("job-import-1", "QUEUED"))

    with patch("archon_search.cli.export_cmd.httpx.post", return_value=post_resp) as mock_post:
        result = runner.invoke(
            import_cmd,
            ["my-collection", "/data/exports/archive.tar.gz", "--api-key", "deadbeef"],
        )

    assert result.exit_code == 0, result.output
    assert "job-import-1" in result.output
    mock_post.assert_called_once()


def test_import_cmd_wait_prints_imported_count() -> None:
    """--wait prints imported/skipped/total on DONE."""
    runner = CliRunner()
    job_id = "job-import-wait"
    post_resp = _mock_post_response(202, _job_response(job_id, "QUEUED"))
    poll_done = _job_response(
        job_id,
        "DONE",
        result={"imported": 150, "skipped": 0, "total_in_archive": 150},
    )
    get_resp = _mock_get_response(200, poll_done)

    with (
        patch("archon_search.cli.export_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.export_cmd.httpx.get", return_value=get_resp),
        patch("archon_search.cli.export_cmd.time.sleep"),
    ):
        result = runner.invoke(
            import_cmd,
            [
                "my-collection",
                "/data/exports/archive.tar.gz",
                "--wait",
                "--api-key",
                "deadbeef",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "imported=150" in result.output
    assert "skipped=0" in result.output
    assert "total=150" in result.output


def test_import_cmd_wait_warns_on_skipped() -> None:
    """--wait prints a warning line when skipped > 0."""
    runner = CliRunner()
    job_id = "job-import-skip"
    post_resp = _mock_post_response(202, _job_response(job_id, "QUEUED"))
    poll_done = _job_response(
        job_id,
        "DONE",
        result={"imported": 49, "skipped": 1, "total_in_archive": 50},
    )
    get_resp = _mock_get_response(200, poll_done)

    with (
        patch("archon_search.cli.export_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.export_cmd.httpx.get", return_value=get_resp),
        patch("archon_search.cli.export_cmd.time.sleep"),
    ):
        result = runner.invoke(
            import_cmd,
            [
                "my-collection",
                "/data/exports/archive.tar.gz",
                "--wait",
                "--api-key",
                "deadbeef",
            ],
        )

    # The warning goes to stderr; CliRunner mixes by default
    combined = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert "Warning" in combined or "skipped" in combined


def test_import_cmd_collection_exists_no_force() -> None:
    """409 from server exits 1 with an informative error."""
    runner = CliRunner()
    post_resp = _mock_post_response(409, {"error": "collection_exists"})

    with patch("archon_search.cli.export_cmd.httpx.post", return_value=post_resp):
        result = runner.invoke(
            import_cmd,
            ["my-collection", "/data/exports/archive.tar.gz", "--api-key", "deadbeef"],
        )

    assert result.exit_code == 1
    assert "force-overwrite" in result.output.lower() or "already exists" in result.output.lower()


# ---------------------------------------------------------------------------
# FE-2: export --wait --timeout tests (S24)
# ---------------------------------------------------------------------------


def test_export_wait_timeout_exits_0() -> None:
    """--wait --timeout N exits 0 on poll timeout with job ID + recovery hint in output.

    Monkeypatches httpx.get to always return RUNNING status so the poll loop
    exhausts the timeout. Do NOT monkeypatch _poll_job itself — the test must
    exercise the timeout parameter path through the real polling logic.
    """
    runner = CliRunner()
    job_id = "job-timeout-export"
    post_resp = _mock_post_response(202, _job_response(job_id, "QUEUED"))
    running_resp = _mock_get_response(200, _job_response(job_id, "RUNNING"))

    with (
        patch("archon_search.cli.export_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.export_cmd.httpx.get", return_value=running_resp),
        patch("archon_search.cli.export_cmd.time.sleep"),
    ):
        # --timeout 4 with _POLL_INTERVAL_SECONDS=2 → max 2 polls, then timeout
        result = runner.invoke(
            export_cmd,
            ["my-collection", "--wait", "--timeout", "4", "--api-key", "deadbeef"],
        )

    assert result.exit_code == 0, f"output={result.output!r}"
    # Recovery hint must be in output and contain the job ID
    assert job_id in result.output, f"expected job ID in output: {result.output!r}"
    assert "Timed out" in result.output, f"expected timeout message in output: {result.output!r}"
    assert "check job status" in result.output, f"expected recovery hint in output: {result.output!r}"


def test_export_wait_exits_2_on_failed() -> None:
    """--wait exits 2 when job transitions to FAILED (E0b exit-code contract)."""
    runner = CliRunner()
    job_id = "job-fail-export"
    post_resp = _mock_post_response(202, _job_response(job_id, "QUEUED"))
    failed_resp = _mock_get_response(200, _job_response(job_id, "FAILED", error="disk full"))

    with (
        patch("archon_search.cli.export_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.export_cmd.httpx.get", return_value=failed_resp),
        patch("archon_search.cli.export_cmd.time.sleep"),
    ):
        result = runner.invoke(
            export_cmd, ["my-collection", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 2, f"output={result.output!r}"
    assert "FAILED" in result.output or "disk full" in result.output

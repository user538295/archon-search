"""Tests for ``archon-search backup`` CLI group (D2 Task 5.1)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from click.testing import CliRunner

from archon_search.cli.backup_cmd import backup_cmd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(status_code: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    return resp


def _job_payload(job_id: str, status: str, **extra) -> dict:
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


# ---------------------------------------------------------------------------
# --now: trigger
# ---------------------------------------------------------------------------


def test_backup_now_prints_job_ids() -> None:
    runner = CliRunner()
    body = {"queued": ["job-a", "job-b"], "skipped": []}
    post_resp = _mock_response(202, body)

    with patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp):
        result = runner.invoke(backup_cmd, ["--now", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "job-a" in result.output
    assert "job-b" in result.output


def test_backup_now_prints_skipped() -> None:
    runner = CliRunner()
    body = {
        "queued": [],
        "skipped": [
            {"collection": "docs", "reason": "excluded"},
            {"collection": "logs", "reason": "already_queued"},
        ],
    }
    post_resp = _mock_response(202, body)

    with patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp):
        result = runner.invoke(backup_cmd, ["--now", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "docs" in result.output
    assert "excluded" in result.output
    assert "logs" in result.output
    assert "already_queued" in result.output


def test_backup_now_wait_polls_until_done() -> None:
    runner = CliRunner()
    post_resp = _mock_response(202, {"queued": ["job-w"], "skipped": []})
    get_responses = [
        _mock_response(200, _job_payload("job-w", "QUEUED")),
        _mock_response(200, _job_payload("job-w", "RUNNING")),
        _mock_response(200, _job_payload("job-w", "DONE")),
    ]
    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", side_effect=get_responses),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 0, result.output
    assert "completed" in result.output.lower()


def test_backup_now_wait_exits_2_on_failed() -> None:
    """E0b: exit code on FAILED changed from 1 → 2."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"queued": ["job-f"], "skipped": []})
    get_resp = _mock_response(200, _job_payload("job-f", "FAILED", error="boom"))
    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", return_value=get_resp),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 2
    assert "FAILED" in result.output or "boom" in result.output


def test_backup_bare_prints_help() -> None:
    runner = CliRunner()
    result = runner.invoke(backup_cmd, [])
    assert result.exit_code == 0
    assert "Usage" in result.output or "backup" in result.output.lower()


# ---------------------------------------------------------------------------
# status subcommand
# ---------------------------------------------------------------------------


def test_backup_status_offline(tmp_path: Path) -> None:
    """No state file, no server → prints disabled and zero collections."""
    runner = CliRunner()

    with (
        patch("archon_search.cli.backup_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.backup_cmd.httpx.get",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(backup_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "Backup" in out


def test_backup_status_with_state_file(tmp_path: Path) -> None:
    state_file = tmp_path / ".backup-state.json"
    state_file.write_text(
        json.dumps({"default/docs": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    # Create a fake archive file under backups/default/
    backups_dir = tmp_path / "backups" / "default"
    backups_dir.mkdir(parents=True)
    (backups_dir / "docs.backup.20260101T000000Z.tar.gz").write_bytes(b"")

    runner = CliRunner()
    with (
        patch("archon_search.cli.backup_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.backup_cmd.httpx.get",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(backup_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "docs" in result.output
    assert "2026-01-01" in result.output


def test_backup_status_json_flag(tmp_path: Path) -> None:
    state_file = tmp_path / ".backup-state.json"
    state_file.write_text(
        json.dumps({"default/docs": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    runner = CliRunner()
    with (
        patch("archon_search.cli.backup_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.backup_cmd.httpx.get",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(
            backup_cmd, ["status", "--json", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "enabled" in payload
    assert "collection_status" in payload


def test_backup_status_server_unavailable_degrades_gracefully(tmp_path: Path) -> None:
    runner = CliRunner()
    with (
        patch("archon_search.cli.backup_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.backup_cmd.httpx.get",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(backup_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0
    assert "server unavailable" in result.output.lower()


# ---------------------------------------------------------------------------
# FE-2: backup --now --wait --timeout tests (S25)
# ---------------------------------------------------------------------------


def test_backup_wait_timeout_exits_0() -> None:
    """--now --wait --timeout N exits 0 on poll timeout with job IDs + recovery hint in output.

    Monkeypatches httpx.get to always return RUNNING so the poll loop exhausts
    the timeout. Do NOT monkeypatch _wait_for_jobs itself — the test exercises
    the timeout parameter through the real polling function.
    """
    runner = CliRunner()
    job_id = "job-timeout-backup"
    post_resp = _mock_response(202, {"queued": [job_id], "skipped": []})
    running_resp = _mock_response(200, _job_payload(job_id, "RUNNING"))

    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", return_value=running_resp),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        # --timeout 4 with _POLL_INTERVAL_SECONDS=2 → max 2 poll rounds, then timeout
        result = runner.invoke(
            backup_cmd,
            ["--now", "--wait", "--timeout", "4", "--api-key", "deadbeef"],
        )

    assert result.exit_code == 0, f"output={result.output!r}"
    # Recovery hint must appear in output and include the job ID
    assert job_id in result.output, f"expected job ID in output: {result.output!r}"
    assert "Timed out" in result.output, f"expected timeout message in output: {result.output!r}"
    assert "poll with" in result.output, f"expected recovery hint in output: {result.output!r}"


def test_backup_wait_exits_2_on_failed() -> None:
    """--now --wait exits 2 when any backup job transitions to FAILED (E0b contract)."""
    runner = CliRunner()
    job_id = "job-fail-backup"
    post_resp = _mock_response(202, {"queued": [job_id], "skipped": []})
    failed_resp = _mock_response(200, _job_payload(job_id, "FAILED", error="storage error"))

    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", return_value=failed_resp),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 2, f"output={result.output!r}"
    assert "FAILED" in result.output or "storage error" in result.output


def test_backup_wait_exits_2_on_failed_expired() -> None:
    """--now --wait exits 2 when any backup job transitions to FAILED_EXPIRED (treated same as FAILED)."""
    runner = CliRunner()
    job_id = "job-fail-expired-backup"
    post_resp = _mock_response(202, {"queued": [job_id], "skipped": []})
    failed_expired_resp = _mock_response(200, _job_payload(job_id, "FAILED_EXPIRED", error="job expired"))

    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", return_value=failed_expired_resp),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 2, f"output={result.output!r}"
    assert "FAILED" in result.output


def test_backup_status_uses_server_data_when_reachable(tmp_path: Path) -> None:
    server_payload = {
        "backup": {
            "enabled": True,
            "interval_hours": 24,
            "last_tick_at": "2026-01-02T10:00:00+00:00",
            "next_run_at": "2026-01-03T10:00:00+00:00",
            "collections_excluded": [],
            "collection_status": [
                {
                    "collection": "docs",
                    "last_backup_at": "2026-01-02T09:00:00+00:00",
                    "archive_count": 3,
                }
            ],
        }
    }
    runner = CliRunner()
    with (
        patch("archon_search.cli.backup_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.backup_cmd.httpx.get",
            return_value=_mock_response(200, server_payload),
        ),
    ):
        result = runner.invoke(backup_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "docs" in result.output
    assert "2026-01-02" in result.output

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


def test_backup_now_prints_collection_alongside_job_id() -> None:
    """New format: each queued item shows 'collection → job_id' (brief 270)."""
    runner = CliRunner()
    body = {
        "queued": [
            {"collection": "my_docs", "job_id": "job-a"},
            {"collection": "project_code", "job_id": "job-b"},
        ],
        "skipped": [],
    }
    post_resp = _mock_response(202, body)

    with patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp):
        result = runner.invoke(backup_cmd, ["--now", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "  my_docs → job-a" in result.output
    assert "  project_code → job-b" in result.output


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


def test_backup_now_no_jobs_queued() -> None:
    """Empty queued list prints 'No jobs queued.' and exits 0."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"queued": [], "skipped": []})
    with patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp):
        result = runner.invoke(backup_cmd, ["--now", "--api-key", "deadbeef"])
    assert result.exit_code == 0, result.output
    assert "No jobs queued." in result.output


def test_backup_now_server_error_exits_1() -> None:
    """Non-202 response from POST /backup/trigger exits 1 with server status code."""
    runner = CliRunner()
    with patch(
        "archon_search.cli.backup_cmd.httpx.post",
        return_value=_mock_response(500, {"detail": "internal error"}),
    ):
        result = runner.invoke(backup_cmd, ["--now", "--api-key", "deadbeef"])
    assert result.exit_code == 1, result.output
    assert "500" in result.output


def test_backup_now_connect_error_exits_1() -> None:
    """ConnectError on POST /backup/trigger exits 1 (server not running)."""
    runner = CliRunner()
    with patch(
        "archon_search.cli.backup_cmd.httpx.post",
        side_effect=httpx.ConnectError("refused"),
    ):
        result = runner.invoke(backup_cmd, ["--now", "--api-key", "deadbeef"])
    assert result.exit_code == 1, result.output


def test_backup_now_wait_polls_until_done() -> None:
    runner = CliRunner()
    post_resp = _mock_response(202, {"queued": [{"collection": "docs", "job_id": "job-w"}], "skipped": []})
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


def test_backup_wait_shows_collection_name_on_done() -> None:
    """--wait output uses collection name, not job_id, for done status (brief 270)."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {"queued": [{"collection": "my_docs", "job_id": "job-w"}], "skipped": []},
    )
    get_responses = [_mock_response(200, _job_payload("job-w", "DONE"))]
    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", side_effect=get_responses),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )
    assert result.exit_code == 0, result.output
    assert "my_docs: DONE" in result.output
    assert "completed" in result.output.lower()


def test_backup_now_wait_exits_2_on_failed() -> None:
    """E0b: exit code on FAILED changed from 1 → 2."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"queued": [{"collection": "docs", "job_id": "job-f"}], "skipped": []})
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


def test_backup_wait_shows_collection_name_on_failed() -> None:
    """--wait FAILED output uses collection name (brief 270)."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {"queued": [{"collection": "my_docs", "job_id": "job-f"}], "skipped": []},
    )
    get_resp = _mock_response(200, _job_payload("job-f", "FAILED", error="disk full"))
    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", return_value=get_resp),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )
    assert result.exit_code == 2
    assert "my_docs" in result.output
    assert "disk full" in result.output


def test_backup_wait_shows_collection_name_on_cancelled() -> None:
    """--wait CANCELLED output uses collection name (brief 270)."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {"queued": [{"collection": "my_docs", "job_id": "job-c"}], "skipped": []},
    )
    get_resp = _mock_response(200, _job_payload("job-c", "CANCELLED"))
    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", return_value=get_resp),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )
    assert result.exit_code == 0, result.output
    assert "my_docs: CANCELLED" in result.output


def test_backup_wait_all_cancelled_final_message() -> None:
    """All-CANCELLED run prints 'Backup finished (N cancelled)' not 'completed' (brief 270)."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {
            "queued": [
                {"collection": "col_a", "job_id": "job-a"},
                {"collection": "col_b", "job_id": "job-b"},
            ],
            "skipped": [],
        },
    )
    responses_by_id = {
        "job-a": _mock_response(200, _job_payload("job-a", "CANCELLED")),
        "job-b": _mock_response(200, _job_payload("job-b", "CANCELLED")),
    }

    def _get_side_effect(url, headers):
        job_id = url.rsplit("/", 1)[-1]
        return responses_by_id[job_id]

    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", side_effect=_get_side_effect),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )
    assert result.exit_code == 0, result.output
    assert "Backup finished (2 collection(s) cancelled)." in result.output
    assert "Backup completed for all collections." not in result.output


def test_backup_wait_done_and_cancelled_final_message() -> None:
    """DONE+CANCELLED mix shows cancelled summary, not 'completed' (brief 270)."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {
            "queued": [
                {"collection": "done_col", "job_id": "job-d"},
                {"collection": "cancel_col", "job_id": "job-c"},
            ],
            "skipped": [],
        },
    )
    responses_by_id = {
        "job-d": _mock_response(200, _job_payload("job-d", "DONE")),
        "job-c": _mock_response(200, _job_payload("job-c", "CANCELLED")),
    }

    def _get_side_effect(url, headers):
        job_id = url.rsplit("/", 1)[-1]
        return responses_by_id[job_id]

    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", side_effect=_get_side_effect),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )
    assert result.exit_code == 0, result.output
    assert "done_col: DONE" in result.output
    assert "cancel_col: CANCELLED" in result.output
    assert "Backup finished (1 collection(s) cancelled)." in result.output
    assert "Backup completed for all collections." not in result.output


def test_backup_wait_failed_and_cancelled_exits_2_no_cancelled_summary() -> None:
    """FAILED+CANCELLED: exit 2 (failed wins), no 'Backup finished' summary printed."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {
            "queued": [
                {"collection": "fail_col", "job_id": "job-f"},
                {"collection": "cancel_col", "job_id": "job-c"},
            ],
            "skipped": [],
        },
    )
    responses_by_id = {
        "job-f": _mock_response(200, _job_payload("job-f", "FAILED", error="disk full")),
        "job-c": _mock_response(200, _job_payload("job-c", "CANCELLED")),
    }

    def _get_side_effect(url, headers):
        job_id = url.rsplit("/", 1)[-1]
        return responses_by_id[job_id]

    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", side_effect=_get_side_effect),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )
    assert result.exit_code == 2, f"output={result.output!r}"
    assert "fail_col: FAILED" in result.output
    assert "Backup finished" not in result.output
    assert "Backup completed for all collections." not in result.output


def test_backup_wait_multi_collection_one_done_one_failed() -> None:
    """Two collections queued: one DONE, one FAILED — exit 2, both collection names in output."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {
            "queued": [
                {"collection": "col_a", "job_id": "job-a"},
                {"collection": "col_b", "job_id": "job-b"},
            ],
            "skipped": [],
        },
    )
    get_done = _mock_response(200, _job_payload("job-a", "DONE"))
    get_failed = _mock_response(200, _job_payload("job-b", "FAILED", error="write error"))
    # side_effect cycles: first call is for one job, second for the other (order may vary)
    # Use side_effect as a dict-keyed mock via a helper
    responses_by_id = {"job-a": get_done, "job-b": get_failed}

    def _get_side_effect(url, headers):
        job_id = url.rsplit("/", 1)[-1]
        return responses_by_id[job_id]

    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", side_effect=_get_side_effect),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )
    assert result.exit_code == 2, f"output={result.output!r}"
    assert "col_b" in result.output, "failed collection name must appear"
    assert "write error" in result.output
    assert "col_a: DONE" in result.output  # DONE prints per-collection line
    assert "col_a: FAILED" not in result.output  # col_a did NOT fail


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
    """--now --wait --timeout N exits 0 on poll timeout with recovery hint in output.

    Monkeypatches httpx.get to always return RUNNING so the poll loop exhausts
    the timeout. Do NOT monkeypatch _wait_for_jobs itself — the test exercises
    the timeout parameter through the real polling function.
    """
    runner = CliRunner()
    job_id = "job-timeout-backup"
    post_resp = _mock_response(202, {"queued": [{"collection": "timeout-col", "job_id": job_id}], "skipped": []})
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
    # job_id appears in the queued listing ("  timeout-col → job-timeout-backup"), not the timeout message
    assert job_id in result.output, f"expected job ID in queued output: {result.output!r}"
    # timeout message now shows collection names (not job IDs)
    assert "timeout-col" in result.output, f"expected collection name in timeout output: {result.output!r}"
    assert "Timed out" in result.output, f"expected timeout message in output: {result.output!r}"
    assert "poll with" in result.output, f"expected recovery hint in output: {result.output!r}"


def test_backup_wait_timeout_with_failed_exits_2() -> None:
    """Timeout while a job is already FAILED exits 2, not 0, with failure message."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {
            "queued": [
                {"collection": "fail-col", "job_id": "job-fail"},
                {"collection": "slow-col", "job_id": "job-slow"},
            ],
            "skipped": [],
        },
    )
    # job-fail immediately fails; job-slow keeps running → timeout fires with failed non-empty
    responses_by_id = {
        "job-fail": _mock_response(200, _job_payload("job-fail", "FAILED", error="crash")),
        "job-slow": _mock_response(200, _job_payload("job-slow", "RUNNING")),
    }

    def _get_side_effect(url, headers):
        job_id = url.rsplit("/", 1)[-1]
        return responses_by_id[job_id]

    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", side_effect=_get_side_effect),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--timeout", "1", "--api-key", "deadbeef"]
        )
    assert result.exit_code == 2, f"output={result.output!r}"
    assert "Some backup jobs failed before timeout" in result.output


def test_backup_wait_exits_2_on_failed() -> None:
    """--now --wait exits 2 when any backup job transitions to FAILED (E0b contract)."""
    runner = CliRunner()
    job_id = "job-fail-backup"
    post_resp = _mock_response(202, {"queued": [{"collection": "fail-col", "job_id": job_id}], "skipped": []})
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
    post_resp = _mock_response(202, {"queued": [{"collection": "expired-col", "job_id": job_id}], "skipped": []})
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
    assert "expired-col" in result.output


def test_backup_wait_http_error_during_poll_exits_1() -> None:
    """Non-connect HTTPError during job polling exits 1 with collection name in message."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {"queued": [{"collection": "my_docs", "job_id": "job-x"}], "skipped": []},
    )
    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch(
            "archon_search.cli.backup_cmd.httpx.get",
            side_effect=httpx.ReadTimeout("slow"),
        ),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )
    assert result.exit_code == 1, result.output
    assert "my_docs" in result.output


def test_backup_wait_connect_error_during_poll_exits_1() -> None:
    """ConnectError during job polling exits 1 (uses server-not-running message)."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {"queued": [{"collection": "my_docs", "job_id": "job-x"}], "skipped": []},
    )
    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch(
            "archon_search.cli.backup_cmd.httpx.get",
            side_effect=httpx.ConnectError("refused"),
        ),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )
    assert result.exit_code == 1, result.output


def test_backup_wait_non200_poll_response_exits_1() -> None:
    """Non-200 from GET /jobs/{id} during polling exits 1 with collection name."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {"queued": [{"collection": "my_docs", "job_id": "job-x"}], "skipped": []},
    )
    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch(
            "archon_search.cli.backup_cmd.httpx.get",
            return_value=_mock_response(500, {}),
        ),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(
            backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"]
        )
    assert result.exit_code == 1, result.output
    assert "my_docs" in result.output


# ---------------------------------------------------------------------------
# Brief 280: --wait progress line
# ---------------------------------------------------------------------------


def test_backup_wait_progress_line_appears_while_pending() -> None:
    """Each poll cycle prints 'Backing up... (N/total complete)' while jobs are pending."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {"queued": [{"collection": "docs", "job_id": "job-w"}], "skipped": []},
    )
    get_responses = [
        _mock_response(200, _job_payload("job-w", "RUNNING")),
        _mock_response(200, _job_payload("job-w", "DONE")),
    ]
    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", side_effect=get_responses),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "Backing up... (0/1 complete)" in result.output


def test_backup_wait_progress_line_multi_collection() -> None:
    """With 2 collections, progress line tracks partial completion: (1/2 complete)."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {
            "queued": [
                {"collection": "col_a", "job_id": "job-a"},
                {"collection": "col_b", "job_id": "job-b"},
            ],
            "skipped": [],
        },
    )
    responses_by_id: dict[str, list] = {
        "job-a": [
            _mock_response(200, _job_payload("job-a", "DONE")),
        ],
        "job-b": [
            _mock_response(200, _job_payload("job-b", "RUNNING")),
            _mock_response(200, _job_payload("job-b", "DONE")),
        ],
    }

    def _get_side_effect(url, headers):
        job_id = url.rsplit("/", 1)[-1]
        resps = responses_by_id[job_id]
        return resps.pop(0) if len(resps) > 1 else resps[0]

    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", side_effect=_get_side_effect),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    # After col_a finishes but col_b is still running, 1/2 shown
    assert "Backing up... (1/2 complete)" in result.output


def test_backup_wait_no_progress_line_when_all_done_first_poll() -> None:
    """No progress line when all jobs complete in the first poll (no sleep needed)."""
    runner = CliRunner()
    post_resp = _mock_response(
        202,
        {"queued": [{"collection": "docs", "job_id": "job-w"}], "skipped": []},
    )
    get_responses = [_mock_response(200, _job_payload("job-w", "DONE"))]
    with (
        patch("archon_search.cli.backup_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.backup_cmd.httpx.get", side_effect=get_responses),
        patch("archon_search.cli.backup_cmd.time.sleep"),
    ):
        result = runner.invoke(backup_cmd, ["--now", "--wait", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "Backing up..." not in result.output


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


def test_backup_status_uses_server_namespace_when_returned(tmp_path: Path) -> None:
    """CLI uses namespace from server response, not the hardcoded 'default'."""
    server_payload = {
        "backup": {
            "enabled": True,
            "interval_hours": 24,
            "last_tick_at": None,
            "next_run_at": None,
            "collections_excluded": [],
            "collection_status": [
                {
                    "collection": "docs",
                    "namespace": "team-a",
                    "last_backup_at": None,
                    "archive_count": 0,
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
    # Non-default namespace must be shown as a prefix.
    assert "team-a/docs" in result.output


def test_backup_status_namespace_fallback_when_field_absent(tmp_path: Path) -> None:
    """If server omits namespace field, CLI falls back to 'default' (backward compat)."""
    server_payload = {
        "backup": {
            "enabled": True,
            "interval_hours": 24,
            "last_tick_at": None,
            "next_run_at": None,
            "collections_excluded": [],
            "collection_status": [
                {
                    "collection": "docs",
                    # no namespace field — old server
                    "last_backup_at": None,
                    "archive_count": 0,
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
    # Default namespace is not printed as a prefix.
    assert "docs" in result.output
    assert "team-a" not in result.output


def test_backup_status_json_includes_namespace(tmp_path: Path) -> None:
    """--json output includes namespace in each collection_status entry (brief 290)."""
    server_payload = {
        "backup": {
            "enabled": True,
            "interval_hours": 24,
            "last_tick_at": None,
            "next_run_at": None,
            "collections_excluded": [],
            "collection_status": [
                {
                    "collection": "docs",
                    "namespace": "team-a",
                    "last_backup_at": None,
                    "archive_count": 0,
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
        result = runner.invoke(
            backup_cmd, ["status", "--json", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    entry = payload["collection_status"][0]
    assert entry["namespace"] == "team-a"


def test_backup_status_offline_non_default_namespace(tmp_path: Path) -> None:
    """Offline path shows correct namespace prefix for non-default namespaces."""
    state_file = tmp_path / ".backup-state.json"
    state_file.write_text(
        json.dumps({"team-b/reports": "2026-03-01T00:00:00+00:00"}),
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
        result = runner.invoke(backup_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "team-b/reports" in result.output

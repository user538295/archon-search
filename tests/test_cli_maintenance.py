"""Tests for ``archon-search maintenance`` CLI group (D5 FE-1).

Covers:
- maintenance status subcommand (offline + JSON flag)
- maintenance run subcommand (trigger + --wait polling)
- main.py registration
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from archon_search.cli.maintenance_cmd import maintenance_cmd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(status_code: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.text = json.dumps(body or {})
    return resp


def _make_state_file(tmp_path: Path, **overrides: object) -> Path:
    """Write a minimal `.maintenance-state.json` and return its path."""
    state: dict = {
        "last_run_at": "2026-06-21T10:00:00+00:00",
        "next_run_at": "2026-06-22T10:00:00+00:00",
        "collection_health": {
            "default/docs": {
                "fts_optimized_at": "2026-06-21T10:00:01+00:00",
                "orphans_removed_last_run": 0,
                "last_retry_at": None,
                "last_error": None,
                "meta_chunk_count": 42,
            }
        },
        "retry_counts": {},
    }
    state.update(overrides)
    state_file = tmp_path / ".maintenance-state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")
    return state_file


def _status_server_payload(
    enabled: bool = True,
    last_run_at: str | None = "2026-06-21T10:00:00+00:00",
    next_run_at: str | None = "2026-06-22T10:00:00+00:00",
) -> dict:
    return {
        "maintenance": {
            "enabled": enabled,
            "interval_hours": 24,
            "last_run_at": last_run_at,
            "next_run_at": next_run_at,
            "collection_health": [
                {
                    "collection": "default/docs",
                    "fts_optimized_at": "2026-06-21T10:00:01+00:00",
                    "orphans_removed_last_run": 0,
                    "last_retry_at": None,
                    "last_error": None,
                    "mutations_since_recompute": 0,
                    "centroid_recompute_threshold": 200,
                    "meta_chunk_count": 42,
                }
            ],
        }
    }


# ---------------------------------------------------------------------------
# status subcommand — offline (state file)
# ---------------------------------------------------------------------------


def test_maintenance_status_offline(tmp_path: Path) -> None:
    """State file present, server unreachable → shows table, exit 0 (S25)."""
    _make_state_file(tmp_path)
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "2026-06-21" in result.output
    assert "docs" in result.output


def test_maintenance_status_no_state_file(tmp_path: Path) -> None:
    """No state file → prints 'no maintenance history', exit 0."""
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "no maintenance history" in result.output.lower()


def test_maintenance_status_json_flag(tmp_path: Path) -> None:
    """--json flag → output is valid JSON with last_run_at key."""
    _make_state_file(tmp_path)
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(
            maintenance_cmd, ["status", "--json", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "last_run_at" in payload


def test_maintenance_status_server_unavailable_graceful(tmp_path: Path) -> None:
    """Server unavailable → 'server unavailable' in output, exit 0."""
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0
    assert "server unavailable" in result.output.lower()


def test_maintenance_status_uses_server_data_when_reachable(tmp_path: Path) -> None:
    """When server is reachable, server data is shown."""
    _make_state_file(tmp_path)
    runner = CliRunner()
    server_payload = _status_server_payload()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(200, server_payload),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "docs" in result.output
    assert "2026-06-21" in result.output


def test_maintenance_status_corrupt_state_file_graceful(tmp_path: Path) -> None:
    """Corrupt state file → graceful fallback (no crash)."""
    state_file = tmp_path / ".maintenance-state.json"
    state_file.write_text("NOT_VALID_JSON", encoding="utf-8")
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0
    # No traceback in output
    assert "Traceback" not in result.output


def test_maintenance_status_with_errors_in_collection_health(tmp_path: Path) -> None:
    """Collection health with last_error shows error text."""
    _make_state_file(
        tmp_path,
        collection_health={
            "default/docs": {
                "fts_optimized_at": None,
                "orphans_removed_last_run": 0,
                "last_retry_at": None,
                "last_error": "FTS index not found",
                "meta_chunk_count": 10,
            }
        },
    )
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "FTS index not found" in result.output


# ---------------------------------------------------------------------------
# run subcommand — trigger
# ---------------------------------------------------------------------------


def test_maintenance_run_triggers_and_exits(tmp_path: Path) -> None:
    """maintenance run → POST /maintenance/trigger, prints 'triggered', exits immediately (S26)."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"status": "triggered"})
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch("archon_search.cli.maintenance_cmd.httpx.post", return_value=post_resp),
    ):
        result = runner.invoke(maintenance_cmd, ["run", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "triggered" in result.output.lower()


def test_maintenance_run_already_triggered(tmp_path: Path) -> None:
    """maintenance run when already_triggered → still exit 0 with informative message."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"status": "already_triggered"})
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch("archon_search.cli.maintenance_cmd.httpx.post", return_value=post_resp),
    ):
        result = runner.invoke(maintenance_cmd, ["run", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    # should mention already triggered or similar
    assert result.output.strip() != ""


def test_maintenance_run_connection_error(tmp_path: Path) -> None:
    """httpx.ConnectError on POST → exit 1 (S26 error path)."""
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.post",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["run", "--api-key", "deadbeef"])

    assert result.exit_code == 1


def test_maintenance_run_server_error(tmp_path: Path) -> None:
    """POST returns 500 → exit 1."""
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.post",
            return_value=_mock_response(500, {"detail": "Internal Server Error"}),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["run", "--api-key", "deadbeef"])

    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# run subcommand — --wait polling
# ---------------------------------------------------------------------------


def test_maintenance_run_wait_polls_until_last_run_at_changes(tmp_path: Path) -> None:
    """--wait: polls GET /status until last_run_at changes (S27)."""
    runner = CliRunner()
    old_run_at = "2026-06-20T10:00:00+00:00"
    new_run_at = "2026-06-21T11:00:00+00:00"

    post_resp = _mock_response(202, {"status": "triggered"})
    # first GET returns old; second returns new
    get_responses = [
        _mock_response(200, _status_server_payload(last_run_at=old_run_at)),
        _mock_response(200, _status_server_payload(last_run_at=old_run_at)),
        _mock_response(200, _status_server_payload(last_run_at=new_run_at)),
    ]
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch("archon_search.cli.maintenance_cmd.httpx.post", return_value=post_resp),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=get_responses,
        ),
        patch("archon_search.cli.maintenance_cmd.time.sleep"),
    ):
        result = runner.invoke(
            maintenance_cmd, ["run", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 0, result.output
    assert new_run_at in result.output or "complete" in result.output.lower()


def test_maintenance_run_wait_timeout(tmp_path: Path) -> None:
    """--wait: exits with non-zero exit code after max polls without change."""
    runner = CliRunner()
    old_run_at = "2026-06-20T10:00:00+00:00"

    post_resp = _mock_response(202, {"status": "triggered"})
    # GET always returns the same last_run_at — never changes
    get_resp = _mock_response(200, _status_server_payload(last_run_at=old_run_at))

    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch("archon_search.cli.maintenance_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.maintenance_cmd.httpx.get", return_value=get_resp),
        patch("archon_search.cli.maintenance_cmd.time.sleep"),
        # override max polls to a small value
        patch("archon_search.cli.maintenance_cmd._WAIT_MAX_POLLS", 3),
    ):
        result = runner.invoke(
            maintenance_cmd, ["run", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code != 0


def test_maintenance_run_wait_server_error_mid_poll(tmp_path: Path) -> None:
    """--wait: mid-poll auth error (4xx) → exit 1 (fatal path).

    5xx errors are transient and loop-continue; 4xx errors are fatal.
    This test exercises the fatal path using a 401 response.

    GET call count breakdown (with _WAIT_MAX_POLLS=3):
      1 pre-POST GET (returns old_run_at — captures baseline)
      1 loop GET (returns 401 — fatal, exits immediately)
      total = 2 responses needed
    """
    runner = CliRunner()
    old_run_at = "2026-06-20T10:00:00+00:00"

    post_resp = _mock_response(202, {"status": "triggered"})
    get_responses = [
        _mock_response(200, _status_server_payload(last_run_at=old_run_at)),
        _mock_response(401, {}),
    ]
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch("archon_search.cli.maintenance_cmd.httpx.post", return_value=post_resp),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=get_responses,
        ),
        patch("archon_search.cli.maintenance_cmd.time.sleep"),
        patch("archon_search.cli.maintenance_cmd._WAIT_MAX_POLLS", 3),
    ):
        result = runner.invoke(
            maintenance_cmd, ["run", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 1


def test_maintenance_run_wait_maintenance_null(tmp_path: Path) -> None:
    """--wait: GET /status returns maintenance=null → no crash, informative message.

    GET call count breakdown (with _WAIT_MAX_POLLS=2):
      1 GET before the POST (capture original_last_run_at)
      2 GETs inside the polling loop
      total = 3 responses needed
    """
    runner = CliRunner()
    post_resp = _mock_response(202, {"status": "triggered"})
    # GET returns a valid payload but with maintenance=null
    get_responses = [
        _mock_response(200, {"maintenance": None}),
        _mock_response(200, {"maintenance": None}),
        _mock_response(200, {"maintenance": None}),
    ]
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch("archon_search.cli.maintenance_cmd.httpx.post", return_value=post_resp),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=get_responses,
        ),
        patch("archon_search.cli.maintenance_cmd.time.sleep"),
        # _WAIT_MAX_POLLS=2 → 1 pre-POST GET + 2 loop GETs = 3 total responses
        patch("archon_search.cli.maintenance_cmd._WAIT_MAX_POLLS", 2),
    ):
        result = runner.invoke(
            maintenance_cmd, ["run", "--wait", "--api-key", "deadbeef"]
        )

    # Should not crash with an exception
    assert "Traceback" not in result.output
    assert result.exit_code != 0  # timed out or gave up


def test_maintenance_run_wait_first_run_success(tmp_path: Path) -> None:
    """--wait: first-ever run where last_run_at starts as None → succeeds when it appears.

    GET call count breakdown (with _WAIT_MAX_POLLS=3):
      1 pre-POST GET (returns last_run_at=None — first-ever run)
      1 loop GET (returns new timestamp — pass completed)
      total = 2 responses needed
    """
    runner = CliRunner()
    new_run_at = "2026-06-21T12:00:00+00:00"

    post_resp = _mock_response(202, {"status": "triggered"})
    get_responses = [
        # pre-POST: first-ever run, no prior last_run_at
        _mock_response(200, _status_server_payload(last_run_at=None)),
        # loop iteration 1: pass completed
        _mock_response(200, _status_server_payload(last_run_at=new_run_at)),
    ]
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch("archon_search.cli.maintenance_cmd.httpx.post", return_value=post_resp),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=get_responses,
        ),
        patch("archon_search.cli.maintenance_cmd.time.sleep"),
        patch("archon_search.cli.maintenance_cmd._WAIT_MAX_POLLS", 3),
    ):
        result = runner.invoke(
            maintenance_cmd, ["run", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 0, result.output
    assert new_run_at in result.output or "complete" in result.output.lower()


# ---------------------------------------------------------------------------
# main.py registration
# ---------------------------------------------------------------------------


def test_main_help_lists_maintenance() -> None:
    """'maintenance' appears in archon-search --help output."""
    from archon_search.cli.main import main

    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "maintenance" in result.output


def test_main_maintenance_subgroup_has_status_and_run() -> None:
    """archon-search maintenance --help shows status and run."""
    from archon_search.cli.main import main

    result = CliRunner().invoke(main, ["maintenance", "--help"])
    assert result.exit_code == 0
    assert "status" in result.output
    assert "run" in result.output

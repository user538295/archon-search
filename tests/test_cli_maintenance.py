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
    model_validation: dict | None = None,
) -> dict:
    payload: dict = {
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
    if model_validation is not None:
        payload["model_validation"] = model_validation
    return payload


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
# status subcommand — model_validation rendering (D6 FE-2)
# ---------------------------------------------------------------------------


def test_maintenance_status_renders_model_validation(tmp_path: Path) -> None:
    """Status payload with model_validation → output shows embedder/reranker (S13)."""
    runner = CliRunner()
    server_payload = _status_server_payload(
        model_validation={
            "embedder_ok": True,
            "reranker_ok": False,
            "provider_warnings": ["CoreMLExecutionProvider unavailable"],
            "validated_at": "2026-06-22T10:00:00Z",
        }
    )
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(200, server_payload),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "embedder_ok: yes" in result.output
    assert "reranker_ok: no" in result.output
    assert "CoreMLExecutionProvider unavailable" in result.output


def test_maintenance_status_renders_pending_probes(tmp_path: Path) -> None:
    """Null probe values (validation still running) → 'pending' rendered (C1-COV-1/2)."""
    runner = CliRunner()
    server_payload = _status_server_payload(
        model_validation={
            "embedder_ok": None,
            "reranker_ok": None,
            "provider_warnings": [],
            "validated_at": None,
        }
    )
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(200, server_payload),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "embedder_ok: pending" in result.output
    assert "reranker_ok: pending" in result.output
    assert "validated_at: pending" in result.output


def test_maintenance_status_renders_multiple_warnings(tmp_path: Path) -> None:
    """Multiple provider_warnings each render on their own line (C1-COV-5)."""
    runner = CliRunner()
    server_payload = _status_server_payload(
        model_validation={
            "embedder_ok": False,
            "reranker_ok": False,
            "provider_warnings": ["warning alpha", "warning beta"],
            "validated_at": "2026-06-22T10:00:00Z",
        }
    )
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(200, server_payload),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "warning alpha" in result.output
    assert "warning beta" in result.output


def test_maintenance_status_model_validation_null_no_crash(tmp_path: Path) -> None:
    """Server sends model_validation=null (real pending shape) → no crash, omitted.

    Unlike the absent-key test, this exercises the ``isinstance(mv, dict)``
    guards with an explicit ``None`` value — the shape the server actually emits
    before background validation completes (C1-COV-3 / C1-EDGE-1).
    """
    runner = CliRunner()
    server_payload = _status_server_payload()
    server_payload["model_validation"] = None  # explicit null, not absent
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(200, server_payload),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "embedder_ok" not in result.output


def test_maintenance_status_no_model_validation_key(tmp_path: Path) -> None:
    """Status payload without model_validation → no crash, section omitted."""
    runner = CliRunner()
    server_payload = _status_server_payload()  # no model_validation key
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(200, server_payload),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["status", "--api-key", "deadbeef"])

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "embedder_ok" not in result.output


def test_maintenance_status_json_includes_model_validation(tmp_path: Path) -> None:
    """--json flag → JSON output includes model_validation with serializable fields."""
    runner = CliRunner()
    server_payload = _status_server_payload(
        model_validation={
            "embedder_ok": True,
            "reranker_ok": True,
            "provider_warnings": [],
            "validated_at": "2026-06-22T10:00:00Z",
        }
    )
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(200, server_payload),
        ),
    ):
        result = runner.invoke(
            maintenance_cmd, ["status", "--json", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)  # must be valid JSON (no raw datetime)
    assert "model_validation" in payload
    mv = payload["model_validation"]
    assert mv["embedder_ok"] is True
    assert mv["reranker_ok"] is True
    assert mv["validated_at"] == "2026-06-22T10:00:00Z"


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
    """httpx.ConnectError on POST → friendly message on stderr, exit 0 (not a program error)."""
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.post",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["run", "--api-key", "deadbeef"])

    assert result.exit_code == 0
    assert "is not running" in result.stderr
    assert "archon-search serve" in result.stderr


def test_maintenance_run_wait_server_not_running(tmp_path: Path) -> None:
    """--wait: ConnectError on pre-flight GET → friendly message on stderr, exit 0."""
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["run", "--wait", "--api-key", "deadbeef"])

    assert result.exit_code == 0
    assert "is not running" in result.stderr
    assert "archon-search serve" in result.stderr


def test_maintenance_run_wait_post_connect_error(tmp_path: Path) -> None:
    """--wait: pre-flight GET succeeds, but POST raises ConnectError → friendly message on stderr, exit 0."""
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(200, _status_server_payload(last_run_at="2026-06-20T10:00:00+00:00")),
        ),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.post",
            side_effect=httpx.ConnectError("nope"),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["run", "--wait", "--api-key", "deadbeef"])

    assert result.exit_code == 0
    assert "is not running" in result.stderr
    assert "archon-search serve" in result.stderr


def test_maintenance_run_wait_preflight_read_timeout_exits_1(tmp_path: Path) -> None:
    """--wait: ReadTimeout on pre-flight GET → error exit 1 (not a connect error)."""
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=httpx.ReadTimeout("timed out"),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["run", "--wait", "--api-key", "deadbeef"])

    assert result.exit_code == 1
    assert "is not running" not in result.stderr
    assert "Error polling server" in result.stderr


def test_maintenance_run_wait_preflight_401_exits_1(tmp_path: Path) -> None:
    """--wait: 401 on pre-flight GET → error exit 1 (auth failure, not server-down)."""
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            return_value=_mock_response(401, {}),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["run", "--wait", "--api-key", "deadbeef"])

    assert result.exit_code == 1
    assert "is not running" not in result.stderr
    assert "server returned 401" in result.stderr


def test_maintenance_run_wait_mid_poll_connect_error_continues(tmp_path: Path) -> None:
    """--wait: ConnectError during mid-poll is transient; polling continues and succeeds.

    GET call count:
      1 pre-flight GET (returns old_run_at — baseline)
      1 loop GET (raises ConnectError — transient, continues)
      1 loop GET (returns new_run_at — success)
      total = 3 responses
    """
    runner = CliRunner()
    old_run_at = "2026-06-20T10:00:00+00:00"
    new_run_at = "2026-06-21T11:00:00+00:00"

    post_resp = _mock_response(202, {"status": "triggered"})
    get_responses = [
        _mock_response(200, _status_server_payload(last_run_at=old_run_at)),
        httpx.ConnectError("transient"),
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
    assert f"Maintenance pass complete. last_run_at={new_run_at}" in result.output
    assert "Timed out" not in result.stderr


def test_maintenance_run_wait_permanent_server_down_mid_poll(tmp_path: Path) -> None:
    """--wait: server dies permanently mid-poll (all polls ConnectError) → timeout exit 0.

    _POLL_INTERVAL_SECONDS=2, --timeout 5 → max_polls = max(1, 5 // 2) = 2 loop polls.
    GET call count:
      1 pre-flight GET (success — baseline)
      2 loop GETs (ConnectError — server permanently down)
      total = 3 responses consumed
    """
    runner = CliRunner()
    old_run_at = "2026-06-20T10:00:00+00:00"
    post_resp = _mock_response(202, {"status": "triggered"})

    get_responses = [
        _mock_response(200, _status_server_payload(last_run_at=old_run_at)),
        httpx.ConnectError("server gone"),
        httpx.ConnectError("server gone"),
        httpx.ConnectError("server gone"),
    ]
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch("archon_search.cli.maintenance_cmd.httpx.post", return_value=post_resp),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.get",
            side_effect=get_responses,
        ) as mock_get,
        patch("archon_search.cli.maintenance_cmd.time.sleep"),
    ):
        result = runner.invoke(
            maintenance_cmd, ["run", "--wait", "--timeout", "5", "--api-key", "deadbeef"]
        )

    # 1 pre-flight GET + 2 loop GETs (both ConnectError) = 3 total.
    # Asserts the ConnectError-continue branch actually ran, not just a generic timeout.
    assert mock_get.call_count == 3
    assert result.exit_code == 0, result.output
    assert "Timed out" in result.stderr
    assert "Maintenance pass complete" not in result.output


def test_maintenance_run_other_http_error_still_exits_1(tmp_path: Path) -> None:
    """Non-connect httpx.HTTPError on POST → raw error + exit 1 (unchanged behaviour)."""
    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch(
            "archon_search.cli.maintenance_cmd.httpx.post",
            side_effect=httpx.ReadTimeout("timed out"),
        ),
    ):
        result = runner.invoke(maintenance_cmd, ["run", "--api-key", "deadbeef"])

    assert result.exit_code == 1
    assert "is not running" not in result.stderr
    assert "Error contacting server" in result.stderr


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
    """--wait: exits 0 (not 1) after timeout with recovery message on stderr (FE-1 S22).

    Breaking change from D5: exit code changed from 1 → 0 on poll timeout.

    GET call count breakdown (--timeout 6, _POLL_INTERVAL_SECONDS=2 → max_polls = 3):
      1 pre-POST GET (returns old_run_at — baseline)
      3 loop GETs (always return old_run_at — never changes)
      total = 4 responses needed (return_value covers all)
    """
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
    ):
        result = runner.invoke(
            maintenance_cmd, ["run", "--wait", "--timeout", "6", "--api-key", "deadbeef"]
        )

    # FE-1: timeout exits 0 (was 1), recovery message on stderr
    assert result.exit_code == 0
    assert "archon-search maintenance status" in result.stderr


def test_maintenance_run_wait_server_error_mid_poll(tmp_path: Path) -> None:
    """--wait: mid-poll auth error (4xx) → exit 1 (fatal path).

    5xx errors are transient and loop-continue; 4xx errors are fatal.
    This test exercises the fatal path using a 401 response.

    GET call count breakdown:
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
    ):
        result = runner.invoke(
            maintenance_cmd, ["run", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 1


def test_wait_for_pass_exits_0_on_timeout(tmp_path: Path) -> None:
    """_wait_for_pass: exhausting timeout exits 0 with recovery message on stderr.

    Monkeypatches httpx.get inside _wait_for_pass to always return a running
    status (last_run_at never changes). Does NOT monkeypatch _wait_for_pass
    itself (per plan spec). Verifies SystemExit(0) is raised with a recovery
    message on stderr containing 'archon-search maintenance status'.
    """
    old_run_at = "2026-06-20T10:00:00+00:00"
    post_resp = _mock_response(202, {"status": "triggered"})
    get_resp = _mock_response(200, _status_server_payload(last_run_at=old_run_at))

    runner = CliRunner()
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch("archon_search.cli.maintenance_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.maintenance_cmd.httpx.get", return_value=get_resp),
        patch("archon_search.cli.maintenance_cmd.time.sleep"),
    ):
        result = runner.invoke(
            maintenance_cmd,
            ["run", "--wait", "--timeout", "4", "--api-key", "deadbeef"],
        )

    assert result.exit_code == 0
    assert "archon-search maintenance status" in (result.output + (result.stderr or ""))


def test_maintenance_run_wait_timeout_option_accepted(tmp_path: Path) -> None:
    """--wait --timeout N: option is accepted; timeout exits 0 with job reference on stderr (S22)."""
    runner = CliRunner()
    old_run_at = "2026-06-20T10:00:00+00:00"

    post_resp = _mock_response(202, {"status": "triggered"})
    get_resp = _mock_response(200, _status_server_payload(last_run_at=old_run_at))

    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch("archon_search.cli.maintenance_cmd.httpx.post", return_value=post_resp),
        patch("archon_search.cli.maintenance_cmd.httpx.get", return_value=get_resp),
        patch("archon_search.cli.maintenance_cmd.time.sleep"),
    ):
        result = runner.invoke(
            maintenance_cmd,
            ["run", "--wait", "--timeout", "5", "--api-key", "deadbeef"],
        )

    assert result.exit_code == 0, result.output
    # stderr should contain "archon-search maintenance status" as recovery hint
    assert "archon-search maintenance status" in result.stderr


def test_maintenance_run_wait_exits_2_on_failed(tmp_path: Path) -> None:
    """--wait: poll detects FAILED pass (collection health last_error non-null) → exit 2 (S23)."""
    runner = CliRunner()
    old_run_at = "2026-06-20T10:00:00+00:00"
    new_run_at = "2026-06-21T11:00:00+00:00"

    post_resp = _mock_response(202, {"status": "triggered"})

    failed_payload = {
        "maintenance": {
            "enabled": True,
            "interval_hours": 24,
            "last_run_at": new_run_at,
            "next_run_at": None,
            "collection_health": [
                {
                    "collection": "default/docs",
                    "fts_optimized_at": None,
                    "orphans_removed_last_run": 0,
                    "last_retry_at": None,
                    "last_error": "FTS index rebuild failed: disk full",
                    "mutations_since_recompute": 0,
                    "centroid_recompute_threshold": 200,
                    "meta_chunk_count": 42,
                }
            ],
        }
    }

    get_responses = [
        # pre-POST: capture baseline
        _mock_response(200, _status_server_payload(last_run_at=old_run_at)),
        # loop: last_run_at changed, but a collection has last_error → FAILED
        _mock_response(200, failed_payload),
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
            maintenance_cmd,
            ["run", "--wait", "--api-key", "deadbeef"],
        )

    assert result.exit_code == 2
    assert "archon-search maintenance status" in result.stderr


def test_maintenance_run_timeout_without_wait_is_silently_ignored(tmp_path: Path) -> None:
    """--timeout without --wait: the option is accepted and silently ignored; command triggers and exits 0."""
    runner = CliRunner()
    post_resp = _mock_response(202, {"status": "triggered"})
    with (
        patch("archon_search.cli.maintenance_cmd.get_data_dir", return_value=tmp_path),
        patch("archon_search.cli.maintenance_cmd.httpx.post", return_value=post_resp),
    ):
        result = runner.invoke(
            maintenance_cmd,
            ["run", "--timeout", "30", "--api-key", "deadbeef"],
        )
    assert result.exit_code == 0
    assert "triggered" in result.output.lower()


def test_maintenance_run_wait_5xx_transient_then_success(tmp_path: Path) -> None:
    """--wait: 5xx during polling is transient; retries and succeeds on next poll.

    GET call count breakdown (--timeout 6 → max_polls = 3):
      1 pre-POST GET (returns old_run_at — baseline)
      1 loop GET (returns 500 — transient, retried)
      1 loop GET (returns 200 with new_run_at — success)
      total = 3 responses needed
    """
    runner = CliRunner()
    old_run_at = "2026-06-20T10:00:00+00:00"
    new_run_at = "2026-06-21T11:00:00+00:00"

    post_resp = _mock_response(202, {"status": "triggered"})
    get_responses = [
        _mock_response(200, _status_server_payload(last_run_at=old_run_at)),  # pre-POST baseline
        _mock_response(500, {}),  # transient server error
        _mock_response(200, _status_server_payload(last_run_at=new_run_at)),  # success
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
            maintenance_cmd,
            ["run", "--wait", "--timeout", "6", "--api-key", "deadbeef"],
        )

    assert result.exit_code == 0, result.output
    assert new_run_at in result.output or "complete" in result.output.lower()


def test_maintenance_run_wait_maintenance_null(tmp_path: Path) -> None:
    """--wait: GET /status returns maintenance=null → no crash, informative message.

    GET call count breakdown (with --timeout 4, _POLL_INTERVAL_SECONDS=2):
      max_polls = 4 // 2 = 2 polls in the loop
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
    ):
        result = runner.invoke(
            maintenance_cmd,
            # --timeout 4 → max_polls = 4 // 2 = 2 loop GETs (+ 1 pre-POST = 3 total)
            ["run", "--wait", "--timeout", "4", "--api-key", "deadbeef"],
        )

    # Should not crash with an exception
    assert "Traceback" not in result.output
    # FE-1: timeout now exits 0 (not 1); recovery message on stderr
    assert result.exit_code == 0


def test_maintenance_run_wait_first_run_success(tmp_path: Path) -> None:
    """--wait: first-ever run where last_run_at starts as None → succeeds when it appears.

    GET call count breakdown:
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
    ):
        result = runner.invoke(
            maintenance_cmd, ["run", "--wait", "--api-key", "deadbeef"]
        )

    assert result.exit_code == 0, result.output
    assert f"Maintenance pass complete. last_run_at={new_run_at}" in result.output


# ---------------------------------------------------------------------------
# _get_maintenance_state unit tests
# ---------------------------------------------------------------------------


def test_get_maintenance_state_propagates_connect_error(tmp_path: Path) -> None:
    """_get_maintenance_state re-raises ConnectError so callers can distinguish server-down."""
    from archon_search.cli.maintenance_cmd import _get_maintenance_state

    with patch(
        "archon_search.cli.maintenance_cmd.httpx.get",
        side_effect=httpx.ConnectError("nope"),
    ):
        with pytest.raises(httpx.ConnectError):
            _get_maintenance_state("http://localhost:9999/status", {"Authorization": "Bearer x"})


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

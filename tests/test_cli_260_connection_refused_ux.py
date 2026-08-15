"""Tests for brief-260: friendly "server not running" messages across CLI commands.

Covers commands that previously surfaced raw ConnectError text to the user:
- key create, key list, key revoke, key rotate
- backup --now
- collection migrate (both --apply and dry-run GET paths)

Also verifies that the shared _SERVER_NOT_RUNNING_MSG constant is used by every
affected module so the wording stays consistent.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(status_code: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body or {}
    resp.text = ""
    return resp


_FRIENDLY_PHRASES = ("not running", "start it first")


@pytest.fixture(autouse=True)
def _isolate_liveness_probes():
    """Keep every connect-failure path off the developer's machine.

    ``_server_connect_fail_msg()`` probes ``{base_url}/ready`` with a bare
    ``httpx.get`` and, when that fails, falls back to the managed service. Neither
    is stubbed by the per-command ``httpx.Client`` mocks below, so without this
    fixture the outcome flips with whether a local server or launchd job happens
    to be up. Both are forced into the "unreachable" branch here, which is the
    precondition these tests are written for.
    """
    from archon_search.cli import _helpers

    with (
        patch.object(_helpers.httpx, "get", side_effect=httpx.ConnectError("refused")),
        patch.object(_helpers, "_get_service", side_effect=NotImplementedError),
    ):
        yield

# ---------------------------------------------------------------------------
# key create
# ---------------------------------------------------------------------------


def test_key_create_server_not_running_friendly_message() -> None:
    """ConnectError on POST /keys → friendly message, exit 1."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.side_effect = httpx.ConnectError("Connection refused")
    with patch("archon_search.cli.key_cmd.httpx.Client", return_value=mock_client):
        result = runner.invoke(
            key_cmd,
            ["create", "--namespace", "default", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    output = result.output + (result.stderr if hasattr(result, "stderr") else "")
    assert any(p in output.lower() for p in _FRIENDLY_PHRASES), repr(output)


def test_key_create_server_not_running_no_raw_errno() -> None:
    """ConnectError on POST /keys → no raw '[Errno 61]' in output."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.side_effect = httpx.ConnectError("[Errno 61] Connection refused")
    with patch("archon_search.cli.key_cmd.httpx.Client", return_value=mock_client):
        result = runner.invoke(
            key_cmd,
            ["create", "--namespace", "default", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "Errno 61" not in result.output


# ---------------------------------------------------------------------------
# key list
# ---------------------------------------------------------------------------


def test_key_list_server_not_running_friendly_message() -> None:
    """ConnectError on GET /keys → friendly message, exit 1."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.side_effect = httpx.ConnectError("Connection refused")
    with patch("archon_search.cli.key_cmd.httpx.Client", return_value=mock_client):
        result = runner.invoke(key_cmd, ["list", "--api-key", "test-key"])

    assert result.exit_code == 1
    assert any(p in result.output.lower() for p in _FRIENDLY_PHRASES), repr(result.output)


# ---------------------------------------------------------------------------
# key revoke
# ---------------------------------------------------------------------------


def test_key_revoke_server_not_running_friendly_message() -> None:
    """ConnectError on DELETE /keys/{id} → friendly message, exit 1."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.delete.side_effect = httpx.ConnectError("Connection refused")
    with patch("archon_search.cli.key_cmd.httpx.Client", return_value=mock_client):
        result = runner.invoke(
            key_cmd,
            ["revoke", "key-abc123", "--yes", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert any(p in result.output.lower() for p in _FRIENDLY_PHRASES), repr(result.output)


# ---------------------------------------------------------------------------
# key rotate
# ---------------------------------------------------------------------------


def test_key_rotate_server_not_running_friendly_message() -> None:
    """ConnectError on POST /keys/rotate → friendly message, exit 1."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.side_effect = httpx.ConnectError("Connection refused")
    with patch("archon_search.cli.key_cmd.httpx.Client", return_value=mock_client):
        result = runner.invoke(key_cmd, ["rotate", "--api-key", "test-key"])

    assert result.exit_code == 1
    assert any(p in result.output.lower() for p in _FRIENDLY_PHRASES), repr(result.output)


def test_key_rotate_server_not_running_no_raw_errno() -> None:
    """ConnectError on POST /keys/rotate → no raw errno text in output."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.side_effect = httpx.ConnectError("[Errno 61] Connection refused")
    with patch("archon_search.cli.key_cmd.httpx.Client", return_value=mock_client):
        result = runner.invoke(key_cmd, ["rotate", "--api-key", "test-key"])

    assert result.exit_code == 1
    assert "Errno 61" not in result.output


# ---------------------------------------------------------------------------
# backup --now
# ---------------------------------------------------------------------------


def test_backup_now_server_not_running_friendly_message() -> None:
    """ConnectError on POST /backup/trigger → friendly message, exit 1."""
    from archon_search.cli.backup_cmd import backup_cmd

    runner = CliRunner()
    with patch(
        "archon_search.cli.backup_cmd.httpx.post",
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        result = runner.invoke(backup_cmd, ["--now", "--api-key", "test-key"])

    assert result.exit_code == 1
    assert any(p in result.output.lower() for p in _FRIENDLY_PHRASES), repr(result.output)


def test_backup_now_server_not_running_no_raw_errno() -> None:
    """ConnectError on POST /backup/trigger → no raw '[Errno 61]' in output."""
    from archon_search.cli.backup_cmd import backup_cmd

    runner = CliRunner()
    with patch(
        "archon_search.cli.backup_cmd.httpx.post",
        side_effect=httpx.ConnectError("[Errno 61] Connection refused"),
    ):
        result = runner.invoke(backup_cmd, ["--now", "--api-key", "test-key"])

    assert result.exit_code == 1
    assert "Errno 61" not in result.output


# ---------------------------------------------------------------------------
# collection migrate
# ---------------------------------------------------------------------------


def test_collection_migrate_apply_server_not_running_friendly_message() -> None:
    """ConnectError on POST /collections/{name}/migrate → friendly message, exit 1."""
    from archon_search.cli.collection import collection

    runner = CliRunner()
    with patch(
        "archon_search.cli.collection.httpx.post",
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        result = runner.invoke(
            collection,
            ["migrate", "mycol", "--apply", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert any(p in result.output.lower() for p in _FRIENDLY_PHRASES), repr(result.output)


def test_collection_migrate_dryrun_server_not_running_friendly_message() -> None:
    """ConnectError on GET /collections/{name}/migrations/pending → friendly message, exit 1."""
    from archon_search.cli.collection import collection

    runner = CliRunner()
    with patch(
        "archon_search.cli.collection.httpx.get",
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        result = runner.invoke(
            collection,
            ["migrate", "mycol", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert any(p in result.output.lower() for p in _FRIENDLY_PHRASES), repr(result.output)


# ---------------------------------------------------------------------------
# Consistent message: all affected modules import _SERVER_NOT_RUNNING_MSG
# ---------------------------------------------------------------------------


def test_shared_constant_in_helpers() -> None:
    """_SERVER_NOT_RUNNING_MSG must live in _helpers so all modules share it."""
    from archon_search.cli._helpers import _SERVER_NOT_RUNNING_MSG

    assert "not running" in _SERVER_NOT_RUNNING_MSG.lower()
    assert "archon-search" in _SERVER_NOT_RUNNING_MSG.lower()


def test_message_consistent_across_modules() -> None:
    """Every affected module uses _server_connect_fail_msg from _helpers.

    The modules no longer import ``_SERVER_NOT_RUNNING_MSG`` directly — every one
    of them routes through ``_server_connect_fail_msg()``, which decides between
    the "not running" and "starting up" wording. That shared function is what must
    be the same object everywhere.
    """
    from archon_search.cli._helpers import _server_connect_fail_msg
    import archon_search.cli.key_cmd as km
    import archon_search.cli.backup_cmd as bm
    import archon_search.cli.maintenance_cmd as mm
    import archon_search.cli.sync as sm
    import archon_search.cli.ingest as im
    import archon_search.cli.graph_cmd as gm
    import archon_search.cli.jobs_cmd as jm
    import archon_search.cli.collection as cm
    import archon_search.cli.export_cmd as em

    for mod in (km, bm, mm, sm, im, gm, jm, cm, em):
        assert hasattr(mod, "_server_connect_fail_msg"), (
            f"{mod.__name__} must import _server_connect_fail_msg from _helpers"
        )
        assert mod._server_connect_fail_msg is _server_connect_fail_msg, (
            f"{mod.__name__}._server_connect_fail_msg must be the same object as "
            "_helpers._server_connect_fail_msg"
        )

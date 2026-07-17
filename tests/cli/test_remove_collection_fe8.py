"""Unit tests for ``archon-search collection remove`` CLI proxy (FE-8).

Tests:
- test_remove_sends_delete_exits_0: mocked 200 → exit 0, success message
- test_remove_409_pinned_only_prints_correct_message: 409 → pinned-only message, exit 1
- test_remove_503_lock_contention_prints_correct_message: 503 → lock-contention message, exit 1
- test_remove_force_option_no_longer_exists: --force absent from command options after conversion
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


# ---------------------------------------------------------------------------
# test_remove_sends_delete_exits_0
# ---------------------------------------------------------------------------


def test_remove_sends_delete_exits_0() -> None:
    """Mocked 200 response → exit 0, success message printed."""
    runner = CliRunner()
    del_resp = _mock_response(200, {"name": "mycol", "deleted": True})

    with patch("archon_search.cli.collection.httpx.delete", return_value=del_resp) as mock_delete:
        result = runner.invoke(
            collection,
            ["remove", "mycol", "--api-url", "http://localhost:8765", "--api-key", "test-key"],
        )

    assert result.exit_code == 0, result.output
    assert "mycol" in result.output
    mock_delete.assert_called_once()
    call_url = mock_delete.call_args[0][0]
    assert "/collections/mycol" in call_url
    _, call_kwargs = mock_delete.call_args
    assert call_kwargs.get("headers", {}).get("Authorization") == "Bearer test-key"


# ---------------------------------------------------------------------------
# test_remove_409_pinned_only_prints_correct_message
# ---------------------------------------------------------------------------


def test_remove_409_pinned_only_prints_correct_message() -> None:
    """409 response → pinned-only message printed, exit 1."""
    runner = CliRunner()
    del_resp = _mock_response(
        409,
        {"detail": "Collection 'mycol' is pinned-only; remove it from 'pinned_collections' in config before deleting."},
    )

    with patch("archon_search.cli.collection.httpx.delete", return_value=del_resp):
        result = runner.invoke(
            collection,
            ["remove", "mycol", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "Cannot remove 'mycol'" in result.output
    assert "pinned" in result.output.lower()
    assert "Un-pin it first" in result.output


# ---------------------------------------------------------------------------
# test_remove_503_lock_contention_prints_correct_message
# ---------------------------------------------------------------------------


def test_remove_503_lock_contention_prints_correct_message() -> None:
    """503 response → lock-contention message printed, exit 1."""
    runner = CliRunner()
    del_resp = _mock_response(503, {"detail": "write in progress"})

    with patch("archon_search.cli.collection.httpx.delete", return_value=del_resp):
        result = runner.invoke(
            collection,
            ["remove", "mycol", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "Cannot remove 'mycol'" in result.output
    assert "write in progress" in result.output.lower()
    assert "Retry after the active job completes" in result.output


# ---------------------------------------------------------------------------
# test_remove_force_option_no_longer_exists
# ---------------------------------------------------------------------------


def test_remove_force_option_no_longer_exists() -> None:
    """--force must not be a recognized option on the remove command after conversion."""
    runner = CliRunner()

    with patch("archon_search.cli.collection.httpx.delete", return_value=_mock_response(200)):
        result = runner.invoke(
            collection,
            ["remove", "mycol", "--force", "--api-key", "test-key"],
        )

    # Click returns exit_code 2 for unrecognized options
    assert result.exit_code == 2
    assert "no such option" in result.output.lower() or "Error" in result.output


# ---------------------------------------------------------------------------
# test_remove_server_not_running
# ---------------------------------------------------------------------------


def test_remove_server_not_running() -> None:
    """ConnectError → 'archon-search serve is not running. Start it first.', exit 1."""
    runner = CliRunner()

    with patch(
        "archon_search.cli.collection.httpx.delete",
        side_effect=httpx.ConnectError("Connection refused"),
    ):
        result = runner.invoke(
            collection,
            ["remove", "mycol", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "archon-search serve is not running" in result.output
    assert "Start it first" in result.output


# ---------------------------------------------------------------------------
# test_remove_404_prints_not_found
# ---------------------------------------------------------------------------


def test_remove_404_prints_not_found() -> None:
    """404 response → 'collection not found' message, exit 1."""
    runner = CliRunner()
    del_resp = _mock_response(404, {"detail": "Collection 'mycol' not found"})

    with patch("archon_search.cli.collection.httpx.delete", return_value=del_resp):
        result = runner.invoke(
            collection,
            ["remove", "mycol", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    assert "mycol" in result.output
    assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# test_remove_unexpected_status_code_prints_status_and_body
# ---------------------------------------------------------------------------


def test_remove_unexpected_status_code_prints_status_and_body() -> None:
    """Non-200/404/409/503 response → status code + body on stderr, exit 1."""
    runner = CliRunner()
    del_resp = _mock_response(500, text="internal server error")

    with patch("archon_search.cli.collection.httpx.delete", return_value=del_resp):
        result = runner.invoke(
            collection,
            ["remove", "mycol", "--api-key", "test-key"],
        )

    assert result.exit_code == 1
    combined = result.output
    assert "500" in combined
    assert "internal server error" in combined

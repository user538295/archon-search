"""Tests for D7 FE-1: CLI key create subcommand and duration parser.

Covers:
- S22: token on stdout, warning banner on stderr
- Duration parser: 30d, 12h, 3600s, ISO-8601 with tz, naive ISO-8601 raises, invalid raises
- CLI create calls POST /keys with correct JSON body and Bearer header
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Duration parser unit tests
# ---------------------------------------------------------------------------


def test_duration_parser_30d():
    """'30d' parses to a timezone-aware datetime ~30 days from now."""
    from archon_search.cli.key_cmd import _parse_expires

    before = datetime.now(UTC)
    result = _parse_expires("30d")
    after = datetime.now(UTC)

    assert result is not None
    assert result.tzinfo is not None
    expected_approx = before + timedelta(days=30)
    diff = abs((result - expected_approx).total_seconds())
    assert diff < 5, f"Expected ~30d from now, got diff={diff}s"


def test_duration_parser_12h():
    """'12h' parses to ~12 hours from now."""
    from archon_search.cli.key_cmd import _parse_expires

    before = datetime.now(UTC)
    result = _parse_expires("12h")

    assert result is not None
    expected_approx = before + timedelta(hours=12)
    diff = abs((result - expected_approx).total_seconds())
    assert diff < 5, f"Expected ~12h from now, got diff={diff}s"


def test_duration_parser_seconds():
    """'3600s' parses to ~1 hour from now."""
    from archon_search.cli.key_cmd import _parse_expires

    before = datetime.now(UTC)
    result = _parse_expires("3600s")

    assert result is not None
    expected_approx = before + timedelta(seconds=3600)
    diff = abs((result - expected_approx).total_seconds())
    assert diff < 5


def test_duration_parser_iso8601():
    """ISO-8601 datetime with UTC offset is parsed as-is (timezone-aware)."""
    from archon_search.cli.key_cmd import _parse_expires

    result = _parse_expires("2025-12-31T23:59:59Z")
    assert result is not None
    assert result.tzinfo is not None
    assert result.year == 2025
    assert result.month == 12
    assert result.day == 31
    assert result.hour == 23
    assert result.minute == 59
    assert result.second == 59


def test_duration_parser_naive_iso8601_raises():
    """Naive ISO-8601 without timezone raises click.BadParameter."""
    from archon_search.cli.key_cmd import _parse_expires

    with pytest.raises(click.BadParameter):
        _parse_expires("2025-12-31T23:59:59")


def test_duration_parser_invalid_raises():
    """Invalid string raises click.BadParameter."""
    from archon_search.cli.key_cmd import _parse_expires

    with pytest.raises(click.BadParameter):
        _parse_expires("not-a-duration")


# ---------------------------------------------------------------------------
# CLI stdout/stderr split (S22)
# ---------------------------------------------------------------------------


def test_key_create_stdout_token_stderr_banner():
    """Token is on stdout only; warning banner is on stderr only (S22).

    Uses result.stdout / result.stderr from Click 8.x CliRunner to verify
    strict separation: token present in stdout, absent from stderr; banner
    present in stderr, absent from stdout.
    """
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    fake_token = "ab" * 32  # 64 hex chars

    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {
        "id": "test-uuid",
        "token": fake_token,
        "namespace": "my-ns",
        "label": None,
        "created_at": "2026-01-01T00:00:00Z",
        "expires_at": None,
        "status": "active",
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["create", "--namespace", "my-ns"],
            env={"ARCHON_SEARCH_API_KEY": "a" * 64},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}\nerr={result.stderr}"
    # Token must appear on stdout (S22)
    assert fake_token in result.stdout, "Token must appear on stdout"
    # Token must NOT appear on stderr (S22 — raw token on stdout only)
    assert fake_token not in result.stderr, "Token must NOT appear on stderr"
    # Warning banner must appear on stderr (S22)
    assert "WARNING" in result.stderr, "Warning banner must appear on stderr"
    # Warning banner must NOT bleed onto stdout
    assert "WARNING" not in result.stdout, "Warning banner must NOT appear on stdout"


# ---------------------------------------------------------------------------
# Integration: CLI calls POST /keys with correct body + Bearer header
# ---------------------------------------------------------------------------


def test_cli_key_create_calls_post_keys():
    """CLI create sends POST /keys with correct JSON body and Authorization header."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    api_key = "a" * 64
    fake_token = "cd" * 32

    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {
        "id": "some-id",
        "token": fake_token,
        "namespace": "target-ns",
        "label": "my-lbl",
        "created_at": "2026-01-01T00:00:00Z",
        "expires_at": None,
        "status": "active",
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["create", "--namespace", "target-ns", "--label", "my-lbl"],
            env={"ARCHON_SEARCH_API_KEY": api_key},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}\nerr={result.stderr}"

    # Verify the HTTP call
    assert mock_client.post.call_count == 1
    call_kwargs = mock_client.post.call_args

    # URL should contain /keys
    call_url = call_kwargs[0][0] if call_kwargs[0] else call_kwargs.kwargs.get("url", "")
    assert "/keys" in call_url, f"Expected POST to /keys, got URL: {call_url}"

    # Authorization header
    headers = call_kwargs.kwargs.get("headers", {})
    if not headers and call_kwargs[1]:
        headers = call_kwargs[1].get("headers", {})
    assert "Authorization" in headers, f"Missing Authorization header: {headers}"
    assert f"Bearer {api_key}" in headers["Authorization"]

    # JSON body
    body_json = call_kwargs.kwargs.get("json", None)
    if body_json is None and call_kwargs[1]:
        body_json = call_kwargs[1].get("json", None)
    assert body_json is not None, "Expected JSON body in POST request"
    assert body_json.get("namespace") == "target-ns"
    assert body_json.get("label") == "my-lbl"


def test_cli_key_create_with_expires_sends_iso_string():
    """CLI key create --expires 30d sends a non-null ISO-8601 expires_at in the body."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    api_key = "a" * 64
    fake_token = "ef" * 32

    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {
        "id": "expire-id",
        "token": fake_token,
        "namespace": "default",
        "label": None,
        "created_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-07-22T00:00:00Z",
        "status": "active",
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["create", "--namespace", "default", "--expires", "30d"],
            env={"ARCHON_SEARCH_API_KEY": api_key},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}\nerr={result.stderr}"
    assert mock_client.post.call_count == 1

    call_kwargs = mock_client.post.call_args
    body_json = call_kwargs.kwargs.get("json") or (call_kwargs[1] or {}).get("json")
    assert body_json is not None
    assert body_json.get("expires_at") is not None, "expires_at must be in the POST body"
    # Must be a parseable ISO-8601 string with timezone info (isoformat() output)
    expires_str = body_json["expires_at"]
    assert isinstance(expires_str, str), "expires_at must be a string"
    parsed = datetime.fromisoformat(expires_str)
    assert parsed.tzinfo is not None, "expires_at must be timezone-aware"

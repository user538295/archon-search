"""Tests for D7 FE-3: CLI key rotate subcommand.

Covers:
- test_cli_key_rotate_no_grace: calls POST /keys/rotate with {} body (no grace_seconds)
- test_cli_key_rotate_with_grace: calls POST /keys/rotate with {"grace_seconds": N}
- test_cli_key_rotate_prints_new_token_stdout: new token on stdout, banner on stderr (S22)
- test_cli_key_rotate_integration: rotate via CLI against TestClient; old token rejected
"""
from __future__ import annotations

import secrets
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_cli_key_rotate_no_grace():
    """'key rotate' with no --grace flag sends POST /keys/rotate with empty {} body.

    grace_seconds must NOT appear in the request body when --grace is not supplied.
    """
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    new_token = secrets.token_hex(32)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "new_key_id": "new-uuid-abc",
        "token": new_token,
        "status": "active",
        "old_key_id": None,
        "old_key_expires_at": None,
        "old_key_status": None,
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["rotate"],
            env={"ARCHON_SEARCH_API_KEY": "a" * 64},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}"
    assert mock_client.post.call_count == 1
    call_kwargs = mock_client.post.call_args
    url = call_kwargs[0][0] if call_kwargs[0] else call_kwargs.kwargs.get("url", "")
    assert "/keys/rotate" in url, f"Expected POST to /keys/rotate, got: {url}"

    body = call_kwargs.kwargs.get("json") or (call_kwargs[1] or {}).get("json", {})
    assert "grace_seconds" not in body, (
        f"grace_seconds must not appear in body when --grace is omitted; got: {body}"
    )


def test_cli_key_rotate_with_grace():
    """'key rotate --grace 60s' sends POST /keys/rotate with {"grace_seconds": 60}."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    new_token = secrets.token_hex(32)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "new_key_id": "new-uuid-xyz",
        "token": new_token,
        "status": "active",
        "old_key_id": "old-uuid-abc",
        "old_key_expires_at": "2026-06-23T12:01:00+00:00",
        "old_key_status": "active",
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["rotate", "--grace", "60s"],
            env={"ARCHON_SEARCH_API_KEY": "a" * 64},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}"
    call_kwargs = mock_client.post.call_args
    body = call_kwargs.kwargs.get("json") or (call_kwargs[1] or {}).get("json", {})
    assert body.get("grace_seconds") == 60, (
        f"Expected grace_seconds=60 in body, got: {body}"
    )


def test_cli_key_rotate_grace_days():
    """'key rotate --grace 2d' sends POST /keys/rotate with {"grace_seconds": 172800}."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    new_token = secrets.token_hex(32)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "new_key_id": "new-uuid",
        "token": new_token,
        "status": "active",
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["rotate", "--grace", "2d"],
            env={"ARCHON_SEARCH_API_KEY": "a" * 64},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}"
    call_kwargs = mock_client.post.call_args
    body = call_kwargs.kwargs.get("json") or (call_kwargs[1] or {}).get("json", {})
    assert body.get("grace_seconds") == 2 * 24 * 3600, (
        f"Expected grace_seconds=172800 for '2d', got: {body}"
    )


def test_cli_key_rotate_grace_hours():
    """'key rotate --grace 12h' sends POST /keys/rotate with {"grace_seconds": 43200}."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    new_token = secrets.token_hex(32)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "new_key_id": "new-uuid",
        "token": new_token,
        "status": "active",
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["rotate", "--grace", "12h"],
            env={"ARCHON_SEARCH_API_KEY": "a" * 64},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}"
    call_kwargs = mock_client.post.call_args
    body = call_kwargs.kwargs.get("json") or (call_kwargs[1] or {}).get("json", {})
    assert body.get("grace_seconds") == 12 * 3600, (
        f"Expected grace_seconds=43200 for '12h', got: {body}"
    )


def test_cli_key_rotate_grace_zero():
    """'key rotate --grace 0s' sends POST /keys/rotate with {"grace_seconds": 0}.

    Explicit zero-grace is distinct from omitting --grace (which sends no
    grace_seconds key).  Both result in immediate revocation, but the explicit
    zero overrides a non-zero server config default.
    """
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    new_token = secrets.token_hex(32)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "new_key_id": "new-uuid",
        "token": new_token,
        "status": "active",
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["rotate", "--grace", "0s"],
            env={"ARCHON_SEARCH_API_KEY": "a" * 64},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}"
    call_kwargs = mock_client.post.call_args
    body = call_kwargs.kwargs.get("json") or (call_kwargs[1] or {}).get("json") or {}
    assert "grace_seconds" in body, (
        f"grace_seconds key must be present in body when --grace 0s is supplied; got: {body}"
    )
    assert body["grace_seconds"] == 0, (
        f"Expected grace_seconds=0 for '--grace 0s', got: {body}"
    )


def test_cli_key_rotate_grace_invalid_raises():
    """'key rotate --grace notavalid' exits non-zero with an error message."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()

    result = runner.invoke(
        key_cmd,
        ["rotate", "--grace", "notavalid"],
        env={"ARCHON_SEARCH_API_KEY": "a" * 64},
    )

    assert result.exit_code != 0, (
        f"Expected non-zero exit for invalid --grace, got: {result.exit_code}"
    )


def test_cli_key_rotate_prints_new_token_stdout():
    """New token on stdout only; warning banner on stderr only (S22).

    Click 8.x removed the mix_stderr constructor param; result.stdout and
    result.stderr are available on the Result object directly.
    """
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    new_token = "abc" * 21 + "ab"  # 64-char hex-like token

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "new_key_id": "new-uuid-123",
        "token": new_token,
        "status": "active",
        "old_key_id": None,
        "old_key_expires_at": None,
        "old_key_status": None,
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["rotate"],
            env={"ARCHON_SEARCH_API_KEY": "a" * 64},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}"

    # S22: token on stdout only — stdout must contain ONLY the token (no metadata).
    # "raw token on stdout only" means stdout.strip() == token exactly.
    assert result.stdout.strip() == new_token, (
        f"stdout must contain only the token; got: {result.stdout!r}"
    )
    assert new_token not in result.stderr, f"Token must not appear in stderr: {result.stderr!r}"

    # S22: warning banner on stderr only
    assert "WARNING" in result.stderr, f"Banner not found in stderr: {result.stderr!r}"
    assert "WARNING" not in result.stdout, f"Banner must not appear in stdout: {result.stdout!r}"


def test_cli_key_rotate_server_error_exits_nonzero():
    """'key rotate' exits non-zero when the server returns a non-200 status."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()

    mock_response = MagicMock()
    mock_response.status_code = 409
    mock_response.text = "Cannot rotate: ARCHON_SEARCH_API_KEY env var is set"

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["rotate"],
            env={"ARCHON_SEARCH_API_KEY": "a" * 64},
        )

    assert result.exit_code != 0, (
        f"Expected non-zero exit for server 409, got: {result.exit_code}"
    )


def test_cli_key_rotate_auth_header_sent():
    """'key rotate' sends the Bearer token in the Authorization header."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    api_key = "b" * 64
    new_token = secrets.token_hex(32)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "new_key_id": "new-uuid",
        "token": new_token,
        "status": "active",
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.post.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["rotate"],
            env={"ARCHON_SEARCH_API_KEY": api_key},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}"
    call_kwargs = mock_client.post.call_args
    headers = call_kwargs.kwargs.get("headers") or (call_kwargs[1] or {}).get("headers", {})
    assert "Authorization" in headers, f"Missing Authorization header: {headers}"
    assert headers["Authorization"] == f"Bearer {api_key}"


# ---------------------------------------------------------------------------
# _parse_grace unit tests
# ---------------------------------------------------------------------------


def test_parse_grace_seconds():
    """'60s' parses to 60 seconds."""
    from archon_search.cli.key_cmd import _parse_grace

    assert _parse_grace("60s") == 60
    assert _parse_grace("1s") == 1
    assert _parse_grace("0s") == 0


def test_parse_grace_hours():
    """'12h' parses to 43200 seconds."""
    from archon_search.cli.key_cmd import _parse_grace

    assert _parse_grace("12h") == 12 * 3600
    assert _parse_grace("1h") == 3600


def test_parse_grace_days():
    """'2d' parses to 172800 seconds."""
    from archon_search.cli.key_cmd import _parse_grace

    assert _parse_grace("2d") == 2 * 86400
    assert _parse_grace("1d") == 86400


def test_parse_grace_invalid_raises():
    """Invalid duration strings raise click.BadParameter."""
    import click

    from archon_search.cli.key_cmd import _parse_grace

    with pytest.raises(click.BadParameter):
        _parse_grace("notavalid")

    with pytest.raises(click.BadParameter):
        _parse_grace("30m")  # minutes not supported

    with pytest.raises(click.BadParameter):
        _parse_grace("")  # empty string


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_cli_key_rotate_integration(tmp_path, monkeypatch):
    """Rotate via CLI against real TestClient; old token rejected after rotation (S6).

    The rotate subcommand calls POST /keys/rotate. After rotation:
    - A new token is returned and displayed on stdout.
    - A second rotation is authenticated with that new token.
    - The first new token (revoked by the second rotation) returns 401.
    """
    from archon_search.key_manager import ENV_VAR as _ENV_VAR
    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(_ENV_VAR, raising=False)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(cfg, job_store, scheduler=scheduler)
    initial_api_key = app.state.api_key

    with TestClient(app) as client:
        from archon_search.cli.key_cmd import key_cmd

        runner = CliRunner()

        def _make_fake_post(test_client):  # noqa: ANN001, ANN202
            """Return a callable that forwards httpx POST calls to test_client."""
            def _fake_post(url, **kwargs):  # noqa: ANN001, ANN202
                path = url.replace("http://localhost:8765", "")
                headers = kwargs.get("headers", {})
                json_body = kwargs.get("json", {})
                real_resp = test_client.post(path, headers=headers, json=json_body)
                mock_r = MagicMock()
                mock_r.status_code = real_resp.status_code
                mock_r.json.return_value = real_resp.json()
                mock_r.text = real_resp.text
                return mock_r
            return _fake_post

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.post.side_effect = _make_fake_post(client)

            # First rotation: auto-generated key → managed key.
            # Pass the API key via --api-key (not via env) so CliRunner.isolation
            # does NOT set ARCHON_SEARCH_API_KEY in os.environ — the server
            # checks os.environ.get(ENV_VAR) and returns 409 when it is set.
            result = runner.invoke(
                key_cmd,
                ["rotate", "--api-key", initial_api_key],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, f"exit={result.exit_code}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        first_new_token = result.stdout.strip()
        assert len(first_new_token) > 0, "Expected a token in stdout"
        assert "WARNING" in result.stderr, "Expected warning banner in stderr"

        # New token must authenticate successfully.
        auth_resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {first_new_token}"},
        )
        assert auth_resp.status_code == 200, f"New token rejected: {auth_resp.text}"

        # Second rotation using the first new token.
        with patch("httpx.Client") as mock_client_cls2:
            mock_client2 = MagicMock()
            mock_client_cls2.return_value.__enter__.return_value = mock_client2
            mock_client2.post.side_effect = _make_fake_post(client)

            result2 = runner.invoke(
                key_cmd,
                ["rotate", "--api-key", first_new_token],
                catch_exceptions=False,
            )

        assert result2.exit_code == 0, f"Second rotation failed: {result2.stdout!r} / {result2.stderr!r}"

        # first_new_token is now revoked — must return 401.
        old_resp = client.get(
            "/keys",
            headers={"Authorization": f"Bearer {first_new_token}"},
        )
        assert old_resp.status_code == 401, (
            f"Revoked first_new_token must return 401 after second rotation; got {old_resp.status_code}"
        )

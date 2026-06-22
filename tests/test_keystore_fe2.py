"""Tests for D7 FE-2: CLI key list and key revoke subcommands.

Covers:
- test_cli_key_list_active_default: calls GET /keys with no status param; prints hint line when hidden_revoked_count > 0
- test_cli_key_list_status_all: passes status=all query param
- test_cli_key_list_status_revoked: passes status=revoked query param; shows only revoked keys
- test_cli_key_revoke_calls_delete: key revoke <id> sends DELETE /keys/{id}
- test_cli_key_list_integration: CLI list against TestClient returns formatted key rows
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_cli_key_list_active_default():
    """'key list' with no flags calls GET /keys with no status param (default=active).

    When the server returns hidden_revoked_count > 0, a hint line is printed.
    """
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "keys": [
            {
                "id": "abc-123",
                "namespace": "default",
                "label": "my-key",
                "created_at": "2026-01-01T00:00:00+00:00",
                "expires_at": None,
                "status": "active",
            }
        ],
        "hidden_revoked_count": 2,
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["list"],
            env={"ARCHON_SEARCH_API_KEY": "a" * 64},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}"
    # Default call should have no explicit status param (server default is active)
    call_kwargs = mock_client.get.call_args
    params = call_kwargs.kwargs.get("params") or (call_kwargs[1] or {}).get("params")
    # params is either None (no params passed) or a dict without "status"
    assert params is None or params.get("status") is None, (
        f"Expected no status param for default, got: {params}"
    )
    # Key id should appear in output
    assert "abc-123" in result.output
    # Hint line should contain the specific count and word "revoked"
    assert "2 revoked key(s)" in result.output


def test_cli_key_list_status_all():
    """'key list --status all' passes status=all query param to GET /keys."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "keys": [],
        "hidden_revoked_count": 0,
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["list", "--status", "all"],
            env={"ARCHON_SEARCH_API_KEY": "a" * 64},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}"
    call_kwargs = mock_client.get.call_args
    params = call_kwargs.kwargs.get("params", {}) or (call_kwargs[1] or {}).get("params", {})
    assert params.get("status") == "all", f"Expected status=all in params, got: {params}"


def test_cli_key_list_status_revoked():
    """'key list --status revoked' passes status=revoked and shows only revoked keys."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "keys": [
            {
                "id": "rev-456",
                "namespace": "default",
                "label": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "expires_at": None,
                "status": "revoked",
            }
        ],
        "hidden_revoked_count": 0,
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["list", "--status", "revoked"],
            env={"ARCHON_SEARCH_API_KEY": "a" * 64},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}"
    call_kwargs = mock_client.get.call_args
    params = call_kwargs.kwargs.get("params", {}) or (call_kwargs[1] or {}).get("params", {})
    assert params.get("status") == "revoked", f"Expected status=revoked in params, got: {params}"
    assert "rev-456" in result.output
    assert "revoked" in result.output


def test_cli_key_list_namespace_filter():
    """'key list --namespace ns-a' passes namespace=ns-a query param to GET /keys."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "keys": [],
        "hidden_revoked_count": 0,
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["list", "--namespace", "ns-a"],
            env={"ARCHON_SEARCH_API_KEY": "a" * 64},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}"
    call_kwargs = mock_client.get.call_args
    params = call_kwargs.kwargs.get("params", {}) or (call_kwargs[1] or {}).get("params", {})
    assert params.get("namespace") == "ns-a", f"Expected namespace=ns-a in params, got: {params}"


def test_cli_key_revoke_calls_delete():
    """'key revoke <id>' sends DELETE /keys/{id} with Bearer auth."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    key_id = "abc-def-123"
    api_key = "b" * 64

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "id": key_id,
        "status": "revoked",
    }

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.delete.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["revoke", key_id],
            env={"ARCHON_SEARCH_API_KEY": api_key},
            catch_exceptions=False,
        )

    assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}"
    assert mock_client.delete.call_count == 1
    call_kwargs = mock_client.delete.call_args
    call_url = call_kwargs[0][0] if call_kwargs[0] else call_kwargs.kwargs.get("url", "")
    assert f"/keys/{key_id}" in call_url, f"Expected DELETE to /keys/{key_id}, got: {call_url}"
    # Check auth header
    headers = call_kwargs.kwargs.get("headers", {}) or (call_kwargs[1] or {}).get("headers", {})
    assert "Authorization" in headers, f"Missing Authorization header: {headers}"
    assert f"Bearer {api_key}" in headers["Authorization"]
    # Confirm success output
    assert key_id in result.output


def test_cli_key_revoke_not_found_exits_nonzero():
    """'key revoke <unknown-id>' exits with non-zero when server returns 404."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Key not found"

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.delete.return_value = mock_response

        result = runner.invoke(
            key_cmd,
            ["revoke", "no-such-id"],
            env={"ARCHON_SEARCH_API_KEY": "a" * 64},
        )

    assert result.exit_code != 0, f"Expected non-zero exit for 404, got: {result.exit_code}"


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_cli_key_list_integration(tmp_path, monkeypatch):
    """CLI list against real TestClient returns formatted key rows (S3)."""
    from tests.integration.conftest import make_real_app

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        from archon_search.cli.key_cmd import key_cmd

        runner = CliRunner()

        # Create a key so there is something to list.
        resp = client.post(
            "/keys",
            json={"namespace": "default"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 201, resp.text
        created_id = resp.json()["id"]

        # Use the runner to invoke the CLI list command, calling the real server.
        api_url = f"http://testserver"
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            # Forward the list call to the real TestClient
            def fake_get(url, **kwargs):  # noqa: ANN001, ANN202
                path = url.replace("http://localhost:8765", "")
                params = kwargs.get("params", {})
                headers = kwargs.get("headers", {})
                real_resp = client.get(path, params=params, headers=headers)
                mock_r = MagicMock()
                mock_r.status_code = real_resp.status_code
                mock_r.json.return_value = real_resp.json()
                mock_r.text = real_resp.text
                return mock_r

            mock_client.get.side_effect = fake_get

            result = runner.invoke(
                key_cmd,
                ["list"],
                env={"ARCHON_SEARCH_API_KEY": api_key},
                catch_exceptions=False,
            )

        assert result.exit_code == 0, f"exit={result.exit_code}\nout={result.output}"
        # The created key's id should appear in the list output
        assert created_id in result.output

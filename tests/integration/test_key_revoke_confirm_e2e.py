"""E2e: `key revoke` confirmation prompt against a real server (Backlog 070).

Full flow: issue a labelled key via POST /keys, then drive the CLI `revoke`
command interactively (typing `y`), forwarding both the pre-flight GET /keys
label lookup and the DELETE /keys/{id} to a real TestClient. Asserts the
human-readable label is shown in the prompt and the key ends up revoked.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def _forwarder(client, method):
    """Return a callable that forwards a mocked httpx call to the TestClient."""

    def _call(url, **kwargs):
        path = url.replace("http://localhost:8765", "")
        real = getattr(client, method)(
            path,
            params=kwargs.get("params"),
            headers=kwargs.get("headers"),
        )
        m = MagicMock()
        m.status_code = real.status_code
        m.json.return_value = real.json()
        m.text = real.text
        return m

    return _call


def test_e2e_revoke_prompt_shows_label_and_revokes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        from archon_search.cli.key_cmd import key_cmd

        # Step 1 — issue a labelled managed key.
        created = client.post(
            "/keys",
            json={"namespace": "default", "label": "production-webhook"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert created.status_code == 201, created.text
        key_id = created.json()["id"]

        # Step 2 — run the CLI revoke interactively, answering "y".
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = _forwarder(client, "get")
            mock_client.delete.side_effect = _forwarder(client, "delete")

            result = CliRunner().invoke(
                key_cmd,
                ["revoke", key_id],
                env={"ARCHON_SEARCH_API_KEY": api_key},
                input="y\n",
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        # The prompt showed the human-readable label and the id.
        assert "production-webhook" in result.output
        assert key_id in result.output

        # Step 3 — the key is revoked on the server.
        listed = client.get(
            "/keys",
            params={"status": "revoked"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        revoked_ids = [k["id"] for k in listed.json()["keys"]]
        assert key_id in revoked_ids, listed.text


def test_e2e_revoke_prompt_declined_keeps_key_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        from archon_search.cli.key_cmd import key_cmd

        created = client.post(
            "/keys",
            json={"namespace": "default", "label": "keep-me"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert created.status_code == 201, created.text
        key_id = created.json()["id"]

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.get.side_effect = _forwarder(client, "get")
            mock_client.delete.side_effect = _forwarder(client, "delete")

            result = CliRunner().invoke(
                key_cmd,
                ["revoke", key_id],
                env={"ARCHON_SEARCH_API_KEY": api_key},
                input="n\n",
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert mock_client.delete.call_count == 0

        # The key is still active on the server.
        listed = client.get(
            "/keys",
            params={"status": "active"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        active_ids = [k["id"] for k in listed.json()["keys"]]
        assert key_id in active_ids, listed.text

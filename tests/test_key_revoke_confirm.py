"""Tests for the `key revoke` confirmation prompt (Backlog 070).

Behavior under test:
- `key revoke <id>` prompts before deleting; the prompt shows the key's label
  (fetched via a best-effort GET /keys) when available.
- Declining (`n`) or pressing Enter cancels with exit code 0 and no DELETE.
- Accepting (`y`) sends DELETE /keys/{id}.
- `--yes` / `-y` skips both the prompt AND the pre-flight label lookup.
- Label lookup is best-effort: a missing key, missing label, non-200, or
  network error falls back to prompting with the raw ID (revoke still proceeds).
- Non-interactive stdin without `--yes` aborts non-zero (CI safety).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

_API_KEY = "b" * 64
_KEY_ID = "abc-def-123"


def _list_response(keys: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"keys": keys, "hidden_revoked_count": 0}
    return resp


def _delete_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"id": _KEY_ID, "status": "revoked"}
    return resp


def _run(args, *, get=None, delete=None, get_side_effect=None, input=None):
    """Invoke the key CLI with httpx.Client mocked. Returns (result, mock_client)."""
    from archon_search.cli.key_cmd import key_cmd

    runner = CliRunner()
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        if get_side_effect is not None:
            mock_client.get.side_effect = get_side_effect
        elif get is not None:
            mock_client.get.return_value = get
        if delete is not None:
            mock_client.delete.return_value = delete
        result = runner.invoke(
            key_cmd,
            args,
            env={"ARCHON_SEARCH_API_KEY": _API_KEY},
            input=input,
        )
    return result, mock_client


# ---------------------------------------------------------------------------
# Prompt fires; label displayed
# ---------------------------------------------------------------------------


def test_revoke_prompts_with_label_and_accepts():
    """`y` at the prompt sends DELETE; the prompt shows the label and id."""
    result, client = _run(
        ["revoke", _KEY_ID],
        get=_list_response([{"id": _KEY_ID, "label": "production-webhook"}]),
        delete=_delete_response(),
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert client.delete.call_count == 1
    assert "production-webhook" in result.output
    assert _KEY_ID in result.output


def test_revoke_decline_aborts_cleanly_no_delete():
    """`n` at the prompt cancels: exit 0, no DELETE sent."""
    result, client = _run(
        ["revoke", _KEY_ID],
        get=_list_response([{"id": _KEY_ID, "label": "production-webhook"}]),
        delete=_delete_response(),
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    assert client.delete.call_count == 0


def test_revoke_enter_defaults_to_no():
    """Pressing Enter (empty) accepts the default No: exit 0, no DELETE."""
    result, client = _run(
        ["revoke", _KEY_ID],
        get=_list_response([{"id": _KEY_ID, "label": "production-webhook"}]),
        delete=_delete_response(),
        input="\n",
    )
    assert result.exit_code == 0, result.output
    assert client.delete.call_count == 0


# ---------------------------------------------------------------------------
# --yes / -y skips prompt AND lookup
# ---------------------------------------------------------------------------


def test_revoke_yes_flag_skips_prompt_and_lookup():
    """`--yes` sends DELETE without prompting and without the label lookup."""
    result, client = _run(
        ["revoke", _KEY_ID, "--yes"],
        delete=_delete_response(),
    )
    assert result.exit_code == 0, result.output
    assert client.get.call_count == 0, "lookup must be skipped with --yes"
    assert client.delete.call_count == 1


def test_revoke_short_y_flag_skips_prompt():
    """`-y` behaves like `--yes`."""
    result, client = _run(
        ["revoke", _KEY_ID, "-y"],
        delete=_delete_response(),
    )
    assert result.exit_code == 0, result.output
    assert client.get.call_count == 0
    assert client.delete.call_count == 1


# ---------------------------------------------------------------------------
# Fallbacks: prompt with raw id when label unavailable
# ---------------------------------------------------------------------------


def test_revoke_fallback_when_key_has_no_label():
    """A key with label=None prompts with the raw id (no quoted label)."""
    result, client = _run(
        ["revoke", _KEY_ID],
        get=_list_response([{"id": _KEY_ID, "label": None}]),
        delete=_delete_response(),
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert client.delete.call_count == 1
    assert '"' not in result.output.split("\n")[0], result.output


def test_revoke_fallback_when_key_not_in_list():
    """An id absent from GET /keys prompts with the raw id; revoke still proceeds."""
    result, client = _run(
        ["revoke", _KEY_ID],
        get=_list_response([{"id": "other-id", "label": "something"}]),
        delete=_delete_response(),
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert client.delete.call_count == 1
    assert "something" not in result.output


def test_revoke_fallback_when_lookup_returns_non_200():
    """A non-200 from the lookup falls back to the raw-id prompt."""
    bad = MagicMock()
    bad.status_code = 500
    bad.text = "boom"
    result, client = _run(
        ["revoke", _KEY_ID],
        get=bad,
        delete=_delete_response(),
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert client.delete.call_count == 1


def test_revoke_fallback_when_lookup_raises():
    """A network error during lookup falls back to the raw-id prompt."""
    result, client = _run(
        ["revoke", _KEY_ID],
        get_side_effect=httpx.ConnectError("down"),
        delete=_delete_response(),
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert client.delete.call_count == 1


def test_revoke_lookup_requests_all_statuses():
    """The label lookup passes status=all so revoked keys are still found."""
    result, client = _run(
        ["revoke", _KEY_ID],
        get=_list_response([{"id": _KEY_ID, "label": "x"}]),
        delete=_delete_response(),
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    call = client.get.call_args
    params = call.kwargs.get("params") or {}
    assert params.get("status") == "all", f"expected status=all, got {params}"


# ---------------------------------------------------------------------------
# CI safety: non-interactive stdin without --yes aborts non-zero
# ---------------------------------------------------------------------------


def test_revoke_non_interactive_without_yes_aborts_nonzero():
    """Empty stdin (piped/CI) with no --yes aborts before any DELETE."""
    result, client = _run(
        ["revoke", _KEY_ID],
        get=_list_response([{"id": _KEY_ID, "label": "x"}]),
        delete=_delete_response(),
        input="",
    )
    assert result.exit_code != 0
    assert client.delete.call_count == 0


# ---------------------------------------------------------------------------
# Integration: --yes path against a real TestClient
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_revoke_yes_integration(tmp_path, monkeypatch):
    """`key revoke --yes` against a real server actually revokes the key."""
    from tests.integration.conftest import make_real_app

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        from archon_search.cli.key_cmd import key_cmd

        created = client.post(
            "/keys",
            json={"namespace": "default", "label": "int-key"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert created.status_code == 201, created.text
        key_id = created.json()["id"]

        def _forward(method):
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

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.delete.side_effect = _forward("delete")
            result = CliRunner().invoke(
                key_cmd,
                ["revoke", key_id, "--yes"],
                env={"ARCHON_SEARCH_API_KEY": api_key},
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert mock_client.get.call_count == 0
        # The key is now revoked on the server.
        listed = client.get(
            "/keys",
            params={"status": "revoked"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        revoked_ids = [k["id"] for k in listed.json()["keys"]]
        assert key_id in revoked_ids, listed.text

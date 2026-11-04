"""Tests for the shared CLI helpers in ``archon_search/cli/_helpers.py``."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from _cli_server_stub import server_reported_stopped

from archon_search.cli._helpers import (
    _SERVER_NOT_RUNNING_MSG,
    _json_body,
    _json_payload,
    _resolve_api_key,
    _response_message,
    _send_or_exit,
    _server_message,
)
from archon_search.config import ConfigError


def test_resolve_api_key_arg_priority() -> None:
    """Explicit --api-key arg wins even when ARCHON_SEARCH_API_KEY env var is set."""
    with patch.dict("os.environ", {"ARCHON_SEARCH_API_KEY": "env-key"}), patch("archon_search.cli._helpers.load_key") as mock_load:
        result = _resolve_api_key("explicit-key")

    assert result == "explicit-key"
    mock_load.assert_not_called()


def test_resolve_api_key_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """None arg + ARCHON_SEARCH_API_KEY set → returns the key ``load_key`` resolves from the environment."""
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "env-key")

    with patch("archon_search.cli._helpers.load_key", return_value="env-key") as mock_load:
        result = _resolve_api_key(None)

    assert result == "env-key"
    mock_load.assert_called_once()


def test_resolve_api_key_file_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """None arg + no env var → falls back to the key file result of ``load_key()``."""
    monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)

    with patch("archon_search.cli._helpers.load_key", return_value="file-key") as mock_load:
        result = _resolve_api_key(None)

    assert result == "file-key"
    mock_load.assert_called_once()


def test_resolve_api_key_no_key_no_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """None arg + no env var + no key file → exits 1 without touching the filesystem.

    Regression for: PermissionError: [Errno 13] Permission denied: '/data'
    In a container, load_or_generate_key() tried to makedirs('/data') when
    no key exists — the CLI must never generate keys, only load them.
    """
    monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
    # Point key file at a path inside tmp_path that does NOT exist — no key on disk.
    monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(tmp_path / "missing.env"))

    with pytest.raises(SystemExit) as exc:
        _resolve_api_key(None)

    assert exc.value.code == 1
    assert "No API key found. Pass --api-key, set ARCHON_SEARCH_API_KEY" in capsys.readouterr().err
    # Must not have written anything to disk.
    assert not any(tmp_path.iterdir())


def test_resolve_api_key_lets_config_error_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unusable ARCHON_SEARCH_API_KEY raises ConfigError; the helper must not swallow it into the no-key exit."""
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "not-a-valid-key")

    with pytest.raises(ConfigError):
        _resolve_api_key(None)


_EMPTY = "the caller's own empty text"


def test_server_message_renders_the_message_of_a_coded_detail() -> None:
    detail = {"code": "invalid_scope_filter", "message": "scope filter is malformed"}

    assert _server_message(detail, "", empty_text=_EMPTY) == "scope filter is malformed"


def test_server_message_joins_a_validation_list_and_drops_the_value_error_prefix() -> None:
    detail = [{"msg": "Value error, first"}, {"msg": "second"}, {"loc": ["body"]}]

    assert _server_message(detail, "", empty_text=_EMPTY) == "first; second"


@pytest.mark.parametrize(
    ("loc", "field"),
    [
        (["body", "top_k"], "top_k"),
        (["body", "filters", 0, "name"], "filters.name"),
        (["query", "limit"], "query.limit"),
        (["body"], ""),
        ([], ""),
    ],
    ids=["body-field", "body-index-nested", "query-param", "body-only", "no-loc"],
)
def test_server_message_names_the_field_of_a_builtin_validation_message(loc: list, field: str) -> None:
    detail = [{"loc": loc, "msg": "Input should be greater than or equal to 1", "type": "greater_than_equal"}]

    expected = "Input should be greater than or equal to 1"
    assert _server_message(detail, "", empty_text=_EMPTY) == (f"{field}: {expected}" if field else expected)


def test_server_message_leaves_a_value_error_message_without_the_field_name() -> None:
    detail = [{"loc": ["body", "query"], "msg": "Value error, query must not be empty", "type": "value_error"}]

    assert _server_message(detail, "", empty_text=_EMPTY) == "query must not be empty"


def test_server_message_cleans_a_hostile_field_name() -> None:
    detail = [{"loc": ["body", "bad\x1b[31m"], "msg": "m"}]

    assert _server_message(detail, "", empty_text=_EMPTY) == "bad [31m: m"


@pytest.mark.parametrize(
    "detail",
    ["bad\x1b[31m text", {"code": "c", "message": "bad\x1b[31m text"}, [{"msg": "bad\x1b[31m text"}]],
    ids=["string", "coded", "list"],
)
def test_server_message_cleans_terminal_controls(detail: object) -> None:
    assert _server_message(detail, "", empty_text=_EMPTY) == "bad [31m text"


def test_server_message_returns_the_callers_empty_text_for_an_empty_detail_and_body() -> None:
    assert _server_message(None, "", empty_text=_EMPTY) == _EMPTY
    assert _server_message([], "", empty_text=_EMPTY) == _EMPTY
    assert _server_message({"code": "c"}, "", empty_text=_EMPTY) == _EMPTY
    assert _server_message("\x1b", "", empty_text=_EMPTY) == _EMPTY


def test_server_message_for_a_coded_detail_without_a_message_uses_the_body_text() -> None:
    assert _server_message({"code": "c"}, "raw body", empty_text=_EMPTY) == "raw body"


def test_server_message_falls_back_to_the_cleaned_body_text() -> None:
    assert _server_message(None, "plain\x1b text", empty_text=_EMPTY) == "plain  text"


@pytest.mark.parametrize(
    ("resp", "expected"),
    [
        (httpx.Response(422, json={"detail": [{"loc": ["body", "top_k"], "msg": "too small"}]}), "top_k: too small"),
        (httpx.Response(409, json={"detail": {"code": "c", "message": "busy"}}), "busy"),
        (httpx.Response(400, json={"detail": "plain"}), "plain"),
        (httpx.Response(502, text="<html>bad\x1b gateway</html>"), "<html>bad  gateway</html>"),
        (httpx.Response(500, json=["not", "an", "object"]), '["not","an","object"]'),
        (httpx.Response(500, text=""), _EMPTY),
    ],
    ids=["list", "coded", "string", "non-json-body", "non-object-json", "empty"],
)
def test_response_message_reads_the_detail_of_the_response_or_its_body_text(resp: httpx.Response, expected: str) -> None:
    assert _response_message(resp, empty_text=_EMPTY) == expected


def test_send_or_exit_connect_failure_prints_the_shared_message_and_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    def refuse() -> httpx.Response:
        raise httpx.ConnectError("refused")

    with server_reported_stopped(), pytest.raises(SystemExit) as exc:
        _send_or_exit("http://x", refuse)

    assert exc.value.code == 1
    assert _SERVER_NOT_RUNNING_MSG in capsys.readouterr().err


def test_send_or_exit_other_transport_error_prints_the_error_and_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    def fail() -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(SystemExit) as exc:
        _send_or_exit("http://x", fail)

    assert exc.value.code == 1
    assert "Error contacting server: slow" in capsys.readouterr().err


_DEEP_JSON = "[" * 100_000


def test_json_payload_turns_a_too_deeply_nested_body_into_a_value_error() -> None:
    with pytest.raises(ValueError, match="nested too deeply"):
        _json_payload(httpx.Response(200, text=_DEEP_JSON))


def test_json_payload_decodes_a_normal_body() -> None:
    assert _json_payload(httpx.Response(200, json={"a": [1]})) == {"a": [1]}


def test_json_body_is_empty_for_a_too_deeply_nested_body() -> None:
    assert _json_body(httpx.Response(500, text=_DEEP_JSON)) == {}

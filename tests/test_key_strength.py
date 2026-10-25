"""Weak API keys are refused at startup, with messages that never echo the key (SecReview #9a)."""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import ConfigError, SearchConfig, load_config
from archon_search.constants import MIN_API_KEY_LENGTH
from archon_search.jobs.store import JobStore
from archon_search.key_manager import (
    ENV_VAR,
    api_key_problem,
    load_key,
    load_or_generate_key,
    namespace_token_problem,
)
from archon_search.server.app import create_app

_SECRET_MARKER = "deadbeef"  # a recognisable fragment: no message may contain it


def _toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "archon-search.toml"
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# Default key (env var / key file)
# ---------------------------------------------------------------------------


def test_a_short_hex_env_key_is_refused_with_a_message_that_does_not_echo_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, _SECRET_MARKER)

    with pytest.raises(ConfigError) as excinfo:
        load_or_generate_key()

    message = str(excinfo.value)
    assert ENV_VAR in message and str(MIN_API_KEY_LENGTH) in message
    assert _SECRET_MARKER not in message


@pytest.mark.parametrize("value", ["X" * 64, "zz" + "a" * 62, "a" * 31 + " ", "é" * 40])
def test_a_non_hex_env_key_is_refused(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ENV_VAR, value)

    with pytest.raises(ConfigError, match="lowercase hex"):
        load_or_generate_key()


def test_load_key_refuses_a_weak_env_key_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "ab" * 8)

    with pytest.raises(ConfigError):
        load_key()


@pytest.mark.parametrize("length", [MIN_API_KEY_LENGTH, MIN_API_KEY_LENGTH + 1, 64])
def test_a_hex_env_key_of_at_least_the_minimum_length_is_accepted(monkeypatch: pytest.MonkeyPatch, length: int) -> None:
    key = "a" * length
    monkeypatch.setenv(ENV_VAR, key)

    assert load_or_generate_key() == (key, "env var")


def test_one_character_below_the_minimum_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_VAR, "a" * (MIN_API_KEY_LENGTH - 1))

    with pytest.raises(ConfigError):
        load_or_generate_key()


def test_a_weak_key_in_the_key_file_is_not_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ENV_VAR)
    key_file = tmp_path / ".search.env"
    key_file.write_text(f"{ENV_VAR}={_SECRET_MARKER}\n")
    monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(key_file))

    assert load_key() is None


def test_api_key_problem_names_the_rule_never_the_value() -> None:
    assert api_key_problem("") == "it is empty"
    assert api_key_problem("a" * 64) is None
    problem = api_key_problem(_SECRET_MARKER)
    assert problem is not None and _SECRET_MARKER not in problem


# ---------------------------------------------------------------------------
# TOML [namespaces] tokens
# ---------------------------------------------------------------------------


def test_an_empty_namespace_token_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(_toml(tmp_path, '[namespaces]\n"" = "ns-a"\n'))

    assert "ns-a" in str(excinfo.value) and "empty" in str(excinfo.value)


def test_a_short_namespace_token_is_refused_without_echoing_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(_toml(tmp_path, f'[namespaces]\n"{_SECRET_MARKER}" = "ns-a"\n'))

    assert _SECRET_MARKER not in str(excinfo.value)
    assert str(MIN_API_KEY_LENGTH) in str(excinfo.value)


@pytest.mark.parametrize("token", ["has space " + "a" * 30, "tab\\t" + "a" * 30, "é" * 40])
def test_a_namespace_token_with_whitespace_or_non_ascii_is_refused(tmp_path: Path, token: str) -> None:
    with pytest.raises(ConfigError, match="printable ASCII"):
        load_config(_toml(tmp_path, f'[namespaces]\n"{token}" = "ns-a"\n'))


def test_a_non_string_namespace_value_is_refused_without_echoing_the_token(tmp_path: Path) -> None:
    token = "t" * 40
    with pytest.raises(ConfigError) as excinfo:
        load_config(_toml(tmp_path, f'[namespaces]\n"{token}" = 7\n'))

    assert token not in str(excinfo.value)


def test_strong_namespace_tokens_load(tmp_path: Path) -> None:
    tokens = {"a" * 40: "ns-a", "B" * 33: "ns-b"}
    body = "[namespaces]\n" + "".join(f'"{token}" = "{ns}"\n' for token, ns in tokens.items())

    assert load_config(_toml(tmp_path, body)).namespaces == tokens


def test_the_same_token_for_two_namespaces_is_not_expressible_in_toml(tmp_path: Path) -> None:
    """A repeated key is a TOML parse error, so no token can ever map to two namespaces."""
    token = "t" * 40
    with pytest.raises(ConfigError):
        load_config(_toml(tmp_path, f'[namespaces]\n"{token}" = "ns-a"\n"{token}" = "ns-b"\n'))


def test_namespace_token_problem_rules() -> None:
    assert namespace_token_problem("a" * MIN_API_KEY_LENGTH) is None
    assert namespace_token_problem("") == "it is empty"
    assert namespace_token_problem("short") is not None


def test_a_namespace_token_equal_to_the_default_key_is_refused_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the TOML record would resolve first and silently demote the default key from admin."""
    default_key = "c" * 64
    monkeypatch.setenv(ENV_VAR, default_key)
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.namespaces = {default_key: "ns-a"}

    with pytest.raises(ConfigError) as excinfo:
        create_app(config, JobStore(path=tmp_path / "jobs.json"))

    assert default_key not in str(excinfo.value)

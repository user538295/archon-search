"""M7: an existing key file holding an unusable key makes the server refuse to start, never silently replace the key.

The env-var path has always failed closed (``ConfigError``). The file path used to log an ERROR and then generate a
new key over the operator's file, locking every client out with nothing but a log line to say why. Refusing leaves the
file untouched, changes no credential, and names the file and the rule; the operator fixes or deletes it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.config import ConfigError
from archon_search.key_manager import ENV_VAR, load_key, load_or_generate_key

_VALUE_MARKER = "deadbeef"  # a recognisable fragment of the invalid value: no message may contain it
_VALID_KEY = "a" * 64


@pytest.fixture
def key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(ENV_VAR, raising=False)
    path = tmp_path / ".search.env"
    monkeypatch.setenv("ARCHON_SEARCH_KEY_FILE", str(path))
    return path


@pytest.mark.parametrize(
    "content",
    [
        f"{ENV_VAR}={_VALUE_MARKER}\n",
        f"{ENV_VAR}={'A' * 64}\n",
        f"{ENV_VAR}=\n",
        f"{ENV_VAR}={'z' * 64}\n",
    ],
    ids=["too-short", "uppercase", "empty-value", "not-hex"],
)
def test_an_invalid_key_in_the_key_file_stops_the_server_and_leaves_the_file_alone(key_file: Path, content: str) -> None:
    key_file.write_text(content)

    with pytest.raises(ConfigError) as refused:
        load_or_generate_key()

    message = str(refused.value)
    assert str(key_file) in message and ENV_VAR in message
    assert _VALUE_MARKER not in message
    assert key_file.read_text() == content
    assert [path.name for path in key_file.parent.iterdir()] == [key_file.name], "no backup, no new key file"


def test_the_refusal_tells_the_operator_what_to_do(key_file: Path) -> None:
    key_file.write_text(f"{ENV_VAR}={_VALUE_MARKER}\n")

    with pytest.raises(ConfigError, match="delete the file"):
        load_or_generate_key()


def test_a_valid_key_file_is_still_used(key_file: Path) -> None:
    key_file.write_text(f"{ENV_VAR}={_VALID_KEY}\n")

    assert load_or_generate_key() == (_VALID_KEY, f"file: {key_file}")


def test_a_missing_key_file_still_generates_a_key(key_file: Path) -> None:
    key, source = load_or_generate_key()

    assert source == "auto-generated"
    assert key_file.read_text() == f"{ENV_VAR}={key}\n"


def test_the_env_key_still_overrides_an_invalid_key_file(key_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit env key is the operator's decision; it is written over the broken file, as it always was."""
    key_file.write_text(f"{ENV_VAR}={_VALUE_MARKER}\n")
    monkeypatch.setenv(ENV_VAR, _VALID_KEY)

    assert load_or_generate_key() == (_VALID_KEY, "env var")
    assert key_file.read_text() == f"{ENV_VAR}={_VALID_KEY}\n"


def test_load_key_for_the_cli_still_answers_none_for_an_invalid_file(key_file: Path) -> None:
    """``load_key`` never generates or refuses on a file: a CLI command just finds no key."""
    key_file.write_text(f"{ENV_VAR}={_VALUE_MARKER}\n")

    assert load_key() is None

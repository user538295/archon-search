"""The wizard never prints the userinfo, query or fragment of a server URL the operator typed or config stored.

The base-URL prompt shows the stored value as its default (reprinted on every re-run) and the "No models available" notice
and the shared-model warning name the address: all of that lands in terminal scrollback and CI logs, so each shows a masked
form of the URL while Enter still accepts, and the config still stores, the real value.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from archon_search.install.wizard import (
    OllamaContextChoice,
    _prompt_llama_cpp_model,
    _prompt_ollama_model,
    _warn_shared_model_contexts,
)
from archon_search.llm_client.server_address import UNKNOWN_HOST

_SECRETS = ("svc-u5er", "s3cr3t", "q9")
_STORED_URL = "http://svc-u5er:s3cr3t@host.test:11434/?t=q9"
_MASKED_URL = "http://host.test:11434"
_MODEL = "m1"


class _Console:
    """``builtins.input`` stand-in that echoes each prompt to stdout, as the real one does, and answers from a script."""

    def __init__(self, answers: list[str]) -> None:
        self._answers: Iterator[str] = iter(answers)

    def __call__(self, prompt: str = "") -> str:
        print(prompt, end="")
        return next(self._answers)


def _assert_no_secret(text: str) -> None:
    for secret in _SECRETS:
        assert secret not in text, secret


def _ask_ollama(default: str, answers: list[str], models: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[tuple[str, str], str]:
    with patch("archon_search.install.wizard._fetch_ollama_models", return_value=models), patch("builtins.input", _Console(answers)):
        result = _prompt_ollama_model("HyDE", default)
    return result, capsys.readouterr().out


def test_the_ollama_prompt_masks_the_stored_default_and_enter_still_stores_the_real_value(capsys: pytest.CaptureFixture[str]) -> None:
    (stored, model), out = _ask_ollama(_STORED_URL, ["", _MODEL], [], capsys)

    assert f"Ollama base URL for HyDE [{_MASKED_URL}]: " in out
    assert f"No models available from {_MASKED_URL}." in out
    _assert_no_secret(out)
    assert (stored, model) == (_STORED_URL, _MODEL)


def test_a_typed_url_is_masked_in_the_notice_and_stored_whole(capsys: pytest.CaptureFixture[str]) -> None:
    (stored, _), out = _ask_ollama("", [_STORED_URL, _MODEL], [], capsys)

    assert f"No models available from {_MASKED_URL}." in out
    _assert_no_secret(out)
    assert stored == _STORED_URL


def test_a_found_model_list_does_not_bring_the_url_back(capsys: pytest.CaptureFixture[str]) -> None:
    (stored, model), out = _ask_ollama(_STORED_URL, ["", "1"], [_MODEL], capsys)

    _assert_no_secret(out)
    assert (stored, model) == (_STORED_URL, _MODEL)


@pytest.mark.parametrize(
    ("default", "shown"),
    [
        ("svc-u5er:s3cr3t@host.test", "host.test"),
        ("https://svc-u5er:s3cr3t@host.test/llama-a/?t=q9#q9", "https://host.test/llama-a"),
        ("host.test:9/api?t=q9", "host.test:9/api"),
        ("http://svc-u5er:s3cr3t@[::1]:11434", "http://[::1]:11434"),
    ],
)
def test_a_stored_default_shows_scheme_host_port_and_path_only(default: str, shown: str, capsys: pytest.CaptureFixture[str]) -> None:
    _, out = _ask_ollama(default, ["", _MODEL], [], capsys)

    assert f"Ollama base URL for HyDE [{shown}]: " in out
    _assert_no_secret(out)


@pytest.mark.parametrize(
    "default",
    [
        "svc-u5er:12345/x@gpu.test",
        "http://gpu.test:11434/svc-u5er:s3cr3t@x",
        "http://gpu.test/?t=q9@s3cr3t",
        "http://gpu.test:11434\x1b[2J/svc-u5er",
    ],
)
def test_a_default_that_may_hide_a_credential_in_its_path_is_not_shown_at_all(default: str, capsys: pytest.CaptureFixture[str]) -> None:
    _, out = _ask_ollama(default, ["", _MODEL], [], capsys)

    assert f"[{UNKNOWN_HOST}]: " in out
    assert f"No models available from {UNKNOWN_HOST}." in out
    _assert_no_secret(out)
    assert "gpu.test" not in out
    assert "\x1b" not in out


def test_the_llama_server_prompt_masks_the_stored_default_too(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("archon_search.install.wizard._fetch_llama_cpp_models", return_value=[_MODEL]),
        patch("builtins.input", _Console(["", "1"])),
    ):
        stored, model = _prompt_llama_cpp_model("HyDE", "http://svc-u5er:s3cr3t@host.test:8080/?t=q9")
    out = capsys.readouterr().out

    assert "llama-server base URL for HyDE [http://host.test:8080]: " in out
    _assert_no_secret(out)
    assert (stored, model) == ("http://svc-u5er:s3cr3t@host.test:8080/?t=q9", _MODEL)


def test_the_shared_model_warning_masks_the_address(capsys: pytest.CaptureFixture[str]) -> None:
    _warn_shared_model_contexts(
        [OllamaContextChoice("HyDE", _STORED_URL, _MODEL, 4096), OllamaContextChoice("RAG Fusion", _STORED_URL, _MODEL, 8192)]
    )
    out = capsys.readouterr().out

    assert f"share the Ollama model {_MODEL} at {_MASKED_URL} but use" in out
    _assert_no_secret(out)

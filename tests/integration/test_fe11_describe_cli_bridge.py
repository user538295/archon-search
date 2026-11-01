"""FE-11 (collection-descriptions plan, S29, S32, S58): ``collection describe`` against the real BE-34 route.

A real app boots with ``[description]`` on a loopback fake model server; the CLI runs through the T-5 bridge. The full
end-to-end set (mixed namespaces, races, shutdown) belongs to T-10; these two prove the CLI and the route agree on the
wire shape.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search.cli.collection import collection
from archon_search.constants import DESCRIBE_NOT_CONFIGURED_DETAIL
from tests._cli_testclient_bridge import cli_against_app
from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "describe-me"
_REPLY = "The description the describe job wrote."
_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.01
_DOCUMENT_REPEATS = 20


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)
    monkeypatch.setattr("archon_search.cli._helpers._POLL_INTERVAL_SECONDS", _POLL_SECONDS)


def _toml(base_url: str, *, enabled: bool) -> str:
    return (
        "[description]\n"
        f"enabled = {str(enabled).lower()}\n"
        'provider = "ollama"\n'
        'model = "description-model"\n'
        f'ollama_base_url = "{base_url}"\n'
        "ollama_context_tokens = 8192\n"
    )


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} did not happen within {_WAIT_SECONDS}s")
        time.sleep(_POLL_SECONDS)


def _boot_with_corpus(
    stack_client: tuple[TestClient, object, str], tmp_path: Path, *, enabled: bool
) -> tuple[TestClient, str]:
    client, _config, api_key = stack_client
    _wait_for(client.app.state._llm_window_task.done, "the context window resolution")
    document = tmp_path / "corpus" / "notes.txt"
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text("Archon search keeps collections of local documents searchable.\n" * _DOCUMENT_REPEATS, encoding="utf-8")
    ingest_file_via_path(client, _COLLECTION, str(document.parent), api_key=api_key)
    lifecycle = client.app.state.description_lifecycle
    if enabled and lifecycle is not None:
        _wait_for(lambda: not lifecycle.is_pending(_COLLECTION), "the ingest's generation to settle")
    return client, api_key


def test_describe_wait_generates_through_the_real_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S29, S58: ``describe <name> --wait`` polls the real job to ``DONE``, prints the outcome and the new text, exits 0."""
    with (
        FakeModelServer(reply_text="The first description the ingest wrote.") as server,
        make_real_app(tmp_path, monkeypatch, toml_content=_toml(server.base_url, enabled=True)) as booted,
    ):
        client, api_key = _boot_with_corpus(booted, tmp_path, enabled=True)
        server.reply_text = _REPLY

        with cli_against_app(client, api_key, monkeypatch) as cli:
            result = cli.invoke(collection, ["describe", _COLLECTION, "--wait"])
            all_result = cli.invoke(collection, ["describe", "--all"])

    assert result.exit_code == 0, result.output + result.stderr
    assert "outcome generated" in result.output
    assert _REPLY in result.output
    assert "description_source: generated" in result.output
    assert all_result.exit_code == 0, all_result.output + all_result.stderr
    assert "already described" in all_result.output


def test_describe_not_configured_prints_the_setup_message_and_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S32: descriptions off: the server's 409 setup message reaches the terminal, once, in single and ``--all`` mode."""
    with (
        FakeModelServer() as server,
        make_real_app(tmp_path, monkeypatch, toml_content=_toml(server.base_url, enabled=False)) as booted,
    ):
        client, api_key = _boot_with_corpus(booted, tmp_path, enabled=False)

        with cli_against_app(client, api_key, monkeypatch) as cli:
            single = cli.invoke(collection, ["describe", _COLLECTION])
            everything = cli.invoke(collection, ["describe", "--all"])

    for result in (single, everything):
        assert result.exit_code == 1, result.output + result.stderr
        assert result.stderr.count(DESCRIBE_NOT_CONFIGURED_DETAIL) == 1

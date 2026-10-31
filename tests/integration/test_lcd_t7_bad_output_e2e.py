"""T-7 (collection-descriptions plan, S53, S54) — what a stored description does when the model answers badly.

Same real app, real LanceDB and loopback ``FakeModelServer`` as T-5 (its helpers are reused). Each test starts a real
ingest and reads what a consumer sees through REST (two of them also ``collection info``, through
``tests/_cli_testclient_bridge.py``); the first one makes the model misbehave on a re-qualifying ingest after a good
description exists, the others from the first ingest:

* an answer that is only thinking, twice (the answer and the one retry) -> status ``failed``, the earlier text is kept
  and the log holds one WARNING that points to the ``think`` setting (S53, and S21 of the plan for the failed status);
* an over-long answer with line breaks, twice -> stored as one line cut at the last sentence end within 600 characters,
  and the retry is the same single message plus the reminder (S53);
* the cloud path: provider ``openai`` is aimed at the fake server through the SDK's own ``OPENAI_BASE_URL`` variable (no
  production key; the model-host guard lets an ``openai`` client through when it targets a ``FakeModelServer``), and the
  fake's ``/v1/chat/completions`` speaks the OpenAI shape, so the excerpts really are sent twice (S53). ``anthropic``
  clients are refused by the guard unconditionally, so that provider has no e2e (the retry code is the same for every
  provider; the wire shape is unit-tested in ``tests/test_llm_client_cloud.py``);
* the ``think`` setting on the Ollama wire: ``default`` sends nothing, ``true`` and ``false`` ride on every call, the retry
  included (S54's sending rules; the ``llama_cpp`` switch, the unsupported-model WARNING and the ignored-by-cloud rules are
  unit-tested in BE-30, and the real-model arms are in ``test_lcd_t7_manual_checklist.md``).

Only the answer and its retry are the same text twice here; an over-long first answer followed by an acceptable retry, and
an empty retry after an over-long answer, are covered by the generator's unit tests.

Run with:
    uv run pytest tests/integration/test_lcd_t7_bad_output_e2e.py --no-cov -q
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.cli.collection import collection as collection_command
from archon_search.constants import MAX_DESCRIPTION_CHARS
from tests._cli_testclient_bridge import cli_against_app
from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies
from tests.integration.test_lcd_t5_first_description_e2e import (
    _CHAT_PATH,
    _COLLECTION,
    _DESCRIBED_DOCUMENTS,
    _FIRST_DOCUMENT_MARKER,
    _FIRST_REPLY,
    _PROVIDER_TIMEOUT_SECONDS,
    _await_description,
    _booted,
    _description_toml,
    _detail,
    _document,
    _filler_documents,
    _ingest,
    _prompt_of,
    _wait_for,
    _write_documents,
)

pytestmark = pytest.mark.integration

_GENERATOR_LOGGER = "archon_search.description_generator"
_ATTEMPTS = 2  # the answer and the one automatic retry
_THINKING_TEXT = "I should weigh how to word the summary"
_THINKING_ONLY = f"<think>The excerpts are field notes; {_THINKING_TEXT}.</think>"
_OPENAI_CHAT_PATH = "/v1/chat/completions"
_OPENAI_MODEL = "description-cloud-model"
_OPENAI_KEY = "test-key-not-real"
_SENTENCES = 40  # well over the limit, so the trim has real sentence ends to choose from
_RETRY_REMINDER_MARK = "Your previous answer was empty or longer than"


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's HTTP_PROXY must not capture the loopback requests."""
    keep_loopback_out_of_proxies(monkeypatch)


def _sentence(index: int) -> str:
    return f"Sentence number {index:02d} covers the valley survey."


def _overlong_answer() -> str:
    """Forty numbered sentences, one per line: more than 600 characters and with line breaks."""
    return "\n".join(_sentence(index) for index in range(_SENTENCES))


def _expected_trim() -> str:
    """The cleaned answer cut after the last whole sentence that fits: sentences joined by one space, at most 600 characters."""
    assert len(_overlong_answer()) > MAX_DESCRIPTION_CHARS  # the answer must outgrow the limit for a trim to exist
    fitting = (MAX_DESCRIPTION_CHARS + 1) // (len(_sentence(0)) + 1)  # n sentences take n * length + (n - 1) characters
    return " ".join(_sentence(index) for index in range(fitting))


def _description_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.WARNING and record.name == _GENERATOR_LOGGER]


def _everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    """Message, arguments and traceback text of every record: a model answer must not hide in any of them."""
    return " ".join(f"{record.getMessage()} {record.args} {record.exc_text}" for record in caplog.records)


def _await_status(client: TestClient, api_key: str, status: str) -> dict[str, Any]:
    latest: dict[str, Any] = {}

    def reached() -> bool:
        latest.update(_detail(client, api_key))
        return latest["description_status"] == status

    _wait_for(reached, f"status {status!r}")
    return latest


def _openai_toml() -> str:
    return (
        "[description]\n"
        "enabled = true\n"
        'provider = "openai"\n'
        f'model = "{_OPENAI_MODEL}"\n'
        f"timeout_seconds = {_PROVIDER_TIMEOUT_SECONDS}\n"
        "think = true\n"
    )


def test_e2e_thinking_only_output_stores_failed_keeps_old_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    corpus = tmp_path / "corpus"
    _write_documents(corpus, _filler_documents(_DESCRIBED_DOCUMENTS))
    with (
        FakeModelServer(reply_text=_FIRST_REPLY) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, api_key),
        cli_against_app(client, api_key, monkeypatch) as cli,
    ):
        _ingest(client, api_key, corpus)
        _await_description(client, api_key, _FIRST_REPLY)
        assert not _description_warnings(caplog)

        # The collection doubles, which asks for a new description; the model now only thinks.
        server.reply_text = _THINKING_ONLY
        _write_documents(corpus, _filler_documents(_DESCRIBED_DOCUMENTS, start=_DESCRIBED_DOCUMENTS))
        _ingest(client, api_key, corpus)
        failed = _await_status(client, api_key, "failed")
        info = cli.invoke(collection_command, ["info", _COLLECTION])

    assert (failed["description"], failed["description_source"]) == (_FIRST_REPLY, "generated")
    assert info.exit_code == 0, info.output
    assert f"description: {_FIRST_REPLY}" in info.output
    assert "description_status: failed" in info.output
    assert len(server.requests_to(_CHAT_PATH)) == 1 + _ATTEMPTS  # the first generation, then the answer and its one retry
    warnings = _description_warnings(caplog)
    assert len(warnings) == 1
    assert "`think`" in warnings[0].getMessage()
    assert _COLLECTION in warnings[0].getMessage()
    assert _THINKING_TEXT not in _everything_logged(caplog)  # the model's own text never reaches the log


def test_e2e_overlong_answer_stored_trimmed_to_one_line_within_600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    corpus = tmp_path / "corpus"
    _write_documents(corpus, {"first.txt": _document(_FIRST_DOCUMENT_MARKER)})
    answer = _overlong_answer()
    assert len(answer) > MAX_DESCRIPTION_CHARS
    with (
        FakeModelServer(reply_text=answer) as server,
        _booted(tmp_path, monkeypatch, _description_toml(server.base_url)) as (client, api_key),
        cli_against_app(client, api_key, monkeypatch) as cli,
    ):
        _ingest(client, api_key, corpus)
        stored = _await_status(client, api_key, "ok")
        info = cli.invoke(collection_command, ["info", _COLLECTION])

    text = stored["description"]
    assert stored["description_source"] == "generated"
    assert "\n" not in text
    assert len(text) <= MAX_DESCRIPTION_CHARS
    assert text == _expected_trim()  # the last whole sentence that fits: not the first one, not a word cut in the middle
    assert info.exit_code == 0, info.output
    assert f"description: {text}" in info.output
    first, retry = (_prompt_of(request) for request in server.requests_to(_CHAT_PATH))
    assert _RETRY_REMINDER_MARK not in first
    assert retry.startswith(first)
    assert retry.removeprefix(first).startswith("\n\n" + _RETRY_REMINDER_MARK)
    assert not _description_warnings(caplog)  # a trimmed answer is stored, not a failure


def test_e2e_cloud_retry_sends_excerpts_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider ``openai`` aimed at the fake server with the SDK's ``OPENAI_BASE_URL`` (read by ``openai.AsyncOpenAI``)."""
    corpus = tmp_path / "corpus"
    _write_documents(corpus, {"first.txt": _document(_FIRST_DOCUMENT_MARKER)})
    monkeypatch.setenv("OPENAI_API_KEY", _OPENAI_KEY)
    with FakeModelServer(reply_text=_overlong_answer()) as server:
        monkeypatch.setenv("OPENAI_BASE_URL", f"{server.base_url}/v1")
        with _booted(tmp_path, monkeypatch, _openai_toml()) as (client, api_key):
            _ingest(client, api_key, corpus)
            stored = _await_status(client, api_key, "ok")
            description_client = client.app.state.description_client
            assert description_client._client is not None  # the SDK client was built by the generation
        assert description_client._client is None  # shutdown closed it: a leaked SDK client is closed on a dead loop at GC

    sent = server.requests_to(_OPENAI_CHAT_PATH)
    assert len(sent) == _ATTEMPTS
    assert not server.requests_to(_CHAT_PATH)  # nothing went to the Ollama endpoint
    assert all(request.body is not None and request.body["model"] == _OPENAI_MODEL for request in sent)
    assert all("think" not in (request.body or {}) for request in sent)  # `think = true` is ignored by a cloud provider (S54)
    prompts = [_prompt_of(request) for request in sent]
    assert all(_FIRST_DOCUMENT_MARKER in prompt for prompt in prompts)  # the excerpts travel with both attempts
    assert _RETRY_REMINDER_MARK not in prompts[0]
    assert prompts[1].startswith(prompts[0]) and _RETRY_REMINDER_MARK in prompts[1]
    assert stored["description"] == _expected_trim()
    assert "\n" not in stored["description"]


@pytest.mark.parametrize(
    ("toml_line", "sent"),
    [("", None), ('think = "default"\n', None), ("think = true\n", True), ("think = false\n", False)],
    ids=["unset", "default", "true", "false"],
)
def test_e2e_think_setting_rides_on_every_ollama_call_retry_included(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml_line: str, sent: bool | None
) -> None:
    """S54: ``default`` (the key unset, or the word) sends no thinking control; ``true`` and ``false`` ride on both attempts."""
    corpus = tmp_path / "corpus"
    _write_documents(corpus, {"first.txt": _document(_FIRST_DOCUMENT_MARKER)})
    with FakeModelServer(reply_text=_overlong_answer()) as server:
        toml = _description_toml(server.base_url) + toml_line  # appended to the [description] section
        with _booted(tmp_path, monkeypatch, toml) as (client, api_key):
            _ingest(client, api_key, corpus)
            _await_status(client, api_key, "ok")

    bodies = [request.body for request in server.requests_to(_CHAT_PATH)]
    assert len(bodies) == _ATTEMPTS
    for body in bodies:
        assert body is not None
        if sent is None:
            assert "think" not in body
        else:
            assert body["think"] is sent

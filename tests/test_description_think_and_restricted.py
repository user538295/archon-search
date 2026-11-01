"""BE-30 (collection-descriptions plan, C1, C2, C4, S54, S56): ``think`` reaches the model as configured and the
restricted-chunk switch is honoured for the two local providers only.

The generator maps ``[description].think`` (``true`` / ``false`` / ``None`` for ``default``) onto the call and the
wrappers decide what goes on the wire; these tests run the real wrappers over recording transports.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Self

import httpx
import ollama
import pytest

from archon_search.config import DescriptionConfig
from archon_search.description_generator import (
    DescriptionFailed,
    DescriptionFailureReason,
    DescriptionGenerated,
    DescriptionGenerator,
)
from archon_search.llm_client.llama_cpp_client import LlamaCppClient
from archon_search.llm_client.ollama_client import OllamaClient
from archon_search.llm_client.protocol import ThinkSetting
from archon_search.llm_client.token_counter import TokenCounter
from tests._hyde_stubs import ScriptedLlmClient
from tests._token_counter_stub import StubTokenizer

_COLLECTION = "handbook"
_NAMESPACE = "default"
_MODEL = "qwen3:8b"
_NON_THINKING_MODEL = "llama3.1:8b"
_OTHER_NON_THINKING_MODEL = "gemma3:4b"
_REPLY = "A handbook of company policies."
_EXCERPT_PREFIX = "excerpt of"
_RESTRICTED_EXCERPT = f"{_EXCERPT_PREFIX} secret-1"
_CONTEXT_TOKENS = 8192
_THINK_HINT = "[description].think is true"
_OLLAMA_URL = "http://ollama.test:11434"
_LLAMA_URL = "http://llama.test:8080"
_GENERATOR_LOGGER = "archon_search.description_generator"

_LOCAL_PROVIDERS = ["ollama", "llama_cpp"]
_OTHER_PROVIDERS = ["anthropic", "openai", "claude_cli"]


class _Store:
    """Two open chunks and one restricted; like the real port, a read keeps restricted chunks only when asked to.
    Records the restricted flag of every read."""

    def __init__(self) -> None:
        self.list_flags: list[bool] = []
        self.read_flags: list[bool] = []

    async def list_chunk_ids(self, collection: str, namespace: str, include_restricted: bool = False) -> list[str]:
        self.list_flags.append(include_restricted)
        return ["open-1", "open-2", "secret-1"] if include_restricted else ["open-1", "open-2"]

    async def read_excerpts(
        self, collection: str, namespace: str, chunk_ids: list[str], include_restricted: bool = False
    ) -> list[str]:
        self.read_flags.append(include_restricted)
        return [f"{_EXCERPT_PREFIX} {chunk_id}" for chunk_id in chunk_ids]


class _RecordingOllamaSdk:
    """Stands in for ``ollama.AsyncClient``: records ``chat`` arguments, answers or raises."""

    def __init__(self, calls: list[dict[str, Any]], error: BaseException | None) -> None:
        self._calls = calls
        self._error = error

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def chat(self, **kwargs: Any) -> ollama.ChatResponse:
        self._calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return ollama.ChatResponse(model=_MODEL, message=ollama.Message(role="assistant", content=_REPLY))


class _OllamaWire:
    """A real ``OllamaClient`` whose SDK is recorded: ``chat_calls`` is what would go to ``/api/chat``."""

    def __init__(self, *, model: str = _MODEL, error: BaseException | None = None) -> None:
        self.chat_calls: list[dict[str, Any]] = []
        self.client = OllamaClient(
            model=model,
            base_url=_OLLAMA_URL,
            context_tokens=_CONTEXT_TOKENS,
            token_counter=TokenCounter(load_tokenizer=StubTokenizer),
            client_factory=lambda host: _RecordingOllamaSdk(self.chat_calls, error),
        )

    def sent_think(self) -> list[object]:
        """The ``think`` argument of every call, ``"absent"`` where none was sent."""
        return [call.get("think", "absent") for call in self.chat_calls]


class _LlamaCppWire:
    """A real ``LlamaCppClient`` over a recording ``MockTransport``: ``chat_bodies`` is what ``/v1/chat/completions`` got."""

    def __init__(self) -> None:
        self.chat_bodies: list[dict[str, Any]] = []
        self.client = LlamaCppClient(model=_MODEL, base_url=_LLAMA_URL, transport=httpx.MockTransport(self._answer))

    def _answer(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": list(range(len(body["content"].split())))})
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": body["messages"][0]["content"]})
        self.chat_bodies.append(body)
        return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": _REPLY}}]})

    def sent_switch(self) -> list[object]:
        """The ``chat_template_kwargs`` of every call, ``"absent"`` where none was sent."""
        return [body.get("chat_template_kwargs", "absent") for body in self.chat_bodies]


async def _generate(client: Any, *, store: _Store | None = None, **config: Any) -> DescriptionGenerated | DescriptionFailed:
    generator = DescriptionGenerator(DescriptionConfig(**config), client, store or _Store())
    return await generator.generate(_COLLECTION, _NAMESPACE)


# --- think: what the generator puts on the call -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(None, ThinkSetting.DEFAULT), (True, ThinkSetting.ON), (False, ThinkSetting.OFF)],
)
async def test_generator_puts_the_configured_think_on_every_call(configured: bool | None, expected: ThinkSetting) -> None:
    client = ScriptedLlmClient(reply=_REPLY)

    await _generate(client, think=configured)

    assert [request.think for request in client.requests] == [expected]


async def test_the_retry_carries_the_same_think_setting() -> None:
    client = ScriptedLlmClient(reply="A line. " * 200)

    await _generate(client, think=False)

    assert [request.think for request in client.requests] == [ThinkSetting.OFF, ThinkSetting.OFF]


@pytest.mark.parametrize(
    ("configured", "setting", "switch"),
    [(None, ThinkSetting.DEFAULT, None), (True, ThinkSetting.ON, True), (False, ThinkSetting.OFF, False)],
)
def test_think_setting_maps_the_config_value_and_names_the_switch_to_send(
    configured: bool | None, setting: ThinkSetting, switch: bool | None
) -> None:
    assert ThinkSetting.from_config(configured) is setting
    assert setting.enabled is switch


# --- think: what the wrappers send ---------------------------------------------------------------------------------


async def test_think_default_sends_no_thinking_control() -> None:
    ollama_wire, llama_wire = _OllamaWire(), _LlamaCppWire()

    await _generate(ollama_wire.client, provider="ollama", think=None)
    await _generate(llama_wire.client, provider="llama_cpp", think=None)

    assert ollama_wire.sent_think() == ["absent"]
    assert llama_wire.sent_switch() == ["absent"]
    assert "enable_thinking" not in json.dumps(llama_wire.chat_bodies)


@pytest.mark.parametrize("configured", [True, False])
async def test_think_true_and_false_sent_to_ollama(configured: bool) -> None:
    wire = _OllamaWire()

    outcome = await _generate(wire.client, provider="ollama", think=configured)

    assert isinstance(outcome, DescriptionGenerated)
    assert wire.sent_think() == [configured]


@pytest.mark.parametrize("configured", [True, False])
async def test_think_true_and_false_sent_to_llama_cpp_as_the_template_switch(configured: bool) -> None:
    wire = _LlamaCppWire()

    outcome = await _generate(wire.client, provider="llama_cpp", think=configured)

    assert isinstance(outcome, DescriptionGenerated)
    assert wire.sent_switch() == [{"enable_thinking": configured}]


# --- think: the WARNING for a model without thinking ---------------------------------------------------------------


def _no_thinking_error(model: str) -> ollama.ResponseError:
    return ollama.ResponseError(f'"{model}" does not support thinking', 400)


async def test_think_true_on_non_thinking_model_warns(caplog: pytest.LogCaptureFixture) -> None:
    wire = _OllamaWire(model=_NON_THINKING_MODEL, error=_no_thinking_error(_NON_THINKING_MODEL))
    caplog.set_level(logging.WARNING)

    outcome = await _generate(wire.client, provider="ollama", model=_NON_THINKING_MODEL, think=True)

    assert isinstance(outcome, DescriptionFailed)
    assert outcome.reason is DescriptionFailureReason.CALL_FAILED
    assert len(wire.chat_calls) == 1, "a provider error is never retried"
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    [warning] = warnings  # one WARNING in total: the specific one replaces the generic failure line
    assert _THINK_HINT in warning
    assert _NON_THINKING_MODEL in warning and "no thinking mode" in warning and '"default" or false' in warning
    assert _EXCERPT_PREFIX not in warning


async def test_the_non_thinking_model_warning_is_logged_once_per_model(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    for model in (_NON_THINKING_MODEL, _NON_THINKING_MODEL, _OTHER_NON_THINKING_MODEL, _OTHER_NON_THINKING_MODEL):
        wire = _OllamaWire(model=model, error=_no_thinking_error(model))
        await _generate(wire.client, provider="ollama", model=model, think=True)

    texts = [record.getMessage() for record in caplog.records if _THINK_HINT in record.getMessage()]
    assert len(texts) == 2
    assert _NON_THINKING_MODEL in texts[0] and _OTHER_NON_THINKING_MODEL in texts[1]


async def test_the_non_thinking_failure_does_not_use_up_the_generic_provider_failure_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    await _generate(
        _OllamaWire(model=_NON_THINKING_MODEL, error=_no_thinking_error(_NON_THINKING_MODEL)).client,
        provider="ollama",
        model=_NON_THINKING_MODEL,
        think=True,
    )
    caplog.clear()

    await _generate(_OllamaWire(error=ConnectionError("refused")).client, provider="ollama", think=True)

    [warning] = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert "Description generation failed" in warning


async def test_any_other_provider_error_gets_no_thinking_warning(caplog: pytest.LogCaptureFixture) -> None:
    wire = _OllamaWire(error=ollama.ResponseError('model "qwen3:8b" not found', 404))
    caplog.set_level(logging.WARNING)

    outcome = await _generate(wire.client, provider="ollama", think=True)

    assert isinstance(outcome, DescriptionFailed)
    warnings = [r.getMessage() for r in caplog.records if r.name == _GENERATOR_LOGGER and r.levelno == logging.WARNING]
    assert not [text for text in warnings if _THINK_HINT in text]
    assert any("Description generation failed" in text for text in warnings)


# --- restricted chunks ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("provider", _LOCAL_PROVIDERS)
@pytest.mark.parametrize("switch", [False, True])
async def test_local_providers_follow_the_restricted_switch(provider: str, switch: bool) -> None:
    store = _Store()
    client = ScriptedLlmClient(reply=_REPLY)

    await _generate(client, store=store, provider=provider, include_restricted_chunks=switch)

    assert store.list_flags == [switch]
    assert store.read_flags == [switch]
    assert (_RESTRICTED_EXCERPT in client.requests[0].prompt) is switch


@pytest.mark.parametrize("provider", _OTHER_PROVIDERS)
@pytest.mark.parametrize("switch", [False, True])
async def test_restricted_chunks_sampled_only_for_local_providers(provider: str, switch: bool) -> None:
    store = _Store()
    client = ScriptedLlmClient(reply=_REPLY)

    await _generate(client, store=store, provider=provider, include_restricted_chunks=switch)

    assert store.list_flags == [False]
    assert store.read_flags == [False]
    assert _RESTRICTED_EXCERPT not in client.requests[0].prompt

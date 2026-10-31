"""BE-7 (collection-descriptions plan, C1, S6): the ``anthropic`` and ``openai`` wrappers of the shared LLM client.

The SDK client is replaced by a recording fake injected through the wrapper's ``client_factory``; the lazy build of
the real SDK client is checked against the SDK module with its constructor patched, so no test builds a real client.
"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import anthropic
import httpx
import openai
import pytest

from archon_search.constants import ANSWER_RESERVE_TOKENS
from archon_search.llm_client.anthropic_client import AnthropicClient
from archon_search.llm_client.openai_client import OpenAIClient
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    InputBudget,
    LlmCallFailure,
    LlmCallRequest,
    LlmCallTimeout,
    LlmClient,
    ThinkSetting,
)
from archon_search.uv_tool import extra_install_hint

_MODEL = "test-model"
_PROMPT = "describe this collection in one line"
_SECRET_PROMPT = "excerpt-that-must-never-leak-into-a-reason"
_SECOND_PROMPT = "a completely different second prompt"
_REPLY = "a one line description"
_MAX_OUTPUT_TOKENS = ANSWER_RESERVE_TOKENS
_TIMEOUT_SECONDS = 5.0
_SHORT_TIMEOUT_SECONDS = 0.05
# Provider prefix and "HTTP 500: " (about 35 characters) plus the bounded server text (200) and its ellipsis.
_MAX_REASON_CHARS = 240
_SERVER_ERROR_STATUS = 500
_REQUEST = httpx.Request("POST", "https://api.test/v1")


@dataclass(frozen=True)
class _Flavour:
    """What differs between the two cloud wrappers, so one test body covers both."""

    provider: str
    key_variable: str
    sdk: Any
    wrapper: Callable[..., Any]
    reply_shape: Callable[[str | None], Any]
    max_tokens_argument: str
    module_file: str
    constructor: str
    extra: str


def _anthropic_reply(text: str | None) -> Any:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text or "")])


def _openai_reply(text: str | None) -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


_ANTHROPIC = _Flavour("anthropic", "ANTHROPIC_API_KEY", anthropic, AnthropicClient, _anthropic_reply, "max_tokens", "anthropic_client", "AsyncAnthropic", "hyde")
_OPENAI = _Flavour("openai", "OPENAI_API_KEY", openai, OpenAIClient, _openai_reply, "max_completion_tokens", "openai_client", "AsyncOpenAI", "openai-provider")
_FLAVOURS = pytest.mark.parametrize("flavour", [_ANTHROPIC, _OPENAI], ids=lambda f: f.provider)


_DEFAULT_REPLY = object()


class _FakeSdkClient:
    """Stands in for ``AsyncAnthropic`` / ``AsyncOpenAI``: records ``create`` arguments, answers or raises."""

    def __init__(self, flavour: _Flavour, *, reply: Any = _DEFAULT_REPLY, error: BaseException | None = None, delay: float = 0.0) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self._reply = reply if reply is not _DEFAULT_REPLY else flavour.reply_shape(_REPLY)
        self._error = error
        self._delay = delay
        endpoint = SimpleNamespace(create=self._create)
        # Only the path the real SDK client has: client.messages.create, or client.chat.completions.create.
        if flavour is _ANTHROPIC:
            self.messages = endpoint
        else:
            self.chat = SimpleNamespace(completions=endpoint)

    async def _create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._reply


def _wrapper(flavour: _Flavour, fake: _FakeSdkClient) -> Any:
    return flavour.wrapper(model=_MODEL, client_factory=lambda: fake)


def _request(prompt: str = _PROMPT, *, timeout: float = _TIMEOUT_SECONDS, think: ThinkSetting = ThinkSetting.DEFAULT) -> LlmCallRequest:
    return LlmCallRequest(prompt=prompt, max_output_tokens=_MAX_OUTPUT_TOKENS, timeout_seconds=timeout, think=think)


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")


@_FLAVOURS
def test_wrapper_satisfies_the_client_protocol(flavour: _Flavour) -> None:
    assert isinstance(_wrapper(flavour, _FakeSdkClient(flavour)), LlmClient)


@_FLAVOURS
async def test_wrapper_returns_the_answer_text(flavour: _Flavour) -> None:
    result = await _wrapper(flavour, _FakeSdkClient(flavour)).call(_request())

    assert result.text == _REPLY


@_FLAVOURS
async def test_cloud_wrappers_send_one_user_message_no_history(flavour: _Flavour) -> None:
    fake = _FakeSdkClient(flavour)
    client = _wrapper(flavour, fake)

    await client.call(_request(_PROMPT))
    await client.call(_request(_SECOND_PROMPT))

    first, second = fake.create_calls
    assert first["messages"] == [{"role": "user", "content": _PROMPT}]
    assert second["messages"] == [{"role": "user", "content": _SECOND_PROMPT}]
    assert first["model"] == second["model"] == _MODEL
    assert first[flavour.max_tokens_argument] == _MAX_OUTPUT_TOKENS
    assert "system" not in first
    assert _PROMPT not in repr(second)


@_FLAVOURS
@pytest.mark.parametrize("think", [ThinkSetting.ON, ThinkSetting.OFF, ThinkSetting.DEFAULT])
async def test_think_is_ignored(flavour: _Flavour, think: ThinkSetting) -> None:
    fake = _FakeSdkClient(flavour)

    await _wrapper(flavour, fake).call(_request(think=think))

    (sent,) = fake.create_calls
    assert set(sent) == {"model", "messages", flavour.max_tokens_argument}


@_FLAVOURS
def test_context_window_is_unbounded(flavour: _Flavour) -> None:
    assert _wrapper(flavour, _FakeSdkClient(flavour)).context_window() == UNBOUNDED


@_FLAVOURS
async def test_input_budget_is_unbounded_and_makes_no_call(flavour: _Flavour) -> None:
    fake = _FakeSdkClient(flavour)

    budget = await _wrapper(flavour, fake).input_budget("a template that is never counted")

    assert budget == InputBudget(tokens=UNBOUNDED)
    assert fake.create_calls == []


@_FLAVOURS
async def test_empty_answer_is_an_empty_text_not_a_failure(flavour: _Flavour) -> None:
    result = await _wrapper(flavour, _FakeSdkClient(flavour, reply=flavour.reply_shape(None))).call(_request())

    assert result.text == ""


async def test_anthropic_wrapper_joins_text_blocks_and_skips_others() -> None:
    reply = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="private reasoning"),
            SimpleNamespace(type="text", text="first "),
            SimpleNamespace(type="text", text="second"),
        ]
    )

    result = await _wrapper(_ANTHROPIC, _FakeSdkClient(_ANTHROPIC, reply=reply)).call(_request())

    assert result.text == "first second"


async def test_anthropic_reply_without_text_blocks_is_an_empty_text() -> None:
    thinking_only = SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="private reasoning")])

    assert (await _wrapper(_ANTHROPIC, _FakeSdkClient(_ANTHROPIC, reply=thinking_only)).call(_request())).text == ""
    assert (await _wrapper(_ANTHROPIC, _FakeSdkClient(_ANTHROPIC, reply=SimpleNamespace(content=[]))).call(_request())).text == ""


async def test_openai_reply_without_choices_is_a_failure() -> None:
    fake = _FakeSdkClient(_OPENAI, reply=SimpleNamespace(choices=[]))

    with pytest.raises(LlmCallFailure, match="unexpected reply"):
        await _wrapper(_OPENAI, fake).call(_request())


@_FLAVOURS
@pytest.mark.parametrize("malformed", ["<html>gateway page</html>", None, SimpleNamespace()], ids=["text", "none", "empty-object"])
async def test_malformed_reply_is_a_failure_not_a_raw_error(flavour: _Flavour, malformed: Any) -> None:
    with pytest.raises(LlmCallFailure, match="unexpected reply"):
        await _wrapper(flavour, _FakeSdkClient(flavour, reply=malformed)).call(_request())


# --- lazy SDK client -------------------------------------------------------------------------------------------------


async def _assert_builds_client_lazily(flavour: _Flavour) -> None:
    fake = _FakeSdkClient(flavour)
    with patch.object(flavour.sdk, flavour.constructor, return_value=fake) as build:
        client = flavour.wrapper(model=_MODEL)
        assert build.call_count == 0

        await client.call(_request())
        assert build.call_count == 1

        await client.call(_request(_SECOND_PROMPT))
        assert build.call_count == 1
    build.assert_called_once_with(max_retries=0)
    assert len(fake.create_calls) == 2


async def test_anthropic_wrapper_builds_client_lazily_on_first_call() -> None:
    await _assert_builds_client_lazily(_ANTHROPIC)


async def test_openai_wrapper_builds_client_lazily_on_first_call() -> None:
    await _assert_builds_client_lazily(_OPENAI)


# --- key guard -------------------------------------------------------------------------------------------------------


async def _assert_raises_when_key_missing(flavour: _Flavour, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(flavour.key_variable)
    built: list[object] = []
    client = flavour.wrapper(model=_MODEL, client_factory=lambda: built.append(object()))

    with pytest.raises(LlmCallFailure, match=flavour.key_variable) as raised:
        await client.call(_request(_SECRET_PROMPT))

    assert not isinstance(raised.value, LlmCallTimeout)
    assert raised.value.provider == flavour.provider
    assert _SECRET_PROMPT not in str(raised.value)
    assert built == []


async def test_anthropic_wrapper_raises_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    await _assert_raises_when_key_missing(_ANTHROPIC, monkeypatch)


async def test_openai_wrapper_raises_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    await _assert_raises_when_key_missing(_OPENAI, monkeypatch)


@_FLAVOURS
def test_is_key_available_follows_the_environment(flavour: _Flavour, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _wrapper(flavour, _FakeSdkClient(flavour))
    assert client.is_key_available() is True

    monkeypatch.delenv(flavour.key_variable)

    assert client.is_key_available() is False


# --- error mapping ---------------------------------------------------------------------------------------------------


@_FLAVOURS
async def test_slow_provider_raises_timeout_naming_the_limit(flavour: _Flavour) -> None:
    client = _wrapper(flavour, _FakeSdkClient(flavour, delay=_TIMEOUT_SECONDS))

    with pytest.raises(LlmCallTimeout, match="0.05s") as raised:
        await client.call(_request(timeout=_SHORT_TIMEOUT_SECONDS))

    assert raised.value.provider == flavour.provider


@_FLAVOURS
async def test_sdk_timeout_error_raises_timeout(flavour: _Flavour) -> None:
    client = _wrapper(flavour, _FakeSdkClient(flavour, error=flavour.sdk.APITimeoutError(request=_REQUEST)))

    with pytest.raises(LlmCallTimeout, match="timed out"):
        await client.call(_request())


@_FLAVOURS
async def test_status_error_carries_status_and_bounded_text_without_the_prompt(flavour: _Flavour) -> None:
    echoed = f"prompt is too long: {_SECRET_PROMPT} " + "x" * 1000
    response = httpx.Response(_SERVER_ERROR_STATUS, request=_REQUEST)
    error = flavour.sdk.InternalServerError(echoed, response=response, body=None)

    with pytest.raises(LlmCallFailure) as raised:
        await _wrapper(flavour, _FakeSdkClient(flavour, error=error)).call(_request(_SECRET_PROMPT))

    reason = str(raised.value)
    assert not isinstance(raised.value, LlmCallTimeout)
    assert f"HTTP {_SERVER_ERROR_STATUS}" in reason
    assert _SECRET_PROMPT not in reason
    assert len(reason) <= _MAX_REASON_CHARS
    assert raised.value.__cause__ is error


@_FLAVOURS
async def test_connection_error_names_the_provider_without_the_prompt(flavour: _Flavour) -> None:
    client = _wrapper(flavour, _FakeSdkClient(flavour, error=flavour.sdk.APIConnectionError(request=_REQUEST)))

    with pytest.raises(LlmCallFailure, match="cannot connect") as raised:
        await client.call(_request(_SECRET_PROMPT))

    assert not isinstance(raised.value, LlmCallTimeout)
    assert _SECRET_PROMPT not in str(raised.value)


@_FLAVOURS
async def test_unexpected_error_is_a_failure_naming_only_its_type(flavour: _Flavour) -> None:
    client = _wrapper(flavour, _FakeSdkClient(flavour, error=RuntimeError(_SECRET_PROMPT)))

    with pytest.raises(LlmCallFailure, match="unexpected RuntimeError") as raised:
        await client.call(_request(_SECRET_PROMPT))

    assert _SECRET_PROMPT not in str(raised.value)


@_FLAVOURS
async def test_cancellation_is_not_swallowed(flavour: _Flavour) -> None:
    task = asyncio.create_task(_wrapper(flavour, _FakeSdkClient(flavour, delay=_TIMEOUT_SECONDS)).call(_request()))
    await asyncio.sleep(_SHORT_TIMEOUT_SECONDS)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@_FLAVOURS
async def test_missing_package_failure_names_the_install_command(flavour: _Flavour, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, flavour.provider, None)
    client = flavour.wrapper(model=_MODEL)
    with pytest.raises(LlmCallFailure, match="not installed") as raised:
        await client.call(_request())

    assert extra_install_hint(flavour.extra) in str(raised.value)


# --- module hygiene --------------------------------------------------------------------------------------------------


@_FLAVOURS
def test_importing_the_wrapper_does_not_import_the_sdk(flavour: _Flavour) -> None:
    code = (
        "import sys; "
        f"import archon_search.llm_client.{flavour.module_file}; "
        f"sys.exit(1 if {flavour.provider!r} in sys.modules else 0)"
    )

    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stderr


@_FLAVOURS
def test_wrapper_module_keeps_the_key_guard_line(flavour: _Flavour) -> None:
    from tests.test_anthropic_key_guards import _GUARD_PATTERN

    source = importlib.import_module(f"archon_search.llm_client.{flavour.module_file}")
    text = Path(source.__file__).read_text(encoding="utf-8")

    assert _GUARD_PATTERN.search(text) is not None

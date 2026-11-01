"""BE-5 (collection-descriptions plan, C1, S6, S7): the Ollama wrapper on the native ``ollama`` SDK.

The SDK client is replaced by a recording fake injected through the wrapper's ``client_factory``; the real SDK
against a loopback server is covered in ``tests/integration/test_llm_client_ollama_fake_server.py``.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any, Self

import httpx
import ollama
import pydantic
import pytest

from archon_search.constants import ANSWER_RESERVE_TOKENS, ASSUMED_CONTEXT_TOKENS
from archon_search.llm_client.ollama_client import OllamaClient
from archon_search.llm_client.protocol import (
    ContextWindowSource,
    ContextWindowTokens,
    InputBudget,
    LlmCallFailure,
    LlmCallRequest,
    LlmCallTimeout,
    LlmClient,
    LlmThinkingNotSupported,
    ThinkSetting,
    TokenCountingClient,
)
from archon_search.llm_client.token_counter import TokenCounter
from archon_search.uv_tool import extra_install_hint
from tests._token_counter_stub import StubTokenizer

_HOST = "http://ollama.test:11434"
_MODEL = "qwen3:8b"
_PROMPT = "describe this collection in one line"
_SECOND_PROMPT = "a completely different second prompt"
_REPLY = "a one line description"
_CONFIGURED_WINDOW = 8192
_MAX_OUTPUT_TOKENS = ANSWER_RESERVE_TOKENS
_TIMEOUT_SECONDS = 5.0
_SHORT_TIMEOUT_SECONDS = 0.05
_MODEL_MAXIMUM = 131072
_MAX_REASON_CHARS = 400  # the bounded server text (200) plus the provider prefix, with room to spare
# (4096 - 1024) tokens of room divided by 1.2 and rounded down, for the assumed window.
_ASSUMED_ROOM_IN_GPT2_TOKENS = 2560
# StubTokenizer counts one token per whitespace-separated word.
_TEMPLATE = "Describe the collection in one sentence."
_TEMPLATE_TOKENS = 6
# (8192 - 1024) tokens of room, divided by 1.2 (the estimate margin) and rounded down.
_ROOM_IN_GPT2_TOKENS = 5973


class _RecordingSdkClient:
    """Stands in for ``ollama.AsyncClient``: records ``chat`` and ``show`` arguments, answers or raises."""

    def __init__(self, host: str, *, reply: str | None = _REPLY, error: BaseException | None = None, delay: float = 0.0,
                 model_info: dict[str, Any] | None = None) -> None:
        self.host = host
        self.chat_calls: list[dict[str, Any]] = []
        self.show_calls: list[str] = []
        self.closed = False
        self._reply = reply
        self._error = error
        self._delay = delay
        self._model_info = model_info

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.closed = True

    async def chat(self, **kwargs: Any) -> ollama.ChatResponse:
        self.chat_calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return ollama.ChatResponse(model=_MODEL, message=ollama.Message(role="assistant", content=self._reply))

    async def show(self, model: str) -> ollama.ShowResponse:
        self.show_calls.append(model)
        if self._error is not None:
            raise self._error
        return ollama.ShowResponse(model_info=self._model_info)


class _Factory:
    """Hands out one fresh recording SDK client per request and keeps them all."""

    def __init__(self, **client_kwargs: Any) -> None:
        self.clients: list[_RecordingSdkClient] = []
        self._client_kwargs = client_kwargs

    def __call__(self, host: str) -> _RecordingSdkClient:
        client = _RecordingSdkClient(host, **self._client_kwargs)
        self.clients.append(client)
        return client

    @property
    def chat_calls(self) -> list[dict[str, Any]]:
        return [call for client in self.clients for call in client.chat_calls]


def _counter() -> TokenCounter:
    return TokenCounter(load_tokenizer=StubTokenizer)


def _wrapper(factory: Callable[[str], Any], context_tokens: int | None = None, counter: TokenCounter | None = None) -> OllamaClient:
    return OllamaClient(
        model=_MODEL,
        base_url=_HOST,
        context_tokens=context_tokens,
        token_counter=counter if counter is not None else _counter(),
        client_factory=factory,
    )


def _request(prompt: str = _PROMPT, *, think: ThinkSetting = ThinkSetting.DEFAULT, timeout: float = _TIMEOUT_SECONDS) -> LlmCallRequest:
    return LlmCallRequest(prompt=prompt, max_output_tokens=_MAX_OUTPUT_TOKENS, timeout_seconds=timeout, think=think)


async def test_ollama_wrapper_sends_num_ctx_when_configured() -> None:
    factory = _Factory()
    wrapper = _wrapper(factory, context_tokens=_CONFIGURED_WINDOW)

    result = await wrapper.call(_request())

    assert result.text == _REPLY
    [chat_call] = factory.chat_calls
    assert chat_call["options"]["num_ctx"] == _CONFIGURED_WINDOW
    assert wrapper.context_window() == ContextWindowTokens(size=_CONFIGURED_WINDOW, source=ContextWindowSource.CONFIGURED)


async def test_ollama_wrapper_omits_num_ctx_when_unset() -> None:
    factory = _Factory()
    wrapper = _wrapper(factory, context_tokens=None)

    await wrapper.call(_request())

    [chat_call] = factory.chat_calls
    assert "num_ctx" not in chat_call["options"]
    assert wrapper.context_window() == ContextWindowTokens(size=ASSUMED_CONTEXT_TOKENS, source=ContextWindowSource.ASSUMED)


async def test_consecutive_calls_send_one_user_message_each() -> None:
    factory = _Factory()
    wrapper = _wrapper(factory, context_tokens=_CONFIGURED_WINDOW)

    await wrapper.call(_request(_PROMPT))
    await wrapper.call(_request(_SECOND_PROMPT))

    first, second = factory.chat_calls
    assert first["messages"] == [{"role": "user", "content": _PROMPT}]
    assert second["messages"] == [{"role": "user", "content": _SECOND_PROMPT}]
    assert len(factory.clients) == 2, "each call builds its own SDK client, so nothing carries over"


async def test_call_sends_model_host_and_answer_ceiling_and_closes_the_client() -> None:
    factory = _Factory()

    await _wrapper(factory).call(_request())

    [client] = factory.clients
    [chat_call] = client.chat_calls
    assert client.host == _HOST
    assert chat_call["model"] == _MODEL
    assert chat_call["options"]["num_predict"] == _MAX_OUTPUT_TOKENS
    assert chat_call.get("stream", False) is False
    assert client.closed


@pytest.mark.parametrize(
    ("setting", "expected"),
    [(ThinkSetting.ON, True), (ThinkSetting.OFF, False)],
)
async def test_think_on_and_off_are_sent_as_the_native_think_option(setting: ThinkSetting, expected: bool) -> None:
    factory = _Factory()

    await _wrapper(factory).call(_request(think=setting))

    assert factory.chat_calls[0]["think"] is expected


async def test_think_default_sends_no_thinking_control() -> None:
    factory = _Factory()

    await _wrapper(factory).call(_request(think=ThinkSetting.DEFAULT))

    assert "think" not in factory.chat_calls[0]


async def test_empty_answer_is_returned_as_empty_text_for_the_caller_to_judge() -> None:
    result = await _wrapper(_Factory(reply=None)).call(_request())

    assert result.text == ""


async def test_slow_provider_raises_timeout_and_closes_the_client() -> None:
    factory = _Factory(delay=1.0)
    wrapper = _wrapper(factory)

    with pytest.raises(LlmCallTimeout) as raised:
        await wrapper.call(_request(timeout=_SHORT_TIMEOUT_SECONDS))

    assert raised.value.provider == "ollama"
    assert factory.clients
    assert all(client.closed for client in factory.clients)


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("Failed to connect to Ollama"),
        ollama.ResponseError("model 'qwen3:8b' not found", 404),
        httpx.ReadError("connection dropped mid-generation"),
        httpx.RemoteProtocolError("peer closed connection"),
        RuntimeError("boom"),
    ],
)
async def test_other_sdk_errors_raise_call_failure_not_timeout_and_close_the_client(error: BaseException) -> None:
    factory = _Factory(error=error)
    wrapper = _wrapper(factory)

    with pytest.raises(LlmCallFailure) as raised:
        await wrapper.call(_request())

    assert not isinstance(raised.value, LlmCallTimeout)
    assert raised.value.provider == "ollama"
    assert factory.clients
    assert all(client.closed for client in factory.clients)


async def test_sdk_error_keeps_the_server_reason_and_status_for_the_operator() -> None:
    wrapper = _wrapper(_Factory(error=ollama.ResponseError('"qwen3:8b" does not support thinking', 400)))

    with pytest.raises(LlmCallFailure, match=r"HTTP 400: .*does not support thinking"):
        await wrapper.call(_request(think=ThinkSetting.ON))


async def test_a_model_without_thinking_support_raises_the_typed_failure() -> None:
    wrapper = _wrapper(_Factory(error=ollama.ResponseError('"qwen3:8b" does not support thinking', 400)))

    with pytest.raises(LlmThinkingNotSupported) as raised:
        await wrapper.call(_request(think=ThinkSetting.ON))

    assert raised.value.provider == "ollama"


@pytest.mark.parametrize(
    "error",
    [ollama.ResponseError('model "qwen3:8b" not found', 404), ollama.ResponseError("does not support thinking", 500)],
)
async def test_other_http_errors_are_not_the_thinking_failure(error: ollama.ResponseError) -> None:
    with pytest.raises(LlmCallFailure) as raised:
        await _wrapper(_Factory(error=error)).call(_request(think=ThinkSetting.ON))

    assert not isinstance(raised.value, LlmThinkingNotSupported)


async def test_server_error_text_that_echoes_the_prompt_is_scrubbed() -> None:
    wrapper = _wrapper(_Factory(error=ollama.ResponseError(f"bad request: {_PROMPT}", 400)))

    with pytest.raises(LlmCallFailure) as raised:
        await wrapper.call(_request())

    assert _PROMPT not in str(raised.value)
    assert "bad request" in str(raised.value)


async def test_server_error_text_is_one_bounded_line() -> None:
    wrapper = _wrapper(_Factory(error=ollama.ResponseError("<html>\n" + "gateway down " * 500, 502)))

    with pytest.raises(LlmCallFailure) as raised:
        await wrapper.call(_request())

    assert "\n" not in raised.value.reason
    assert len(raised.value.reason) < _MAX_REASON_CHARS


async def test_reply_that_is_not_an_ollama_reply_raises_call_failure() -> None:
    with pytest.raises(pydantic.ValidationError) as not_a_show_reply:
        ollama.ShowResponse.model_validate({"unexpected": "shape"})
    wrapper = _wrapper(_Factory(error=not_a_show_reply.value))

    with pytest.raises(LlmCallFailure, match="is the base URL an Ollama server"):
        await wrapper.model_max_context_tokens(_TIMEOUT_SECONDS)


async def test_missing_package_raises_call_failure_with_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ollama", None)  # `import ollama` now raises ModuleNotFoundError
    wrapper = OllamaClient(model=_MODEL, base_url=_HOST, context_tokens=None, token_counter=_counter())

    with pytest.raises(LlmCallFailure, match="not installed") as raised:
        await wrapper.call(_request())

    assert extra_install_hint("ollama") in str(raised.value)


def test_importing_the_wrapper_does_not_import_the_sdk() -> None:
    code = "import sys; import archon_search.llm_client.ollama_client; sys.exit('ollama' in sys.modules)"

    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)

    assert completed.returncode == 0, completed.stderr.decode()


def test_wrapper_implements_the_shared_client_protocol() -> None:
    assert isinstance(_wrapper(_Factory()), LlmClient)


async def test_input_budget_is_the_gpt2_estimate_for_the_configured_window() -> None:
    wrapper = _wrapper(_Factory(), context_tokens=_CONFIGURED_WINDOW)

    budget = await wrapper.input_budget(_TEMPLATE)

    assert budget == InputBudget(tokens=_ROOM_IN_GPT2_TOKENS - _TEMPLATE_TOKENS)


async def test_input_budget_uses_the_assumed_window_when_unset() -> None:
    wrapper = _wrapper(_Factory(), context_tokens=None)

    budget = await wrapper.input_budget(_TEMPLATE)

    assert budget == InputBudget(tokens=_ASSUMED_ROOM_IN_GPT2_TOKENS - _TEMPLATE_TOKENS)


async def test_failing_counter_raises_call_failure() -> None:
    def broken_loader() -> StubTokenizer:
        raise OSError("tokenizer cannot be downloaded")

    wrapper = _wrapper(_Factory(), context_tokens=_CONFIGURED_WINDOW, counter=TokenCounter(load_tokenizer=broken_loader))

    with pytest.raises(LlmCallFailure) as raised:
        await wrapper.input_budget(_TEMPLATE)

    assert raised.value.provider == "ollama"


async def test_input_budget_counts_a_template_once_per_cached_template() -> None:
    """The gpt2 count of the fixed template is not repeated for every search that sizes a query."""
    stub = StubTokenizer()
    wrapper = _wrapper(_Factory(), context_tokens=_CONFIGURED_WINDOW, counter=TokenCounter(load_tokenizer=lambda: stub))

    budgets = [await wrapper.input_budget(_TEMPLATE) for _ in range(4)]

    assert budgets == [InputBudget(tokens=_ROOM_IN_GPT2_TOKENS - _TEMPLATE_TOKENS)] * 4
    assert stub.calls == [_TEMPLATE]


async def test_the_first_template_count_runs_off_the_event_loop_thread() -> None:
    loop_thread = threading.get_ident()
    threads: list[int] = []

    def load() -> StubTokenizer:
        threads.append(threading.get_ident())  # the first count loads (and may download) the tokenizer
        return StubTokenizer()

    wrapper = _wrapper(_Factory(), context_tokens=_CONFIGURED_WINDOW, counter=TokenCounter(load_tokenizer=load))

    await wrapper.input_budget(_TEMPLATE)

    assert threads and threads[0] != loop_thread


async def test_a_cached_template_is_sized_without_a_hop_to_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    wrapper = _wrapper(_Factory(), context_tokens=_CONFIGURED_WINDOW)
    first = await wrapper.input_budget(_TEMPLATE)

    async def no_thread_expected(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("a cache hit must not leave the event loop thread")

    monkeypatch.setattr(asyncio, "to_thread", no_thread_expected)

    assert await wrapper.input_budget(_TEMPLATE) == first


async def test_input_budget_keeps_one_count_per_template_and_does_not_cache_query_counts() -> None:
    stub = StubTokenizer()
    wrapper = _wrapper(_Factory(), context_tokens=_CONFIGURED_WINDOW, counter=TokenCounter(load_tokenizer=lambda: stub))

    await wrapper.input_budget(_TEMPLATE)
    await wrapper.input_budget(_SECOND_PROMPT)
    await wrapper.input_budget(_TEMPLATE)
    await wrapper.count_tokens(_PROMPT)
    await wrapper.count_tokens(_PROMPT)

    assert stub.calls == [_TEMPLATE, _SECOND_PROMPT, _PROMPT, _PROMPT]


async def test_a_failed_template_count_is_not_cached() -> None:
    attempts: list[str] = []

    def flaky_loader() -> StubTokenizer:
        attempts.append("load")
        if len(attempts) == 1:
            raise OSError("tokenizer cannot be downloaded")
        return StubTokenizer()

    wrapper = _wrapper(_Factory(), context_tokens=_CONFIGURED_WINDOW, counter=TokenCounter(load_tokenizer=flaky_loader))
    with pytest.raises(LlmCallFailure):
        await wrapper.input_budget(_TEMPLATE)

    assert await wrapper.input_budget(_TEMPLATE) == InputBudget(tokens=_ROOM_IN_GPT2_TOKENS - _TEMPLATE_TOKENS)


def test_token_ceiling_is_the_estimate_bound_of_the_counter() -> None:
    wrapper = _wrapper(_Factory(), context_tokens=_CONFIGURED_WINDOW)

    assert wrapper.token_ceiling("order 66 and 🙂") == TokenCounter.ceiling("order 66 and 🙂")


async def test_count_tokens_is_the_gpt2_count_the_budget_is_measured_in() -> None:
    wrapper = _wrapper(_Factory(), context_tokens=_CONFIGURED_WINDOW)

    assert isinstance(wrapper, TokenCountingClient)
    assert await wrapper.count_tokens(_TEMPLATE) == _TEMPLATE_TOKENS


async def test_failing_counter_raises_call_failure_when_counting_tokens() -> None:
    def broken_loader() -> StubTokenizer:
        raise OSError("tokenizer cannot be downloaded")

    wrapper = _wrapper(_Factory(), counter=TokenCounter(load_tokenizer=broken_loader))

    with pytest.raises(LlmCallFailure) as raised:
        await wrapper.count_tokens(_TEMPLATE)

    assert raised.value.provider == "ollama"


async def test_model_maximum_is_read_from_show_under_the_architecture_key() -> None:
    factory = _Factory(model_info={"general.architecture": "qwen3", "qwen3.context_length": _MODEL_MAXIMUM})

    maximum = await _wrapper(factory).model_max_context_tokens(_TIMEOUT_SECONDS)

    assert maximum == _MODEL_MAXIMUM
    assert factory.clients[0].show_calls == [_MODEL]
    assert factory.clients[0].closed


@pytest.mark.parametrize(
    "model_info",
    [None, {}, {"general.architecture": "llama"}, {"general.architecture": "llama", "llama.context_length": "huge"}],
)
async def test_model_maximum_is_none_when_show_does_not_state_it(model_info: dict[str, Any] | None) -> None:
    assert await _wrapper(_Factory(model_info=model_info)).model_max_context_tokens(_TIMEOUT_SECONDS) is None


async def test_model_maximum_failure_raises_call_failure() -> None:
    wrapper = _wrapper(_Factory(error=ConnectionError("down")))

    with pytest.raises(LlmCallFailure):
        await wrapper.model_max_context_tokens(_TIMEOUT_SECONDS)

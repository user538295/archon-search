"""BE-5 (collection-descriptions plan, C1, S6, S7): the real ``ollama`` SDK against the loopback fake server."""

from __future__ import annotations

import pytest

from archon_search.constants import ANSWER_RESERVE_TOKENS
from archon_search.llm_client.ollama_client import OllamaClient
from archon_search.llm_client.protocol import (
    LlmCallFailure,
    LlmCallRequest,
    LlmCallTimeout,
    ThinkSetting,
)
from archon_search.llm_client.token_counter import TokenCounter
from tests._fake_model_server import (
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)
from tests._token_counter_stub import StubTokenizer

pytestmark = pytest.mark.integration

_LOOPBACK = "127.0.0.1"
_MODEL = "fake-model"
_PROMPT = "describe this collection in one line"
_REPLY = "a one line description"
_CONFIGURED_WINDOW = 8192
_MODEL_MAXIMUM = 32768
_TIMEOUT_SECONDS = 10.0
_SHORT_TIMEOUT_SECONDS = 0.2
_LONGER_THAN_THE_TIMEOUT_SECONDS = 1.0


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's HTTP_PROXY must not capture the loopback requests."""
    keep_loopback_out_of_proxies(monkeypatch)


def _wrapper(base_url: str, context_tokens: int | None) -> OllamaClient:
    return OllamaClient(
        model=_MODEL,
        base_url=base_url,
        context_tokens=context_tokens,
        token_counter=TokenCounter(load_tokenizer=StubTokenizer),
    )


def _request(think: ThinkSetting = ThinkSetting.DEFAULT) -> LlmCallRequest:
    return LlmCallRequest(prompt=_PROMPT, max_output_tokens=ANSWER_RESERVE_TOKENS, timeout_seconds=_TIMEOUT_SECONDS, think=think)


async def test_ollama_wrapper_round_trips_against_fake_server() -> None:
    with FakeModelServer(reply_text=_REPLY) as server:
        result = await _wrapper(server.base_url, _CONFIGURED_WINDOW).call(_request(ThinkSetting.OFF))

    assert result.text == _REPLY
    [recorded] = server.requests_to("/api/chat")
    assert recorded.body is not None
    assert recorded.body["model"] == _MODEL
    assert recorded.body["messages"] == [{"role": "user", "content": _PROMPT}]
    assert recorded.body["options"]["num_ctx"] == _CONFIGURED_WINDOW
    assert recorded.body["options"]["num_predict"] == ANSWER_RESERVE_TOKENS
    assert recorded.body["think"] is False
    assert recorded.body["stream"] is False


async def test_unset_window_sends_no_num_ctx_over_the_wire() -> None:
    with FakeModelServer(reply_text=_REPLY) as server:
        await _wrapper(server.base_url, None).call(_request())

    [recorded] = server.requests_to("/api/chat")
    assert recorded.body is not None
    assert "num_ctx" not in recorded.body["options"]
    assert "think" not in recorded.body


async def test_model_maximum_round_trips_against_fake_server() -> None:
    with FakeModelServer(ollama_context_length=_MODEL_MAXIMUM) as server:
        maximum = await _wrapper(server.base_url, None).model_max_context_tokens(_TIMEOUT_SECONDS)

    assert maximum == _MODEL_MAXIMUM
    assert server.requests_to("/api/show")[0].body == {"model": _MODEL}


async def test_server_error_status_becomes_call_failure() -> None:
    with FakeModelServer(status_overrides={"/api/chat": 500}) as server, pytest.raises(LlmCallFailure) as raised:
        await _wrapper(server.base_url, None).call(_request())

    assert "HTTP 500" in str(raised.value)


async def test_unknown_model_on_show_becomes_call_failure() -> None:
    with FakeModelServer(status_overrides={"/api/show": 404}) as server, pytest.raises(LlmCallFailure) as raised:
        await _wrapper(server.base_url, None).model_max_context_tokens(_TIMEOUT_SECONDS)

    assert "HTTP 404" in str(raised.value)


async def test_unreachable_server_becomes_call_failure() -> None:
    with unreachable_port() as port:
        wrapper = _wrapper(f"http://{_LOOPBACK}:{port}", None)

        with pytest.raises(LlmCallFailure) as raised:
            await wrapper.call(_request())

    assert "cannot connect" in str(raised.value)


async def test_slow_server_becomes_call_timeout_through_the_real_sdk() -> None:
    request = LlmCallRequest(
        prompt=_PROMPT, max_output_tokens=ANSWER_RESERVE_TOKENS, timeout_seconds=_SHORT_TIMEOUT_SECONDS
    )
    with FakeModelServer(response_delay_seconds=_LONGER_THAN_THE_TIMEOUT_SECONDS) as server, pytest.raises(LlmCallTimeout):
        await _wrapper(server.base_url, None).call(request)

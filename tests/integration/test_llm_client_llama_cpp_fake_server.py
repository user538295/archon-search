"""BE-6 (collection-descriptions plan, C1, S8, S11): the ``llama_cpp`` wrapper against the loopback fake server."""

from __future__ import annotations

import pytest

from archon_search.constants import (
    ANSWER_RESERVE_TOKENS,
    ASSUMED_CONTEXT_TOKENS,
    LLAMA_CPP_JOIN_SLACK_TOKENS,
)
from archon_search.llm_client import llama_cpp_client
from archon_search.llm_client.llama_cpp_client import LlamaCppClient
from archon_search.llm_client.protocol import (
    ContextWindowSource,
    ContextWindowTokens,
    InputBudget,
    LlmCallFailure,
    LlmCallRequest,
    LlmCallTimeout,
    ThinkSetting,
)
from tests._fake_model_server import (
    FAKE_CHAT_CLOSE,
    FAKE_CHAT_FRAME_TOKENS,
    FAKE_CHAT_OPEN,
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)

pytestmark = pytest.mark.integration

_LOOPBACK = "127.0.0.1"
_MODEL = "fake-model"
_PROMPT = "describe this collection in one line"
_REPLY = "a one line description"
_WINDOW = 8192
_TIMEOUT_SECONDS = 10.0
_SHORT_TIMEOUT_SECONDS = 0.2
_LONGER_THAN_THE_TIMEOUT_SECONDS = 1.0
_SLOW_ANSWER_SECONDS = 0.4  # one sizing request fits the deadline below, the two together do not
_SIZING_DEADLINE_SECONDS = 0.6
_GENEROUS_DEADLINE_SECONDS = 30.0
_SHORT_ALLOWANCE_SECONDS = 0.3  # the sizing deadline and the per-request allowance of the wedged-server test
# The fake's tokenize counts one token per whitespace-separated word.
_TEMPLATE = "Describe the collection in one sentence."
_TEMPLATE_TOKENS = 6


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _request(timeout: float = _TIMEOUT_SECONDS) -> LlmCallRequest:
    return LlmCallRequest(prompt=_PROMPT, max_output_tokens=ANSWER_RESERVE_TOKENS, timeout_seconds=timeout)


async def test_llama_cpp_wrapper_against_fake_server() -> None:
    with FakeModelServer(reply_text=_REPLY, llama_n_ctx=_WINDOW) as server:
        client = LlamaCppClient(model=_MODEL, base_url=server.base_url)

        window = await client.probe_context_window(_TIMEOUT_SECONDS)
        budget = await client.input_budget(_TEMPLATE, max_output_tokens=ANSWER_RESERVE_TOKENS)
        result = await client.call(_request())

        assert window == ContextWindowTokens(size=_WINDOW, source=ContextWindowSource.PROBED)
        assert budget == InputBudget(
            tokens=_WINDOW - ANSWER_RESERVE_TOKENS - _TEMPLATE_TOKENS - FAKE_CHAT_FRAME_TOKENS - LLAMA_CPP_JOIN_SLACK_TOKENS
        )
        assert result.text == _REPLY
        assert [request.path for request in server.requests] == [
            "/props",
            "/apply-template",
            "/tokenize",
            "/v1/chat/completions",
        ]
        assert server.requests_to("/tokenize")[0].body == {"content": f"{FAKE_CHAT_OPEN} {_TEMPLATE} {FAKE_CHAT_CLOSE}"}
        assert server.requests_to("/v1/chat/completions")[0].body == {
            "model": _MODEL,
            "max_tokens": ANSWER_RESERVE_TOKENS,
            "messages": [{"role": "user", "content": _PROMPT}],
        }


@pytest.mark.parametrize(
    ("think", "expected_switch"),
    [
        (ThinkSetting.ON, {"enable_thinking": True}),
        (ThinkSetting.OFF, {"enable_thinking": False}),
        (ThinkSetting.DEFAULT, None),
    ],
)
async def test_llama_cpp_think_switch_against_fake_server(think: ThinkSetting, expected_switch: dict[str, bool] | None) -> None:
    with FakeModelServer(reply_text=_REPLY) as server:
        client = LlamaCppClient(model=_MODEL, base_url=server.base_url)

        await client.call(LlmCallRequest(prompt=_PROMPT, max_output_tokens=ANSWER_RESERVE_TOKENS, timeout_seconds=_TIMEOUT_SECONDS, think=think))

        [recorded] = server.requests_to("/v1/chat/completions")
        assert recorded.body is not None
        assert recorded.body.get("chat_template_kwargs") == expected_switch
        assert ("chat_template_kwargs" in recorded.body) is (expected_switch is not None)


async def test_llama_cpp_wrapper_maps_a_server_error_without_the_prompt() -> None:
    with FakeModelServer(status_overrides={"/v1/chat/completions": 503}) as server:
        client = LlamaCppClient(model=_MODEL, base_url=server.base_url)
        with pytest.raises(LlmCallFailure) as raised:
            await client.call(_request())
        assert "HTTP 503" in raised.value.reason
        assert _PROMPT not in str(raised.value)


async def test_llama_cpp_wrapper_maps_a_slow_server_to_timeout() -> None:
    with FakeModelServer(response_delay_seconds=_LONGER_THAN_THE_TIMEOUT_SECONDS) as slow:
        client = LlamaCppClient(model=_MODEL, base_url=slow.base_url)
        with pytest.raises(LlmCallTimeout):
            await client.call(_request(_SHORT_TIMEOUT_SECONDS))


async def test_llama_cpp_wrapper_probe_failure_against_fake_server_assumes_4096() -> None:
    with FakeModelServer(status_overrides={"/props": 503}) as server:
        client = LlamaCppClient(model=_MODEL, base_url=server.base_url)

        window = await client.probe_context_window(_TIMEOUT_SECONDS)

        assert window == ContextWindowTokens(size=ASSUMED_CONTEXT_TOKENS, source=ContextWindowSource.ASSUMED)


async def test_llama_cpp_wrapper_reports_unreachable_server() -> None:
    with unreachable_port() as port:
        client = LlamaCppClient(model=_MODEL, base_url=f"http://{_LOOPBACK}:{port}")

        with pytest.raises(LlmCallFailure) as raised:
            await client.call(_request())

    assert not isinstance(raised.value, LlmCallTimeout)
    assert "cannot connect" in raised.value.reason


async def test_llama_cpp_sizing_against_a_server_without_apply_template_names_the_endpoint() -> None:
    with FakeModelServer(status_overrides={"/apply-template": 404}) as server:
        client = LlamaCppClient(model=_MODEL, base_url=server.base_url)

        with pytest.raises(LlmCallFailure) as raised:
            await client.input_budget(_TEMPLATE, max_output_tokens=ANSWER_RESERVE_TOKENS)

    assert raised.value.reason.startswith("POST /apply-template: HTTP 404: ")
    assert _TEMPLATE not in raised.value.reason


async def test_llama_cpp_sizing_is_bounded_as_a_whole_not_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each of the two sizing requests answers within its own allowance, yet together they outlast the one deadline."""
    monkeypatch.setattr(llama_cpp_client, "SIZING_DEADLINE_SECONDS", _SIZING_DEADLINE_SECONDS)
    with FakeModelServer(response_delay_seconds=_SLOW_ANSWER_SECONDS) as slow:
        client = LlamaCppClient(model=_MODEL, base_url=slow.base_url)

        with pytest.raises(LlmCallTimeout) as raised:
            await client.input_budget(_TEMPLATE, max_output_tokens=ANSWER_RESERVE_TOKENS)

        monkeypatch.setattr(llama_cpp_client, "SIZING_DEADLINE_SECONDS", _GENEROUS_DEADLINE_SECONDS)
        within = await client.input_budget(_TEMPLATE, max_output_tokens=ANSWER_RESERVE_TOKENS)

    # The first request answers in 0.4 s; the deadline (0.6 s) fires while the second, /tokenize, is waited on.
    assert raised.value.reason == f"POST /tokenize: sizing the template took longer than {_SIZING_DEADLINE_SECONDS:g}s"
    assert isinstance(within.tokens, int)


async def test_llama_cpp_sizing_of_a_wedged_server_names_the_endpoint_it_waited_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server holds every answer past both the deadline and the per-request allowance (both shrunk to 0.3 s), as the
    shipped settings, which are equal, would: the failure still names the request in flight."""
    monkeypatch.setattr(llama_cpp_client, "SIZING_DEADLINE_SECONDS", _SHORT_ALLOWANCE_SECONDS)
    monkeypatch.setattr(llama_cpp_client, "TOKENIZE_TIMEOUT_SECONDS", _SHORT_ALLOWANCE_SECONDS)
    with FakeModelServer(response_delay_seconds=_LONGER_THAN_THE_TIMEOUT_SECONDS) as wedged:
        client = LlamaCppClient(model=_MODEL, base_url=wedged.base_url)

        with pytest.raises(LlmCallTimeout) as raised:
            await client.input_budget(_TEMPLATE, max_output_tokens=ANSWER_RESERVE_TOKENS)

    assert raised.value.reason.startswith("POST /apply-template: ")
    assert _TEMPLATE not in raised.value.reason

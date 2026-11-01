"""BE-6 (collection-descriptions plan, C1, S8, S11): the ``llama_cpp`` wrapper over ``httpx``.

The HTTP transport is a recording ``httpx.MockTransport``; the real server shapes against the loopback fake are in
``tests/integration/test_llm_client_llama_cpp_fake_server.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

import httpx
import pytest

from archon_search.constants import ANSWER_RESERVE_TOKENS, ASSUMED_CONTEXT_TOKENS
from archon_search.llm_client.llama_cpp_client import LlamaCppClient
from archon_search.llm_client.protocol import (
    ContextWindowSource,
    ContextWindowTokens,
    InputBudget,
    LlmCallFailure,
    LlmCallRequest,
    LlmCallTimeout,
    LlmClient,
    ThinkSetting,
    TokenCountingClient,
)

_HOST = "http://llama.test:8080"
_MODEL = "qwen3-8b"
_PROMPT = "describe this collection in one line"
_REPLY = "a one line description"
_PROBED_WINDOW = 16384
_TIMEOUT_SECONDS = 5.0
_SHORT_TIMEOUT_SECONDS = 0.05
_NEVER_SECONDS = 60.0
_TEMPLATE = "Describe the collection in one sentence."
_TEMPLATE_TOKENS = 6
_FRAME_TOKENS = 6  # the framing ``_applied`` adds, counted as words by the fake tokenizer: system block, markers
_MAX_REASON_CHARS = 400
_LOGGER_NAME = "archon_search.llm_client.llama_cpp_client"


def _applied(content: str) -> str:
    return f"<|system|> You are helpful. <|user|> {content} <|assistant|>"


class _Server:
    """Records every request and answers by path; a path in ``failures`` raises that exception instead."""

    def __init__(self, *, props: object | None = None, failures: dict[str, Exception] | None = None, statuses: dict[str, tuple[int, object]] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._props = props if props is not None else {"default_generation_settings": {"n_ctx": _PROBED_WINDOW}}
        self._failures = failures or {}
        self._statuses = statuses or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self._failures:
            raise self._failures[path]
        if path in self._statuses:
            status, body = self._statuses[path]
            return httpx.Response(status, json=body)
        if path == "/props":
            return httpx.Response(200, json=self._props)
        if path == "/apply-template":
            return httpx.Response(200, json={"prompt": _applied(json.loads(request.content)["messages"][0]["content"])})
        if path == "/tokenize":
            return httpx.Response(200, json={"tokens": list(range(len(json.loads(request.content)["content"].split())))})
        return httpx.Response(200, json={"choices": [{"index": 0, "message": {"role": "assistant", "content": _REPLY}}]})

    def bodies(self, path: str) -> list[dict[str, object]]:
        return [json.loads(request.content) for request in self.requests if request.url.path == path]


def _client(server: Callable[[httpx.Request], httpx.Response]) -> LlamaCppClient:
    return LlamaCppClient(model=_MODEL, base_url=_HOST, transport=httpx.MockTransport(server))


def _request(prompt: str = _PROMPT) -> LlmCallRequest:
    return LlmCallRequest(prompt=prompt, max_output_tokens=ANSWER_RESERVE_TOKENS, timeout_seconds=_TIMEOUT_SECONDS)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == logging.WARNING and record.name == _LOGGER_NAME]


async def test_llama_cpp_window_read_from_props() -> None:
    server = _Server()
    client = _client(server)

    window = await client.probe_context_window(_TIMEOUT_SECONDS)

    assert window == ContextWindowTokens(size=_PROBED_WINDOW, source=ContextWindowSource.PROBED)
    assert client.context_window() == window
    [probe] = server.requests
    assert (probe.method, probe.url.path) == ("GET", "/props")


async def test_llama_cpp_window_is_assumed_until_probed() -> None:
    client = _client(_Server())

    assert client.context_window() == ContextWindowTokens(size=ASSUMED_CONTEXT_TOKENS, source=ContextWindowSource.ASSUMED)


@pytest.mark.parametrize(
    "server",
    [
        pytest.param(_Server(failures={"/props": httpx.ConnectError("refused")}), id="unreachable"),
        pytest.param(_Server(failures={"/props": httpx.ReadTimeout("slow")}), id="timeout"),
        pytest.param(_Server(statuses={"/props": (503, {"error": {"message": "loading"}})}), id="http-error"),
        pytest.param(_Server(props={"total_slots": 1}), id="no-window-field"),
        pytest.param(_Server(props={"default_generation_settings": {"n_ctx": 0}}), id="zero-window"),
        pytest.param(_Server(props={"default_generation_settings": {"n_ctx": "big"}}), id="non-integer-window"),
        pytest.param(_Server(props=["not", "an", "object"]), id="not-an-object"),
    ],
)
async def test_llama_cpp_probe_failure_assumes_4096_and_warns(server: _Server, caplog: pytest.LogCaptureFixture) -> None:
    client = _client(server)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        window = await client.probe_context_window(_TIMEOUT_SECONDS)
        await client.probe_context_window(_TIMEOUT_SECONDS)

    assert window == ContextWindowTokens(size=4096, source=ContextWindowSource.ASSUMED)
    assert client.context_window() == window
    [warning] = _warnings(caplog)
    assert "llama.test:8080" in warning.getMessage()
    assert "4096" in warning.getMessage()


async def test_llama_cpp_probe_failure_warns_once_per_host(caplog: pytest.LogCaptureFixture) -> None:
    other_host = "http://other.test:8080"
    failing = _Server(failures={"/props": httpx.ConnectError("refused")})
    first = _client(failing)
    second = LlamaCppClient(model=_MODEL, base_url=other_host, transport=httpx.MockTransport(failing))

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await first.probe_context_window(_TIMEOUT_SECONDS)
        await second.probe_context_window(_TIMEOUT_SECONDS)

    assert len(_warnings(caplog)) == 2
    assert {"llama.test:8080" in record.getMessage() for record in _warnings(caplog)} == {True, False}


async def test_llama_cpp_unreachable_server_warning_says_unreachable_and_the_assumed_window(caplog: pytest.LogCaptureFixture) -> None:
    client = _client(_Server(failures={"/props": httpx.ConnectError("refused")}))

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await client.probe_context_window(_TIMEOUT_SECONDS)

    [warning] = _warnings(caplog)
    message = warning.getMessage()
    assert "llama.test:8080" in message
    assert "not reachable" in message
    assert str(ASSUMED_CONTEXT_TOKENS) in message


async def test_llama_cpp_server_that_answers_without_a_window_is_not_called_unreachable(caplog: pytest.LogCaptureFixture) -> None:
    client = _client(_Server(props={"total_slots": 1}))

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await client.probe_context_window(_TIMEOUT_SECONDS)

    [warning] = _warnings(caplog)
    assert "not reachable" not in warning.getMessage()
    assert "without a usable context window" in warning.getMessage()
    assert str(ASSUMED_CONTEXT_TOKENS) in warning.getMessage()


@pytest.mark.parametrize("second_url", ["http://llama.test:8080/v1", "http://user:secret@llama.test:8080/?token=abc", "llama.test:8080"])
async def test_llama_cpp_one_server_in_any_spelling_is_one_warning(second_url: str, caplog: pytest.LogCaptureFixture) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    failing = httpx.MockTransport(refuse)
    first = LlamaCppClient(model=_MODEL, base_url=_HOST, transport=failing)
    second = LlamaCppClient(model=_MODEL, base_url=second_url, transport=failing)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await first.probe_context_window(_TIMEOUT_SECONDS)
        await second.probe_context_window(_TIMEOUT_SECONDS)

    assert len(_warnings(caplog)) == 1


async def test_llama_cpp_unreachable_and_no_window_are_separate_findings(caplog: pytest.LogCaptureFixture) -> None:
    """A server that went down and one that answers without a window are different problems: neither hides the other."""
    down = _client(_Server(failures={"/props": httpx.ConnectError("refused")}))
    no_window = _client(_Server(props={"total_slots": 1}))

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await down.probe_context_window(_TIMEOUT_SECONDS)
        await no_window.probe_context_window(_TIMEOUT_SECONDS)

    assert len(_warnings(caplog)) == 2


async def test_llama_cpp_successful_probe_after_failure_replaces_assumed_window() -> None:
    state = {"up": False}

    def server(request: httpx.Request) -> httpx.Response:
        if not state["up"]:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"default_generation_settings": {"n_ctx": _PROBED_WINDOW}})

    client = _client(server)
    await client.probe_context_window(_TIMEOUT_SECONDS)
    state["up"] = True

    window = await client.probe_context_window(_TIMEOUT_SECONDS)

    assert window == ContextWindowTokens(size=_PROBED_WINDOW, source=ContextWindowSource.PROBED)


async def test_llama_cpp_call_sends_one_user_message_and_returns_text() -> None:
    server = _Server()
    client = _client(server)

    first = await client.call(_request())
    await client.call(_request("a completely different second prompt"))

    assert first.text == _REPLY
    assert server.bodies("/v1/chat/completions") == [
        {"model": _MODEL, "max_tokens": ANSWER_RESERVE_TOKENS, "messages": [{"role": "user", "content": _PROMPT}]},
        {"model": _MODEL, "max_tokens": ANSWER_RESERVE_TOKENS, "messages": [{"role": "user", "content": "a completely different second prompt"}]},
    ]


async def test_llama_cpp_call_http_error_carries_server_message_and_no_prompt() -> None:
    message = f"request ({_PROMPT}) exceeds the available context size"
    server = _Server(statuses={"/v1/chat/completions": (400, {"error": {"code": 400, "message": message, "type": "exceed_context_size_error"}})})

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).call(_request())

    assert raised.value.provider == "llama_cpp"
    assert "HTTP 400" in raised.value.reason
    assert "exceeds the available context size" in raised.value.reason
    assert _PROMPT not in str(raised.value)
    assert len(raised.value.reason) <= _MAX_REASON_CHARS


async def test_llama_cpp_call_http_error_without_json_body_still_raises_failure() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=f"bad gateway for {_PROMPT}")

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).call(_request())

    assert "HTTP 502" in raised.value.reason
    assert _PROMPT not in str(raised.value)


async def test_llama_cpp_call_timeout_raises_llm_call_timeout() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LlmCallTimeout) as raised:
        await _client(server).call(_request())

    assert raised.value.reason == "no answer within 5s"
    assert _PROMPT not in str(raised.value)


async def test_llama_cpp_call_unreachable_server_raises_failure() -> None:
    server = _Server(failures={"/v1/chat/completions": httpx.ConnectError(f"refused {_PROMPT}")})

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).call(_request())

    assert not isinstance(raised.value, LlmCallTimeout)
    assert "cannot connect" in raised.value.reason
    assert _PROMPT not in str(raised.value)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"choices": []}, id="no-choices"),
        pytest.param({"choices": [{"message": {"content": None}}]}, id="null-content"),
        pytest.param({"choices": [{"message": {"content": 7}}]}, id="non-string-content"),
        pytest.param(["not", "an", "object"], id="not-an-object"),
    ],
)
async def test_llama_cpp_call_malformed_answer_raises_failure(body: object) -> None:
    server = _Server(statuses={"/v1/chat/completions": (200, body)})

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).call(_request())

    assert "unexpected reply" in raised.value.reason


async def test_llama_cpp_call_non_json_answer_raises_failure() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not a model server</html>")

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).call(_request())

    assert "unexpected reply" in raised.value.reason


async def test_llama_cpp_input_budget_uses_the_servers_exact_count_of_the_framed_template() -> None:
    server = _Server()
    client = _client(server)
    await client.probe_context_window(_TIMEOUT_SECONDS)

    budget = await client.input_budget(_TEMPLATE)

    assert budget == InputBudget(tokens=_PROBED_WINDOW - ANSWER_RESERVE_TOKENS - _TEMPLATE_TOKENS - _FRAME_TOKENS)
    assert server.bodies("/tokenize") == [{"content": _applied(_TEMPLATE)}]


async def test_llama_cpp_counts_tokens_with_the_servers_tokenizer_and_is_a_token_counting_client() -> None:
    """The unit of its budget is the server's token: callers that size passages ask the client (``TokenCountingClient``)."""
    server = _Server()
    client = _client(server)

    assert isinstance(client, TokenCountingClient)
    assert await client.count_tokens(_TEMPLATE) == _TEMPLATE_TOKENS
    assert server.bodies("/tokenize") == [{"content": _TEMPLATE}]


async def test_llama_cpp_input_budget_failure_raises_llm_call_failure() -> None:
    server = _Server(statuses={"/tokenize": (500, {"error": {"message": "tokenizer down"}})})

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).input_budget(_TEMPLATE)

    assert "HTTP 500" in raised.value.reason
    assert _TEMPLATE not in str(raised.value)


async def test_llama_cpp_input_budget_malformed_tokenize_answer_raises_failure() -> None:
    server = _Server(statuses={"/tokenize": (200, {"tokens": "many"})})

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).input_budget(_TEMPLATE)

    assert "unexpected reply" in raised.value.reason


async def test_llama_cpp_input_budget_asks_the_server_to_frame_the_template_as_one_user_message() -> None:
    # T-2 item 1: a prompt of exactly the budget came back as 29 prompt tokens more than budget + template
    # (llama-server wraps the user message in the model's chat template), which ate the reserved answer space.
    server = _Server()

    await _client(server).input_budget(_TEMPLATE)

    assert [request.url.path for request in server.requests] == ["/apply-template", "/tokenize"]
    assert server.bodies("/apply-template") == [{"messages": [{"role": "user", "content": _TEMPLATE}]}]


async def test_llama_cpp_input_budget_apply_template_failure_raises_llm_call_failure() -> None:
    server = _Server(statuses={"/apply-template": (404, {"error": {"message": "File Not Found"}})})

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).input_budget(_TEMPLATE)

    assert "HTTP 404" in raised.value.reason
    assert _TEMPLATE not in str(raised.value)


async def test_llama_cpp_input_budget_malformed_apply_template_answer_raises_failure() -> None:
    server = _Server(statuses={"/apply-template": (200, {"prompt": 7})})

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).input_budget(_TEMPLATE)

    assert "unexpected reply" in raised.value.reason


_ASSUMED_BUDGET = ASSUMED_CONTEXT_TOKENS - ANSWER_RESERVE_TOKENS - _TEMPLATE_TOKENS - _FRAME_TOKENS
_OTHER_TEMPLATE = "Summarise this other collection in one sentence."
_OTHER_TEMPLATE_TOKENS = 7
_TEMPLATE_REQUESTS = ["/apply-template", "/tokenize"]  # what sizing one template asks the server: render it, count it


def _paths(server: _Server) -> list[str]:
    return [request.url.path for request in server.requests]


async def test_llama_cpp_input_budget_counts_a_template_once_per_process() -> None:
    """The template is the fixed part of every query: its framed count is asked of the server once, not per search."""
    server = _Server()
    client = _client(server)

    first = await client.input_budget(_TEMPLATE)
    second = await client.input_budget(_TEMPLATE)
    third = await client.input_budget(_TEMPLATE)

    assert first == second == third == InputBudget(tokens=_ASSUMED_BUDGET)
    assert _paths(server) == _TEMPLATE_REQUESTS


async def test_llama_cpp_input_budget_keeps_one_count_per_template() -> None:
    server = _Server()
    client = _client(server)

    await client.input_budget(_TEMPLATE)
    other = await client.input_budget(_OTHER_TEMPLATE)
    await client.input_budget(_TEMPLATE)
    await client.input_budget(_OTHER_TEMPLATE)

    assert other == InputBudget(tokens=ASSUMED_CONTEXT_TOKENS - ANSWER_RESERVE_TOKENS - _OTHER_TEMPLATE_TOKENS - _FRAME_TOKENS)
    assert _paths(server) == _TEMPLATE_REQUESTS * 2


async def test_llama_cpp_cached_template_count_does_not_freeze_the_assumed_window() -> None:
    """The window is resolved after start-up (BE-10): a budget asked for before it must not outlive the answer, and the
    template is counted again because a server that answers may be another model than the one assumed."""
    server = _Server()
    client = _client(server)
    assumed = await client.input_budget(_TEMPLATE)

    await client.probe_context_window(_TIMEOUT_SECONDS)
    probed = await client.input_budget(_TEMPLATE)
    await client.input_budget(_TEMPLATE)

    assert assumed == InputBudget(tokens=_ASSUMED_BUDGET)
    assert probed == InputBudget(tokens=_PROBED_WINDOW - ANSWER_RESERVE_TOKENS - _TEMPLATE_TOKENS - _FRAME_TOKENS)
    assert _paths(server) == [*_TEMPLATE_REQUESTS, "/props", *_TEMPLATE_REQUESTS]


async def test_llama_cpp_failed_probe_keeps_the_cached_template_count() -> None:
    server = _Server(failures={"/props": httpx.ConnectError("refused")})
    client = _client(server)
    await client.input_budget(_TEMPLATE)

    await client.probe_context_window(_TIMEOUT_SECONDS)
    await client.input_budget(_TEMPLATE)

    assert _paths(server) == [*_TEMPLATE_REQUESTS, "/props"]


async def test_llama_cpp_failed_template_count_is_not_cached() -> None:
    state = {"up": False}

    def server(request: httpx.Request) -> httpx.Response:
        if not state["up"]:
            raise httpx.ConnectError("refused")
        return _Server()(request)

    client = _client(server)
    with pytest.raises(LlmCallFailure):
        await client.input_budget(_TEMPLATE)
    state["up"] = True

    assert await client.input_budget(_TEMPLATE) == InputBudget(tokens=_ASSUMED_BUDGET)


async def test_llama_cpp_query_count_is_not_cached() -> None:
    """Only the template is a fixed part: every text counted through ``count_tokens`` goes to the server."""
    server = _Server()
    client = _client(server)

    await client.count_tokens(_PROMPT)
    await client.count_tokens(_PROMPT)

    assert _paths(server) == ["/tokenize", "/tokenize"]


def test_llama_cpp_client_satisfies_the_llm_client_protocol() -> None:
    assert isinstance(_client(_Server()), LlmClient)


async def test_llama_cpp_failed_reprobe_keeps_the_probed_window() -> None:
    state = {"up": True}

    def server(request: httpx.Request) -> httpx.Response:
        if not state["up"]:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"default_generation_settings": {"n_ctx": _PROBED_WINDOW}})

    client = _client(server)
    probed = await client.probe_context_window(_TIMEOUT_SECONDS)
    state["up"] = False

    assert await client.probe_context_window(_TIMEOUT_SECONDS) == probed


@pytest.mark.parametrize("window", [True, -1, 8192.0], ids=["bool", "negative", "float"])
async def test_llama_cpp_probe_rejects_unusable_n_ctx_values(window: object) -> None:
    client = _client(_Server(props={"default_generation_settings": {"n_ctx": window}}))

    assert (await client.probe_context_window(_TIMEOUT_SECONDS)).source is ContextWindowSource.ASSUMED


async def test_llama_cpp_probe_warning_hides_credentials_of_the_base_url(caplog: pytest.LogCaptureFixture) -> None:
    failing = _Server(failures={"/props": httpx.ConnectError("refused")})
    client = LlamaCppClient(model=_MODEL, base_url="http://user:secret@llama.test:8080/?token=abc", transport=httpx.MockTransport(failing))

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await client.probe_context_window(_TIMEOUT_SECONDS)

    [warning] = _warnings(caplog)
    assert "llama.test:8080" in warning.getMessage()
    assert "secret" not in warning.getMessage()
    assert "abc" not in warning.getMessage()


@pytest.mark.parametrize(
    ("think", "expected"),
    [(ThinkSetting.ON, {"enable_thinking": True}), (ThinkSetting.OFF, {"enable_thinking": False})],
)
async def test_llama_cpp_call_sends_the_template_thinking_switch(think: ThinkSetting, expected: dict[str, bool]) -> None:
    # llama-server reads the per-request ``chat_template_kwargs`` (tools/server README, ``enable_thinking``).
    server = _Server()
    request = LlmCallRequest(prompt=_PROMPT, max_output_tokens=ANSWER_RESERVE_TOKENS, timeout_seconds=_TIMEOUT_SECONDS, think=think)

    await _client(server).call(request)

    assert server.bodies("/v1/chat/completions")[0]["chat_template_kwargs"] == expected


async def test_llama_cpp_call_sends_no_thinking_control_by_default() -> None:
    server = _Server()

    await _client(server).call(_request())

    assert set(server.bodies("/v1/chat/completions")[0]) == {"model", "max_tokens", "messages"}


async def test_llama_cpp_call_total_deadline_raises_llm_call_timeout() -> None:
    async def never_answers(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(_NEVER_SECONDS)
        return httpx.Response(200)

    client = LlamaCppClient(model=_MODEL, base_url=_HOST, transport=httpx.MockTransport(never_answers))

    with pytest.raises(LlmCallTimeout) as raised:
        await client.call(LlmCallRequest(prompt=_PROMPT, max_output_tokens=ANSWER_RESERVE_TOKENS, timeout_seconds=_SHORT_TIMEOUT_SECONDS))

    assert raised.value.reason == "no answer within 0.05s"


async def test_llama_cpp_call_malformed_base_url_raises_failure_not_a_raw_exception() -> None:
    client = LlamaCppClient(model=_MODEL, base_url="http://host:80a80")

    with pytest.raises(LlmCallFailure):
        await client.call(_request())
    assert (await client.probe_context_window(_TIMEOUT_SECONDS)).source is ContextWindowSource.ASSUMED


async def test_llama_cpp_call_dropped_connection_raises_failure_naming_only_the_error_type() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection dropped", request=request)

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).call(_request())

    assert raised.value.reason == "the connection to the llama-server failed (ReadError)"
    assert not isinstance(raised.value, LlmCallTimeout)


async def test_llama_cpp_call_non_utf8_answer_raises_failure() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xfe\x00")

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).call(_request())

    assert "unexpected reply" in raised.value.reason


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param({"error": "plain text error"}, "plain text error", id="string-error"),
        pytest.param({"error": {"code": 500}}, '{"error":{"code":500}}', id="dict-without-message-reads-body-text"),
        pytest.param(["a", "list"], '["a","list"]', id="not-an-object-reads-body-text"),
    ],
)
async def test_llama_cpp_call_error_body_shapes(body: object, expected: str) -> None:
    server = _Server(statuses={"/v1/chat/completions": (500, body)})

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).call(_request())

    assert raised.value.reason == f"HTTP 500: {expected}"



async def test_llama_cpp_probe_with_unparseable_base_url_assumes_4096_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    client = LlamaCppClient(model=_MODEL, base_url="http://[::1")

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        window = await client.probe_context_window(_TIMEOUT_SECONDS)

    assert window.source is ContextWindowSource.ASSUMED
    assert len(_warnings(caplog)) == 1

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
from archon_search.llm_client.llama_cpp_client import (
    LlamaCppClient,
    warn_llama_cpp_unreachable,
)
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
_THINK_OFF_BLOCK = "<think> </think>"
_THINK_OFF_FRAME_TOKENS = 2  # a Qwen3-style template adds this empty block to the generation prompt when thinking is off
_MAX_REASON_CHARS = 400
_LOGGER_NAME = "archon_search.llm_client.llama_cpp_client"


def _applied(content: str, *, thinking_off: bool = False) -> str:
    framed = f"<|system|> You are helpful. <|user|> {content} <|assistant|>"
    return f"{framed} {_THINK_OFF_BLOCK}" if thinking_off else framed


def _echoed_prompt(body: dict[str, object]) -> str:
    """The text a request carries as its prompt, on any of the three endpoints that take one."""
    if "content" in body:
        return str(body["content"])
    messages = body["messages"]
    assert isinstance(messages, list)
    return str(messages[0]["content"])


class _Server:
    """Records every request and answers by path; a path in ``failures`` raises that exception instead.

    A path in ``echoing`` answers its HTTP status with an error that quotes the request's own prompt, as a server that
    rejects a prompt may. ``/apply-template`` honours ``chat_template_kwargs.enable_thinking`` like a Qwen3 template.
    """

    def __init__(
        self,
        *,
        props: object | None = None,
        failures: dict[str, Exception] | None = None,
        statuses: dict[str, tuple[int, object]] | None = None,
        echoing: dict[str, int] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._props = props if props is not None else {"default_generation_settings": {"n_ctx": _PROBED_WINDOW}}
        self._failures = failures or {}
        self._statuses = statuses or {}
        self._echoing = echoing or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self._failures:
            raise self._failures[path]
        if path in self._statuses:
            status, body = self._statuses[path]
            return httpx.Response(status, json=body)
        if path in self._echoing:
            echo = _echoed_prompt(json.loads(request.content))
            return httpx.Response(self._echoing[path], json={"error": {"message": f"cannot process: {echo}"}})
        if path == "/props":
            return httpx.Response(200, json=self._props)
        if path == "/apply-template":
            body = json.loads(request.content)
            thinking_off = body.get("chat_template_kwargs", {}).get("enable_thinking") is False
            return httpx.Response(200, json={"prompt": _applied(body["messages"][0]["content"], thinking_off=thinking_off)})
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


async def test_llama_cpp_unreachable_server_warning_text(caplog: pytest.LogCaptureFixture) -> None:
    """The one line says both consequences and no more: calls fail, and the assumed window applies only where none was read."""
    client = _client(_Server(failures={"/props": httpx.ConnectError("refused")}))

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await client.probe_context_window(_TIMEOUT_SECONDS)

    [warning] = _warnings(caplog)
    assert warning.getMessage() == (
        "llama_cpp: the server at llama.test:8080 is not reachable (cannot connect to the llama-server); "
        f"requests to it fail until it answers, and where its context window could not be read, {ASSUMED_CONTEXT_TOKENS} tokens are assumed"
    )


async def test_llama_cpp_unreachable_warning_is_attributed_to_the_shared_client_logger(caplog: pytest.LogCaptureFixture) -> None:
    """Deliberate: the line is shared by every path that finds the outage, so it belongs to the client's logger, not to the
    probe that happened to log it (a filter on ``archon_search.model_validation`` does not see it)."""
    client = _client(_Server(failures={"/props": httpx.ConnectError("refused")}))

    with caplog.at_level(logging.WARNING):
        await client.probe_context_window(_TIMEOUT_SECONDS)

    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert record.name == _LOGGER_NAME == "archon_search.llm_client.llama_cpp_client"


async def test_llama_cpp_unreachable_server_probe_logs_the_exception_types_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """The cause the WARNING's one-line reason hides stays diagnosable at DEBUG, as types only (a message may repeat credentials)."""
    client = _client(_Server(failures={"/props": httpx.ConnectError("refused")}))

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        await client.probe_context_window(_TIMEOUT_SECONDS)

    [debug] = [r for r in caplog.records if r.levelno == logging.DEBUG and r.name == _LOGGER_NAME]
    assert debug.getMessage() == "llama_cpp: probe of llama.test:8080 failed (LlmCallFailure < ConnectError)"
    assert "refused" not in caplog.text


def test_warn_llama_cpp_unreachable_without_a_reason_says_so_instead_of_printing_none(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        warn_llama_cpp_unreachable("http://user:s3cr3t@llama.test:8080/v1", None)

    [warning] = _warnings(caplog)
    assert "None" not in warning.getMessage()
    assert "(no reason given)" in warning.getMessage()
    assert "s3cr3t" not in warning.getMessage()


def test_warn_llama_cpp_unreachable_labels_the_url_and_logs_the_cause_types_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    cause = ConnectionError("password=s3cr3t")
    cause.__cause__ = OSError("s3cr3t")

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        warn_llama_cpp_unreachable("HTTP://U:P@LLAMA.test.:8080/x", "refused", cause)

    [warning] = _warnings(caplog)
    assert warning.getMessage().startswith("llama_cpp: the server at llama.test:8080 is not reachable (refused)")
    [debug] = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug.getMessage() == "llama_cpp: probe of llama.test:8080 failed (ConnectionError < OSError)"
    assert "s3cr3t" not in caplog.text


async def test_llama_cpp_http_error_probe_reason_carries_the_status_and_the_servers_bounded_text(caplog: pytest.LogCaptureFixture) -> None:
    """What the line carries about the cause: the failure's own one-line reason (the server's error text is at most
    ``MAX_SERVER_TEXT_CHARS`` characters), nothing from the configured URL."""
    client = _client(_Server(statuses={"/props": (503, {"error": {"message": "loading model"}})}))

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await client.probe_context_window(_TIMEOUT_SECONDS)

    [warning] = _warnings(caplog)
    assert "(HTTP 503: loading model)" in warning.getMessage()


async def test_llama_cpp_server_that_answers_without_a_window_is_not_called_unreachable(caplog: pytest.LogCaptureFixture) -> None:
    client = _client(_Server(props={"total_slots": 1}))

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await client.probe_context_window(_TIMEOUT_SECONDS)

    [warning] = _warnings(caplog)
    assert "not reachable" not in warning.getMessage()
    assert "without a usable context window" in warning.getMessage()
    assert str(ASSUMED_CONTEXT_TOKENS) in warning.getMessage()


@pytest.mark.parametrize(
    "second_url",
    ["http://llama.test:8080/v1", "http://user:secret@llama.test:8080/?token=abc", "HTTP://LLAMA.test.:8080", "http://llama.test:8080#frag"],
)
async def test_llama_cpp_warning_is_kept_per_host_port_label(second_url: str, caplog: pytest.LogCaptureFixture) -> None:
    """Two base URLs whose ``host_port`` label is the same share one WARNING (a statement about the label, not about what a
    real HTTP client would reach: the transport here is a mock)."""
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    failing = httpx.MockTransport(refuse)
    first = LlamaCppClient(model=_MODEL, base_url=_HOST, transport=failing)
    second = LlamaCppClient(model=_MODEL, base_url=second_url, transport=failing)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await first.probe_context_window(_TIMEOUT_SECONDS)
        await second.probe_context_window(_TIMEOUT_SECONDS)

    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize("other_url", ["http://127.0.0.1:8080", "http://llama.test:8081", "http://llama.test"])
async def test_llama_cpp_a_different_label_is_a_different_warning(other_url: str, caplog: pytest.LogCaptureFixture) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    failing = httpx.MockTransport(refuse)
    first = LlamaCppClient(model=_MODEL, base_url=_HOST, transport=failing)
    second = LlamaCppClient(model=_MODEL, base_url=other_url, transport=failing)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await first.probe_context_window(_TIMEOUT_SECONDS)
        await second.probe_context_window(_TIMEOUT_SECONDS)

    assert len(_warnings(caplog)) == 2


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

    assert raised.value.reason == "POST /tokenize: HTTP 500: tokenizer down"


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


async def test_llama_cpp_input_budget_without_apply_template_endpoint_names_the_endpoint() -> None:
    """A llama-server older than ``/apply-template`` answers 404 'File Not Found': the failure must say which request failed."""
    server = _Server(statuses={"/apply-template": (404, {"error": {"message": "File Not Found"}})})

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).input_budget(_TEMPLATE)

    assert raised.value.reason == "POST /apply-template: HTTP 404: File Not Found"
    assert _paths(server) == ["/apply-template"]


async def test_llama_cpp_input_budget_names_the_endpoint_of_a_malformed_apply_template_answer() -> None:
    server = _Server(statuses={"/apply-template": (200, {"prompt": 7})})

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).input_budget(_TEMPLATE)

    assert raised.value.reason.startswith("POST /apply-template: unexpected reply")


async def test_llama_cpp_sizing_timeout_keeps_its_type_and_names_the_endpoint() -> None:
    def server(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LlmCallTimeout) as raised:
        await _client(server).input_budget(_TEMPLATE)

    assert raised.value.reason.startswith("POST /apply-template: no answer within ")


@pytest.mark.parametrize("path", ["/apply-template", "/tokenize"])
async def test_llama_cpp_sizing_failure_masks_the_prompt_the_server_echoes(path: str) -> None:
    """The server's error quotes the prompt it was sent; the failure reason carries a marker instead of it."""
    server = _Server(echoing={path: 400})

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).input_budget(_TEMPLATE)

    assert raised.value.reason == f"POST {path}: HTTP 400: cannot process: [prompt]"
    assert _TEMPLATE not in str(raised.value)


async def test_llama_cpp_count_tokens_failure_masks_the_prompt_the_server_echoes() -> None:
    server = _Server(echoing={"/tokenize": 400})

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).count_tokens(_PROMPT)

    assert raised.value.reason == "POST /tokenize: HTTP 400: cannot process: [prompt]"
    assert _PROMPT not in str(raised.value)


async def test_llama_cpp_call_failure_masks_the_prompt_the_server_echoes() -> None:
    server = _Server(echoing={"/v1/chat/completions": 400})

    with pytest.raises(LlmCallFailure) as raised:
        await _client(server).call(_request())

    assert raised.value.reason == "HTTP 400: cannot process: [prompt]"
    assert _PROMPT not in str(raised.value)


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


async def test_llama_cpp_input_budget_counts_a_template_once_per_cached_template() -> None:
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


@pytest.mark.parametrize(
    "base_url",
    [
        "http://user:s3cr3t@llama.test:8080/?token=t0k3n",
        "http://user:s3cr3t/x@llama.test:8080",
        "http://user:s3cr3t?x@llama.test:8080",
        "http://user:s3cr3t#x@llama.test:8080",
        "http://user:12345/x@llama.test:80",
        "http://user/x@llama.test:80",
        "http://k3y?en@llama.test:8080",
    ],
)
async def test_llama_cpp_probe_logs_never_carry_credentials_of_the_base_url(base_url: str, caplog: pytest.LogCaptureFixture) -> None:
    """No log line of the failed probe (WARNING and DEBUG, message and traceback) holds a piece of the userinfo or the query."""
    failing = _Server(failures={"/props": httpx.ConnectError("refused")})
    client = LlamaCppClient(model=_MODEL, base_url=base_url, transport=httpx.MockTransport(failing))

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        await client.probe_context_window(_TIMEOUT_SECONDS)

    [warning] = _warnings(caplog)
    assert warning.getMessage().startswith("llama_cpp: the server at ")
    logged = caplog.text + "".join(logging.Formatter().formatException(r.exc_info) for r in caplog.records if r.exc_info)
    for secret in ("s3cr3t", "t0k3n", "user", "12345", "k3y"):
        assert secret not in logged, secret


async def test_llama_cpp_successful_reprobe_with_an_unchanged_window_still_recounts_the_template() -> None:
    """A server that answers again may be another model with the same window: the cached template count is dropped."""
    server = _Server()
    client = _client(server)
    await client.probe_context_window(_TIMEOUT_SECONDS)
    await client.input_budget(_TEMPLATE)

    await client.probe_context_window(_TIMEOUT_SECONDS)
    await client.input_budget(_TEMPLATE)

    assert _paths(server) == ["/props", *_TEMPLATE_REQUESTS, "/props", *_TEMPLATE_REQUESTS]


async def test_llama_cpp_template_count_in_flight_when_the_probe_succeeds_is_not_stored() -> None:
    """The count that was being taken from the server before the probe answered belongs to the server it asked: it is
    returned to its caller but not kept, so the next budget asks again."""
    started = asyncio.Event()
    release = asyncio.Event()
    served = _Server()
    state = {"gate": True}

    async def server(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tokenize" and state["gate"]:
            state["gate"] = False
            started.set()
            await release.wait()
        return served(request)

    client = _client(server)
    in_flight = asyncio.create_task(client.input_budget(_TEMPLATE))
    await asyncio.wait_for(started.wait(), _TIMEOUT_SECONDS)

    await client.probe_context_window(_TIMEOUT_SECONDS)
    release.set()
    await in_flight
    served.requests.clear()
    await client.input_budget(_TEMPLATE)

    assert _paths(served) == _TEMPLATE_REQUESTS


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


@pytest.mark.parametrize(
    ("think", "expected"),
    [
        (ThinkSetting.DEFAULT, None),
        (ThinkSetting.ON, {"enable_thinking": True}),
        (ThinkSetting.OFF, {"enable_thinking": False}),
    ],
)
async def test_llama_cpp_input_budget_frames_the_template_with_the_thinking_switch_the_call_sends(
    think: ThinkSetting, expected: dict[str, bool] | None
) -> None:
    """Qwen3's template adds an empty think block when thinking is off: the sizing must render what the call will render."""
    server = _Server()
    client = _client(server)

    await client.input_budget(_TEMPLATE, think=think)
    await client.call(LlmCallRequest(prompt=_PROMPT, max_output_tokens=ANSWER_RESERVE_TOKENS, timeout_seconds=_TIMEOUT_SECONDS, think=think))

    [applied] = server.bodies("/apply-template")
    [chat] = server.bodies("/v1/chat/completions")
    assert applied.get("chat_template_kwargs") == expected
    assert chat.get("chat_template_kwargs") == applied.get("chat_template_kwargs")


async def test_llama_cpp_input_budget_counts_the_framing_the_thinking_switch_adds() -> None:
    client = _client(_Server())
    await client.probe_context_window(_TIMEOUT_SECONDS)
    plain = _PROBED_WINDOW - ANSWER_RESERVE_TOKENS - _TEMPLATE_TOKENS - _FRAME_TOKENS

    assert await client.input_budget(_TEMPLATE) == InputBudget(tokens=plain)
    assert await client.input_budget(_TEMPLATE, think=ThinkSetting.ON) == InputBudget(tokens=plain)
    assert await client.input_budget(_TEMPLATE, think=ThinkSetting.OFF) == InputBudget(tokens=plain - _THINK_OFF_FRAME_TOKENS)


async def test_llama_cpp_input_budget_keeps_one_count_per_template_and_thinking_setting() -> None:
    server = _Server()
    client = _client(server)

    for _ in range(2):
        for think in ThinkSetting:
            await client.input_budget(_TEMPLATE, think=think)

    assert _paths(server) == _TEMPLATE_REQUESTS * len(ThinkSetting)


async def test_llama_cpp_successful_probe_drops_the_counts_of_every_thinking_setting() -> None:
    server = _Server()
    client = _client(server)
    for think in ThinkSetting:
        await client.input_budget(_TEMPLATE, think=think)
    server.requests.clear()

    await client.probe_context_window(_TIMEOUT_SECONDS)
    for think in ThinkSetting:
        await client.input_budget(_TEMPLATE, think=think)

    assert _paths(server) == ["/props", *_TEMPLATE_REQUESTS * len(ThinkSetting)]

"""Sizing the HyDE and RAG Fusion requests to the model window (S9, S11, ``llm_client.fit``).

Both generators put the user's query into a prompt template, so both size that query the way the other features
size their input: the client's ``input_budget`` for the feature's own template, the query counted by the client
itself, the head of the query kept. A cloud client has no budget and gets the query whole (S9); a window with no room
for the query skips the feature for that call with one WARNING that names the window and never holds query text.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.config import ConfigError, HyDEConfig, RAGFusionConfig, load_config
from archon_search.constants import ANSWER_RESERVE_TOKENS
from archon_search.hyde import (
    HYDE_MAX_OUTPUT_TOKENS,
    HYDE_PROMPT_TEMPLATE,
    HyDEGenerator,
)
from archon_search.llm_client.ollama_client import OllamaClient
from archon_search.llm_client.protocol import (
    LlmCallFailure,
    LlmCallRequest,
    LlmCallResult,
    LlmClient,
)
from archon_search.llm_client.token_counter import TokenCounter
from archon_search.rag_fusion import (
    RAG_FUSION_MAX_VARIANT_CHARS,
    RAG_FUSION_PROMPT_TEMPLATE,
    RAG_FUSION_TOKENS_PER_QUERY,
    RAGFusionGenerator,
)
from tests._fake_model_server import (
    FAKE_CHAT_FRAME_TOKENS,
    FakeModelServer,
    keep_loopback_out_of_proxies,
)
from tests._hyde_stubs import BudgetedLlmClient, ScriptedLlmClient
from tests._query_sizing_helpers import (
    ACCEPTED_FILL_PERCENT,
    LARGEST_REAL_OVER_ESTIMATE,
    PerCharacterTokenizer,
    words,
)
from tests._token_counter_stub import StubTokenizer

_NUM_QUERIES = 2
_WINDOW = 4096
_SECRET_QUERY_MARKER = "very-unique-secret-query"
_CLOUD_QUERY_TOKENS = 51_000  # S9: "a ~51k-token prompt"


@dataclass(frozen=True)
class Feature:
    """One generator under test, driven through its single public method."""

    name: str
    logger: str
    render: Callable[[str], str]  # the feature's prompt template around a query
    start: Callable[[LlmClient, str], Callable[[str], Awaitable[object]]]
    failed: Callable[[object], bool]

    def template(self) -> str:
        """The template with an empty query: the fixed part of every prompt."""
        return self.render("")

    def sent_query(self, prompt: str) -> str:
        """The query part of a prompt the client received."""
        before, after = self.render("\0").split("\0")
        assert prompt.startswith(before)
        assert prompt.endswith(after)
        return prompt[len(before) : len(prompt) - len(after)]


def _hyde_runner(client: LlmClient, provider: str) -> Callable[[str], Awaitable[object]]:
    embedder = MagicMock()
    embedder.embed_one = AsyncMock(return_value=[0.1, 0.2])
    generator = HyDEGenerator(HyDEConfig(enabled=True, provider=provider, timeout_seconds=5.0), client)

    async def run(query: str) -> object:
        return await generator.generate(query, embedder=embedder)

    return run


def _rag_runner(client: LlmClient, provider: str) -> Callable[[str], Awaitable[object]]:
    generator = RAGFusionGenerator(
        RAGFusionConfig(enabled=True, provider=provider, timeout_seconds=5.0, num_queries=_NUM_QUERIES), client
    )
    return generator.generate_variants


HYDE = Feature(
    name="hyde",
    logger="archon_search.hyde",
    render=lambda query: HYDE_PROMPT_TEMPLATE.format(query=query),
    start=_hyde_runner,
    failed=lambda result: result is None,
)
RAG_FUSION = Feature(
    name="rag_fusion",
    logger="archon_search.rag_fusion",
    render=lambda query: RAG_FUSION_PROMPT_TEMPLATE.format(
        num_queries=_NUM_QUERIES, max_chars=RAG_FUSION_MAX_VARIANT_CHARS, query=query
    ),
    start=_rag_runner,
    failed=lambda result: result == [],
)
FEATURES = pytest.mark.parametrize("feature", [HYDE, RAG_FUSION], ids=lambda feature: feature.name)


def _success_reply(feature: Feature) -> str:
    return "a hypothetical passage" if feature is HYDE else "first variant\nsecond variant"


def _warnings(caplog: pytest.LogCaptureFixture, feature: Feature) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == feature.logger]


class _FakeOllamaSdk:
    """Stands in for ``ollama.AsyncClient``: records the chat arguments."""

    calls: list[dict[str, Any]]

    def __init__(self, calls: list[dict[str, Any]], reply: str) -> None:
        self.calls = calls
        self._reply = reply

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def chat(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=self._reply))


def _ollama_client(
    feature: Feature, tokenizer: Callable[[], Any], calls: list[dict[str, Any]], window: int = _WINDOW
) -> OllamaClient:
    return OllamaClient(
        model="small-model",
        base_url="http://ollama.test:11434",
        context_tokens=window,
        token_counter=TokenCounter(load_tokenizer=tokenizer),
        client_factory=lambda host: _FakeOllamaSdk(calls, _success_reply(feature)),
    )


# --- cloud: whole (S9) --------------------------------------------------------------------------------------------


@FEATURES
@pytest.mark.parametrize("provider", ["openai", "anthropic", "claude_cli"])
async def test_a_cloud_query_of_51k_tokens_goes_to_the_provider_whole(feature: Feature, provider: str) -> None:
    client = ScriptedLlmClient(reply=_success_reply(feature))  # unbounded window, unbounded budget, cannot count
    query = words(_CLOUD_QUERY_TOKENS)

    result = await feature.start(client, provider)(query)

    assert not feature.failed(result)
    [request] = client.requests
    assert feature.sent_query(request.prompt) == query


# --- local providers: trimmed to the window -----------------------------------------------------------------------


@FEATURES
async def test_the_budget_is_asked_for_the_features_own_template_with_an_empty_query(feature: Feature) -> None:
    client = BudgetedLlmClient(window_tokens=_WINDOW, budget=500, reply=_success_reply(feature))

    await feature.start(client, "ollama")("a short query")

    assert client.templates == [feature.template()]


@FEATURES
async def test_a_query_inside_the_budget_is_sent_unchanged(feature: Feature) -> None:
    client = BudgetedLlmClient(window_tokens=_WINDOW, budget=500, reply=_success_reply(feature))
    query = words(500)  # exactly the budget

    await feature.start(client, "ollama")(query)

    assert feature.sent_query(client.requests[0].prompt) == query


@FEATURES
async def test_a_query_over_the_budget_is_cut_to_a_prefix_at_a_word_boundary(feature: Feature) -> None:
    client = BudgetedLlmClient(window_tokens=_WINDOW, budget=60, reply=_success_reply(feature))
    query = words(500)

    result = await feature.start(client, "ollama")(query)

    assert not feature.failed(result), "a trimmed query still gets its expansion"
    sent = feature.sent_query(client.requests[0].prompt)
    assert query.startswith(sent)
    assert query[len(sent)] == " "
    assert 60 * ACCEPTED_FILL_PERCENT // 100 <= len(sent.split()) <= 60


@FEATURES
@pytest.mark.parametrize(
    ("query", "tokenizer"),
    [
        (words(4000), StubTokenizer),
        ("検索拡張生成の長い質問" * 600, PerCharacterTokenizer),
        (" ".join(["20260101123456789"] * 600), StubTokenizer),
    ],
    ids=["english", "cjk", "digits"],
)
async def test_a_long_query_on_a_4096_ollama_window_fits_template_query_and_reserve(
    feature: Feature, query: str, tokenizer: Callable[[], Any]
) -> None:
    calls: list[dict[str, Any]] = []
    client = _ollama_client(feature, tokenizer, calls)
    estimate = TokenCounter(load_tokenizer=tokenizer).count

    result = await feature.start(client, "ollama")(query)

    assert not feature.failed(result)
    [call] = calls
    sent = feature.sent_query(call["messages"][0]["content"])
    # By the same estimator the budget uses, with the largest real-over-estimate ratio, the call fits the window.
    estimated = estimate(feature.template()) + estimate(sent)
    assert estimated * LARGEST_REAL_OVER_ESTIMATE + ANSWER_RESERVE_TOKENS <= _WINDOW
    assert query.startswith(sent)
    assert len(sent) < len(query), "the query had to be trimmed"
    # ... and the cut is not wasteful: the query takes most of the room that is left.
    room = int((_WINDOW - ANSWER_RESERVE_TOKENS) / LARGEST_REAL_OVER_ESTIMATE) - estimate(feature.template())
    assert estimate(sent) >= room * ACCEPTED_FILL_PERCENT // 100


@pytest.mark.integration
@FEATURES
async def test_llama_cpp_counts_exactly_through_the_server_and_fits_the_window(
    feature: Feature, monkeypatch: pytest.MonkeyPatch
) -> None:
    from archon_search.llm_client.llama_cpp_client import LlamaCppClient

    keep_loopback_out_of_proxies(monkeypatch)
    window = 1300  # the fake server counts one token per word and frames a message with two more
    query = words(2000)

    with FakeModelServer(reply_text=_success_reply(feature), llama_n_ctx=window) as server:
        client = LlamaCppClient(model="local-model", base_url=server.base_url)
        assert (await client.probe_context_window(5.0)).size == window
        result = await feature.start(client, "llama_cpp")(query)
        [chat] = server.requests_to("/v1/chat/completions")

    assert not feature.failed(result)
    assert chat.body is not None
    prompt = chat.body["messages"][0]["content"]
    template_words = len(feature.template().split())
    prompt_tokens = len(prompt.split()) + FAKE_CHAT_FRAME_TOKENS
    assert prompt_tokens + ANSWER_RESERVE_TOKENS <= window
    room = window - ANSWER_RESERVE_TOKENS - FAKE_CHAT_FRAME_TOKENS - template_words
    sent_words = len(prompt.split()) - template_words
    assert room * ACCEPTED_FILL_PERCENT // 100 <= sent_words <= room
    assert prompt.endswith(f"{words(sent_words)}\n---")


# --- no room: the feature is skipped for that call ----------------------------------------------------------------


@FEATURES
async def test_a_window_with_no_room_skips_the_feature_with_one_warning_naming_the_window(
    feature: Feature, caplog: pytest.LogCaptureFixture
) -> None:
    client = BudgetedLlmClient(window_tokens=1100, budget=0, reply=_success_reply(feature))
    run = feature.start(client, "ollama")
    query = f"{_SECRET_QUERY_MARKER} {words(50)}"

    with caplog.at_level(logging.DEBUG):
        results = [await run(f"{query} {attempt}") for attempt in range(3)]

    assert all(feature.failed(result) for result in results)
    assert client.requests == [], "no call is made when the window has no room for the query"
    [warning] = _warnings(caplog, feature)
    assert "1100 tokens" in warning.getMessage()
    assert "too little" in warning.getMessage()
    assert _SECRET_QUERY_MARKER not in " ".join(f"{r.getMessage()} {r.args}" for r in caplog.records)


@FEATURES
async def test_a_budget_too_small_for_one_word_is_no_room_too(feature: Feature, caplog: pytest.LogCaptureFixture) -> None:
    client = BudgetedLlmClient(window_tokens=1100, budget=30, tokens_per_word=50, reply=_success_reply(feature))

    with caplog.at_level(logging.WARNING):
        result = await feature.start(client, "ollama")(words(40))

    assert feature.failed(result)
    assert client.requests == []
    assert len(_warnings(caplog, feature)) == 1


@FEATURES
async def test_no_room_and_a_provider_failure_each_log_their_own_first_warning(
    feature: Feature, caplog: pytest.LogCaptureFixture
) -> None:
    no_room = BudgetedLlmClient(window_tokens=1100, budget=0)
    failing = ScriptedLlmClient(failure=LlmCallFailure("ollama", "connection refused"))

    with caplog.at_level(logging.WARNING):
        await feature.start(failing, "ollama")("query one")
        await feature.start(no_room, "ollama")("query two")
        await feature.start(no_room, "ollama")("query three")

    messages = [warning.getMessage() for warning in _warnings(caplog, feature)]
    assert len(messages) == 2
    assert "connection refused" in messages[0]
    assert "1100 tokens" in messages[1]


@FEATURES
async def test_a_failing_count_is_a_provider_failure_with_the_ordinary_warning(
    feature: Feature, caplog: pytest.LogCaptureFixture
) -> None:
    client = BudgetedLlmClient(
        window_tokens=_WINDOW, budget=50, count_failure=LlmCallFailure("llama_cpp", "cannot connect to the llama-server")
    )
    run = feature.start(client, "llama_cpp")

    with caplog.at_level(logging.WARNING):
        results = [await run(f"{_SECRET_QUERY_MARKER} {attempt} {words(100)}") for attempt in range(2)]

    assert all(feature.failed(result) for result in results)
    assert client.requests == []
    [warning] = _warnings(caplog, feature)
    assert "cannot connect to the llama-server" in warning.getMessage()
    assert _SECRET_QUERY_MARKER not in warning.getMessage()


@FEATURES
async def test_a_failure_text_echoing_the_trimmed_query_is_masked(feature: Feature, caplog: pytest.LogCaptureFixture) -> None:
    """What the provider received is the cut query: that is the text a server can echo back, and the log never holds it."""

    class Echoing(BudgetedLlmClient):
        async def call(self, request: LlmCallRequest) -> LlmCallResult:
            raise LlmCallFailure("ollama", f'rejected "{feature.sent_query(request.prompt)}"')

    client = Echoing(window_tokens=_WINDOW, budget=40)
    query = f"{_SECRET_QUERY_MARKER} {words(300)}"

    with caplog.at_level(logging.WARNING):
        assert feature.failed(await feature.start(client, "ollama")(query))

    [warning] = _warnings(caplog, feature)
    assert "rejected" in warning.getMessage()
    assert _SECRET_QUERY_MARKER not in warning.getMessage()
    assert "word10" not in warning.getMessage()


# --- the answer ceiling stays inside the reserve the budget holds back --------------------------------------------


def test_the_largest_rag_fusion_answer_fits_the_reserve_the_budget_holds_back(tmp_path: Path) -> None:
    """The budget leaves ``ANSWER_RESERVE_TOKENS`` for the answer; ``num_queries`` is capped so 150 per variant stays inside."""
    config_file = tmp_path / "archon-search.toml"
    config_file.write_text("[rag_fusion]\nnum_queries = 5\n")
    largest = load_config(config_file).rag_fusion.num_queries
    config_file.write_text("[rag_fusion]\nnum_queries = 6\n")

    with pytest.raises(ConfigError):
        load_config(config_file)
    assert RAG_FUSION_TOKENS_PER_QUERY * largest <= ANSWER_RESERVE_TOKENS


def test_the_hyde_answer_ceiling_fits_the_reserve_the_budget_holds_back() -> None:
    assert HYDE_MAX_OUTPUT_TOKENS <= ANSWER_RESERVE_TOKENS


async def test_hyde_and_rag_fusion_each_log_their_own_no_room_warning(caplog: pytest.LogCaptureFixture) -> None:
    """The once-per-process keys are per feature: one feature's warning never silences the other's."""
    no_room = BudgetedLlmClient(window_tokens=1100, budget=0)

    with caplog.at_level(logging.WARNING):
        await HYDE.start(no_room, "ollama")("query one")
        await RAG_FUSION.start(no_room, "ollama")("query two")

    assert len(_warnings(caplog, HYDE)) == 1
    assert len(_warnings(caplog, RAG_FUSION)) == 1

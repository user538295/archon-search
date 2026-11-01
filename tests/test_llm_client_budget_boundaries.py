"""The invariant "prompt tokens + the call's ``max_output_tokens`` <= window" at the edges the counts cannot see.

* ``llama_cpp``: the template and the query are counted apart but sent joined, and a token spans the join (the blank line of
  an empty template is ONE token, the same lines around a query are TWO). A query cut to exactly the budget must still leave
  the answer ceiling inside ``n_ctx`` (measured on a real llama-server: 4097 > 4096 before ``LLAMA_CPP_JOIN_SLACK_TOKENS``).
* ``ollama``: the chat template adds REAL tokens (9 to 33 measured) that the estimate does not count; they come off the
  window before the margin divides it, so a query at the budget fits even at the worst real/estimate ratio.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from archon_search.hyde import HYDE_MAX_OUTPUT_TOKENS, HYDE_PROMPT_TEMPLATE
from archon_search.llm_client.fit import fit_query_to_window
from archon_search.llm_client.llama_cpp_client import LlamaCppClient
from archon_search.llm_client.ollama_client import OllamaClient
from archon_search.llm_client.token_counter import TokenCounter
from archon_search.rag_fusion import (
    RAG_FUSION_MAX_VARIANT_CHARS,
    RAG_FUSION_PROMPT_TEMPLATE,
    RAG_FUSION_TOKENS_PER_QUERY,
)
from tests._token_counter_stub import StubTokenizer

_HOST = "http://llama.test:8080"
_TIMEOUT_SECONDS = 5.0
_WORST_REAL_OVER_ESTIMATE = 1.2
# Real framing tokens measured on the Ollama models (llama3.1:8b 9, gemma3:1b 8, qwen2.5 29, tinyllama 33).
_FRAMING_TOKENS = [9, 29, 33, 64]  # the three real ones, and the allowance itself: a model that framed with all of it still fits
_TOKEN = re.compile(r"\n+|\S+")  # a run of newlines is one token, like "\n\n" in a real vocabulary: the join boundary
_STARVATION_FLOOR = {2048: 900, 4096: 2000, 8192: 5000}  # estimate tokens a query keeps at the smallest feature room

Render = Callable[[str], str]


def _hyde(query: str) -> str:
    return HYDE_PROMPT_TEMPLATE.format(query=query)


def _rag(variants: int) -> Render:
    def render(query: str) -> str:
        return RAG_FUSION_PROMPT_TEMPLATE.format(num_queries=variants, max_chars=RAG_FUSION_MAX_VARIANT_CHARS, query=query)

    return render


FEATURES = [
    pytest.param(_hyde, HYDE_MAX_OUTPUT_TOKENS, id="hyde"),
    pytest.param(_rag(1), RAG_FUSION_TOKENS_PER_QUERY, id="rag1"),
    pytest.param(_rag(5), RAG_FUSION_TOKENS_PER_QUERY * 5, id="rag5"),
]


def _framed(content: str) -> str:
    return f"<|user|>\n{content}\n<|assistant|>\n"


def _llama_server(window: int, tokens_of: Callable[[str], list[int]]) -> Callable[[httpx.Request], httpx.Response]:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": window}})
        body: dict[str, Any] = json.loads(request.content)
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": _framed(body["messages"][0]["content"])})
        return httpx.Response(200, json={"tokens": tokens_of(body["content"])})

    return answer


def _newline_runs_are_one_token(text: str, extra_join_tokens: int = 0, templates: frozenset[str] = frozenset()) -> list[int]:
    """Tokens of *text*. A framed prompt that holds a query costs *extra_join_tokens* more (a wider join than the newline's);
    the framed empty *templates* do not: their count is the one the budget is made from, the join exists only once a query is in."""
    tokens = len(_TOKEN.findall(text))
    if text.startswith("<|user|>") and text not in templates:
        tokens += extra_join_tokens
    return list(range(tokens))


def _one_token_per_byte(text: str) -> list[int]:
    return list(text.encode("utf-8"))


async def _llama(window: int, tokens_of: Callable[[str], list[int]] = _newline_runs_are_one_token) -> LlamaCppClient:
    client = LlamaCppClient(model="m", base_url=_HOST, transport=httpx.MockTransport(_llama_server(window, tokens_of)))
    assert (await client.probe_context_window(_TIMEOUT_SECONDS)).size == window
    return client


async def _prompt_tokens(client: LlamaCppClient, prompt: str) -> int:
    """What the server holds for the call: the prompt as the chat template frames it, counted whole."""
    return await client.count_tokens(_framed(prompt))


def _words(count: int) -> str:
    return " ".join("w" * (1 + index % 7) for index in range(count))


@pytest.mark.parametrize("window", [2048, 4096, 8192])
@pytest.mark.parametrize(("render", "ceiling"), FEATURES)
async def test_llama_cpp_query_of_exactly_the_budget_leaves_the_answer_ceiling_inside_the_window(
    render: Render, ceiling: int, window: int
) -> None:
    client = await _llama(window)
    budget = (await client.input_budget(render(""), max_output_tokens=ceiling)).tokens
    assert isinstance(budget, int)
    query = _words(budget)
    assert await client.count_tokens(query) == budget

    fitted = await fit_query_to_window(client, render(""), query, provider="llama_cpp", max_output_tokens=ceiling)

    assert fitted == query  # exactly the budget is accepted whole ...
    assert await _prompt_tokens(client, render(fitted)) + ceiling <= window  # ... and the answer ceiling still fits


@pytest.mark.parametrize(("render", "ceiling"), FEATURES)
async def test_llama_cpp_query_cut_to_the_budget_leaves_the_answer_ceiling_inside_the_window(render: Render, ceiling: int) -> None:
    window = 4096
    client = await _llama(window)
    long_query = _words(20_000)

    fitted = await fit_query_to_window(client, render(""), long_query, provider="llama_cpp", max_output_tokens=ceiling)

    assert 0 < len(fitted) < len(long_query)
    assert await _prompt_tokens(client, render(fitted)) + ceiling <= window


@pytest.mark.parametrize("extra_join_tokens", [1, 3], ids=["worst-measured-join-of-2", "join-of-4-the-whole-slack"])
@pytest.mark.parametrize(("render", "ceiling"), FEATURES)
async def test_llama_cpp_query_of_exactly_the_budget_fits_with_the_widest_join_the_slack_covers(
    render: Render, ceiling: int, extra_join_tokens: int
) -> None:
    """The measured worst surplus is 2 tokens (the join of the graph template); the allowance is 4, twice that."""
    window = 4096
    client = await _llama(window, _wide_join(render, extra_join_tokens))
    budget = (await client.input_budget(render(""), max_output_tokens=ceiling)).tokens
    assert isinstance(budget, int)

    fitted = await fit_query_to_window(client, render(""), _words(budget), provider="llama_cpp", max_output_tokens=ceiling)

    # The newline's own token plus *extra_join_tokens* are the surplus: 2 and 4, and at 4 the window is filled exactly.
    assert await _prompt_tokens(client, render(fitted)) + ceiling <= window


@pytest.mark.parametrize(("render", "ceiling"), FEATURES)
async def test_a_join_wider_than_the_slack_is_the_limit_the_slack_does_not_cover(render: Render, ceiling: int) -> None:
    """Pins the tightness: a join of 5 tokens (4 extra beside the newline's own) overruns by one, so 4 is no more than needed."""
    window = 4096
    client = await _llama(window, _wide_join(render, 4))
    budget = (await client.input_budget(render(""), max_output_tokens=ceiling)).tokens
    assert isinstance(budget, int)

    fitted = await fit_query_to_window(client, render(""), _words(budget), provider="llama_cpp", max_output_tokens=ceiling)

    assert await _prompt_tokens(client, render(fitted)) + ceiling == window + 1


def _wide_join(render: Render, extra_join_tokens: int) -> Callable[[str], list[int]]:
    templates = frozenset({_framed(render(""))})
    return lambda text: _newline_runs_are_one_token(text, extra_join_tokens, templates)


async def test_the_join_boundary_really_costs_a_token_on_this_fake() -> None:
    """Guards the fixture: without the join cost the tests above would pass for the wrong reason."""
    client = await _llama(4096)

    empty = await _prompt_tokens(client, _hyde(""))
    one_word = await _prompt_tokens(client, _hyde("w"))

    assert one_word - empty == 2  # the word, and the second newline token of the blank line it splits


@pytest.mark.parametrize("ceiling", [HYDE_MAX_OUTPUT_TOKENS, RAG_FUSION_TOKENS_PER_QUERY * 5])
async def test_llama_cpp_byte_fast_path_query_never_overruns_the_window(ceiling: int) -> None:
    window = 4096
    client = await _llama(window, _one_token_per_byte)  # the worst case ``token_ceiling`` bounds
    budget = (await client.input_budget(_hyde(""), max_output_tokens=ceiling)).tokens
    assert isinstance(budget, int)
    query = "x" * (budget - 1)  # ceiling = bytes + 1 = budget: the largest query the fast path sends uncounted
    assert client.token_ceiling(query) == budget
    counted: list[str] = []
    real_count = client.count_tokens

    async def spying_count(text: str) -> int:
        counted.append(text)
        return await real_count(text)

    client.count_tokens = spying_count  # type: ignore[method-assign]

    fitted = await fit_query_to_window(client, _hyde(""), query, provider="llama_cpp", max_output_tokens=ceiling)

    assert fitted == query
    assert counted == []  # the fast path: not one request spent on the query
    assert await real_count(_framed(_hyde(fitted))) + ceiling <= window


def _ollama(window: int) -> OllamaClient:
    return OllamaClient(
        model="m",
        base_url="http://ollama.test:11434",
        context_tokens=window,
        token_counter=TokenCounter(load_tokenizer=StubTokenizer),
        client_factory=lambda host: object(),
    )


@pytest.mark.parametrize("framing", _FRAMING_TOKENS)
@pytest.mark.parametrize("window", [2048, 4096, 8192])
@pytest.mark.parametrize(("render", "ceiling"), FEATURES)
async def test_ollama_prompt_at_the_budget_fits_with_the_chat_framing_at_the_worst_ratio(
    render: Render, ceiling: int, window: int, framing: int
) -> None:
    counter = TokenCounter(load_tokenizer=StubTokenizer)
    budget = (await _ollama(window).input_budget(render(""), max_output_tokens=ceiling)).tokens
    assert isinstance(budget, int)
    query = _words(budget)
    prompt_estimate = counter.count(render(query))
    assert counter.count(query) == budget

    # The real tokenizer counts up to 20% over the estimate, and the chat template adds its own tokens on top.
    real_tokens = math.ceil(prompt_estimate * _WORST_REAL_OVER_ESTIMATE) + framing + ceiling

    assert real_tokens <= window


@pytest.mark.parametrize("window", sorted(_STARVATION_FLOOR))
async def test_no_feature_starves_at_the_common_windows(window: int) -> None:
    """The framing allowance leaves every feature room for a real query, even at 2048 with five variants."""
    client = _ollama(window)

    for render, ceiling in ((_hyde, HYDE_MAX_OUTPUT_TOKENS), (_rag(5), RAG_FUSION_TOKENS_PER_QUERY * 5)):
        budget = (await client.input_budget(render(""), max_output_tokens=ceiling)).tokens
        assert isinstance(budget, int)
        assert budget >= _STARVATION_FLOOR[window]

"""What sizing a query costs the model server on every search (HyDE and RAG Fusion), through the real app.

The window-fit of a query (``llm_client/fit.py``) used to ask the server to render and count the fixed prompt template on
every search, and to count every query however short: on ``llama_cpp`` about three to twelve local requests per
expansion, on ``ollama`` a gpt2 count of the template and of the query. Now the template is counted once per process and a
query whose UTF-8 length is within the budget skips the count (a token covers at least one byte). Only a query longer than
the room left goes through the counting loop. The recorder of ``FakeModelServer`` (llama.cpp) and a recording
``TokenCounter.count`` (Ollama's in-process estimate) show what is asked.

Run with:
    uv run pytest tests/integration/test_lcd_gap6_query_sizing_cost_e2e.py --no-cov -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from archon_search.constants import ANSWER_RESERVE_TOKENS
from archon_search.llm_client.fit import fit_text_to_budget
from archon_search.llm_client.token_counter import TokenCounter
from tests._fake_model_server import (
    FAKE_CHAT_FRAME_TOKENS,
    FakeModelServer,
    keep_loopback_out_of_proxies,
)
from tests._query_sizing_helpers import words
from tests.integration.conftest import ingest_doc, make_real_app
from tests.integration.test_lcd_gap2_query_sizing_e2e import (
    _COLLECTION,
    _DOC_PATH,
    _DOC_TEXT,
    _OPENAI_CHAT_PATH,
    FEATURES,
    Feature,
    _await_warmup,
    _await_window_resolution,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)

_LLAMA_CPP_WINDOW = 2048
_OLLAMA_WINDOW = 4096
_SHORT_QUERY = "migration rollout steps"
_LONG_QUERY_WORDS = 3000
_SEARCHES = 3
_TOKENIZE_PATH = "/tokenize"
_APPLY_TEMPLATE_PATH = "/apply-template"
_TEMPLATE_PATHS = (_APPLY_TEMPLATE_PATH, _TOKENIZE_PATH)  # sizing one template: render it, count it


def _llama_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feature: Feature, server: FakeModelServer):
    toml = feature.toml(provider="llama_cpp", llama_cpp_base_url=server.base_url)
    return make_real_app(tmp_path, monkeypatch, toml_content=toml)


def _sizing_requests(server: FakeModelServer) -> list[str]:
    """The paths of every request the model server saw that is about sizing a prompt, in order."""
    return [request.path for request in server.requests if request.path in (_TOKENIZE_PATH, _APPLY_TEMPLATE_PATH)]


@FEATURES
def test_e2e_llama_cpp_a_short_query_costs_the_server_nothing_after_the_template_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feature: Feature
) -> None:
    with FakeModelServer(reply_text=feature.reply, llama_n_ctx=_LLAMA_CPP_WINDOW) as server:
        with _llama_boot(tmp_path, monkeypatch, feature, server) as (client, _cfg, key):
            ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
            _await_warmup(client)
            _await_window_resolution(client)
            sizing_after_each_search: list[list[str]] = []
            for _ in range(_SEARCHES):
                assert feature.applied(feature.search(client, key, _SHORT_QUERY))
                sizing_after_each_search.append(_sizing_requests(server))
        chats = server.requests_to(_OPENAI_CHAT_PATH)

    assert sizing_after_each_search[0] == list(_TEMPLATE_PATHS), "the first search counts the template once"
    assert sizing_after_each_search[1] == sizing_after_each_search[0], "the second search asks the server nothing about sizing"
    assert sizing_after_each_search[2] == sizing_after_each_search[0], "nor does the third"
    assert [feature.sent_query(chat.body["messages"][0]["content"]) for chat in chats if chat.body] == [_SHORT_QUERY] * _SEARCHES


@FEATURES
def test_e2e_llama_cpp_a_long_query_costs_exactly_the_counts_of_its_fit_and_the_template_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feature: Feature
) -> None:
    query = f"{_SHORT_QUERY} {words(_LONG_QUERY_WORDS)}"
    template_words = len(feature.template.split())
    budget = _LLAMA_CPP_WINDOW - ANSWER_RESERVE_TOKENS - FAKE_CHAT_FRAME_TOKENS - template_words  # the fake counts words
    reference_counts: list[str] = []

    async def reference_count(text: str) -> int:
        reference_counts.append(text)
        return len(text.split())

    asyncio.run(fit_text_to_budget(query, budget, reference_count))  # the counts a fit of this query takes, by construction
    assert len(reference_counts) > 1, "the long query must need the loop for this test to prove anything"

    with (
        FakeModelServer(reply_text=feature.reply, llama_n_ctx=_LLAMA_CPP_WINDOW) as server,
        _llama_boot(tmp_path, monkeypatch, feature, server) as (client, _cfg, key),
    ):
        ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
        _await_warmup(client)
        _await_window_resolution(client)
        assert feature.applied(feature.search(client, key, query))
        after_first = _sizing_requests(server)
        assert feature.applied(feature.search(client, key, query))
        after_second = _sizing_requests(server)

    fit_counts = [_TOKENIZE_PATH] * len(reference_counts)
    assert after_first == [*_TEMPLATE_PATHS, *fit_counts]
    assert after_second[len(after_first) :] == fit_counts, "the second search repeats the fit of the query and nothing else"


@FEATURES
def test_e2e_ollama_a_short_query_is_not_counted_and_the_template_is_counted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feature: Feature
) -> None:
    counted: list[str] = []
    estimate = TokenCounter.count

    def recording(self: TokenCounter, text: str) -> int:
        counted.append(text)
        return estimate(self, text)

    monkeypatch.setattr(TokenCounter, "count", recording)
    with FakeModelServer(reply_text=feature.reply) as server:
        toml = feature.toml(provider="ollama", ollama_base_url=server.base_url, ollama_context_tokens=_OLLAMA_WINDOW)
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, key):
            ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
            _await_warmup(client)
            for _ in range(_SEARCHES):
                assert feature.applied(feature.search(client, key, _SHORT_QUERY))

    assert counted == [feature.template], "the template once, and never the short query"


@FEATURES
def test_e2e_ollama_a_long_query_is_counted_by_its_fit_and_the_template_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feature: Feature
) -> None:
    query = f"{_SHORT_QUERY} {words(_LONG_QUERY_WORDS)}"
    counted: list[str] = []
    estimate = TokenCounter.count

    def recording(self: TokenCounter, text: str) -> int:
        counted.append(text)
        return estimate(self, text)

    monkeypatch.setattr(TokenCounter, "count", recording)
    with FakeModelServer(reply_text=feature.reply) as server:
        toml = feature.toml(provider="ollama", ollama_base_url=server.base_url, ollama_context_tokens=_OLLAMA_WINDOW)
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, key):
            ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
            _await_warmup(client)
            assert feature.applied(feature.search(client, key, query))
            after_first = list(counted)
            assert feature.applied(feature.search(client, key, query))
            after_second = list(counted)

    assert after_first[0] == feature.template
    assert feature.template not in after_first[1:], "the template is counted once"
    query_counts = after_first[1:]
    assert query_counts, "the long query went through the counting loop"
    assert len(after_second) - len(after_first) == len(query_counts), "the second search repeats the fit of the query and nothing else"

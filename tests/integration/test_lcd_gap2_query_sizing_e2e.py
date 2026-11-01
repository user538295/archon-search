"""Gap fix (S9, S11, BE-4) — HyDE and RAG Fusion size their query to the model window, through the real app.

``make_real_app`` boots the real lifespan from a ``[hyde]`` / ``[rag_fusion]`` TOML section with a real LanceDB store;
only the model host is faked (``tests/_fake_model_server.py``, a real HTTP server the real ``ollama`` and ``openai``
SDKs and the ``llama_cpp`` wrapper's ``httpx`` talk to). The wire is what is asserted: the chat request the server
recorded holds the query cut to the window on a small Ollama or llama.cpp model, and the whole query on a cloud
provider (``openai``, aimed at the fake through the SDK's own ``OPENAI_BASE_URL``, like T-7). A window with no room for
the query skips the feature with one WARNING and no model call.

Run with:
    uv run pytest tests/integration/test_lcd_gap2_query_sizing_e2e.py --no-cov -q
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.constants import ANSWER_RESERVE_TOKENS
from archon_search.embedder import ModelEmbedder
from archon_search.hyde import HYDE_PROMPT_TEMPLATE
from archon_search.llm_client.token_counter import TokenCounter
from archon_search.rag_fusion import (
    RAG_FUSION_MAX_VARIANT_CHARS,
    RAG_FUSION_PROMPT_TEMPLATE,
)
from archon_search.server.schemas import WarmupResult
from tests._fake_model_server import (
    FAKE_CHAT_FRAME_TOKENS,
    FakeModelServer,
    keep_loopback_out_of_proxies,
)
from tests._query_sizing_helpers import (
    ACCEPTED_FILL_PERCENT,
    LARGEST_REAL_OVER_ESTIMATE,
    words,
)
from tests._token_counter_stub import StubTokenizer
from tests.integration.conftest import ingest_doc, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "sizing-e2e"
_DOC_TEXT = "Migration procedure documentation for the rollout."
_DOC_PATH = "/docs/migration.md"
_QUERY_MARKER = "zebra-quokka-7731"
_NUM_QUERIES = 2
_OLLAMA_WINDOW = 4096
_LLAMA_CPP_WINDOW = 2048
_NO_ROOM_WINDOW = ANSWER_RESERVE_TOKENS  # exactly the answer reserve: nothing is left for the template or the query
_CLOUD_QUERY_WORDS = 51_000  # S9: "a ~51k-token prompt"
_REQUEST_LIMIT_MB = 1
_REQUEST_TOO_LARGE = 413
_LOCAL_QUERY_WORDS = 8000
_SEARCHES = 3
_CALL_TIMEOUT_SECONDS = 10  # generous: the first call also pays the lazy SDK import
_WARMUP_WAIT_SECONDS = 30.0
_OLLAMA_CHAT_PATH = "/api/chat"
_OPENAI_CHAT_PATH = "/v1/chat/completions"
_OPENAI_KEY = "test-key-not-real"
_MODEL = "fake-model"


@dataclass(frozen=True)
class Feature:
    """One of the two query-expansion features, as the app exposes it."""

    section: str  # the TOML section and the request flag
    reply: str
    applied_key: str
    failure_warning: str | None  # ``expansion_warning`` of a fallback answer
    template: str  # the feature's prompt template with an empty query
    prompt_around_query: tuple[str, str]  # the fixed text before and after the query in every prompt

    def toml(self, **provider_lines: str | int) -> str:
        lines = [f"[{self.section}]", "enabled = true", f'model = "{_MODEL}"', f"timeout_seconds = {_CALL_TIMEOUT_SECONDS}"]
        if self.section == "rag_fusion":
            lines.append(f"num_queries = {_NUM_QUERIES}")
        lines += [f"{key} = {json.dumps(value)}" for key, value in provider_lines.items()]
        return "\n".join(lines) + "\n"

    def search(self, client: TestClient, api_key: str, query: str) -> dict[str, Any]:
        response = client.post(
            "/search",
            json={"query": query, "collection": _COLLECTION, self.section: True},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def applied(self, data: dict[str, Any]) -> bool:
        return data[self.applied_key] is True

    def sent_query(self, prompt: str) -> str:
        before, after = self.prompt_around_query
        assert prompt.startswith(before)
        assert prompt.endswith(after)
        return prompt[len(before) : len(prompt) - len(after)]


def _around(template: str) -> tuple[str, str]:
    before, after = template.format(query="\0", num_queries=_NUM_QUERIES, max_chars=RAG_FUSION_MAX_VARIANT_CHARS).split("\0")
    return before, after


HYDE = Feature(
    section="hyde",
    reply="A hypothetical passage about the migration rollout.",
    applied_key="hyde_applied",
    failure_warning="HyDE expansion failed",
    template=HYDE_PROMPT_TEMPLATE.format(query=""),
    prompt_around_query=_around(HYDE_PROMPT_TEMPLATE),
)
RAG_FUSION = Feature(
    section="rag_fusion",
    reply="rollout steps\nupgrade runbook",
    applied_key="rag_fusion_applied",
    failure_warning=None,
    template=RAG_FUSION_PROMPT_TEMPLATE.format(num_queries=_NUM_QUERIES, max_chars=RAG_FUSION_MAX_VARIANT_CHARS, query=""),
    prompt_around_query=_around(RAG_FUSION_PROMPT_TEMPLATE),
)
FEATURES = pytest.mark.parametrize("feature", [HYDE, RAG_FUSION], ids=lambda feature: feature.section)


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


@pytest.fixture
def embedded_texts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every text the embedder is asked to embed, in order (wraps the autouse stub's ``encode``)."""
    seen: list[str] = []
    stubbed: Callable[[ModelEmbedder, list[str]], list[list[float]]] = ModelEmbedder.encode

    def recording(self: ModelEmbedder, texts: list[str]) -> list[list[float]]:
        seen.extend(texts)
        return stubbed(self, texts)

    monkeypatch.setattr(ModelEmbedder, "encode", recording)
    return seen


def _await_warmup(client: TestClient) -> None:
    """A search that starts before warm-up ends goes lexical-only and skips query expansion."""
    deadline = time.monotonic() + _WARMUP_WAIT_SECONDS
    while getattr(client.app.state, "warmup_result", None) in (None, WarmupResult.PENDING):
        assert time.monotonic() < deadline, "lifespan warm-up did not finish"
        time.sleep(0.01)
    assert client.app.state.warmup_result == WarmupResult.DONE


def _await_window_resolution(client: TestClient) -> None:
    """Wait for the lifespan's background window resolution (it probes ``/props`` of a llama.cpp client)."""
    task = client.app.state._llm_window_task
    deadline = time.monotonic() + _WARMUP_WAIT_SECONDS
    while not task.done():
        assert time.monotonic() < deadline, "context windows were not resolved"
        time.sleep(0.01)


def _queries_in_chats(server: FakeModelServer, feature: Feature, path: str) -> list[str]:
    chats = server.requests_to(path)
    assert chats, "the provider was never called"
    return [feature.sent_query(chat.body["messages"][0]["content"]) for chat in chats if chat.body is not None]


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    feature: Feature,
    toml: str,
    query: str,
    *,
    wait_for_windows: bool = False,
    searches: int = 1,
) -> list[dict[str, Any]]:
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, key):
        ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
        _await_warmup(client)
        if wait_for_windows:
            _await_window_resolution(client)
        return [feature.search(client, key, query) for _ in range(searches)]


# --- Ollama, 4096 window ------------------------------------------------------------------------------------------


@FEATURES
@pytest.mark.parametrize(
    "query",
    [f"{_QUERY_MARKER} {words(_LOCAL_QUERY_WORDS)}", " ".join([f"{_QUERY_MARKER}-20260101123456789"] * 3000)],
    ids=["english", "digits"],
)
def test_e2e_ollama_4096_window_query_is_cut_to_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embedded_texts: list[str], feature: Feature, query: str
) -> None:
    estimate = TokenCounter(load_tokenizer=StubTokenizer).count  # the estimator the app's Ollama client sizes with
    with FakeModelServer(reply_text=feature.reply) as server:
        toml = feature.toml(provider="ollama", ollama_base_url=server.base_url, ollama_context_tokens=_OLLAMA_WINDOW)
        [data] = _run(tmp_path, monkeypatch, feature, toml, query)
        [sent] = _queries_in_chats(server, feature, _OLLAMA_CHAT_PATH)

    assert feature.applied(data), "the trimmed query still gets its expansion"
    assert query.startswith(sent)
    assert len(sent) < len(query)
    assert sent == sent.rstrip()
    assert query[len(sent)] == " ", "the cut falls on a word boundary"
    assert (estimate(feature.template) + estimate(sent)) * LARGEST_REAL_OVER_ESTIMATE + ANSWER_RESERVE_TOKENS <= _OLLAMA_WINDOW
    room = int((_OLLAMA_WINDOW - ANSWER_RESERVE_TOKENS) / LARGEST_REAL_OVER_ESTIMATE) - estimate(feature.template)
    assert estimate(sent) >= room * ACCEPTED_FILL_PERCENT // 100, "the cut is not wasteful"
    # The original query still drives the search: RAG Fusion embeds it whole next to the variants; HyDE embeds the
    # hypothesis instead, and neither embeds the cut text.
    assert sent not in embedded_texts
    if feature is RAG_FUSION:
        assert query in embedded_texts


# --- llama.cpp: the probed window, an exact count -----------------------------------------------------------------


@FEATURES
def test_e2e_llama_cpp_probed_window_query_is_cut_by_the_servers_own_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feature: Feature
) -> None:
    query = f"{_QUERY_MARKER} {words(_LOCAL_QUERY_WORDS)}"
    with FakeModelServer(reply_text=feature.reply, llama_n_ctx=_LLAMA_CPP_WINDOW) as server:
        toml = feature.toml(provider="llama_cpp", llama_cpp_base_url=server.base_url)
        [data] = _run(tmp_path, monkeypatch, feature, toml, query, wait_for_windows=True)
        [sent] = _queries_in_chats(server, feature, _OPENAI_CHAT_PATH)
        tokenized = server.requests_to("/tokenize")

    assert feature.applied(data)
    assert tokenized, "llama.cpp is counted through the server's own /tokenize"
    assert query.startswith(sent)
    assert len(sent) < len(query)
    template_words = len(feature.template.split())
    room = _LLAMA_CPP_WINDOW - ANSWER_RESERVE_TOKENS - FAKE_CHAT_FRAME_TOKENS - template_words  # the fake counts words
    sent_words = len(sent.split())
    assert room * ACCEPTED_FILL_PERCENT // 100 <= sent_words <= room


# --- cloud: whole (S9) --------------------------------------------------------------------------------------------


@FEATURES
def test_e2e_openai_51k_token_query_goes_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feature: Feature) -> None:
    """``openai`` is aimed at the fake with the SDK's own ``OPENAI_BASE_URL`` (the model-host guard lets it through)."""
    query = f"{_QUERY_MARKER} {words(_CLOUD_QUERY_WORDS)}"
    monkeypatch.setenv("OPENAI_API_KEY", _OPENAI_KEY)
    with FakeModelServer(reply_text=feature.reply) as server:
        monkeypatch.setenv("OPENAI_BASE_URL", f"{server.base_url}/v1")
        [data] = _run(tmp_path, monkeypatch, feature, feature.toml(provider="openai"), query)
        [sent] = _queries_in_chats(server, feature, _OPENAI_CHAT_PATH)
        counted = [request.path for request in server.requests if request.path in ("/tokenize", "/apply-template")]

    assert feature.applied(data)
    assert sent == query, "S9: a cloud provider receives the whole prompt, no setting and no trimming"
    assert counted == [], "nothing is counted for a cloud provider"


# --- no room ------------------------------------------------------------------------------------------------------


@FEATURES
def test_e2e_window_with_no_room_skips_the_feature_with_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, feature: Feature
) -> None:
    caplog.set_level(logging.DEBUG)
    query = f"{_QUERY_MARKER} migration procedure"
    with FakeModelServer(reply_text=feature.reply) as server:
        toml = feature.toml(provider="ollama", ollama_base_url=server.base_url, ollama_context_tokens=_NO_ROOM_WINDOW)
        responses = _run(tmp_path, monkeypatch, feature, toml, query, searches=_SEARCHES)
        chats = server.requests_to(_OLLAMA_CHAT_PATH)

    for data in responses:
        assert not feature.applied(data)
        assert [hit["source_path"] for hit in data["results"]] == [_DOC_PATH], "the search itself still answers"
        if feature.failure_warning is not None:
            assert data["expansion_warning"] == feature.failure_warning
    assert chats == [], "no model call is made for a window with no room"
    skipped = [r for r in caplog.records if r.levelno >= logging.WARNING and "skipped" in r.getMessage()]
    [warning] = skipped
    assert f"{_NO_ROOM_WINDOW} tokens" in warning.getMessage()
    for record in caplog.records:
        assert _QUERY_MARKER not in f"{record.getMessage()} {record.args} {record.exc_text}"


# --- the request-size limit bounds a query that is never cut ------------------------------------------------------


@FEATURES
def test_e2e_a_query_over_the_request_size_limit_is_refused_before_any_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feature: Feature
) -> None:
    """A cloud provider gets the whole query, so ``[server].max_request_body_mb`` is its only bound (S9): beyond it
    the request is a 413 and nothing is sent to the model."""
    monkeypatch.setenv("OPENAI_API_KEY", _OPENAI_KEY)
    query = "x" * (_REQUEST_LIMIT_MB * 1024 * 1024 + 1)
    with FakeModelServer(reply_text=feature.reply) as server:
        monkeypatch.setenv("OPENAI_BASE_URL", f"{server.base_url}/v1")
        toml = f"[server]\nmax_request_body_mb = {_REQUEST_LIMIT_MB}\n" + feature.toml(provider="openai")
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, key):
            ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
            _await_warmup(client)
            response = client.post(
                "/search",
                json={"query": query, "collection": _COLLECTION, feature.section: True},
                headers={"Authorization": f"Bearer {key}"},
            )
        chats = server.requests_to(_OPENAI_CHAT_PATH)

    assert response.status_code == _REQUEST_TOO_LARGE
    assert response.json()["code"] == "request_body_too_large"
    assert chats == []

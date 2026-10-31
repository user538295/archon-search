"""T-1 (collection-descriptions plan) — HyDE search through the real app on a fake Ollama server.

``make_real_app`` boots the real lifespan from a ``[hyde]`` TOML section (provider ``ollama``) with a real LanceDB store;
only the model host is faked (``tests/_fake_model_server.py``, a real HTTP server the real ``ollama`` SDK talks to).
What a consumer sees is asserted: the hypothetical document the embedder received instead of the query, ``hyde_applied``,
the ``num_ctx`` that reached the wire (and its absence when none is configured), and what the logs hold.

Run with:
    uv run pytest tests/integration/test_lcd_t1_hyde_ollama_e2e.py --no-cov -q
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.embedder import ModelEmbedder
from archon_search.server.schemas import WarmupResult
from tests._fake_model_server import (
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)
from tests.integration.conftest import ingest_doc, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "hyde-e2e"
_QUERY_MARKER = "zebra-quokka-7731"
_QUERY = f"{_QUERY_MARKER} migration procedure"
_HYPOTHESIS_MARKER = "narwhal-4410"
_HYPOTHESIS = f"Hypothetical passage describing the {_HYPOTHESIS_MARKER} rollout."
_DOC_TEXT = "Migration procedure documentation for the rollout."
_DOC_PATH = "/docs/migration.md"
_NUM_CTX = 6144
_HYDE_MODEL = "fake-model"
_HYDE_LOGGER = "archon_search.hyde"
_COMPOSITION_LOGGER = "archon_search.server.llm_composition"
_SEARCHES_WHEN_DOWN = 3
_CALL_TIMEOUT_SECONDS = 10  # generous: the first call also pays the lazy `ollama` import inside the timeout
_SHORT_CALL_TIMEOUT_SECONDS = 1
_SLOW_ANSWER_SECONDS = 3.0
_WARMUP_WAIT_SECONDS = 30.0
_ERROR_STATUS = 500
_CHAT_PATH = "/api/chat"


def _hyde_toml(
    base_url: str, *, num_ctx: int | None = _NUM_CTX, timeout_seconds: int = _CALL_TIMEOUT_SECONDS
) -> str:
    context = f"ollama_context_tokens = {num_ctx}\n" if num_ctx is not None else ""
    return (
        "[hyde]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        f'model = "{_HYDE_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        f"timeout_seconds = {timeout_seconds}\n"
        f"{context}"
    )


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's HTTP_PROXY must not capture the loopback requests."""
    keep_loopback_out_of_proxies(monkeypatch)


@pytest.fixture
def embedded_texts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every text the embedder is asked to embed, in order.

    Wraps ``ModelEmbedder.encode`` as the autouse ``_stub_model_backends`` fixture left it (it is set up first).
    """
    seen: list[str] = []
    stubbed: Callable[[ModelEmbedder, list[str]], list[list[float]]] = ModelEmbedder.encode

    def recording(self: ModelEmbedder, texts: list[str]) -> list[list[float]]:
        seen.extend(texts)
        return stubbed(self, texts)

    monkeypatch.setattr(ModelEmbedder, "encode", recording)
    return seen


def _await_warmup(client: TestClient) -> None:
    """Wait for the lifespan warm-up: a search that starts before it ends goes lexical-only and skips HyDE."""
    deadline = time.monotonic() + _WARMUP_WAIT_SECONDS
    while getattr(client.app.state, "warmup_result", None) in (None, WarmupResult.PENDING):
        assert time.monotonic() < deadline, "lifespan warm-up did not finish"
        time.sleep(0.01)
    assert client.app.state.warmup_result == WarmupResult.DONE


def _await_window_resolution(client: TestClient) -> None:
    """Wait for the lifespan's background context-window resolution (it logs the unset-window WARNING)."""
    task = client.app.state._llm_window_task
    deadline = time.monotonic() + _WARMUP_WAIT_SECONDS
    while not task.done():
        assert time.monotonic() < deadline, "context windows were not resolved"
        time.sleep(0.01)


def _search(client: TestClient, api_key: str) -> dict[str, Any]:
    response = client.post(
        "/search",
        json={"query": _QUERY, "collection": _COLLECTION, "hyde": True},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _assert_fell_back(responses: list[dict[str, Any]]) -> None:
    for data in responses:
        assert data["hyde_applied"] is False
        assert data["expansion_used"] is False
        assert data["expansion_warning"] == "HyDE expansion failed"
        assert [hit["source_path"] for hit in data["results"]] == [_DOC_PATH]


def _assert_one_hyde_warning_without_query(
    caplog: pytest.LogCaptureFixture, *, reason: str
) -> None:
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "HyDE" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].name == _HYDE_LOGGER
    assert reason in warnings[0].getMessage()
    for record in caplog.records:
        rendered = f"{record.getMessage()} {record.args} {record.exc_text}"
        assert _QUERY_MARKER not in rendered


def test_e2e_hyde_through_shared_client_sends_num_ctx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, embedded_texts: list[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    with FakeModelServer(reply_text=_HYPOTHESIS) as server:
        with make_real_app(tmp_path, monkeypatch, toml_content=_hyde_toml(server.base_url)) as (client, _cfg, key):
            ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
            _await_warmup(client)
            data = _search(client, key)
        chats = server.requests_to(_CHAT_PATH)

    assert data["hyde_applied"] is True
    assert data["expansion_used"] is True
    assert data["expansion_warning"] is None
    assert [hit["source_path"] for hit in data["results"]] == [_DOC_PATH]
    assert len(chats) == 1
    body = chats[0].body
    assert body is not None
    assert body["model"] == _HYDE_MODEL
    assert body["options"]["num_ctx"] == _NUM_CTX
    assert _QUERY in body["messages"][0]["content"]
    # The hypothetical document, not the query, is what was embedded as the ANN lookup text.
    assert _HYPOTHESIS in embedded_texts
    assert _QUERY not in embedded_texts
    # No log line holds the query or the hypothesis.
    for record in caplog.records:
        rendered = f"{record.getMessage()} {record.args}"
        assert _QUERY_MARKER not in rendered
        assert _HYPOTHESIS_MARKER not in rendered


def test_e2e_hyde_without_configured_context_warns_at_startup_and_sends_no_num_ctx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    with FakeModelServer(reply_text=_HYPOTHESIS) as server:
        toml = _hyde_toml(server.base_url, num_ctx=None)
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, key):
            ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
            _await_warmup(client)
            _await_window_resolution(client)
            data = _search(client, key)
        chats = server.requests_to(_CHAT_PATH)

    assert data["hyde_applied"] is True
    assert len(chats) == 1
    assert chats[0].body is not None
    assert "num_ctx" not in chats[0].body["options"]
    [startup_warning] = [
        r for r in caplog.records if r.name == _COMPOSITION_LOGGER and r.levelno >= logging.WARNING
    ]
    assert "[hyde].ollama_context_tokens" in startup_warning.getMessage()


def test_e2e_hyde_provider_down_falls_back_with_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, embedded_texts: list[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    with unreachable_port() as port:
        toml = _hyde_toml(f"http://127.0.0.1:{port}")
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, key):
            ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
            _await_warmup(client)
            responses = [_search(client, key) for _ in range(_SEARCHES_WHEN_DOWN)]

    _assert_fell_back(responses)
    _assert_one_hyde_warning_without_query(caplog, reason="cannot connect")
    assert _QUERY in embedded_texts  # the plain query is what got embedded


def test_e2e_hyde_provider_error_status_falls_back_with_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with FakeModelServer(status_overrides={_CHAT_PATH: _ERROR_STATUS}) as server:
        with make_real_app(tmp_path, monkeypatch, toml_content=_hyde_toml(server.base_url)) as (client, _cfg, key):
            ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
            _await_warmup(client)
            responses = [_search(client, key) for _ in range(_SEARCHES_WHEN_DOWN)]
        chats = server.requests_to(_CHAT_PATH)

    _assert_fell_back(responses)
    assert len(chats) == _SEARCHES_WHEN_DOWN
    _assert_one_hyde_warning_without_query(caplog, reason=f"HTTP {_ERROR_STATUS}")


def test_e2e_hyde_slow_provider_times_out_and_falls_back_with_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with FakeModelServer(reply_text=_HYPOTHESIS, response_delay_seconds=_SLOW_ANSWER_SECONDS) as server:
        toml = _hyde_toml(server.base_url, timeout_seconds=_SHORT_CALL_TIMEOUT_SECONDS)
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, key):
            ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
            _await_warmup(client)
            responses = [_search(client, key) for _ in range(2)]

    _assert_fell_back(responses)
    _assert_one_hyde_warning_without_query(caplog, reason="no answer within")
    for record in caplog.records:
        assert _HYPOTHESIS_MARKER not in record.getMessage()

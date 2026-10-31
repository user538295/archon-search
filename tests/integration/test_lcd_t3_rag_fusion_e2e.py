"""T-3 (collection-descriptions plan) — RAG Fusion fallback and the refuse-to-start rule through the real app.

``make_real_app`` boots the app from a ``[rag_fusion]`` TOML section (provider ``ollama``) with a real LanceDB
store; only the model host is faked (``tests/_fake_model_server.py``). A consumer sees: the HTTP ``/search`` answer
(``rag_fusion_applied``, ``rag_fusion_queries_used``, the hits), what the embedder was asked to embed, and what the logs
hold. Boot refusal goes through the TOML loader and ``create_app`` (``_check_provider_deps``) with the real error text.
Query embedding and reranking are stubbed by the suite's autouse fixture (zero vectors), so the hits cannot tell fused
from single-query search: the flags, the embedded texts, the provider requests and the logs carry the assertions.

Run with:
    uv run pytest tests/integration/test_lcd_t3_rag_fusion_e2e.py --no-cov -q
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.config import ConfigError
from archon_search.embedder import ModelEmbedder
from archon_search.server.schemas import WarmupResult
from archon_search.uv_tool import extra_install_hint
from tests._fake_model_server import (
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)
from tests._install_hint_leak import (
    assert_install_hint_logged_not_sent,
    patch_install_hint,
)
from tests.integration.conftest import ingest_doc, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "rag-e2e"
_QUERY_MARKER = "zebra-quokka-7731"
_QUERY = f"{_QUERY_MARKER} migration procedure"
_VARIANT_MARKER = "narwhal-4410"
_VARIANTS = (f"{_VARIANT_MARKER} rollout steps", f"{_VARIANT_MARKER} upgrade runbook")
_DOC_TEXT = "Migration procedure documentation for the rollout."
_DOC_PATH = "/docs/migration.md"
_RAG_MODEL = "fake-model"
_RAG_LOGGER = "archon_search.rag_fusion"
_SEARCHES = 3
_CALL_TIMEOUT_SECONDS = 10  # generous: the first call also pays the lazy `ollama` import
_WARMUP_WAIT_SECONDS = 30.0
_ERROR_STATUS = 500
_CHAT_PATH = "/api/chat"
_NUM_CTX = 6144
_SHORT_CALL_TIMEOUT_SECONDS = 1
_SLOW_ANSWER_SECONDS = 3.0
_TIMEOUT_SEARCHES = 2
#: one line of control characters (invalid), the marker on a line of its own so a logged raw answer would show it
_INVALID_VARIANT_ANSWER = f"\x01\x02\x03\n{_VARIANT_MARKER}\x07\x08"


def _rag_toml(base_url: str, *, timeout_seconds: int = _CALL_TIMEOUT_SECONDS) -> str:
    return (
        "[rag_fusion]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        f'model = "{_RAG_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        f"timeout_seconds = {timeout_seconds}\n"
        f"ollama_context_tokens = {_NUM_CTX}\n"
        f"num_queries = {len(_VARIANTS)}\n"
    )


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's HTTP_PROXY must not capture the loopback requests."""
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


def _search(client: TestClient, api_key: str) -> dict[str, Any]:
    response = client.post(
        "/search",
        json={"query": _QUERY, "collection": _COLLECTION, "rag_fusion": True},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _run_searches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_url: str) -> list[dict[str, Any]]:
    with make_real_app(tmp_path, monkeypatch, toml_content=_rag_toml(base_url)) as (client, _cfg, key):
        ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
        _await_warmup(client)
        return [_search(client, key) for _ in range(_SEARCHES)]


def _assert_single_query_answers(responses: list[dict[str, Any]]) -> None:
    for data in responses:
        assert data["rag_fusion_applied"] is False
        assert data["rag_fusion_attempted"] is True
        assert data["rag_fusion_queries_used"] == 0
        assert data["expansion_used"] is False
        assert [hit["source_path"] for hit in data["results"]] == [_DOC_PATH]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Every WARNING+ record about RAG Fusion, from the generator or from the pipeline's own fallback lines."""
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and "rag fusion" in r.getMessage().lower().replace("_", " ")
    ]


def _assert_one_rag_warning_without_query(
    caplog: pytest.LogCaptureFixture, *, reason: str, answer_reaches_client: bool = False
) -> None:
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0].name == _RAG_LOGGER
    assert reason in warnings[0].getMessage()
    for record in caplog.records:
        rendered = f"{record.getMessage()} {record.args} {record.exc_text}"
        assert _QUERY_MARKER not in rendered
        if answer_reaches_client:  # only an answer the client really read could leak its variants
            assert _VARIANT_MARKER not in rendered


def test_e2e_rag_fusion_provider_down_single_query_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, embedded_texts: list[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    with unreachable_port() as port:
        responses = _run_searches(tmp_path, monkeypatch, f"http://127.0.0.1:{port}")

    _assert_single_query_answers(responses)
    _assert_one_rag_warning_without_query(caplog, reason="cannot connect")
    assert _QUERY in embedded_texts  # the plain query is what got searched


def test_e2e_rag_fusion_error_status_single_query_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with FakeModelServer(status_overrides={_CHAT_PATH: _ERROR_STATUS}) as server:
        responses = _run_searches(tmp_path, monkeypatch, server.base_url)
        chats = server.requests_to(_CHAT_PATH)

    _assert_single_query_answers(responses)
    assert len(chats) == _SEARCHES  # every search still tried the provider
    _assert_one_rag_warning_without_query(caplog, reason=f"HTTP {_ERROR_STATUS}")


def test_e2e_rag_fusion_answer_without_valid_variant_single_query_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, embedded_texts: list[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    with FakeModelServer(reply_text=_INVALID_VARIANT_ANSWER) as server:
        responses = _run_searches(tmp_path, monkeypatch, server.base_url)

    _assert_single_query_answers(responses)
    _assert_one_rag_warning_without_query(caplog, reason="no valid variant", answer_reaches_client=True)
    assert not any(_VARIANT_MARKER in text or "\x01" in text for text in embedded_texts)


def test_e2e_rag_fusion_slow_provider_times_out_single_query_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with FakeModelServer(reply_text="\n".join(_VARIANTS), response_delay_seconds=_SLOW_ANSWER_SECONDS) as server:
        toml = _rag_toml(server.base_url, timeout_seconds=_SHORT_CALL_TIMEOUT_SECONDS)
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, key):
            ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
            _await_warmup(client)
            responses = [_search(client, key) for _ in range(_TIMEOUT_SEARCHES)]
        chats = server.requests_to(_CHAT_PATH)

    _assert_single_query_answers(responses)
    _assert_one_rag_warning_without_query(caplog, reason="no answer within")
    assert len(chats) == _TIMEOUT_SEARCHES  # every search still tried the provider


def test_e2e_rag_fusion_working_provider_fuses_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, embedded_texts: list[str]
) -> None:
    """Control for the fallback tests: the same boot with a working provider does fuse, and logs no warning."""
    caplog.set_level(logging.DEBUG)
    with FakeModelServer(reply_text="\n".join(_VARIANTS)) as server:
        responses = _run_searches(tmp_path, monkeypatch, server.base_url)
        chats = server.requests_to(_CHAT_PATH)

    for data in responses:
        assert data["rag_fusion_applied"] is True
        assert data["rag_fusion_attempted"] is True
        assert data["rag_fusion_queries_used"] == len(_VARIANTS)  # variants only, the original is not counted
        assert [hit["source_path"] for hit in data["results"]] == [_DOC_PATH]
    assert len(chats) == _SEARCHES
    for chat in chats:
        assert chat.body is not None
        assert chat.body["model"] == _RAG_MODEL
        assert chat.body["options"]["num_ctx"] == _NUM_CTX
        assert _QUERY in chat.body["messages"][0]["content"]
    assert _QUERY in embedded_texts  # the original query is searched next to the variants
    assert all(variant in embedded_texts for variant in _VARIANTS)
    assert not _warnings(caplog)
    for record in caplog.records:
        assert _QUERY_MARKER not in f"{record.getMessage()} {record.args} {record.exc_text}"


@pytest.mark.parametrize("section", ["hyde", "rag_fusion"])
def test_e2e_hyde_and_rag_fusion_missing_package_refuse_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: str
) -> None:
    """The ``ollama`` guard fires even when the feature is switched off (an explicit provider is an opt-in): the
    TOML loader and ``create_app`` refuse with the real error text. BE-13's integration tests cover the enabled case.
    The plan's "unlike graph" contrast is not asserted here: the graph provider-package guard arrives in BE-16."""
    monkeypatch.setitem(sys.modules, "ollama", None)
    toml = f'[{section}]\nenabled = false\nprovider = "ollama"\nmodel = "qwen2.5:3b"\n'

    with (
        pytest.raises(ConfigError) as refusal,
        make_real_app(tmp_path, monkeypatch, toml_content=toml),
    ):
        pytest.fail(f"the app started although the [{section}] provider package is missing")

    assert str(refusal.value) == (
        f"[{section}] provider='ollama' but the 'ollama' package is not installed; "
        f"run: {extra_install_hint('ollama')}"
    )


@pytest.mark.parametrize("section", ["hyde", "rag_fusion"])
def test_e2e_disabled_default_anthropic_provider_boots_without_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: str
) -> None:
    """The ``anthropic`` guard fires only for an enabled feature: ``anthropic`` is the default provider, so a plain
    install without the extra must still boot. The unit tests pin the guard; this adds the TOML loader and a booted app."""
    monkeypatch.setitem(sys.modules, "anthropic", None)
    toml = f"[{section}]\nenabled = false\n"

    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, _key):
        assert client.get("/health").status_code == 200


def test_e2e_rag_fusion_package_lost_after_boot_single_query_and_hint_off_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A package that disappears after the boot check is no client error: the search answers 200 on the single query,
    and the install command (it can carry credentials) reaches the server log only."""
    patch_install_hint(monkeypatch, "archon_search.uv_tool")
    caplog.set_level(logging.WARNING)
    with FakeModelServer(reply_text="\n".join(_VARIANTS)) as server:
        with make_real_app(tmp_path, monkeypatch, toml_content=_rag_toml(server.base_url)) as (client, _cfg, key):
            ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
            _await_warmup(client)
            monkeypatch.setitem(sys.modules, "ollama", None)
            data = _search(client, key)
            wire = json.dumps(data)
        assert server.requests_to(_CHAT_PATH) == []

    _assert_single_query_answers([data])
    assert_install_hint_logged_not_sent(caplog, wire)

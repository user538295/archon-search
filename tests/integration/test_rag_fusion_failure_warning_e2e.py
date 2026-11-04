"""RAG Fusion through the real app on a fake Ollama server: a failed generation tells the user, as HyDE does.

``make_real_app`` boots the real lifespan from a ``[rag_fusion]`` TOML section (provider ``ollama``) with a real LanceDB
store; only the model host is faked (``tests/_fake_model_server.py``). A closed port and a model the server does not know
(HTTP 404 on the chat endpoint, what Ollama answers for a wrong model name) used to leave ``expansion_warning`` null next to
``rag_fusion_attempted: true, rag_fusion_applied: false``. The tests assert what the REST consumer sees, on the single and the
multi collection path, and that the query never reaches a response warning or a log line.

Run with:
    uv run pytest tests/integration/test_rag_fusion_failure_warning_e2e.py --no-cov -q
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.server.schemas import WarmupResult
from tests._fake_model_server import (
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)
from tests.integration.conftest import ingest_doc, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "rag-fusion-warning-e2e"
_QUERY_MARKER = "zebra-quokka-7731"
_QUERY = f"{_QUERY_MARKER} migration procedure"
_DOC_TEXT = "Migration procedure documentation for the rollout."
_DOC_PATH = "/docs/migration.md"
_MODEL = "wrong-model-name"
_CALL_TIMEOUT_SECONDS = 10  # generous: the first call also pays the lazy `ollama` import inside the timeout
_SHORT_CALL_TIMEOUT_SECONDS = 1
_SLOW_ANSWER_SECONDS = 3.0
_WARMUP_WAIT_SECONDS = 30.0
_NOT_FOUND_STATUS = 404
_CHAT_PATH = "/api/chat"
_SEARCHES = 2
_VARIANTS = 2
_FAILED = "RAG Fusion expansion failed"
_TIMED_OUT = "RAG Fusion timed out"
_RAG_FUSION_LOGGER = "archon_search.rag_fusion"
_PATHS = pytest.mark.parametrize("multi", [False, True], ids=["single", "multi"])


def _rag_fusion_toml(base_url: str, *, timeout_seconds: int = _CALL_TIMEOUT_SECONDS) -> str:
    return (
        "[rag_fusion]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        f'model = "{_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        f"timeout_seconds = {timeout_seconds}\n"
    )


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's HTTP_PROXY must not capture the loopback requests."""
    keep_loopback_out_of_proxies(monkeypatch)


def _await_warmup(client: TestClient) -> None:
    """Wait for the lifespan warm-up: a search that starts before it ends goes lexical-only and skips RAG Fusion."""
    deadline = time.monotonic() + _WARMUP_WAIT_SECONDS
    while getattr(client.app.state, "warmup_result", None) in (None, WarmupResult.PENDING):
        assert time.monotonic() < deadline, "lifespan warm-up did not finish"
        time.sleep(0.01)
    assert client.app.state.warmup_result == WarmupResult.DONE


def _search(client: TestClient, api_key: str, *, multi: bool) -> dict[str, Any]:
    selection: dict[str, Any] = {"collections": [_COLLECTION]} if multi else {"collection": _COLLECTION}
    response = client.post(
        "/search",
        json={"query": _QUERY, "rag_fusion": True, **selection},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _assert_fell_back(responses: list[dict[str, Any]], warning: str) -> None:
    for data in responses:
        assert data["rag_fusion_attempted"] is True
        assert data["rag_fusion_applied"] is False
        assert data["rag_fusion_queries_used"] == 0
        assert data["expansion_used"] is False
        assert data["expansion_warning"] == warning
        assert [hit["source_path"] for hit in data["results"]] == [_DOC_PATH]


def _assert_one_warning_without_query(caplog: pytest.LogCaptureFixture, *, reason: str) -> None:
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == _RAG_FUSION_LOGGER]
    assert len(warnings) == 1, [w.getMessage() for w in warnings]
    assert reason in warnings[0].getMessage()
    for record in caplog.records:
        assert _QUERY_MARKER not in f"{record.getMessage()} {record.args} {record.exc_text}"


@_PATHS
def test_e2e_rag_fusion_provider_down_warns_on_every_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, multi: bool
) -> None:
    caplog.set_level(logging.DEBUG)
    with (
        unreachable_port() as port,
        make_real_app(tmp_path, monkeypatch, toml_content=_rag_fusion_toml(f"http://127.0.0.1:{port}")) as (
            client,
            _cfg,
            key,
        ),
    ):
        ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
        _await_warmup(client)
        responses = [_search(client, key, multi=multi) for _ in range(_SEARCHES)]

    _assert_fell_back(responses, _FAILED)
    _assert_one_warning_without_query(caplog, reason="cannot connect")


@_PATHS
def test_e2e_rag_fusion_wrong_model_name_warns_on_every_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, multi: bool
) -> None:
    caplog.set_level(logging.DEBUG)
    with (
        FakeModelServer(status_overrides={_CHAT_PATH: _NOT_FOUND_STATUS}) as server,
        make_real_app(tmp_path, monkeypatch, toml_content=_rag_fusion_toml(server.base_url)) as (client, _cfg, key),
    ):
        ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
        _await_warmup(client)
        responses = [_search(client, key, multi=multi) for _ in range(_SEARCHES)]
    chats = server.requests_to(_CHAT_PATH)

    _assert_fell_back(responses, _FAILED)
    assert len(chats) == _SEARCHES
    _assert_one_warning_without_query(caplog, reason=f"HTTP {_NOT_FOUND_STATUS}")


@_PATHS
def test_e2e_rag_fusion_slow_provider_times_out_with_its_own_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, multi: bool
) -> None:
    caplog.set_level(logging.DEBUG)
    with FakeModelServer(response_delay_seconds=_SLOW_ANSWER_SECONDS) as server:
        toml = _rag_fusion_toml(server.base_url, timeout_seconds=_SHORT_CALL_TIMEOUT_SECONDS)
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, key):
            ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
            _await_warmup(client)
            responses = [_search(client, key, multi=multi)]

    _assert_fell_back(responses, _TIMED_OUT)
    _assert_one_warning_without_query(caplog, reason="no answer within")


@_PATHS
def test_e2e_rag_fusion_answer_without_a_variant_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi: bool
) -> None:
    with (
        FakeModelServer(reply_text="   ") as server,
        make_real_app(tmp_path, monkeypatch, toml_content=_rag_fusion_toml(server.base_url)) as (client, _cfg, key),
    ):
        ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
        _await_warmup(client)
        responses = [_search(client, key, multi=multi)]

    _assert_fell_back(responses, _FAILED)


@_PATHS
def test_e2e_rag_fusion_success_has_no_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi: bool) -> None:
    with (
        FakeModelServer(reply_text="variant one\nvariant two") as server,
        make_real_app(tmp_path, monkeypatch, toml_content=_rag_fusion_toml(server.base_url)) as (client, _cfg, key),
    ):
        ingest_doc(client, _COLLECTION, _DOC_TEXT, _DOC_PATH, api_key=key)
        _await_warmup(client)
        data = _search(client, key, multi=multi)

    assert data["rag_fusion_attempted"] is True
    assert data["rag_fusion_applied"] is True
    assert data["rag_fusion_queries_used"] == _VARIANTS
    assert data["expansion_used"] is True
    assert data["expansion_warning"] is None

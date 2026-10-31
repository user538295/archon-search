"""BE-10 (collection-descriptions plan, C1, S5, S67, S68): the server composes each feature's client from its own section.

A real app (``make_real_app``) boots with ``[hyde]`` (and, from BE-12, ``[rag_fusion]``) on a loopback fake model server; the lifespan resolves the window
of every enabled feature's client in the background and logs the startup WARNINGs, and the server answers either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.constants import ANSWER_RESERVE_TOKENS, ASSUMED_CONTEXT_TOKENS
from archon_search.llm_client import claude_cli_client, llama_cpp_client
from archon_search.llm_client.protocol import (
    ContextWindowSource,
    ContextWindowTokens,
    LlmCallRequest,
    LlmClient,
)
from archon_search.server import app as app_module
from archon_search.server import llm_composition
from archon_search.server.llm_composition import build_llm_client
from tests import _fake_claude
from tests._fake_model_server import (
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_LOOPBACK = "127.0.0.1"
_HYDE_MODEL = "hyde-model"
_GRAPH_MODEL = "graph-model"
_PROMPT = "write a short passage"
_TIMEOUT_SECONDS = 10.0
_CONFIGURED_WINDOW = 8192
_MODEL_MAXIMUM = 32768
_ABOVE_THE_MAXIMUM = 65536
_WINDOW_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.01
_COMPOSITION_LOGGER = llm_composition.__name__
_LLAMA_CPP_LOGGER = llama_cpp_client.__name__
_PROBED_WINDOW = 16384
_UNUSED_URL = "http://ollama.test:11434"
_HYDE_REPLY = "hyde reply"
_GRAPH_REPLY = "graph reply"

Boot = Callable[[str], tuple[TestClient, SearchConfig]]


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's HTTP_PROXY must not capture the loopback requests."""
    keep_loopback_out_of_proxies(monkeypatch)


def _hyde_toml(*, base_url: str, enabled: bool = True, context: int | None = None, provider: str = "ollama") -> str:
    url_key = "llama_cpp_base_url" if provider == "llama_cpp" else "ollama_base_url"
    lines = ["[hyde]", f"enabled = {str(enabled).lower()}", f'provider = "{provider}"', f'model = "{_HYDE_MODEL}"', f'{url_key} = "{base_url}"']
    if context is not None:
        lines.append(f"ollama_context_tokens = {context}")
    return "\n".join(lines) + "\n"


def _await_window_resolution(client: TestClient) -> None:
    """Block until the lifespan's background window resolution has finished (it runs on the portal thread's loop)."""
    task = client.app.state._llm_window_task
    deadline = time.monotonic() + _WINDOW_WAIT_SECONDS
    while not task.done():
        if time.monotonic() > deadline:
            raise TimeoutError(f"context windows were not resolved within {_WINDOW_WAIT_SECONDS}s")
        time.sleep(_POLL_SECONDS)


@pytest.fixture
def boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Boot]:
    """Boot a real app on a TOML, wait for the window resolution, and close the app at the end of the test."""
    with contextlib.ExitStack() as stack:

        def _boot(toml: str) -> tuple[TestClient, SearchConfig]:
            client, config, _api_key = stack.enter_context(make_real_app(tmp_path, monkeypatch, toml_content=toml))
            _await_window_resolution(client)
            return client, config

        yield _boot


def _composition_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.WARNING and record.name == _COMPOSITION_LOGGER]


def _call(client: LlmClient) -> str:
    request = LlmCallRequest(prompt=_PROMPT, max_output_tokens=ANSWER_RESERVE_TOKENS, timeout_seconds=_TIMEOUT_SECONDS)
    return asyncio.run(client.call(request)).text


def test_unset_ollama_window_warns_at_startup_and_assumes_4096(boot: Boot, caplog: pytest.LogCaptureFixture) -> None:
    with FakeModelServer(reply_text=_HYDE_REPLY) as server, caplog.at_level(logging.WARNING):
        client, _config = boot(_hyde_toml(base_url=server.base_url))
        hyde_client = client.app.state.hyde_client

        reply = _call(hyde_client)

    [warning] = _composition_warnings(caplog)
    message = warning.getMessage()
    assert "[hyde].ollama_context_tokens" in message
    assert str(ASSUMED_CONTEXT_TOKENS) in message
    assert "num_ctx" in message
    assert hyde_client.context_window() == ContextWindowTokens(size=ASSUMED_CONTEXT_TOKENS, source=ContextWindowSource.ASSUMED)
    assert reply == _HYDE_REPLY
    [chat] = server.requests_to("/api/chat")
    assert chat.body is not None
    assert "num_ctx" not in chat.body["options"]
    assert server.requests_to("/api/show") == []


def test_set_ollama_window_is_sent_and_warns_nothing(boot: Boot, caplog: pytest.LogCaptureFixture) -> None:
    with FakeModelServer(ollama_context_length=_MODEL_MAXIMUM) as server, caplog.at_level(logging.WARNING):
        client, _config = boot(_hyde_toml(base_url=server.base_url, context=_CONFIGURED_WINDOW))

        _call(client.app.state.hyde_client)

    assert _composition_warnings(caplog) == []
    [chat] = server.requests_to("/api/chat")
    assert chat.body is not None
    assert chat.body["options"]["num_ctx"] == _CONFIGURED_WINDOW


def test_oversized_ollama_window_warns_with_model_maximum(boot: Boot, caplog: pytest.LogCaptureFixture) -> None:
    with FakeModelServer(ollama_context_length=_MODEL_MAXIMUM) as server, caplog.at_level(logging.WARNING):
        client, _config = boot(_hyde_toml(base_url=server.base_url, context=_ABOVE_THE_MAXIMUM))
        health = client.get("/health")

    [warning] = _composition_warnings(caplog)
    message = warning.getMessage()
    assert "[hyde].ollama_context_tokens" in message
    assert str(_ABOVE_THE_MAXIMUM) in message
    assert str(_MODEL_MAXIMUM) in message
    assert len(server.requests_to("/api/show")) == 1
    assert health.status_code == 200


def test_disabled_hyde_resolves_nothing_and_warns_nothing(boot: Boot, caplog: pytest.LogCaptureFixture) -> None:
    with FakeModelServer() as server, caplog.at_level(logging.WARNING):
        boot(_hyde_toml(base_url=server.base_url, enabled=False))

    assert _composition_warnings(caplog) == []
    assert server.requests == []


def test_failed_llama_cpp_probe_warns_at_startup_and_the_server_boots(boot: Boot, caplog: pytest.LogCaptureFixture) -> None:
    with unreachable_port() as port, caplog.at_level(logging.WARNING):
        client, _config = boot(_hyde_toml(base_url=f"http://{_LOOPBACK}:{port}", provider="llama_cpp"))
        health = client.get("/health")

    [warning] = [record for record in caplog.records if record.levelno == logging.WARNING and record.name == _LLAMA_CPP_LOGGER]
    assert str(ASSUMED_CONTEXT_TOKENS) in warning.getMessage()
    assert client.app.state.hyde_client.context_window() == ContextWindowTokens(
        size=ASSUMED_CONTEXT_TOKENS, source=ContextWindowSource.ASSUMED
    )
    assert health.status_code == 200


def test_llama_cpp_window_is_probed_from_its_own_host_at_startup(boot: Boot, caplog: pytest.LogCaptureFixture) -> None:
    with FakeModelServer(llama_n_ctx=_PROBED_WINDOW) as server, caplog.at_level(logging.WARNING):
        client, _config = boot(_hyde_toml(base_url=server.base_url, provider="llama_cpp"))

    assert client.app.state.hyde_client.context_window() == ContextWindowTokens(size=_PROBED_WINDOW, source=ContextWindowSource.PROBED)
    assert len(server.requests_to("/props")) == 1
    assert _composition_warnings(caplog) == []


def test_shutdown_cancels_a_window_resolution_still_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def never_finishes(features: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(app_module, "resolve_context_windows", never_finishes)
    with make_real_app(tmp_path, monkeypatch, toml_content=_hyde_toml(base_url=_UNUSED_URL)) as (client, _config, _api_key):
        task = client.app.state._llm_window_task
        assert not task.done()

    assert task.cancelled()


def test_hyde_claude_cli_client_retries_a_non_zero_exit_once(boot: Boot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_cli_client, "RETRY_BACKOFF_SECONDS", 0.0)
    fake = _fake_claude.install(tmp_path / "bin", reply=_HYDE_REPLY, exit_codes=(1, 0))
    monkeypatch.setenv("PATH", f"{fake.path.parent}{os.pathsep}{os.environ['PATH']}")
    client, _config = boot('[hyde]\nenabled = true\nprovider = "claude_cli"\n')

    reply = _call(client.app.state.hyde_client)

    assert reply == _HYDE_REPLY
    assert len(fake.invocations()) == 2


def test_hyde_and_graph_keep_their_own_provider_model_and_host(boot: Boot) -> None:
    """HyDE on Ollama (the app's own client) and graph on llama_cpp (built from [graph] by the same factory, as the app will once
    graph composes its client): each call reaches its own server with its own model, and the other server sees nothing of it."""
    with FakeModelServer(reply_text=_HYDE_REPLY) as ollama_host, FakeModelServer(reply_text=_GRAPH_REPLY) as llama_host:
        toml = _hyde_toml(base_url=ollama_host.base_url, context=_CONFIGURED_WINDOW) + (
            f'[graph]\nprovider = "llama_cpp"\nextraction_model = "{_GRAPH_MODEL}"\nllama_cpp_base_url = "{llama_host.base_url}"\n'
        )
        client, config = boot(toml)
        graph_client = build_llm_client(
            provider=config.graph.provider or "",
            model=config.graph.extraction_model or "",
            ollama_base_url=config.graph.ollama_base_url,
            llama_cpp_base_url=config.graph.llama_cpp_base_url,
            ollama_context_tokens=config.graph.ollama_context_tokens,
        )

        hyde_reply = _call(client.app.state.hyde_client)
        graph_reply = _call(graph_client)

    assert (hyde_reply, graph_reply) == (_HYDE_REPLY, _GRAPH_REPLY)
    # Besides its chat call, the Ollama host got the window check's `/api/show`, and the llama host got model validation's
    # `/v1/models` reachability probe (`[graph]` is llama_cpp); neither is what this test is about.
    assert [request.path for request in ollama_host.requests if request.path != "/api/show"] == ["/api/chat"]
    assert [request.path for request in llama_host.requests if request.path != "/v1/models"] == ["/v1/chat/completions"]
    [hyde_request] = ollama_host.requests_to("/api/chat")
    [graph_request] = llama_host.requests_to("/v1/chat/completions")
    assert hyde_request.body is not None
    assert hyde_request.body["model"] == _HYDE_MODEL
    assert graph_request.body is not None
    assert graph_request.body["model"] == _GRAPH_MODEL


# ---------------------------------------------------------------------------
# BE-12: RAG Fusion composes its own client from [rag_fusion] the same way
# ---------------------------------------------------------------------------

_RAG_FUSION_MODEL = "rag-fusion-model"
_RAG_FUSION_REPLY = "rag fusion reply"


def _rag_fusion_toml(*, base_url: str, enabled: bool = True, context: int | None = None) -> str:
    lines = [
        "[rag_fusion]",
        f"enabled = {str(enabled).lower()}",
        'provider = "ollama"',
        f'model = "{_RAG_FUSION_MODEL}"',
        f'ollama_base_url = "{base_url}"',
    ]
    if context is not None:
        lines.append(f"ollama_context_tokens = {context}")
    return "\n".join(lines) + "\n"


def test_unset_rag_fusion_ollama_window_warns_at_startup_naming_its_own_section(
    boot: Boot, caplog: pytest.LogCaptureFixture
) -> None:
    with FakeModelServer(reply_text=_RAG_FUSION_REPLY) as server, caplog.at_level(logging.WARNING):
        client, _config = boot(_rag_fusion_toml(base_url=server.base_url))
        rag_fusion_client = client.app.state.rag_fusion_client

        reply = _call(rag_fusion_client)

    [warning] = _composition_warnings(caplog)
    assert "[rag_fusion].ollama_context_tokens" in warning.getMessage()
    assert rag_fusion_client.context_window() == ContextWindowTokens(
        size=ASSUMED_CONTEXT_TOKENS, source=ContextWindowSource.ASSUMED
    )
    assert reply == _RAG_FUSION_REPLY
    [chat] = server.requests_to("/api/chat")
    assert chat.body is not None
    assert chat.body["model"] == _RAG_FUSION_MODEL
    assert "num_ctx" not in chat.body["options"]
    assert server.requests_to("/api/show") == []


def test_oversized_rag_fusion_ollama_window_warns_with_model_maximum(
    boot: Boot, caplog: pytest.LogCaptureFixture
) -> None:
    with FakeModelServer(ollama_context_length=_MODEL_MAXIMUM) as server, caplog.at_level(logging.WARNING):
        boot(_rag_fusion_toml(base_url=server.base_url, context=_ABOVE_THE_MAXIMUM))

    [warning] = _composition_warnings(caplog)
    message = warning.getMessage()
    assert "[rag_fusion].ollama_context_tokens" in message
    assert str(_ABOVE_THE_MAXIMUM) in message
    assert str(_MODEL_MAXIMUM) in message
    assert len(server.requests_to("/api/show")) == 1


def test_set_rag_fusion_ollama_window_is_sent_and_warns_nothing(boot: Boot, caplog: pytest.LogCaptureFixture) -> None:
    with FakeModelServer(ollama_context_length=_MODEL_MAXIMUM) as server, caplog.at_level(logging.WARNING):
        client, _config = boot(_rag_fusion_toml(base_url=server.base_url, context=_CONFIGURED_WINDOW))
        _call(client.app.state.rag_fusion_client)

    assert _composition_warnings(caplog) == []
    [chat] = server.requests_to("/api/chat")
    assert chat.body is not None
    assert chat.body["options"]["num_ctx"] == _CONFIGURED_WINDOW


def test_disabled_rag_fusion_resolves_nothing_and_warns_nothing(boot: Boot, caplog: pytest.LogCaptureFixture) -> None:
    with FakeModelServer() as server, caplog.at_level(logging.WARNING):
        boot(_rag_fusion_toml(base_url=server.base_url, enabled=False))

    assert _composition_warnings(caplog) == []
    assert server.requests == []


def test_rag_fusion_and_hyde_each_warn_once_for_their_own_unset_window(
    boot: Boot, caplog: pytest.LogCaptureFixture
) -> None:
    with FakeModelServer() as server, caplog.at_level(logging.WARNING):
        boot(_hyde_toml(base_url=server.base_url) + _rag_fusion_toml(base_url=server.base_url))

    messages = [warning.getMessage() for warning in _composition_warnings(caplog)]
    assert len(messages) == 2
    assert any("[hyde].ollama_context_tokens" in message for message in messages)
    assert any("[rag_fusion].ollama_context_tokens" in message for message in messages)


def test_rag_fusion_claude_cli_client_retries_a_non_zero_exit_once(
    boot: Boot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_cli_client, "RETRY_BACKOFF_SECONDS", 0.0)
    fake = _fake_claude.install(tmp_path / "bin", reply=_RAG_FUSION_REPLY, exit_codes=(1, 0))
    monkeypatch.setenv("PATH", f"{fake.path.parent}{os.pathsep}{os.environ['PATH']}")
    client, _config = boot('[rag_fusion]\nenabled = true\nprovider = "claude_cli"\n')

    reply = _call(client.app.state.rag_fusion_client)

    assert reply == _RAG_FUSION_REPLY
    assert len(fake.invocations()) == 2

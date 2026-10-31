"""BE-15 (collection-descriptions plan, C1, S4, S64, S67): graph community summaries run on the shared client.

* The app composes ``app.state.graph_client`` from ``[graph]`` through ``build_llm_client`` with ``rate_limit_rpm`` set to
  ``extraction_rate_limit_rpm``, and resolves its window at startup like HyDE and RAG Fusion.
* On ``claude_cli`` a community summary reaches the (fake) ``claude`` program on standard input, and the rpm cap throttles it.
* The startup probe classifies a timeout through the shared ``LlmCallTimeout``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.community_builder import CommunityBuilder
from archon_search.config import GraphConfig, SearchConfig
from archon_search.llm_client.protocol import LlmClient
from archon_search.model_validation import (
    EXTRACTION_MODEL_PROBE_ERROR_WARNING,
    EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING,
    _probe_extraction_model,
)
from archon_search.server import app as app_module
from archon_search.server.llm_composition import build_llm_client
from tests import _fake_claude
from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies
from tests._graph_engine_stub import install_graph_engine_stub
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_GRAPH_MODEL = "graph-model"
_RATE_LIMIT_RPM = 2
_UNUSED_URL = "http://ollama.test:11434"
_REJECTED_STATUS = 400
_WINDOW_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.01
_SLOW_ANSWER_SECONDS = 3.0
_PROBE_TIMEOUT_SECONDS = 0.3
_PATIENT_TIMEOUT_SECONDS = 10.0
_VALIDATION_WAIT_SECONDS = 60.0
_SUMMARY = "the graph summary"
_CHUNK_TEXT = "Alpha depends on Beta for storage."


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _graph_toml(provider: str | None, **extra: object) -> str:
    lines = ["[graph]"]
    if provider is not None:
        lines += [f'provider = "{provider}"', f'extraction_model = "{_GRAPH_MODEL}"']
    for key, value in extra.items():
        lines.append(f'{key} = "{value}"' if isinstance(value, str) else f"{key} = {str(value).lower()}")
    return "\n".join(lines) + "\n"


def _await_window_resolution(client: Any) -> None:
    task = client.app.state._llm_window_task
    deadline = time.monotonic() + _WINDOW_WAIT_SECONDS
    while not task.done():
        if time.monotonic() > deadline:
            raise TimeoutError("context windows were not resolved in time")
        time.sleep(_POLL_SECONDS)


class _BuildSpy:
    """Wraps the app's ``build_llm_client`` and records the keyword arguments of every call, by provider and model."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> LlmClient:
        self.calls.append(kwargs)
        return build_llm_client(**kwargs)

    def graph_call(self) -> dict[str, Any]:
        (call,) = [c for c in self.calls if c["model"] == _GRAPH_MODEL]
        return call


@pytest.mark.parametrize("provider", ["anthropic", "openai", "ollama", "claude_cli", "llama_cpp"])
def test_app_builds_the_graph_client_from_graph_with_the_extraction_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """All five providers get a graph client from ``[graph]`` (its own host, model and Ollama window), built with
    ``rate_limit_rpm=extraction_rate_limit_rpm`` (the client layer applies it to anthropic and claude_cli only)."""
    spy = _BuildSpy()
    monkeypatch.setattr(app_module, "build_llm_client", spy)
    toml = _graph_toml(
        provider,
        extraction_rate_limit_rpm=_RATE_LIMIT_RPM,
        ollama_base_url=_UNUSED_URL,
        llama_cpp_base_url="http://llama.test:8080",
        ollama_context_tokens=8192,
    )
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _config, _api_key):
        graph_client = client.app.state.graph_client

    call = spy.graph_call()
    assert call["provider"] == provider
    assert call["rate_limit_rpm"] == _RATE_LIMIT_RPM
    assert call["ollama_base_url"] == _UNUSED_URL
    assert call["llama_cpp_base_url"] == "http://llama.test:8080"
    assert call["ollama_context_tokens"] == 8192
    assert not call.get("retry_on_nonzero_exit", False), "graph prompts hold excerpts a retry would send twice"
    assert graph_client is not None


def test_app_has_no_graph_client_without_a_graph_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _BuildSpy()
    monkeypatch.setattr(app_module, "build_llm_client", spy)
    toml = _graph_toml(None, extraction_model=_GRAPH_MODEL)  # a model without a provider builds no client
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _config, _api_key):
        assert client.app.state.graph_client is None
    assert all(c["model"] != _GRAPH_MODEL for c in spy.calls)


def test_graph_window_is_resolved_at_startup_when_graph_is_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S8/S67: an enabled graph on llama_cpp reads its window from the server's properties like HyDE and RAG Fusion."""
    install_graph_engine_stub(monkeypatch)
    with FakeModelServer(llama_n_ctx=16384) as server:
        toml = _graph_toml("llama_cpp", enabled=True, llama_cpp_base_url=server.base_url)
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _config, _api_key):
            _await_window_resolution(client)
            window = client.app.state.graph_client.context_window()

    assert server.requests_to("/props")
    assert window.size == 16384  # type: ignore[union-attr]


def test_startup_probe_runs_on_its_own_unthrottled_graph_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifespan hands the model validation a factory that builds a throwaway graph client without the rate limit:
    the probe reaches the model once at startup, and the production client (which keeps the limit) is a different
    instance, so the probe never takes one of its slots."""
    install_graph_engine_stub(monkeypatch)
    spy = _BuildSpy()
    monkeypatch.setattr(app_module, "build_llm_client", spy)
    with FakeModelServer() as server:
        toml = _graph_toml(
            "ollama", enabled=True, ollama_base_url=server.base_url, extraction_rate_limit_rpm=_RATE_LIMIT_RPM
        )
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _config, _api_key):
            deadline = time.monotonic() + _VALIDATION_WAIT_SECONDS
            while client.app.state.model_validation is None:
                assert time.monotonic() < deadline, "the model validation did not finish in time"
                time.sleep(_POLL_SECONDS)
            production_client = client.app.state.graph_client

    graph_builds = [c for c in spy.calls if c["model"] == _GRAPH_MODEL]
    assert [c["rate_limit_rpm"] for c in graph_builds] == [_RATE_LIMIT_RPM, None]
    assert len(server.requests_to("/api/chat")) == 1, "the probe's one call"
    assert production_client is not None


def _install_fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _fake_claude.FakeClaude:
    fake = _fake_claude.install(tmp_path / "bin", reply=_SUMMARY)
    monkeypatch.setenv("PATH", f"{fake.path.parent}{os.pathsep}{os.environ['PATH']}")
    return fake


def _three_community_builder(client: LlmClient, config: GraphConfig) -> CommunityBuilder:
    from archon_search.graph_types import EntityType, GraphNode

    nodes = [
        GraphNode(id=name, entity_name=name, entity_type=EntityType.concept, source_doc_id=f"doc-{name}", collection_name="col")
        for name in ("a", "b", "c")
    ]
    graph_store = MagicMock()
    graph_store.get_all_nodes = AsyncMock(return_value=nodes)
    graph_store.get_all_edges = AsyncMock(return_value=[])
    graph_store.write_communities = AsyncMock(return_value=None)
    search_store = MagicMock()

    async def _chunks(_collection: str, doc_id: str) -> list[dict]:
        return [{"chunk_id": f"chunk-{doc_id}", "doc_id": doc_id, "text": _CHUNK_TEXT, "vector": [1.0, 0.0]}]

    search_store.get_chunks_for_doc = _chunks
    return CommunityBuilder(graph_store, config, search_store=search_store, graph_client=client)


def test_graph_on_claude_cli_sends_stdin_prompt_and_throttles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S64: with graph on `claude_cli` the summary prompt reaches the `claude` program on standard input (never in its
    argument list), and `extraction_rate_limit_rpm` throttles it: with a cap of two, the third community of a build keeps
    no summary, costs no third spawn, and the build still completes with one WARNING."""
    fake = _install_fake_claude(tmp_path, monkeypatch)
    toml = _graph_toml("claude_cli", extraction_rate_limit_rpm=_RATE_LIMIT_RPM)
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, config, _api_key):
        builder = _three_community_builder(client.app.state.graph_client, config.graph)
        with (
            patch("archon_search.community_builder._run_leiden_partition_sync", return_value=[["a"], ["b"], ["c"]]),
            caplog.at_level(logging.WARNING, logger="archon_search.community_builder"),
        ):
            communities = asyncio.run(builder.build("col", "default"))

    assert [c.summary_text for c in communities] == [_SUMMARY, _SUMMARY, None]
    invocations = fake.invocations()
    assert len(invocations) == _RATE_LIMIT_RPM
    for invocation in invocations:
        assert "knowledge-graph summariser" in invocation.stdin
        assert _CHUNK_TEXT in invocation.stdin
        assert not any(_CHUNK_TEXT in argument or "summariser" in argument for argument in invocation.argv)
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name == "archon_search.community_builder"
    ]
    assert len(warnings) == 1 and "rate limit exhausted" in warnings[0]
    assert _CHUNK_TEXT not in caplog.text


def _llama_probe_config(server: FakeModelServer, timeout_seconds: float) -> SearchConfig:
    return SearchConfig(
        graph=GraphConfig(
            enabled=True,
            provider="llama_cpp",
            extraction_model=_GRAPH_MODEL,
            llama_cpp_base_url=server.base_url,
            extraction_timeout_seconds=timeout_seconds,
        )
    )


def _real_client_factory(config: SearchConfig) -> Callable[[], LlmClient]:
    graph = config.graph
    return lambda: build_llm_client(
        provider=graph.provider or "",
        model=graph.extraction_model or "",
        ollama_base_url=graph.ollama_base_url,
        llama_cpp_base_url=graph.llama_cpp_base_url,
        ollama_context_tokens=graph.ollama_context_tokens,
    )


def test_extraction_probe_classifies_timeout_through_shared_failure_type() -> None:
    """A server that answers slower than the probe's timeout still reads as a possible reasoning model: the real wrapper
    raises the shared ``LlmCallTimeout`` and the probe maps that to the timeout warning."""
    with FakeModelServer(response_delay_seconds=_SLOW_ANSWER_SECONDS) as server:
        config = _llama_probe_config(server, _PROBE_TIMEOUT_SECONDS)
        warnings = asyncio.run(_probe_extraction_model(config, graph_client_factory=_real_client_factory(config)))

    assert warnings == [EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING]


def test_extraction_probe_passes_on_a_prompt_answered_in_time_and_reads_a_rejecting_server_as_misconfiguration() -> None:
    with FakeModelServer(reply_text="Alpha and Beta are related.") as server:
        config = _llama_probe_config(server, _PATIENT_TIMEOUT_SECONDS)
        assert asyncio.run(_probe_extraction_model(config, graph_client_factory=_real_client_factory(config))) == []
        (request,) = server.requests_to("/v1/chat/completions")
        assert request.body is not None and request.body["model"] == _GRAPH_MODEL

    with FakeModelServer(status_overrides={"/v1/chat/completions": _REJECTED_STATUS}) as rejecting:
        config = _llama_probe_config(rejecting, _PATIENT_TIMEOUT_SECONDS)
        warnings = asyncio.run(_probe_extraction_model(config, graph_client_factory=_real_client_factory(config)))

    assert warnings == [EXTRACTION_MODEL_PROBE_ERROR_WARNING]

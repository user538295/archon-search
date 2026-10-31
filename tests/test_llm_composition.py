"""BE-10 (collection-descriptions plan, C1, S5, S67, S68): per-feature composition of the shared LLM client.

``build_llm_client`` turns one feature's settings into its own client; ``resolve_context_windows`` then resolves every
client's window per client (Ollama ``show`` for the model maximum, ``llama_cpp`` ``/props``) and logs the startup WARNINGs.
The servers are the loopback fakes of ``tests/_fake_model_server.py``, reached through the real SDK and ``httpx``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import openai
import pytest

from archon_search.constants import ANSWER_RESERVE_TOKENS, ASSUMED_CONTEXT_TOKENS
from archon_search.llm_client import claude_cli_client, llama_cpp_client
from archon_search.llm_client.anthropic_client import AnthropicClient
from archon_search.llm_client.claude_cli_client import ClaudeCliClient
from archon_search.llm_client.llama_cpp_client import LlamaCppClient
from archon_search.llm_client.ollama_client import OllamaClient
from archon_search.llm_client.openai_client import OpenAIClient
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindowSource,
    ContextWindowTokens,
    LlmCallFailure,
    LlmCallRequest,
    LlmClient,
)
from archon_search.server import llm_composition
from archon_search.server.llm_composition import (
    FeatureClient,
    build_llm_client,
    resolve_context_windows,
)
from tests import _fake_claude
from tests._fake_model_server import (
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)

_LOOPBACK = "127.0.0.1"
_MODEL = "fake-model"
_OLLAMA_URL = "http://ollama.test:11434"
_LLAMA_CPP_URL = "http://llama.test:8080"
_FIRST_WINDOW = 8192
_SECOND_WINDOW = 16384
_MODEL_MAXIMUM = 32768
_ABOVE_THE_MAXIMUM = 65536
_WITHIN_THE_MAXIMUM = 16384
_COMPOSITION_LOGGER = llm_composition.__name__
_LLAMA_CPP_LOGGER = llama_cpp_client.__name__
_SHORT_TIMEOUT_SECONDS = 0.2
_LONGER_THAN_THE_TIMEOUT_SECONDS = 3.0
_GENEROUS_BOUND_SECONDS = 2.5
_LLM_CLIENT_PACKAGE = "archon_search.llm_client"
_PROMPT = "write a short passage"
_CLAUDE_REPLY = "second try"
_CALL_TIMEOUT_SECONDS = 30.0


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's HTTP_PROXY must not capture the loopback requests."""
    keep_loopback_out_of_proxies(monkeypatch)


def _client(
    provider: str,
    *,
    ollama_url: str = _OLLAMA_URL,
    llama_cpp_url: str = _LLAMA_CPP_URL,
    context: int | None = None,
    rate_limit_rpm: int | None = None,
    retry: bool = False,
) -> LlmClient:
    return build_llm_client(
        provider=provider,
        model=_MODEL,
        ollama_base_url=ollama_url,
        llama_cpp_base_url=llama_cpp_url,
        ollama_context_tokens=context,
        retry_on_nonzero_exit=retry,
        rate_limit_rpm=rate_limit_rpm,
    )


def _warnings(caplog: pytest.LogCaptureFixture, logger_name: str = _COMPOSITION_LOGGER) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.WARNING and record.name == logger_name]


@pytest.mark.parametrize(
    ("provider", "wrapper"),
    [
        ("anthropic", AnthropicClient),
        ("openai", OpenAIClient),
        ("ollama", OllamaClient),
        ("claude_cli", ClaudeCliClient),
        ("llama_cpp", LlamaCppClient),
    ],
)
def test_build_llm_client_returns_the_wrapper_of_the_provider(provider: str, wrapper: type) -> None:
    client = _client(provider)

    assert isinstance(client, wrapper)
    assert isinstance(client, LlmClient)


def test_build_llm_client_rejects_an_unknown_provider() -> None:
    with pytest.raises(ValueError, match="bogus"):
        _client("bogus")


def test_ollama_client_is_built_with_its_configured_context() -> None:
    client = _client("ollama", context=_FIRST_WINDOW)

    assert client.context_window() == ContextWindowTokens(size=_FIRST_WINDOW, source=ContextWindowSource.CONFIGURED)


def test_cloud_clients_report_an_unbounded_window() -> None:
    assert _client("anthropic").context_window() == UNBOUNDED
    assert _client("openai").context_window() == UNBOUNDED


async def test_each_llama_cpp_client_probes_its_own_host() -> None:
    with FakeModelServer(llama_n_ctx=_FIRST_WINDOW) as first, FakeModelServer(llama_n_ctx=_SECOND_WINDOW) as second:
        first_client = _client("llama_cpp", llama_cpp_url=first.base_url)
        second_client = _client("llama_cpp", llama_cpp_url=second.base_url)

        await resolve_context_windows([FeatureClient("hyde", first_client), FeatureClient("graph", second_client)])

    assert [request.path for request in first.requests] == ["/props"]
    assert [request.path for request in second.requests] == ["/props"]
    assert first_client.context_window() == ContextWindowTokens(size=_FIRST_WINDOW, source=ContextWindowSource.PROBED)
    assert second_client.context_window() == ContextWindowTokens(size=_SECOND_WINDOW, source=ContextWindowSource.PROBED)


async def test_failed_llama_cpp_probe_warns_and_assumes_the_window(caplog: pytest.LogCaptureFixture) -> None:
    with unreachable_port() as port:
        client = _client("llama_cpp", llama_cpp_url=f"http://{_LOOPBACK}:{port}")

        with caplog.at_level(logging.WARNING):
            await resolve_context_windows([FeatureClient("hyde", client)])

    assert client.context_window() == ContextWindowTokens(size=ASSUMED_CONTEXT_TOKENS, source=ContextWindowSource.ASSUMED)
    [warning] = _warnings(caplog, _LLAMA_CPP_LOGGER)
    assert str(ASSUMED_CONTEXT_TOKENS) in warning.getMessage()


async def test_unset_ollama_window_warns_once_per_feature(caplog: pytest.LogCaptureFixture) -> None:
    hyde = FeatureClient("hyde", _client("ollama"))
    rag_fusion = FeatureClient("rag_fusion", _client("ollama"))

    with caplog.at_level(logging.WARNING):
        await resolve_context_windows([hyde, rag_fusion])
        await resolve_context_windows([hyde, rag_fusion])

    messages = [record.getMessage() for record in _warnings(caplog)]
    assert len(messages) == 2
    assert "[hyde].ollama_context_tokens" in messages[0]
    assert "[rag_fusion].ollama_context_tokens" in messages[1]
    for message in messages:
        assert str(ASSUMED_CONTEXT_TOKENS) in message
        assert "num_ctx" in message


async def test_unset_ollama_window_needs_no_request_to_the_server() -> None:
    with FakeModelServer() as server:
        await resolve_context_windows([FeatureClient("hyde", _client("ollama", ollama_url=server.base_url))])

    assert server.requests == []


async def test_oversized_ollama_window_warns_with_the_model_maximum(caplog: pytest.LogCaptureFixture) -> None:
    with FakeModelServer(ollama_context_length=_MODEL_MAXIMUM) as server:
        client = _client("ollama", ollama_url=server.base_url, context=_ABOVE_THE_MAXIMUM)

        with caplog.at_level(logging.WARNING):
            await resolve_context_windows([FeatureClient("graph", client)])

    [warning] = _warnings(caplog)
    message = warning.getMessage()
    assert "[graph].ollama_context_tokens" in message
    assert str(_ABOVE_THE_MAXIMUM) in message
    assert str(_MODEL_MAXIMUM) in message
    assert [request.path for request in server.requests] == ["/api/show"]
    assert client.context_window() == ContextWindowTokens(size=_ABOVE_THE_MAXIMUM, source=ContextWindowSource.CONFIGURED)


async def test_oversized_ollama_window_warns_once_per_process(caplog: pytest.LogCaptureFixture) -> None:
    with FakeModelServer(ollama_context_length=_MODEL_MAXIMUM) as server:
        feature = FeatureClient("hyde", _client("ollama", ollama_url=server.base_url, context=_ABOVE_THE_MAXIMUM))

        with caplog.at_level(logging.WARNING):
            await resolve_context_windows([feature])
            await resolve_context_windows([feature])

    assert len(_warnings(caplog)) == 1


@pytest.mark.parametrize("configured", [_WITHIN_THE_MAXIMUM, _MODEL_MAXIMUM])
async def test_ollama_window_up_to_the_model_maximum_logs_nothing(configured: int, caplog: pytest.LogCaptureFixture) -> None:
    with FakeModelServer(ollama_context_length=_MODEL_MAXIMUM) as server:
        client = _client("ollama", ollama_url=server.base_url, context=configured)

        with caplog.at_level(logging.INFO):
            await resolve_context_windows([FeatureClient("hyde", client)])

    assert [record for record in caplog.records if record.name == _COMPOSITION_LOGGER] == []


async def test_model_without_a_stated_maximum_is_not_checked(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    async def no_maximum(self: OllamaClient, timeout_seconds: float) -> None:
        return None

    monkeypatch.setattr(OllamaClient, "model_max_context_tokens", no_maximum)

    with caplog.at_level(logging.INFO):
        await resolve_context_windows([FeatureClient("hyde", _client("ollama", context=_ABOVE_THE_MAXIMUM))])

    assert [record for record in caplog.records if record.name == _COMPOSITION_LOGGER] == []


async def test_failed_model_details_read_skips_the_check_without_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with unreachable_port() as port:
        client = _client("ollama", ollama_url=f"http://{_LOOPBACK}:{port}", context=_ABOVE_THE_MAXIMUM)

        with caplog.at_level(logging.INFO):
            await resolve_context_windows([FeatureClient("hyde", client)])

    assert _warnings(caplog) == []
    [skipped] = [record for record in caplog.records if record.name == _COMPOSITION_LOGGER]
    assert skipped.levelno == logging.INFO
    assert "[hyde]" in skipped.getMessage()


async def test_unanswered_window_reads_are_bounded_by_the_resolve_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(llm_composition, "WINDOW_RESOLVE_TIMEOUT_SECONDS", _SHORT_TIMEOUT_SECONDS)
    with FakeModelServer(response_delay_seconds=_LONGER_THAN_THE_TIMEOUT_SECONDS) as server:
        ollama = _client("ollama", ollama_url=server.base_url, context=_ABOVE_THE_MAXIMUM)
        llama_cpp = _client("llama_cpp", llama_cpp_url=server.base_url)

        with caplog.at_level(logging.INFO):
            started = time.monotonic()
            await resolve_context_windows([FeatureClient("hyde", ollama), FeatureClient("graph", llama_cpp)])
            elapsed = time.monotonic() - started

    assert elapsed < _GENEROUS_BOUND_SECONDS
    assert llama_cpp.context_window() == ContextWindowTokens(size=ASSUMED_CONTEXT_TOKENS, source=ContextWindowSource.ASSUMED)
    assert _warnings(caplog) == []
    assert len(_warnings(caplog, _LLAMA_CPP_LOGGER)) == 1
    [skipped] = [record for record in caplog.records if record.name == _COMPOSITION_LOGGER]
    assert skipped.levelno == logging.INFO
    assert "[hyde]" in skipped.getMessage()


async def test_a_broken_check_is_logged_and_leaves_the_other_features_resolved(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(self: OllamaClient, timeout_seconds: float) -> int:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(OllamaClient, "model_max_context_tokens", broken)
    with FakeModelServer(llama_n_ctx=_FIRST_WINDOW) as server:
        llama_cpp = _client("llama_cpp", llama_cpp_url=server.base_url)

        with caplog.at_level(logging.WARNING):
            await resolve_context_windows(
                [FeatureClient("hyde", _client("ollama", context=_ABOVE_THE_MAXIMUM)), FeatureClient("graph", llama_cpp)]
            )

    [warning] = _warnings(caplog)
    assert "[hyde]" in warning.getMessage()
    assert "RuntimeError" in warning.getMessage()
    assert "secret detail" not in warning.getMessage()
    assert llama_cpp.context_window() == ContextWindowTokens(size=_FIRST_WINDOW, source=ContextWindowSource.PROBED)


async def test_cloud_and_claude_cli_clients_resolve_without_a_log_record(caplog: pytest.LogCaptureFixture) -> None:
    features = [FeatureClient(provider, _client(provider)) for provider in ("anthropic", "openai", "claude_cli")]

    with caplog.at_level(logging.INFO):
        await resolve_context_windows(features)

    assert [record for record in caplog.records if record.name.startswith((_COMPOSITION_LOGGER, _LLM_CLIENT_PACKAGE))] == []


@pytest.mark.parametrize(("retry", "invocations"), [(True, 2), (False, 1)])
async def test_claude_cli_transport_retry_follows_the_option_of_the_feature(
    retry: bool, invocations: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_cli_client, "RETRY_BACKOFF_SECONDS", 0.0)
    fake = _fake_claude.install(tmp_path / "bin", reply=_CLAUDE_REPLY, exit_codes=(1, 0))
    monkeypatch.setenv("PATH", f"{fake.path.parent}{os.pathsep}{os.environ['PATH']}")
    client = build_llm_client(
        provider="claude_cli",
        model=_MODEL,
        ollama_base_url=_OLLAMA_URL,
        llama_cpp_base_url=_LLAMA_CPP_URL,
        ollama_context_tokens=None,
        retry_on_nonzero_exit=retry,
    )
    request = LlmCallRequest(prompt=_PROMPT, max_output_tokens=ANSWER_RESERVE_TOKENS, timeout_seconds=_CALL_TIMEOUT_SECONDS)

    if retry:
        assert (await client.call(request)).text == _CLAUDE_REPLY
    else:
        with pytest.raises(LlmCallFailure):
            await client.call(request)

    assert len(fake.invocations()) == invocations


# --- the graph rate limiter (BE-14, C1, S64) -------------------------------------------------------------------------

_GRAPH_RPM = 2
_ATTEMPTS_PAST_THE_CAP = _GRAPH_RPM + 3
_REPLY = "a summary"
_EXHAUSTED_REASON = f"rate limit exhausted ({_GRAPH_RPM} rpm); retry later"
_REQUEST = LlmCallRequest(prompt=_PROMPT, max_output_tokens=ANSWER_RESERVE_TOKENS, timeout_seconds=_CALL_TIMEOUT_SECONDS)
_SDK_REPLY = SimpleNamespace(
    content=[SimpleNamespace(type="text", text=_REPLY)],
    choices=[SimpleNamespace(message=SimpleNamespace(content=_REPLY))],
)
_OLLAMA_CHAT_PATH = "/api/chat"
_LLAMA_CPP_CHAT_PATH = "/v1/chat/completions"


class _CountingSdkClient:
    """Stands in for ``AsyncAnthropic`` / ``AsyncOpenAI``: counts the requests that reach the SDK."""

    def __init__(self) -> None:
        self.requests = 0
        endpoint = SimpleNamespace(create=self._create)
        self.messages = endpoint
        self.chat = SimpleNamespace(completions=endpoint)

    async def _create(self, **kwargs: object) -> SimpleNamespace:
        self.requests += 1
        return _SDK_REPLY


@contextlib.contextmanager
def _backend(provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Callable[[], int]]]:
    """Stand up what *provider* talks to; yields the host to compose against and a count of the requests that reached it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    match provider:
        case "claude_cli":
            fake = _fake_claude.install(tmp_path / "bin", reply=_REPLY)
            monkeypatch.setenv("PATH", f"{fake.path.parent}{os.pathsep}{os.environ['PATH']}")
            yield _OLLAMA_URL, lambda: len(fake.invocations())
        case "anthropic" | "openai":
            sdk = _CountingSdkClient()
            module, constructor = (anthropic, "AsyncAnthropic") if provider == "anthropic" else (openai, "AsyncOpenAI")
            with patch.object(module, constructor, return_value=sdk):
                yield _OLLAMA_URL, lambda: sdk.requests
        case "ollama" | "llama_cpp":
            path = _OLLAMA_CHAT_PATH if provider == "ollama" else _LLAMA_CPP_CHAT_PATH
            with FakeModelServer(reply_text=_REPLY) as server:
                yield server.base_url, lambda: len(server.requests_to(path))
        case _:
            raise AssertionError(f"no backend for {provider}")


async def _served_and_refused(client: LlmClient) -> tuple[int, list[LlmCallFailure]]:
    """Call *client* ``_ATTEMPTS_PAST_THE_CAP`` times in a row: how many were answered, and the failures of the others."""
    served = 0
    refused: list[LlmCallFailure] = []
    for _ in range(_ATTEMPTS_PAST_THE_CAP):
        try:
            await client.call(_REQUEST)
            served += 1
        except LlmCallFailure as failure:
            refused.append(failure)
    return served, refused


@pytest.mark.parametrize("provider", ["anthropic", "claude_cli"])
async def test_graph_limiter_applies_to_anthropic_and_claude_cli(provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _backend(provider, tmp_path, monkeypatch) as (host, requests):
        client = _client(provider, ollama_url=host, rate_limit_rpm=_GRAPH_RPM)

        served, refused = await _served_and_refused(client)

        assert served == _GRAPH_RPM
        assert [failure.reason for failure in refused] == [_EXHAUSTED_REASON] * (_ATTEMPTS_PAST_THE_CAP - _GRAPH_RPM)
        assert requests() == _GRAPH_RPM


async def test_each_graph_client_has_its_own_limiter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _backend("claude_cli", tmp_path, monkeypatch) as (_, requests):
        first_served, _ = await _served_and_refused(_client("claude_cli", rate_limit_rpm=_GRAPH_RPM))
        second_served, _ = await _served_and_refused(_client("claude_cli", rate_limit_rpm=_GRAPH_RPM))

        assert (first_served, second_served) == (_GRAPH_RPM, _GRAPH_RPM)
        assert requests() == 2 * _GRAPH_RPM


@pytest.mark.parametrize(
    ("provider", "rate_limit_rpm", "retry"),
    [
        pytest.param("claude_cli", None, True, id="hyde_and_rag_fusion_claude_cli"),
        pytest.param("anthropic", None, False, id="anthropic_without_a_cap"),
        pytest.param("openai", _GRAPH_RPM, False, id="openai_graph"),
        pytest.param("ollama", _GRAPH_RPM, False, id="ollama_graph"),
        pytest.param("llama_cpp", _GRAPH_RPM, False, id="llama_cpp_graph"),
    ],
)
async def test_limiter_not_applied_to_hyde_claude_cli_or_other_graph_providers(
    provider: str, rate_limit_rpm: int | None, retry: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _backend(provider, tmp_path, monkeypatch) as (host, requests):
        client = _client(provider, ollama_url=host, llama_cpp_url=host, rate_limit_rpm=rate_limit_rpm, retry=retry)

        served, refused = await _served_and_refused(client)

        assert (served, refused) == (_ATTEMPTS_PAST_THE_CAP, [])
        assert requests() == _ATTEMPTS_PAST_THE_CAP


async def test_graph_claude_cli_client_is_composed_without_the_transport_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_cli_client, "RETRY_BACKOFF_SECONDS", 0.0)
    fake = _fake_claude.install(tmp_path / "bin", reply=_REPLY, exit_codes=(1, 0))
    monkeypatch.setenv("PATH", f"{fake.path.parent}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(LlmCallFailure, match="exit status 1"):
        await _client("claude_cli", rate_limit_rpm=_GRAPH_RPM).call(_REQUEST)

    assert len(fake.invocations()) == 1

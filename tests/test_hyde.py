"""Tests for archon_search.hyde — HyDEGenerator on the shared LLM client."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.config import HyDEConfig
from archon_search.hyde import HyDEGenerator
from archon_search.llm_client.protocol import LlmCallFailure, LlmCallTimeout
from tests._hyde_stubs import ScriptedLlmClient

_HYDE_LOGGER = "archon_search.hyde"
_EXPECTED_MAX_OUTPUT_TOKENS = 200


def _make_embedder(dim: int = 4) -> Any:
    """Return a mock Embedder whose embed_one returns a list of `dim` floats."""
    embedder = MagicMock()
    embedder.embed_one = AsyncMock(return_value=[0.1] * dim)
    return embedder


def _make_config(**kwargs: Any) -> HyDEConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "model": "claude-haiku-4-5-20251001",
        "timeout_seconds": 5.0,
        "max_requests_per_minute": 60,
    }
    defaults.update(kwargs)
    return HyDEConfig(**defaults)


# ---------------------------------------------------------------------------
# _query_fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_is_16_hex_chars() -> None:
    from archon_search.hyde import _query_fingerprint

    fp = _query_fingerprint("How do I uninstall the CLI?")
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_deterministic() -> None:
    from archon_search.hyde import _query_fingerprint

    assert _query_fingerprint("query") == _query_fingerprint("query")


def test_fingerprint_different_queries() -> None:
    from archon_search.hyde import _query_fingerprint

    assert _query_fingerprint("query A") != _query_fingerprint("query B")


def test_fingerprint_is_the_keyed_hmac_not_bare_sha256(monkeypatch: pytest.MonkeyPatch) -> None:
    """HyDE's log fingerprint is the shared keyed one (tests/test_privacy_fingerprint.py covers the keying)."""
    from archon_search import _privacy
    from archon_search.hyde import _query_fingerprint

    salt = b"s" * 32
    monkeypatch.setattr(_privacy, "_fingerprint_key", salt)
    query = "test query"

    assert _query_fingerprint(query) == hmac.digest(salt, query.encode(), "sha256").hex()[:16]
    assert _query_fingerprint(query) != hashlib.sha256(query.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# generate — happy path and the request it sends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_embeds_the_hypothesis_the_client_returned() -> None:
    client = ScriptedLlmClient(reply="  A hypothetical passage.  ")
    embedder = _make_embedder(dim=4)

    result = await HyDEGenerator(_make_config(), client).generate("what is hyde", embedder=embedder)

    assert result == [0.1] * 4
    embedder.embed_one.assert_awaited_once_with("A hypothetical passage.")


@pytest.mark.asyncio
async def test_generate_sends_one_templated_request_with_the_hyde_ceilings() -> None:
    client = ScriptedLlmClient()
    config = _make_config(timeout_seconds=7.5)

    await HyDEGenerator(config, client).generate("what is hyde", embedder=_make_embedder())

    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.max_output_tokens == _EXPECTED_MAX_OUTPUT_TOKENS
    assert request.timeout_seconds == 7.5
    assert request.prompt.startswith("Write a short passage that would directly answer the following question.")
    assert "---\nwhat is hyde\n---" in request.prompt


@pytest.mark.asyncio
async def test_query_truncated_to_2000_chars() -> None:
    """Queries longer than 2000 chars are truncated in the prompt sent to the LLM."""
    client = ScriptedLlmClient()

    await HyDEGenerator(_make_config(), client).generate("x" * 3000, embedder=_make_embedder())

    prompt = client.requests[0].prompt
    assert "x" * 2000 in prompt
    assert "x" * 2001 not in prompt


# ---------------------------------------------------------------------------
# generate — failure contract (S2): no document, hyde_applied false upstream
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        LlmCallFailure("anthropic", "ANTHROPIC_API_KEY is not set"),
        LlmCallTimeout("ollama", "no answer within 5.0s"),
    ],
    ids=["failure", "timeout"],
)
@pytest.mark.asyncio
async def test_hyde_failure_returns_no_document_and_flags_not_applied(failure: LlmCallFailure) -> None:
    """A raising client yields no document, so the request reports ``hyde_applied: false`` and searches the plain query."""
    from archon_search.server._hyde import RequestHyDE

    config = _make_config()
    embedder = _make_embedder()
    generator = HyDEGenerator(config, ScriptedLlmClient(failure=failure))
    request = RequestHyDE.for_request(
        "test query",
        requested=True,
        rag_fusion=False,
        rag_fusion_enabled=False,
        graph_mode=None,
        generator=generator,
        config=config,
        timeout=5.0,
    )

    assert await generator.generate("test query", embedder=embedder) is None
    outcome = await request.resolve(embedder, embedder_warm=True)

    assert outcome.applied is False
    assert outcome.vector is None
    assert outcome.expansion_warning is not None
    embedder.embed_one.assert_not_awaited()


@pytest.mark.parametrize(
    ("query", "echo"),
    [
        ("very-unique-secret-query-97531", lambda q: q),
        ("very-unique  secret\nquery 97531", lambda q: " ".join(q.split())),  # clients collapse whitespace
        ('very-unique "secret" query\n97531', lambda q: json.dumps(q)[1:-1]),  # a JSON error body
        ("very-unique-secret-query-" + "x" * 3000, lambda q: q[:2000]),  # the cut that went into the prompt
    ],
    ids=["verbatim", "whitespace-collapsed", "json-escaped", "prompt-cut"],
)
@pytest.mark.asyncio
async def test_hyde_failure_text_echoing_the_query_is_masked_in_the_warning(
    caplog: pytest.LogCaptureFixture, query: str, echo: Callable[[str], str]
) -> None:
    """A server that echoes the query in its error never puts it in the log, in any form it may echo it."""
    echoed = echo(query)
    failure = LlmCallFailure("ollama", f'model not found for "{echoed}"')
    generator = HyDEGenerator(_make_config(), ScriptedLlmClient(failure=failure))

    with caplog.at_level(logging.DEBUG):
        assert await generator.generate(query, embedder=_make_embedder()) is None

    [warning] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    message = warning.getMessage()
    assert echoed not in message
    assert "model not found" in message


@pytest.mark.asyncio
async def test_generate_empty_hypothesis_returns_none() -> None:
    embedder = _make_embedder()

    result = await HyDEGenerator(_make_config(), ScriptedLlmClient(reply="   \n")).generate(
        "test query", embedder=embedder
    )

    assert result is None
    embedder.embed_one.assert_not_awaited()


# ---------------------------------------------------------------------------
# Logging: one WARNING per process, fingerprint only (S2, S15)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hyde_logs_one_warning_per_process_without_query_text(caplog: pytest.LogCaptureFixture) -> None:
    """Many failures log one WARNING; no log line holds the query or the hypothesis."""
    query = "very-unique-secret-query-12345"
    hypothesis = "very-unique-secret-hypothesis-67890"
    from archon_search.hyde import _query_fingerprint

    client = ScriptedLlmClient(reply=hypothesis)
    generator = HyDEGenerator(_make_config(), client)
    embedder = _make_embedder()

    with caplog.at_level(logging.DEBUG):
        assert await generator.generate(f"{query}-ok", embedder=embedder) is not None
        client.failure = LlmCallTimeout("anthropic", "no answer")
        for attempt in range(5):
            assert await generator.generate(f"{query}-{attempt}", embedder=embedder) is None
        assert await generator.generate(query, embedder=embedder) is None

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == _HYDE_LOGGER]
    assert len(warnings) == 1
    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert query not in log_text
    assert hypothesis not in log_text
    assert _query_fingerprint(f"{query}-0") in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_hyde_empty_hypothesis_logs_no_hypothesis_text(caplog: pytest.LogCaptureFixture) -> None:
    secret_query = "very-unique-secret-query-24680"
    generator = HyDEGenerator(_make_config(), ScriptedLlmClient(reply=" "))

    with caplog.at_level(logging.DEBUG):
        await generator.generate(secret_query, embedder=_make_embedder())
        await generator.generate(secret_query, embedder=_make_embedder())

    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1
    assert secret_query not in " ".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Rate limiting (generator-level bucket kept)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_fallback(caplog: pytest.LogCaptureFixture) -> None:
    """When the token bucket is exhausted, generate() returns None with a WARNING and does not call the client."""
    client = ScriptedLlmClient()
    gen = HyDEGenerator(_make_config(max_requests_per_minute=1), client)
    embedder = _make_embedder()

    result1 = await gen.generate("query 1", embedder=embedder)
    with caplog.at_level(logging.WARNING, logger=_HYDE_LOGGER):
        result2 = await gen.generate("query 2", embedder=embedder)

    assert result1 is not None, "First call should succeed with 1 token"
    assert result2 is None, "Second call should be rate-limited"
    assert len(client.requests) == 1
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("provider", ["ollama", "claude_cli", "llama_cpp"])
@pytest.mark.asyncio
async def test_rate_limit_skipped_for_local_providers(provider: str) -> None:
    """ollama, claude_cli and llama_cpp are not throttled by the bucket."""
    client = ScriptedLlmClient()
    gen = HyDEGenerator(_make_config(provider=provider, max_requests_per_minute=1), client)
    embedder = _make_embedder()

    assert await gen.generate("query 1", embedder=embedder) is not None
    assert await gen.generate("query 2", embedder=embedder) is not None
    assert len(client.requests) == 2


# ---------------------------------------------------------------------------
# is_key_available (kept for /status)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "env_var"),
    [("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY")],
)
def test_is_key_available_follows_the_provider_environment_key(
    monkeypatch: pytest.MonkeyPatch, provider: str, env_var: str
) -> None:
    generator = HyDEGenerator(_make_config(provider=provider), ScriptedLlmClient())

    monkeypatch.delenv(env_var, raising=False)
    assert generator.is_key_available() is False
    monkeypatch.setenv(env_var, "key")
    assert generator.is_key_available() is True


@pytest.mark.parametrize("provider", ["ollama", "claude_cli", "llama_cpp"])
def test_is_key_available_is_true_for_keyless_providers(provider: str) -> None:
    assert HyDEGenerator(_make_config(provider=provider), ScriptedLlmClient()).is_key_available() is True


# ---------------------------------------------------------------------------
# Real LlamaCppClient against a loopback fake llama-server (S1, S2)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hyde_end_to_end_with_llama_cpp_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """S1: a HyDEGenerator over a real ``LlamaCppClient`` embeds the server's answer and reports HyDE applied."""
    from archon_search.llm_client.llama_cpp_client import LlamaCppClient
    from archon_search.server._hyde import RequestHyDE
    from archon_search.server._search_budget import SEARCH_TIMEOUT_SECONDS
    from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies

    keep_loopback_out_of_proxies(monkeypatch)
    passage = "A hypothetical passage about archon search."
    embedder = _make_embedder(dim=3)

    with FakeModelServer(reply_text=passage) as server:
        config = _make_config(provider="llama_cpp", llama_cpp_base_url=server.base_url)
        generator = HyDEGenerator(config, LlamaCppClient(model="local-model", base_url=server.base_url))
        outcome = await RequestHyDE(
            query="archon search query", requested=True, suppressed=False,
            generator=generator, config=config, timeout=SEARCH_TIMEOUT_SECONDS,
        ).resolve(embedder, embedder_warm=True)

    assert outcome.applied is True
    assert outcome.vector == [0.1] * 3
    embedder.embed_one.assert_awaited_once_with(passage)
    [chat] = server.requests_to("/v1/chat/completions")
    assert chat.body is not None
    assert chat.body["max_tokens"] == _EXPECTED_MAX_OUTPUT_TOKENS
    assert chat.body["messages"][0]["role"] == "user"
    assert "archon search query" in chat.body["messages"][0]["content"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_llama_cpp_fallback_on_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """S2: llama-server unreachable → the client raises, HyDE falls back with no exception and embeds nothing."""
    from archon_search.llm_client.llama_cpp_client import LlamaCppClient
    from archon_search.server._hyde import RequestHyDE
    from archon_search.server._search_budget import SEARCH_TIMEOUT_SECONDS
    from tests._fake_model_server import keep_loopback_out_of_proxies, unreachable_port

    keep_loopback_out_of_proxies(monkeypatch)
    embedder = _make_embedder(dim=3)

    with unreachable_port() as port:
        base_url = f"http://127.0.0.1:{port}"
        config = _make_config(provider="llama_cpp", llama_cpp_base_url=base_url)
        generator = HyDEGenerator(config, LlamaCppClient(model="local-model", base_url=base_url))
        outcome = await RequestHyDE(
            query="archon search query", requested=True, suppressed=False,
            generator=generator, config=config, timeout=SEARCH_TIMEOUT_SECONDS,
        ).resolve(embedder, embedder_warm=True)

    assert outcome.vector is None
    assert outcome.applied is False
    embedder.embed_one.assert_not_awaited()

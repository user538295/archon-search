"""Tests for archon_search.rag_fusion — RAGFusionGenerator over the shared LLM client, and _query_fingerprint.

The generator takes an injected ``LlmClient`` (``app.state.rag_fusion_client``); every test here drives it with the
scripted stand-in of ``tests/_hyde_stubs.py``, so no provider package or network is involved.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

import pytest

from archon_search.config import RAGFusionConfig
from archon_search.llm_client.protocol import LlmCallFailure, LlmCallTimeout
from archon_search.rag_fusion import (
    RAG_FUSION_MAX_VARIANT_CHARS,
    RAG_FUSION_PROMPT_TEMPLATE,
    RAG_FUSION_TOKENS_PER_QUERY,
    RAGFusionGenerator,
)
from archon_search.rag_fusion_failure import (
    RAG_FUSION_FAILED_WARNING,
    RAG_FUSION_TIMED_OUT_WARNING,
    RAGFusionFailure,
    RAGFusionFailureReason,
)
from tests._hyde_stubs import ScriptedLlmClient

_WARN_LOGGER = "archon_search.rag_fusion"
_SECRET_QUERY = "very-unique-secret-query-rag-12345"


def _make_config(**kwargs: Any) -> RAGFusionConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "model": "claude-haiku-4-5-20251001",
        "timeout_seconds": 5.0,
        "max_requests_per_minute": 60,
        "num_queries": 2,
    }
    defaults.update(kwargs)
    return RAGFusionConfig(**defaults)


def _generator(reply: str = "v1\nv2", failure: Exception | None = None, **config: Any) -> tuple[RAGFusionGenerator, ScriptedLlmClient]:
    client = ScriptedLlmClient(reply=reply, failure=failure)
    return RAGFusionGenerator(_make_config(**config), client), client


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


async def _failed(generator: RAGFusionGenerator, query: str = "original query") -> RAGFusionFailure:
    """The failure *generator* raises for *query*; the test fails when it returns variants instead."""
    with pytest.raises(RAGFusionFailure) as caught:
        await generator.generate_variants(query)
    return caught.value


# ---------------------------------------------------------------------------
# _query_fingerprint (from _privacy.py)
# ---------------------------------------------------------------------------


def test_fingerprint_is_16_hex_chars() -> None:
    from archon_search._privacy import _query_fingerprint

    fp = _query_fingerprint("How do I uninstall the CLI?")
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_is_a_keyed_hmac_not_bare_sha256() -> None:
    import hmac

    from archon_search import _privacy
    from archon_search._privacy import (
        _query_fingerprint,
        configure_query_fingerprint_salt,
    )

    saved = _privacy._fingerprint_key
    try:
        configure_query_fingerprint_salt(b"k" * 32)
        query = "test query"
        assert _query_fingerprint(query) == hmac.digest(b"k" * 32, query.encode(), "sha256").hex()[:16]
        assert _query_fingerprint(query) != hashlib.sha256(query.encode()).hexdigest()[:16]
    finally:
        _privacy._fingerprint_key = saved


def test_fingerprint_importable_from_hyde() -> None:
    """hyde.py must re-export _query_fingerprint from _privacy.py."""
    from archon_search._privacy import _query_fingerprint as privacy_fp
    from archon_search.hyde import _query_fingerprint as hyde_fp

    assert hyde_fp("test") == privacy_fp("test")


# ---------------------------------------------------------------------------
# RAGFusionGenerator._validate_variant
# ---------------------------------------------------------------------------


def test_validate_variant_valid() -> None:
    """Normal text ≤500 chars returns stripped text."""
    assert _generator()[0]._validate_variant("  hello world  ") == "hello world"


def test_validate_variant_too_long() -> None:
    """Variant of 501 chars returns None."""
    assert _generator()[0]._validate_variant("a" * 501) is None


def test_validate_variant_exactly_500_chars() -> None:
    """Variant of exactly 500 chars is valid."""
    assert _generator()[0]._validate_variant("a" * 500) == "a" * 500


@pytest.mark.parametrize("control", ["\x00", "\x7f", "\x80", "\x9f", "\x1b"])
def test_validate_variant_control_characters_rejected(control: str) -> None:
    """NUL, ESC, DEL and the C1 controls reject the variant."""
    assert _generator()[0]._validate_variant(f"hello{control}world") is None


def test_validate_variant_tab_allowed() -> None:
    """Variant containing \\t returns stripped text (not rejected)."""
    assert _generator()[0]._validate_variant("hello\tworld") == "hello\tworld"


def test_validate_variant_trailing_newline_stripped() -> None:
    assert _generator()[0]._validate_variant("hello world\n") == "hello world"


def test_validate_variant_empty_after_strip() -> None:
    """Variant of only whitespace returns None."""
    assert _generator()[0]._validate_variant("   \n\t  ") is None


# ---------------------------------------------------------------------------
# generate_variants — the call, and parsing (S15: _validate_variant is live)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_variants_success() -> None:
    """The client's two lines become two variants, each ≤500 chars."""
    generator, _client = _generator("variant one alternative query\nvariant two different phrasing")

    result = await generator.generate_variants("original query")

    assert result == ["variant one alternative query", "variant two different phrasing"]


@pytest.mark.asyncio
async def test_generate_variants_sends_one_request_with_template_ceiling_and_timeout() -> None:
    """One call: the decomposition template around the query and num_queries, ceiling 150 per query, the section timeout."""
    generator, client = _generator("v1\nv2\nv3", num_queries=3, timeout_seconds=7.5)

    await generator.generate_variants("my query")

    [request] = client.requests
    assert "my query" in request.prompt
    assert "generate 3 alternative" in request.prompt
    assert "Output exactly 3 queries" in request.prompt
    assert f"under {RAG_FUSION_MAX_VARIANT_CHARS} characters" in request.prompt
    assert request.max_output_tokens == RAG_FUSION_TOKENS_PER_QUERY * 3 == 450
    assert request.timeout_seconds == 7.5


@pytest.mark.asyncio
async def test_a_long_query_goes_to_a_client_without_a_limit_whole() -> None:
    """S9: a client with no input budget (cloud) receives the whole query; there is no fixed character cut."""
    generator, client = _generator()
    query = "x" * 3000

    await generator.generate_variants(query)

    [request] = client.requests
    assert request.prompt == RAG_FUSION_PROMPT_TEMPLATE.format(
        num_queries=2, max_chars=RAG_FUSION_MAX_VARIANT_CHARS, query=query
    )


@pytest.mark.asyncio
async def test_variants_validated_and_trimmed_to_num_queries(caplog: pytest.LogCaptureFixture) -> None:
    """Over-long, control-character and blank lines are dropped, and the survivors are cut to num_queries."""
    reply = "\n".join(
        [
            "  first good variant  ",
            "x" * 501,
            "",
            "control\x00character",
            "escape\x1bsequence",
            "second good variant",
            "surplus variant one",
            "surplus variant two",
        ]
    )
    generator, _client = _generator(reply, num_queries=2)

    with caplog.at_level(logging.WARNING):
        result = await generator.generate_variants("original query")

    assert result == ["first good variant", "second good variant"]
    assert not _warnings(caplog), "a full set of valid variants must not warn"


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "bare-cr"])
async def test_each_line_break_style_separates_variants(separator: str) -> None:
    """LF, CRLF and a bare CR each end a variant: two lines are two variants, not one with a control character."""
    generator, _client = _generator(separator.join(["first variant", "second variant"]))

    assert await generator.generate_variants("original query") == ["first variant", "second variant"]


@pytest.mark.asyncio
async def test_a_line_with_an_inner_control_character_is_dropped_not_split() -> None:
    """Vertical tab and form feed are control characters: the line is rejected whole instead of becoming two variants."""
    generator, _client = _generator("good variant\nbad\x0bvariant\nbad\x0cvariant\nlast good", num_queries=3)

    assert await generator.generate_variants("original query") == ["good variant", "last good"]

@pytest.mark.asyncio
async def test_variants_fewer_than_requested_keep_the_valid_ones_and_warn_once(caplog: pytest.LogCaptureFixture) -> None:
    """Only valid lines survive; a shortfall is one WARNING per process, with a count and no variant text."""
    generator, _client = _generator(f"valid variant\n{'x' * 501}\nhello\x00world", num_queries=3)

    with caplog.at_level(logging.WARNING):
        first = await generator.generate_variants("query 1")
        second = await generator.generate_variants("query 2")

    assert first == second == ["valid variant"]
    [warning] = _warnings(caplog)
    assert "requested 3 variants but got 1 valid ones" in warning.getMessage()
    assert "valid variant" not in warning.getMessage()


@pytest.mark.asyncio
async def test_a_shortfall_does_not_use_up_the_failure_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A shortfall and a failed call are different conditions: each one logs its own first WARNING."""
    short, _ = _generator("only one variant", num_queries=3)
    failing, _ = _generator(failure=LlmCallFailure("ollama", "connection refused"))

    with caplog.at_level(logging.WARNING):
        await short.generate_variants("query 1")
        await _failed(failing, "query 2")
        await _failed(failing, "query 3")

    messages = [warning.getMessage() for warning in _warnings(caplog)]
    assert len(messages) == 2
    assert "requested 3 variants" in messages[0]
    assert "generation failed" in messages[1]


@pytest.mark.asyncio
async def test_a_failed_call_and_an_answer_without_variants_share_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Both mean 'no variants this time': one WARNING per process between them, whichever comes first."""
    failing, _ = _generator(failure=LlmCallFailure("ollama", "connection refused"))
    empty, _ = _generator("   \n  ")

    with caplog.at_level(logging.WARNING):
        await _failed(failing, "query 1")
        await _failed(empty, "query 2")

    assert len(_warnings(caplog)) == 1


@pytest.mark.asyncio
async def test_generate_variants_only_blank_lines_is_no_variants_with_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    generator, _client = _generator("   \n  \n  ")

    with caplog.at_level(logging.WARNING):
        failure = await _failed(generator, "test query")

    assert failure.reason is RAGFusionFailureReason.NO_VARIANTS
    assert len(_warnings(caplog)) == 1


# ---------------------------------------------------------------------------
# Failure -> RAGFusionFailure with a typed reason (the pipeline then searches the single query and says so)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "reason", "warning"),
    [
        (
            LlmCallFailure("anthropic", "the ANTHROPIC_API_KEY is not set"),
            RAGFusionFailureReason.PROVIDER_FAILED,
            RAG_FUSION_FAILED_WARNING,
        ),
        (LlmCallTimeout("ollama", "no answer in 5s"), RAGFusionFailureReason.TIMEOUT, RAG_FUSION_TIMED_OUT_WARNING),
    ],
    ids=["failure", "timeout"],
)
async def test_a_failed_call_raises_a_typed_failure(
    failure: LlmCallFailure, reason: RAGFusionFailureReason, warning: str
) -> None:
    """A raised LlmCallFailure (or its timeout subclass) becomes a RAGFusionFailure: the reason and the fixed warning."""
    generator, client = _generator(failure=failure)

    raised = await _failed(generator)

    assert raised.reason is reason
    assert raised.warning == warning
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_an_exhausted_rate_limit_raises_a_failure_without_a_call() -> None:
    generator, client = _generator("v1\nv2", max_requests_per_minute=1)
    await generator.generate_variants("query 1")

    raised = await _failed(generator, "query 2")

    assert raised.reason is RAGFusionFailureReason.RATE_LIMITED
    assert raised.warning == RAG_FUSION_FAILED_WARNING
    assert len(client.requests) == 1, "the rate-limited call must not reach the client"


@pytest.mark.asyncio
async def test_a_failure_carries_no_query_prompt_or_provider_text() -> None:
    """C1: the provider's failure text may echo the query; the raised failure holds the reason and nothing else."""
    generator, _client = _generator(failure=LlmCallFailure("openai", f"rejected: {_SECRET_QUERY}"))

    raised = await _failed(generator, _SECRET_QUERY)

    assert str(raised) == RAGFusionFailureReason.PROVIDER_FAILED.value
    assert _SECRET_QUERY not in f"{raised!s} {raised!r} {raised.args} {raised.warning}"
    assert raised.__cause__ is None
    assert raised.__suppress_context__ is True, "the provider's exception must not print beside the failure"


@pytest.mark.parametrize(
    ("reason", "warning"),
    [
        (RAGFusionFailureReason.TIMEOUT, "RAG Fusion timed out"),
        (RAGFusionFailureReason.RATE_LIMITED, "RAG Fusion expansion failed"),
        (RAGFusionFailureReason.NO_ROOM, "RAG Fusion expansion failed"),
        (RAGFusionFailureReason.PROVIDER_FAILED, "RAG Fusion expansion failed"),
        (RAGFusionFailureReason.NO_VARIANTS, "RAG Fusion expansion failed"),
    ],
)
def test_every_reason_has_one_fixed_warning(reason: RAGFusionFailureReason, warning: str) -> None:
    assert RAGFusionFailure(reason).warning == warning


@pytest.mark.asyncio
async def test_rag_logs_one_warning_per_process_without_query_text(caplog: pytest.LogCaptureFixture) -> None:
    """Many failing calls log one WARNING: the query fingerprint, the failure reason, and neither query nor prompt."""
    from archon_search._privacy import _query_fingerprint

    first_query = f"{_SECRET_QUERY} 0"
    generator, client = _generator(failure=LlmCallFailure("ollama", f"server said: bad request for {first_query}"))

    with caplog.at_level(logging.INFO):
        failures = [await _failed(generator, f"{_SECRET_QUERY} {attempt}") for attempt in range(5)]

    assert {f.reason for f in failures} == {RAGFusionFailureReason.PROVIDER_FAILED}
    assert len(client.requests) == 5
    [warning] = [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == _WARN_LOGGER]
    assert _query_fingerprint(f"{_SECRET_QUERY} 0") in warning.getMessage()
    assert "bad request for" in warning.getMessage(), "the failure reason must stay readable"
    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert _SECRET_QUERY not in log_text
    assert "search query decomposer" not in log_text, "the prompt must never be logged"


@pytest.mark.asyncio
async def test_failure_text_echoing_the_query_is_masked(caplog: pytest.LogCaptureFixture) -> None:
    """A server that echoes the query back (even JSON-escaped or whitespace-collapsed) never puts it in the log."""
    query = 'say "hello"\n  to   the café'
    echoes = " / ".join(
        [json.dumps(query)[1:-1], json.dumps(query, ensure_ascii=False)[1:-1], " ".join(query.split())]
    )
    generator, _client = _generator(failure=LlmCallFailure("openai", f"rejected: {echoes}"))

    with caplog.at_level(logging.WARNING):
        await _failed(generator, query)

    [warning] = _warnings(caplog)
    assert "rejected:" in warning.getMessage()
    assert "hello" not in warning.getMessage()
    assert "café" not in warning.getMessage()


@pytest.mark.asyncio
async def test_failures_in_one_generator_silence_the_next_in_the_same_process(caplog: pytest.LogCaptureFixture) -> None:
    """The once-per-process key is not per generator instance: a second generator stays quiet."""
    failure = LlmCallFailure("ollama", "connection refused")
    with caplog.at_level(logging.WARNING):
        await _failed(_generator(failure=failure)[0], "q1")
        await _failed(_generator(failure=failure)[0], "q2")

    assert len(_warnings(caplog)) == 1


# ---------------------------------------------------------------------------
# Rate limiting (generator-level token bucket stays; the client has none for RAG Fusion)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_fallback(caplog: pytest.LogCaptureFixture) -> None:
    """When the token bucket is exhausted, generate_variants() raises a rate-limit failure with a WARNING and makes no call."""
    generator, client = _generator("v1\nv2", max_requests_per_minute=1)

    result1 = await generator.generate_variants("query 1")
    with caplog.at_level(logging.WARNING, logger=_WARN_LOGGER):
        failure = await _failed(generator, "query 2")

    assert result1 == ["v1", "v2"]
    assert failure.reason is RAGFusionFailureReason.RATE_LIMITED
    assert len(client.requests) == 1, "the rate-limited call must not reach the client"
    assert _warnings(caplog), "WARNING should be logged on rate limit"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["llama_cpp", "ollama", "claude_cli"])
async def test_rate_limit_skipped_for_local_providers(provider: str) -> None:
    """llama_cpp, ollama and claude_cli have no API cap: the bucket is never consulted."""
    generator, client = _generator("v1\nv2", provider=provider, max_requests_per_minute=1)
    generator._rpm_tokens = 0

    results = [await generator.generate_variants(f"query {i}") for i in range(3)]

    assert results == [["v1", "v2"]] * 3
    assert len(client.requests) == 3
    assert generator._rpm_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "openai"])
async def test_rate_limit_applies_to_cloud_providers(provider: str) -> None:
    generator, client = _generator("v1\nv2", provider=provider)
    generator._rpm_tokens = 0

    assert (await _failed(generator, "test query")).reason is RAGFusionFailureReason.RATE_LIMITED
    assert client.requests == []


@pytest.mark.asyncio
async def test_concurrent_generate_variants_respects_token_limit() -> None:
    """With capacity 2, exactly 2 of 5 concurrent calls return variants and 3 are refused by the rate limit."""
    generator, client = _generator("v1\nv2", max_requests_per_minute=2, num_queries=2)

    results = await asyncio.gather(*[generator.generate_variants(f"query {i}") for i in range(5)], return_exceptions=True)

    assert len([r for r in results if r == ["v1", "v2"]]) == 2
    refused = [r for r in results if isinstance(r, RAGFusionFailure)]
    assert len(refused) == 3
    assert {r.reason for r in refused} == {RAGFusionFailureReason.RATE_LIMITED}
    assert len(client.requests) == 2


# ---------------------------------------------------------------------------
# Privacy: fingerprint only, no raw query in logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_no_raw_query_in_log(caplog: pytest.LogCaptureFixture) -> None:
    """The WARNING holds the fingerprint, not the raw query string."""
    from archon_search._privacy import _query_fingerprint

    generator, _client = _generator(failure=LlmCallTimeout("ollama", "no answer"))

    with caplog.at_level(logging.WARNING):
        failure = await _failed(generator, _SECRET_QUERY)

    assert failure.reason is RAGFusionFailureReason.TIMEOUT
    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert _SECRET_QUERY not in log_text, f"Raw query leaked into log: {log_text!r}"
    assert _query_fingerprint(_SECRET_QUERY) in log_text


@pytest.mark.asyncio
async def test_variant_text_never_in_log(caplog: pytest.LogCaptureFixture) -> None:
    """Even a shortfall WARNING never carries the variant text the model produced."""
    generator, _client = _generator("secret variant text alpha", num_queries=3)

    with caplog.at_level(logging.DEBUG):
        await generator.generate_variants("original query")

    assert len(_warnings(caplog)) == 1, "the shortfall must have been logged for this test to mean anything"
    assert "secret variant text alpha" not in " ".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------


def test_is_key_available_reads_the_environment_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    generator, _client = _generator(provider="openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert generator.is_key_available() is False

    monkeypatch.setenv("OPENAI_API_KEY", "sk-late-set")
    assert generator.is_key_available() is True


# ---------------------------------------------------------------------------
# Real LlamaCppClient against a loopback fake llama-server (S2)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rag_fusion_end_to_end_with_llama_cpp_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """S2: a RAGFusionGenerator over a real ``LlamaCppClient`` decomposes the query from the server's two lines."""
    from archon_search.llm_client.llama_cpp_client import LlamaCppClient
    from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies

    keep_loopback_out_of_proxies(monkeypatch)

    with FakeModelServer(reply_text="variant one\nvariant two") as server:
        config = _make_config(provider="llama_cpp", llama_cpp_base_url=server.base_url, num_queries=2)
        generator = RAGFusionGenerator(config, LlamaCppClient(model="local-model", base_url=server.base_url))
        variants = await generator.generate_variants("archon search query")

    assert variants == ["variant one", "variant two"]
    [chat] = server.requests_to("/v1/chat/completions")
    assert chat.body is not None
    assert chat.body["max_tokens"] == RAG_FUSION_TOKENS_PER_QUERY * 2
    assert "archon search query" in chat.body["messages"][0]["content"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rag_fusion_llama_cpp_unreachable_raises_a_provider_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """llama-server unreachable → the client raises, RAG Fusion raises a provider failure with one WARNING."""
    from archon_search.llm_client.llama_cpp_client import LlamaCppClient
    from tests._fake_model_server import keep_loopback_out_of_proxies, unreachable_port

    keep_loopback_out_of_proxies(monkeypatch)

    with unreachable_port() as port, caplog.at_level(logging.WARNING):
        base_url = f"http://127.0.0.1:{port}"
        config = _make_config(provider="llama_cpp", llama_cpp_base_url=base_url)
        generator = RAGFusionGenerator(config, LlamaCppClient(model="local-model", base_url=base_url))
        failures = [await _failed(generator, "archon search query") for _ in range(2)]

    assert {f.reason for f in failures} == {RAGFusionFailureReason.PROVIDER_FAILED}
    assert len([r for r in _warnings(caplog) if r.name == _WARN_LOGGER]) == 1

"""Tests for archon_search.rag_fusion — TDD for RAGFusionGenerator and _query_fingerprint.

All tests use mocked Anthropic clients to avoid real API calls.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from archon_search.config import RAGFusionConfig


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


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


def _make_mock_anthropic_response(text: str) -> Any:
    content_block = MagicMock()
    content_block.text = text
    response = MagicMock()
    response.content = [content_block]
    return response


def _make_mock_anthropic_module(response_text: str | None = None, side_effect: Any = None) -> Any:
    """Return a mock anthropic module with a client whose messages.create returns response_text."""
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    if side_effect is not None:
        mock_client.messages.create = AsyncMock(side_effect=side_effect)
    else:
        mock_client.messages.create = AsyncMock(
            return_value=_make_mock_anthropic_response(response_text or "")
        )
    mock_mod = MagicMock()
    mock_mod.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_mod.APIError = Exception
    return mock_mod


# ---------------------------------------------------------------------------
# _query_fingerprint (from _privacy.py)
# ---------------------------------------------------------------------------


def test_fingerprint_is_16_hex_chars() -> None:
    from archon_search._privacy import _query_fingerprint

    fp = _query_fingerprint("How do I uninstall the CLI?")
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_matches_sha256() -> None:
    from archon_search._privacy import _query_fingerprint

    query = "test query"
    expected = hashlib.sha256(query.encode()).hexdigest()[:16]
    assert _query_fingerprint(query) == expected


def test_fingerprint_importable_from_hyde() -> None:
    """hyde.py must re-export _query_fingerprint from _privacy.py."""
    from archon_search.hyde import _query_fingerprint as hyde_fp
    from archon_search._privacy import _query_fingerprint as privacy_fp

    # Both should return the same result (same function or same implementation)
    assert hyde_fp("test") == privacy_fp("test")


# ---------------------------------------------------------------------------
# RAGFusionDependencyError
# ---------------------------------------------------------------------------


def test_rag_fusion_dependency_error_is_runtime_error() -> None:
    from archon_search.rag_fusion import RAGFusionDependencyError

    assert issubclass(RAGFusionDependencyError, RuntimeError)


# ---------------------------------------------------------------------------
# RAGFusionGenerator._validate_variant
# ---------------------------------------------------------------------------


def test_validate_variant_valid() -> None:
    """Normal text ≤500 chars returns stripped text."""
    from archon_search.rag_fusion import RAGFusionGenerator

    config = _make_config()
    with patch.dict("sys.modules", {"anthropic": _make_mock_anthropic_module()}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        result = gen._validate_variant("  hello world  ")
        importlib.reload(rf_mod)

    assert result == "hello world"


def test_validate_variant_too_long() -> None:
    """Variant of 501 chars returns None."""
    from archon_search.rag_fusion import RAGFusionGenerator

    config = _make_config()
    with patch.dict("sys.modules", {"anthropic": _make_mock_anthropic_module()}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        result = gen._validate_variant("a" * 501)
        importlib.reload(rf_mod)

    assert result is None


def test_validate_variant_exactly_500_chars() -> None:
    """Variant of exactly 500 chars is valid."""
    from archon_search.rag_fusion import RAGFusionGenerator

    config = _make_config()
    with patch.dict("sys.modules", {"anthropic": _make_mock_anthropic_module()}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        result = gen._validate_variant("a" * 500)
        importlib.reload(rf_mod)

    assert result == "a" * 500


def test_validate_variant_control_sequences() -> None:
    """Variant containing \\x00 returns None."""
    from archon_search.rag_fusion import RAGFusionGenerator

    config = _make_config()
    with patch.dict("sys.modules", {"anthropic": _make_mock_anthropic_module()}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        result = gen._validate_variant("hello\x00world")
        importlib.reload(rf_mod)

    assert result is None


def test_validate_variant_del_char_rejected() -> None:
    """Variant containing \\x7F (DEL) returns None."""
    from archon_search.rag_fusion import RAGFusionGenerator

    config = _make_config()
    with patch.dict("sys.modules", {"anthropic": _make_mock_anthropic_module()}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        result = gen._validate_variant("hello\x7fworld")
        importlib.reload(rf_mod)

    assert result is None


def test_validate_variant_c1_control_rejected() -> None:
    """Variant containing \\x80 (C1 control) returns None."""
    from archon_search.rag_fusion import RAGFusionGenerator

    config = _make_config()
    with patch.dict("sys.modules", {"anthropic": _make_mock_anthropic_module()}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        result = gen._validate_variant("hello\x80world")
        importlib.reload(rf_mod)

    assert result is None


def test_validate_variant_tab_allowed() -> None:
    """Variant containing \\t returns stripped text (not rejected)."""
    from archon_search.rag_fusion import RAGFusionGenerator

    config = _make_config()
    with patch.dict("sys.modules", {"anthropic": _make_mock_anthropic_module()}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        result = gen._validate_variant("hello\tworld")
        importlib.reload(rf_mod)

    assert result == "hello\tworld"


def test_validate_variant_newline_in_content() -> None:
    """Single line with trailing \\n strips to valid text."""
    from archon_search.rag_fusion import RAGFusionGenerator

    config = _make_config()
    with patch.dict("sys.modules", {"anthropic": _make_mock_anthropic_module()}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        result = gen._validate_variant("hello world\n")
        importlib.reload(rf_mod)

    assert result == "hello world"


def test_validate_variant_empty_after_strip() -> None:
    """Variant of only whitespace returns None."""
    from archon_search.rag_fusion import RAGFusionGenerator

    config = _make_config()
    with patch.dict("sys.modules", {"anthropic": _make_mock_anthropic_module()}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        result = gen._validate_variant("   \n\t  ")
        importlib.reload(rf_mod)

    assert result is None


# ---------------------------------------------------------------------------
# generate_variants — package not installed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_variants_package_not_installed() -> None:
    """generate_variants() raises RAGFusionDependencyError when anthropic is not installed."""
    with patch.dict("sys.modules", {"anthropic": None}):  # type: ignore[dict-item]
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(_make_config())
        with pytest.raises(rf_mod.RAGFusionDependencyError, match="Install archon-search\\[rag_fusion\\]"):
            await gen.generate_variants("test query")
        importlib.reload(rf_mod)


# ---------------------------------------------------------------------------
# generate_variants — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_variants_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock returns 2 valid variant lines; result is list of 2 strings, each ≤500 chars."""
    config = _make_config(num_queries=2)
    response_text = "variant one alternative query\nvariant two different phrasing"
    mock_mod = _make_mock_anthropic_module(response_text)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_mod}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        result = await gen.generate_variants("original query")
        importlib.reload(rf_mod)

    assert isinstance(result, list)
    assert len(result) == 2
    for v in result:
        assert isinstance(v, str)
        assert len(v) <= 500


# ---------------------------------------------------------------------------
# generate_variants — fallback paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_variants_timeout_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """generate_variants() re-raises asyncio.TimeoutError and logs WARNING when LLM times out.

    BE-2: generate_variants() now re-raises TimeoutError so the pipeline can distinguish
    timeout from empty-variant success. The WARNING is still logged before re-raising.
    """
    config = _make_config()
    mock_mod = _make_mock_anthropic_module(side_effect=asyncio.TimeoutError())

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_mod}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        with caplog.at_level(logging.WARNING, logger="archon_search.rag_fusion"):
            with pytest.raises(asyncio.TimeoutError):
                await gen.generate_variants("original query")
        importlib.reload(rf_mod)

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "WARNING should be logged on timeout before re-raise"
    # Verify 16-char hex fingerprint appears in logs
    log_text = " ".join(r.getMessage() for r in warning_records)
    # Fingerprint must be present (16 hex chars)
    import re

    hex_pattern = re.compile(r"[0-9a-f]{16}")
    assert hex_pattern.search(log_text), f"16-char hex fingerprint not found in log: {log_text!r}"


@pytest.mark.asyncio
async def test_generate_variants_api_error_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate_variants() re-raises when anthropic.APIError is raised.

    BE-2: generate_variants() now re-raises all exceptions (including APIError) so the
    pipeline can detect and signal the failure via rag_fusion_warning.
    """
    config = _make_config()

    class MockAPIError(Exception):
        pass

    mock_mod = _make_mock_anthropic_module(side_effect=MockAPIError("rate limited"))
    mock_mod.APIError = MockAPIError

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_mod}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        with pytest.raises(MockAPIError):
            await gen.generate_variants("test query")
        importlib.reload(rf_mod)


@pytest.mark.asyncio
async def test_generate_variants_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock returns only whitespace lines; result is []."""
    config = _make_config()
    mock_mod = _make_mock_anthropic_module("   \n  \n  ")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_mod}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        result = await gen.generate_variants("test query")
        importlib.reload(rf_mod)

    assert result == []


@pytest.mark.asyncio
async def test_generate_variants_no_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """generate_variants() returns [] and logs WARNING exactly once when no API key is set."""
    config = _make_config()
    mock_mod = _make_mock_anthropic_module("v1\nv2")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with patch.dict("sys.modules", {"anthropic": mock_mod}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        with caplog.at_level(logging.WARNING, logger="archon_search.rag_fusion"):
            result1 = await gen.generate_variants("query 1")
            result2 = await gen.generate_variants("query 2")
        importlib.reload(rf_mod)

    assert result1 == []
    assert result2 == []
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warning_records) == 1, (
        f"Expected exactly 1 WARNING for missing API key, got {len(warning_records)}: "
        + str([r.getMessage() for r in warning_records])
    )


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When token bucket is exhausted, generate_variants() returns [] with WARNING."""
    config = _make_config(max_requests_per_minute=1)
    mock_mod = _make_mock_anthropic_module("v1\nv2")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_mod}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)

        # First call should succeed (1 token available)
        result1 = await gen.generate_variants("query 1")
        # Second call should be rate-limited (0 tokens left)
        with caplog.at_level(logging.WARNING, logger="archon_search.rag_fusion"):
            result2 = await gen.generate_variants("query 2")
        importlib.reload(rf_mod)

    assert result1 == ["v1", "v2"] or len(result1) > 0, "First call should succeed"
    assert result2 == [], "Second call should be rate-limited"
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "WARNING should be logged on rate limit"


@pytest.mark.asyncio
async def test_concurrent_generate_variants_respects_token_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With capacity 2, exactly 2 calls return results and 3 return []."""
    config = _make_config(max_requests_per_minute=2, num_queries=2)
    mock_mod = _make_mock_anthropic_module("v1\nv2")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_mod}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)

        results = await asyncio.gather(*[gen.generate_variants(f"query {i}") for i in range(5)])
        importlib.reload(rf_mod)

    successful = [r for r in results if r]
    rate_limited = [r for r in results if not r]
    assert len(successful) == 2, f"Expected 2 successful calls, got {len(successful)}: {results}"
    assert len(rate_limited) == 3, f"Expected 3 rate-limited calls, got {len(rate_limited)}: {results}"


# ---------------------------------------------------------------------------
# Validation edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_variants_malformed_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock returns 3 lines: 1 valid, 1 too-long, 1 with control seq; result is [valid_one]."""
    config = _make_config(num_queries=3)
    response_text = f"valid variant\n{'x' * 501}\nhello\x00world"
    mock_mod = _make_mock_anthropic_module(response_text)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_mod}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        result = await gen.generate_variants("original query")
        importlib.reload(rf_mod)

    assert result == ["valid variant"]


@pytest.mark.asyncio
async def test_generate_variants_more_than_requested_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock returns 5 lines for num_queries=2; result has ≤2 items."""
    config = _make_config(num_queries=2)
    response_text = "v1\nv2\nv3\nv4\nv5"
    mock_mod = _make_mock_anthropic_module(response_text)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_mod}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        result = await gen.generate_variants("test query")
        importlib.reload(rf_mod)

    assert len(result) <= 2


# ---------------------------------------------------------------------------
# Privacy: fingerprint only, no raw query in logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_no_raw_query_in_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """WARNING log must contain the fingerprint but NOT the raw query string."""
    query = "very-unique-secret-query-rag-12345"
    config = _make_config()
    mock_mod = _make_mock_anthropic_module(side_effect=asyncio.TimeoutError())

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_mod}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        fp = rf_mod._query_fingerprint(query)
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

        with caplog.at_level(logging.WARNING, logger="archon_search.rag_fusion"):
            with pytest.raises(asyncio.TimeoutError):
                await gen.generate_variants(query)
        importlib.reload(rf_mod)

    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert query not in log_text, f"Raw query leaked into log: {log_text!r}"
    assert fp in log_text, f"Fingerprint not found in log: {log_text!r}"


# ---------------------------------------------------------------------------
# Query truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_truncated_to_2000_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Queries longer than 2000 chars are truncated in the prompt sent to the LLM."""
    config = _make_config(num_queries=2)
    prompt_capture: list[dict[str, Any]] = []

    async def capture_create(**kwargs: Any) -> Any:
        prompt_capture.append(kwargs)
        return _make_mock_anthropic_response("v1\nv2")

    mock_mod = MagicMock()
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = capture_create
    mock_mod.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_mod.APIError = Exception

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    long_query = "x" * 3000

    with patch.dict("sys.modules", {"anthropic": mock_mod}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        await gen.generate_variants(long_query)
        importlib.reload(rf_mod)

    assert prompt_capture, "No prompt was captured"
    messages = prompt_capture[0].get("messages", [])
    assert messages, "No messages in captured prompt"
    prompt_content = messages[0]["content"]
    assert "x" * 2001 not in prompt_content, "Query was not truncated to ≤2000 chars"
    assert "x" * 2000 in prompt_content, "Truncated query (2000 chars) not found in prompt"


# ---------------------------------------------------------------------------
# Prompt structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_variants_prompt_contains_query_and_num_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt must contain query text and num_queries; max_tokens = 150 * num_queries."""
    config = _make_config(num_queries=3)
    prompt_capture: list[dict[str, Any]] = []
    max_tokens_capture: list[int] = []

    async def capture_create(**kwargs: Any) -> Any:
        prompt_capture.append(kwargs)
        max_tokens_capture.append(kwargs.get("max_tokens", 0))
        return _make_mock_anthropic_response("v1\nv2\nv3")

    mock_mod = MagicMock()
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = capture_create
    mock_mod.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_mod.APIError = Exception

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_mod}):
        import importlib

        import archon_search.rag_fusion as rf_mod

        importlib.reload(rf_mod)
        gen = rf_mod.RAGFusionGenerator(config)
        await gen.generate_variants("my query")
        importlib.reload(rf_mod)

    assert prompt_capture, "No prompt captured"
    messages = prompt_capture[0].get("messages", [])
    prompt_text = messages[0]["content"]
    assert "my query" in prompt_text, f"Query not found in prompt: {prompt_text!r}"
    assert "3" in prompt_text, f"num_queries=3 not found in prompt: {prompt_text!r}"
    assert max_tokens_capture[0] == 150 * 3, (
        f"Expected max_tokens={150 * 3}, got {max_tokens_capture[0]}"
    )

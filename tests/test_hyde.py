"""Tests for archon_search.hyde — TDD for HyDEGenerator."""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.config import HyDEConfig

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


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


def _make_mock_anthropic_response(text: str) -> Any:
    content_block = MagicMock()
    content_block.text = text
    response = MagicMock()
    response.content = [content_block]
    return response


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
# HyDEGenerator init
# ---------------------------------------------------------------------------


def test_generator_init_without_anthropic_package() -> None:
    """If anthropic is not installed, HyDEGenerator initialises without raising."""
    # G10 BE-1: must also patch the provider module so the try/except ImportError
    # in HyDEGenerator.__init__ fires (the provider is lazy-imported there).
    with patch.dict(  # type: ignore[dict-item]
        "sys.modules",
        {
            "anthropic": None,
            "archon_search.providers.anthropic_provider": None,
        },
    ):
        import archon_search.hyde as hyde_mod
        gen = hyde_mod.HyDEGenerator(_make_config())
        assert gen._provider_available is False


def test_generator_init_with_anthropic_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """If anthropic is available, _provider_available is True."""
    mock_anthropic = MagicMock()
    mock_anthropic.AsyncAnthropic = MagicMock(return_value=MagicMock())
    with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
        import archon_search.hyde as hyde_mod
        gen = hyde_mod.HyDEGenerator(_make_config())
        assert gen._provider_available is True


# ---------------------------------------------------------------------------
# HyDEGenerator.generate — package not installed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_package_not_installed() -> None:
    """generate() raises HyDEDependencyError — the class every transport maps to its fixed 422 — when anthropic is missing."""
    # G10 BE-1: must also patch the provider module so the try/except ImportError
    # in HyDEGenerator.__init__ fires (the provider is lazy-imported there).
    with patch.dict(  # type: ignore[dict-item]
        "sys.modules",
        {
            "anthropic": None,
            "archon_search.providers.anthropic_provider": None,
        },
    ):
        import archon_search.hyde as hyde_mod
        gen = hyde_mod.HyDEGenerator(_make_config())
        with pytest.raises(hyde_mod.HyDEDependencyError, match="Install archon-search\\[hyde\\]"):
            await gen.generate("test query", embedder=_make_embedder())


# ---------------------------------------------------------------------------
# HyDEGenerator.generate — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate() returns a list of floats matching the embedder dimension."""
    dim = 8
    embedder = _make_embedder(dim=dim)
    config = _make_config()

    mock_anthropic_mod = MagicMock()
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(
        return_value=_make_mock_anthropic_response("A hypothetical passage.")
    )
    mock_anthropic_mod.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_anthropic_mod.APIError = Exception

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_anthropic_mod}):
        import archon_search.hyde as hyde_mod
        gen = hyde_mod.HyDEGenerator(config)
        result = await gen.generate("how do I do X?", embedder=embedder)

    assert result is not None
    assert isinstance(result, list)
    assert len(result) == dim
    embedder.embed_one.assert_awaited_once_with("A hypothetical passage.")


# ---------------------------------------------------------------------------
# Fallback paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_timeout_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """generate() returns None and logs WARNING when the LLM call times out."""
    embedder = _make_embedder()
    config = _make_config()

    mock_anthropic_mod = MagicMock()
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=TimeoutError())
    mock_anthropic_mod.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_anthropic_mod.APIError = Exception

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_anthropic_mod}):
        import archon_search.hyde as hyde_mod
        gen = hyde_mod.HyDEGenerator(config)
        with caplog.at_level(logging.WARNING, logger="archon_search.hyde"):
            result = await gen.generate("how do I do X?", embedder=embedder)

    assert result is None
    assert any("WARNING" in r.levelname or r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_generate_api_error_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate() returns None when anthropic.APIError is raised."""
    embedder = _make_embedder()
    config = _make_config()

    class MockAPIError(Exception):
        pass

    mock_anthropic_mod = MagicMock()
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=MockAPIError("rate limited"))
    mock_anthropic_mod.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_anthropic_mod.APIError = MockAPIError

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_anthropic_mod}):
        import archon_search.hyde as hyde_mod
        gen = hyde_mod.HyDEGenerator(config)
        result = await gen.generate("test query", embedder=embedder)

    assert result is None


@pytest.mark.asyncio
async def test_generate_empty_response_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """generate() returns None when the LLM response text is empty after strip."""
    embedder = _make_embedder()
    config = _make_config()

    mock_anthropic_mod = MagicMock()
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(
        return_value=_make_mock_anthropic_response("   ")  # whitespace only
    )
    mock_anthropic_mod.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_anthropic_mod.APIError = Exception

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_anthropic_mod}):
        import archon_search.hyde as hyde_mod
        gen = hyde_mod.HyDEGenerator(config)
        with caplog.at_level(logging.WARNING, logger="archon_search.hyde"):
            result = await gen.generate("test query", embedder=embedder)

    assert result is None


@pytest.mark.asyncio
async def test_generate_no_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """generate() returns None and logs WARNING exactly once when no API key is set."""
    embedder = _make_embedder()
    config = _make_config()

    mock_anthropic_mod = MagicMock()
    mock_anthropic_mod.AsyncAnthropic = MagicMock(return_value=MagicMock())
    mock_anthropic_mod.APIError = Exception

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with patch.dict("sys.modules", {"anthropic": mock_anthropic_mod}):
        import archon_search.hyde as hyde_mod
        gen = hyde_mod.HyDEGenerator(config)

        with caplog.at_level(logging.WARNING, logger="archon_search.hyde"):
            result1 = await gen.generate("query 1", embedder=embedder)
            result2 = await gen.generate("query 2", embedder=embedder)

    assert result1 is None
    assert result2 is None
    # WARNING should be logged exactly once (one-time warning flag)
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warning_records) == 1


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When token bucket is exhausted, generate() returns None with a WARNING."""
    embedder = _make_embedder()
    config = _make_config(max_requests_per_minute=1)

    mock_anthropic_mod = MagicMock()
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(
        return_value=_make_mock_anthropic_response("hypothesis text")
    )
    mock_anthropic_mod.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_anthropic_mod.APIError = Exception

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_anthropic_mod}):
        import archon_search.hyde as hyde_mod
        gen = hyde_mod.HyDEGenerator(config)

        # First call should succeed (1 token available)
        result1 = await gen.generate("query 1", embedder=embedder)
        # Second call should be rate-limited (0 tokens left)
        with caplog.at_level(logging.WARNING, logger="archon_search.hyde"):
            result2 = await gen.generate("query 2", embedder=embedder)

    assert result1 is not None, "First call should succeed with 1 token"
    assert result2 is None, "Second call should be rate-limited"
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_rate_limit_skip_includes_llama_cpp_hyde() -> None:
    """provider='llama_cpp' must skip rate limiting, mirroring ollama/claude_cli."""
    embedder = _make_embedder()
    config = _make_config(provider="llama_cpp", max_requests_per_minute=1)

    mock_provider = MagicMock()
    mock_provider.generate_hypothetical_doc = AsyncMock(return_value="hypothesis text")

    from archon_search.hyde import HyDEGenerator

    gen = HyDEGenerator(config, provider=mock_provider)

    result1 = await gen.generate("query 1", embedder=embedder)
    result2 = await gen.generate("query 2", embedder=embedder)

    assert result1 is not None, "First call should succeed"
    assert result2 is not None, "llama_cpp must skip rate limiting like ollama/claude_cli"


# ---------------------------------------------------------------------------
# Privacy: fingerprint only, no raw query in logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_no_raw_query(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """WARNING log must contain the fingerprint but NOT the raw query string."""
    query = "very-unique-secret-query-12345"
    embedder = _make_embedder()
    config = _make_config()

    mock_anthropic_mod = MagicMock()
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=TimeoutError())
    mock_anthropic_mod.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_anthropic_mod.APIError = Exception

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with patch.dict("sys.modules", {"anthropic": mock_anthropic_mod}):
        import archon_search.hyde as hyde_mod
        gen = hyde_mod.HyDEGenerator(config)
        fp = hyde_mod._query_fingerprint(query)
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

        with caplog.at_level(logging.WARNING, logger="archon_search.hyde"):
            await gen.generate(query, embedder=embedder)

    log_text = " ".join(r.getMessage() for r in caplog.records)
    # Raw query must NOT appear in logs
    assert query not in log_text, f"Raw query leaked into log: {log_text!r}"
    # Fingerprint SHOULD appear in logs
    assert fp in log_text, f"Fingerprint not found in log: {log_text!r}"


# ---------------------------------------------------------------------------
# Query truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_truncated_to_2000_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Queries longer than 2000 chars are truncated in the prompt sent to the LLM."""
    embedder = _make_embedder()
    config = _make_config()

    prompt_capture: list[str] = []

    async def capture_create(**kwargs: Any) -> Any:
        messages = kwargs.get("messages", [])
        if messages:
            prompt_capture.append(messages[0]["content"])
        return _make_mock_anthropic_response("hypothesis text")

    mock_anthropic_mod = MagicMock()
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = capture_create
    mock_anthropic_mod.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_anthropic_mod.APIError = Exception

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    long_query = "x" * 3000

    with patch.dict("sys.modules", {"anthropic": mock_anthropic_mod}):
        import archon_search.hyde as hyde_mod
        gen = hyde_mod.HyDEGenerator(config)
        await gen.generate(long_query, embedder=embedder)

    assert prompt_capture, "No prompt was captured"
    # The prompt should contain at most 2000 chars of the query (it's truncated to 2000)
    assert "x" * 2001 not in prompt_capture[0], (
        "Query was not truncated — prompt contains > 2000 consecutive 'x' chars"
    )
    assert "x" * 2000 in prompt_capture[0], (
        "Truncated query (2000 chars) not found in prompt"
    )

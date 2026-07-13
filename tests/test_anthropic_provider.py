"""Unit tests for AnthropicQueryExpansionProvider (BE-1).

Tests the Anthropic adapter that implements QueryExpansionProvider.
All tests use mock Anthropic clients so the anthropic package is never
accessed against the real API.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.query_expansion_protocol import QueryExpansionProvider


# ---------------------------------------------------------------------------
# Helpers — build a mock anthropic module
# ---------------------------------------------------------------------------

def _make_mock_anthropic(content_text: str = "hypothetical answer") -> MagicMock:
    """Return a fake ``anthropic`` module with a working AsyncAnthropic client."""
    mock_content = SimpleNamespace(text=content_text)
    mock_response = SimpleNamespace(content=[mock_content])

    mock_messages = MagicMock()
    mock_messages.create = AsyncMock(return_value=mock_response)

    mock_client = MagicMock()
    mock_client.messages = mock_messages

    class _FakeAPIError(Exception):
        pass

    mock_module = MagicMock()
    mock_module.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_module.APIError = _FakeAPIError
    return mock_module


# ---------------------------------------------------------------------------
# Protocol conformance tests
# ---------------------------------------------------------------------------


def test_anthropic_hyde_provider_satisfies_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """AnthropicQueryExpansionProvider for HyDE must be an instance of QueryExpansionProvider."""
    mock_anthropic = _make_mock_anthropic()
    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)

    from archon_search.providers.anthropic_provider import AnthropicQueryExpansionProvider  # noqa: PLC0415

    provider = AnthropicQueryExpansionProvider(model="claude-3-5-haiku-latest")
    assert isinstance(provider, QueryExpansionProvider), (
        "AnthropicQueryExpansionProvider must satisfy the QueryExpansionProvider Protocol"
    )


def test_anthropic_rag_fusion_provider_satisfies_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same provider class is used for both HyDE and RAG Fusion — must satisfy the protocol."""
    mock_anthropic = _make_mock_anthropic()
    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)

    from archon_search.providers.anthropic_provider import AnthropicQueryExpansionProvider  # noqa: PLC0415

    provider = AnthropicQueryExpansionProvider(model="claude-3-5-haiku-latest")
    assert isinstance(provider, QueryExpansionProvider), (
        "AnthropicQueryExpansionProvider must satisfy the QueryExpansionProvider Protocol for RAG Fusion"
    )


# ---------------------------------------------------------------------------
# generate_hypothetical_doc tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_generate_hypothetical_doc_returns_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock AsyncAnthropic → provider returns the hypothesis text (not a vector)."""
    mock_anthropic = _make_mock_anthropic("A hypothetical answer about archon search.")
    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from archon_search.providers.anthropic_provider import AnthropicQueryExpansionProvider  # noqa: PLC0415

    provider = AnthropicQueryExpansionProvider(model="claude-3-5-haiku-latest")
    result = await provider.generate_hypothetical_doc("what is archon search?")

    assert isinstance(result, str), f"expected str, got {type(result)}"
    assert "hypothetical" in result.lower() or "archon" in result.lower(), (
        f"expected hypothesis text back, got: {result!r}"
    )


@pytest.mark.asyncio
async def test_anthropic_generate_hypothetical_doc_returns_none_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider returns None (does not raise) when the Anthropic call times out."""
    import asyncio  # noqa: PLC0415

    mock_anthropic = MagicMock()

    class _FakeAPIError(Exception):
        pass

    mock_messages = MagicMock()
    mock_messages.create = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_client = MagicMock()
    mock_client.messages = mock_messages
    mock_anthropic.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_anthropic.APIError = _FakeAPIError

    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)

    from archon_search.providers.anthropic_provider import AnthropicQueryExpansionProvider  # noqa: PLC0415

    provider = AnthropicQueryExpansionProvider(model="claude-3-5-haiku-latest")
    result = await provider.generate_hypothetical_doc("test query")

    assert result is None, f"expected None on timeout, got: {result!r}"


# ---------------------------------------------------------------------------
# decompose_query tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_decompose_query_returns_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock client returns multi-line text → decompose_query returns list[str]."""
    mock_anthropic = _make_mock_anthropic("variant one\nvariant two\nvariant three")
    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from archon_search.providers.anthropic_provider import AnthropicQueryExpansionProvider  # noqa: PLC0415

    provider = AnthropicQueryExpansionProvider(model="claude-3-5-haiku-latest")
    result = await provider.decompose_query("search query", num_queries=3)

    assert isinstance(result, list), f"expected list, got {type(result)}"
    assert len(result) >= 1, "expected at least one variant"
    for item in result:
        assert isinstance(item, str), f"expected str items, got: {type(item)}"


@pytest.mark.asyncio
async def test_anthropic_decompose_query_returns_empty_list_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider returns [] (does not raise) when the Anthropic call times out."""
    import asyncio  # noqa: PLC0415

    mock_anthropic = MagicMock()

    class _FakeAPIError(Exception):
        pass

    mock_messages = MagicMock()
    mock_messages.create = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_client = MagicMock()
    mock_client.messages = mock_messages
    mock_anthropic.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_anthropic.APIError = _FakeAPIError

    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)

    from archon_search.providers.anthropic_provider import AnthropicQueryExpansionProvider  # noqa: PLC0415

    provider = AnthropicQueryExpansionProvider(model="claude-3-5-haiku-latest")
    result = await provider.decompose_query("test query")

    assert result == [], f"expected [] on timeout, got: {result!r}"


@pytest.mark.asyncio
async def test_anthropic_generate_hypothetical_doc_returns_none_when_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider returns None (does not raise) when ANTHROPIC_API_KEY is not set."""
    mock_anthropic = _make_mock_anthropic("some text")
    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from archon_search.providers.anthropic_provider import AnthropicQueryExpansionProvider  # noqa: PLC0415

    provider = AnthropicQueryExpansionProvider(model="claude-3-5-haiku-latest")
    result = await provider.generate_hypothetical_doc("test query")
    assert result is None


@pytest.mark.asyncio
async def test_anthropic_generate_hypothetical_doc_returns_none_on_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider returns None on any non-timeout exception."""
    class _FakeAPIError(Exception):
        pass

    mock_messages = MagicMock()
    mock_messages.create = AsyncMock(side_effect=_FakeAPIError("api error"))
    mock_client = MagicMock()
    mock_client.messages = mock_messages
    mock_anthropic = MagicMock()
    mock_anthropic.AsyncAnthropic = MagicMock(return_value=mock_client)
    mock_anthropic.APIError = _FakeAPIError
    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    from archon_search.providers.anthropic_provider import AnthropicQueryExpansionProvider  # noqa: PLC0415

    provider = AnthropicQueryExpansionProvider(model="claude-3-5-haiku-latest")
    result = await provider.generate_hypothetical_doc("test query")
    assert result is None


@pytest.mark.asyncio
async def test_anthropic_decompose_query_returns_empty_when_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider returns [] (does not raise) when ANTHROPIC_API_KEY is not set."""
    mock_anthropic = _make_mock_anthropic("variant one\nvariant two")
    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    from archon_search.providers.anthropic_provider import AnthropicQueryExpansionProvider  # noqa: PLC0415

    provider = AnthropicQueryExpansionProvider(model="claude-3-5-haiku-latest")
    result = await provider.decompose_query("test query")
    assert result == []

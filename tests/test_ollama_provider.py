"""Unit tests for OllamaQueryExpansionProvider (BE-3).

Tests the Ollama adapter that implements QueryExpansionProvider.
All tests use mock ollama clients so the ollama package is never
accessed against a real server.

Privacy invariant (S14): error-path log messages must contain
``_query_fingerprint(query)`` and must NOT contain the raw query string.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._privacy import _query_fingerprint
from archon_search.query_expansion_protocol import QueryExpansionProvider


# ---------------------------------------------------------------------------
# Helpers — build a mock ollama module
# ---------------------------------------------------------------------------


def _make_mock_ollama(content_text: str = "hypothetical answer") -> MagicMock:
    """Return a fake ``ollama`` module with a working AsyncClient using chat()."""
    # Ollama chat() response shape: response.message.content
    mock_message = SimpleNamespace(content=content_text)
    mock_response = SimpleNamespace(message=mock_message)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=mock_response)

    mock_module = MagicMock()
    mock_module.AsyncClient = MagicMock(return_value=mock_client)
    return mock_module


def _make_mock_ollama_with_client(mock_client: MagicMock) -> MagicMock:
    """Return a fake ``ollama`` module wrapping a given mock client."""
    mock_module = MagicMock()
    mock_module.AsyncClient = MagicMock(return_value=mock_client)
    return mock_module


# ---------------------------------------------------------------------------
# generate_hypothetical_doc tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_generate_hypothetical_doc_returns_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock AsyncClient.chat returns response → provider returns text extracted from message.content."""
    mock_ollama = _make_mock_ollama("A hypothetical passage about the query.")
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    # Force module reimport with patched sys.modules
    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    result = await provider.generate_hypothetical_doc("what is archon search?")

    assert isinstance(result, str), f"expected str, got {type(result)}"
    assert result, "expected non-empty string"
    assert "hypothetical" in result.lower() or "passage" in result.lower(), (
        f"expected hypothesis text back, got: {result!r}"
    )


@pytest.mark.asyncio
async def test_ollama_decompose_query_returns_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock client returns multi-query response → decompose_query returns list[str]."""
    mock_ollama = _make_mock_ollama("variant one\nvariant two\nvariant three")
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    result = await provider.decompose_query("search query", num_queries=3)

    assert isinstance(result, list), f"expected list, got {type(result)}"
    assert len(result) >= 1, "expected at least one variant"
    for item in result:
        assert isinstance(item, str), f"expected str items, got: {type(item)}"
    assert len(result) == 3, f"expected 3 variants, got {len(result)}"


@pytest.mark.asyncio
async def test_ollama_generate_timeout_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AsyncClient.chat raises asyncio.TimeoutError → generate_hypothetical_doc returns None (does not raise)."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_ollama = _make_mock_ollama_with_client(mock_client)
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    result = await provider.generate_hypothetical_doc("test query")

    assert result is None, f"expected None on TimeoutError, got: {result!r}"


@pytest.mark.asyncio
async def test_ollama_generate_arbitrary_exception_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock AsyncClient.chat raises RuntimeError → generate_hypothetical_doc returns None.

    Proves the 'never raises' C1 contract for non-timeout errors (DA-TEST-C1-I-6).
    """
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=RuntimeError("sdk error"))
    mock_ollama = _make_mock_ollama_with_client(mock_client)
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    result = await provider.generate_hypothetical_doc("test query")

    assert result is None, f"expected None on RuntimeError, got: {result!r}"


@pytest.mark.asyncio
async def test_ollama_decompose_timeout_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AsyncClient.chat raises asyncio.TimeoutError → decompose_query returns []."""
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=asyncio.TimeoutError)
    mock_ollama = _make_mock_ollama_with_client(mock_client)
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    result = await provider.decompose_query("test query")

    assert result == [], f"expected [] on TimeoutError, got: {result!r}"


@pytest.mark.asyncio
async def test_ollama_decompose_arbitrary_exception_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock raises ConnectionError → decompose_query returns [].

    Proves the 'never raises' C1 contract for non-timeout errors (DA-TEST-C1-I-6).
    """
    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=ConnectionError("connection refused"))
    mock_ollama = _make_mock_ollama_with_client(mock_client)
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    result = await provider.decompose_query("test query")

    assert result == [], f"expected [] on ConnectionError, got: {result!r}"


# ---------------------------------------------------------------------------
# Rate limit tests (S11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_no_rate_limit_enforcement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S11: After many calls in a tight loop, all succeed — rate limit NOT applied.

    OllamaQueryExpansionProvider has no _rpm_tokens or rate-limit state.
    The generator's call site (Root-4) skips the token bucket for Ollama.
    """
    mock_ollama = _make_mock_ollama("some response")
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")

    # Assert the provider has no rate-limit state attributes
    assert not hasattr(provider, "_rpm_tokens"), (
        "OllamaQueryExpansionProvider must not have _rpm_tokens (no rate limiting)"
    )
    assert not hasattr(provider, "_rpm_lock"), (
        "OllamaQueryExpansionProvider must not have _rpm_lock (no rate limiting)"
    )

    # Run 100 calls — all must succeed (return a str, not None)
    successes = 0
    for _ in range(100):
        result = await provider.generate_hypothetical_doc("test query")
        if result is not None:
            successes += 1

    assert successes == 100, (
        f"Expected all 100 calls to succeed without rate limiting, got {successes} successes"
    )


# ---------------------------------------------------------------------------
# Privacy invariant tests (S14)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_error_path_uses_query_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S14: Error-path logs contain _query_fingerprint(query) and NOT the raw query string.

    Both conditions required per DA-TEST-C1-I-5.
    """
    raw_query = "this is the raw secret query text that must not appear in logs"
    expected_fingerprint = _query_fingerprint(raw_query)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(side_effect=RuntimeError("simulated error"))
    mock_ollama = _make_mock_ollama_with_client(mock_client)
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")

    with caplog.at_level(logging.WARNING, logger="archon_search.providers.ollama_provider"):
        result = await provider.generate_hypothetical_doc(raw_query)

    assert result is None, "expected None on error"
    assert caplog.text, "expected at least one warning log message"
    assert expected_fingerprint in caplog.text, (
        f"Expected fingerprint {expected_fingerprint!r} in log, got: {caplog.text!r}"
    )
    assert raw_query not in caplog.text, (
        f"Raw query must NOT appear in logs (privacy invariant S14), but found it in: {caplog.text!r}"
    )


# ---------------------------------------------------------------------------
# Protocol conformance tests (C1-I-20)
# ---------------------------------------------------------------------------


def test_ollama_provider_satisfies_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1-I-20: OllamaQueryExpansionProvider is an instance of QueryExpansionProvider (runtime_checkable)."""
    mock_ollama = _make_mock_ollama("text")
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    assert isinstance(provider, QueryExpansionProvider), (
        "OllamaQueryExpansionProvider must satisfy the QueryExpansionProvider protocol"
    )


# ---------------------------------------------------------------------------
# Malformed response tests (C1-I-21)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_generate_malformed_response_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1-I-21: response.message exists but has no 'content' attribute → generate_hypothetical_doc returns None."""
    # SimpleNamespace with .message but no .content attribute on message
    mock_message = SimpleNamespace()  # no 'content' attr → AttributeError on access
    mock_response = SimpleNamespace(message=mock_message)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=mock_response)
    mock_ollama = _make_mock_ollama_with_client(mock_client)
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    result = await provider.generate_hypothetical_doc("test query")

    assert result is None, f"expected None on malformed response, got: {result!r}"


# ---------------------------------------------------------------------------
# Package-absent tests (C1-I-22)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_generate_package_absent_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C1-I-22: When the ollama package is absent, generate_hypothetical_doc returns None and logs a warning with fingerprint."""
    raw_query = "test query for absent package"
    expected_fingerprint = _query_fingerprint(raw_query)

    # Simulate absent package: sys.modules['ollama'] = None causes ImportError on import
    monkeypatch.setitem(sys.modules, "ollama", None)
    monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider", raising=False)

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")

    with caplog.at_level(logging.WARNING, logger="archon_search.providers.ollama_provider"):
        result = await provider.generate_hypothetical_doc(raw_query)

    assert result is None, f"expected None when ollama package is absent, got: {result!r}"
    assert expected_fingerprint in caplog.text, (
        f"Expected fingerprint {expected_fingerprint!r} in log, got: {caplog.text!r}"
    )
    assert raw_query not in caplog.text, (
        f"Raw query must NOT appear in logs (privacy invariant S14), but found it in: {caplog.text!r}"
    )


# ---------------------------------------------------------------------------
# decompose_query filtering tests (C1-I-23)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_decompose_blank_lines_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1-I-23: Blank lines in the response are excluded from the result."""
    mock_ollama = _make_mock_ollama("\nvariant one\n\nvariant two\n\nvariant three\n")
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    result = await provider.decompose_query("search query", num_queries=3)

    assert "" not in result, f"blank lines must be excluded, got: {result!r}"
    assert all(v.strip() for v in result), f"all variants must be non-blank, got: {result!r}"
    assert len(result) == 3, f"expected 3 non-blank variants, got {len(result)}: {result!r}"


@pytest.mark.asyncio
async def test_ollama_decompose_long_lines_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1-I-23: Lines exceeding 500 characters are excluded from the result."""
    long_line = "x" * 501
    mock_ollama = _make_mock_ollama(f"variant one\n{long_line}\nvariant two")
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    result = await provider.decompose_query("search query", num_queries=3)

    assert long_line not in result, f"long line must be excluded, got: {result!r}"
    assert all(len(v) <= 500 for v in result), f"all variants must be <=500 chars, got: {result!r}"


@pytest.mark.asyncio
async def test_ollama_decompose_control_chars_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1-I-23: Lines containing control characters are excluded from the result."""
    control_line = "bad\x00line"
    mock_ollama = _make_mock_ollama(f"variant one\n{control_line}\nvariant two")
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    result = await provider.decompose_query("search query", num_queries=3)

    assert control_line not in result, f"control-char line must be excluded, got: {result!r}"
    assert not any("\x00" in v for v in result), (
        f"no result may contain control chars, got: {result!r}"
    )


@pytest.mark.asyncio
async def test_ollama_decompose_truncates_to_num_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C1-I-23: Response with 5 valid lines truncated to num_queries=3."""
    mock_ollama = _make_mock_ollama(
        "variant one\nvariant two\nvariant three\nvariant four\nvariant five"
    )
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    result = await provider.decompose_query("search query", num_queries=3)

    assert len(result) == 3, f"expected exactly 3 variants (truncated from 5), got {len(result)}: {result!r}"


# ---------------------------------------------------------------------------
# Non-str content normalization tests (C2-I-2, C2-I-20)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_generate_non_str_content_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2-I-2: response.message.content is a truthy non-str → generate_hypothetical_doc returns None.

    Proves the 'never raises' C1 contract for the normalization path:
    a list, int, or other non-str would raise AttributeError on .strip() without this guard.
    """
    # content is a list — truthy, non-str
    mock_message = SimpleNamespace(content=[123, "some", "text"])
    mock_response = SimpleNamespace(message=mock_message)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=mock_response)
    mock_ollama = _make_mock_ollama_with_client(mock_client)
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    result = await provider.generate_hypothetical_doc("test query")

    assert result is None, f"expected None for non-str content, got: {result!r}"


@pytest.mark.asyncio
async def test_ollama_decompose_non_str_content_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2-I-20: response.message.content is a truthy non-str → decompose_query returns [].

    Proves the 'never raises' C1 contract: without the isinstance guard,
    a list.split() would raise AttributeError, escaping the method.
    """
    # content is a list — truthy, non-str, would raise AttributeError on .split()
    mock_message = SimpleNamespace(content=[123, "some", "text"])
    mock_response = SimpleNamespace(message=mock_message)

    mock_client = MagicMock()
    mock_client.chat = AsyncMock(return_value=mock_response)
    mock_ollama = _make_mock_ollama_with_client(mock_client)
    monkeypatch.setitem(sys.modules, "ollama", mock_ollama)

    if "archon_search.providers.ollama_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.ollama_provider")

    from archon_search.providers.ollama_provider import OllamaQueryExpansionProvider  # noqa: PLC0415

    provider = OllamaQueryExpansionProvider(model="llama3.2")
    result = await provider.decompose_query("test query")

    assert result == [], f"expected [] for non-str content, got: {result!r}"

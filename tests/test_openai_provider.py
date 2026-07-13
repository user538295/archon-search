"""Unit tests for OpenAIQueryExpansionProvider (BE-6).

Tests the OpenAI adapter that implements QueryExpansionProvider.
All tests use mock openai clients so the openai package is never
accessed against a real server.

Privacy invariant (S14): error-path log messages must contain
``_query_fingerprint(query)`` and must NOT contain the raw query string.
"""
from __future__ import annotations

import logging
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search._privacy import _query_fingerprint
from archon_search.query_expansion_protocol import QueryExpansionProvider


# ---------------------------------------------------------------------------
# Helpers — build a mock openai module
# ---------------------------------------------------------------------------


def _make_mock_openai(content_text: str = "hypothetical answer") -> MagicMock:
    """Return a fake ``openai`` module with a working AsyncOpenAI using chat.completions.create().

    OpenAI chat response shape: response.choices[0].message.content
    """
    mock_message = SimpleNamespace(content=content_text)
    mock_choice = SimpleNamespace(message=mock_message)
    mock_response = SimpleNamespace(choices=[mock_choice])

    mock_completions = MagicMock()
    mock_completions.create = AsyncMock(return_value=mock_response)

    mock_chat = MagicMock()
    mock_chat.completions = mock_completions

    mock_client = MagicMock()
    mock_client.chat = mock_chat

    mock_module = MagicMock()
    mock_module.AsyncOpenAI = MagicMock(return_value=mock_client)
    return mock_module


def _make_mock_openai_with_client(mock_client: MagicMock) -> MagicMock:
    """Return a fake ``openai`` module wrapping a given mock client."""
    mock_module = MagicMock()
    mock_module.AsyncOpenAI = MagicMock(return_value=mock_client)
    return mock_module


def _make_mock_client_raising(exc: Exception) -> MagicMock:
    """Return a mock client whose chat.completions.create raises exc."""
    mock_completions = MagicMock()
    mock_completions.create = AsyncMock(side_effect=exc)

    mock_chat = MagicMock()
    mock_chat.completions = mock_completions

    mock_client = MagicMock()
    mock_client.chat = mock_chat
    return mock_client


# ---------------------------------------------------------------------------
# generate_hypothetical_doc tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_generate_hypothetical_doc_returns_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock AsyncOpenAI.chat.completions.create returns response → provider returns hypothesis text.

    DA-TEST-C1-I-3: use monkeypatch.setitem, not direct assignment.
    Response shape: choices[0].message.content.
    """
    mock_openai = _make_mock_openai("A hypothetical passage about the query.")
    monkeypatch.setitem(sys.modules, "openai", mock_openai)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    if "archon_search.providers.openai_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.openai_provider")

    from archon_search.providers.openai_provider import OpenAIQueryExpansionProvider  # noqa: PLC0415

    provider = OpenAIQueryExpansionProvider(model="gpt-4o-mini")
    result = await provider.generate_hypothetical_doc("what is archon search?")

    assert isinstance(result, str), f"expected str, got {type(result)}"
    assert result, "expected non-empty string"
    assert "hypothetical" in result.lower() or "passage" in result.lower(), (
        f"expected hypothesis text back, got: {result!r}"
    )


@pytest.mark.asyncio
async def test_openai_decompose_query_returns_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock client returns multi-query response → decompose_query returns list[str]."""
    mock_openai = _make_mock_openai("variant one\nvariant two\nvariant three")
    monkeypatch.setitem(sys.modules, "openai", mock_openai)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    if "archon_search.providers.openai_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.openai_provider")

    from archon_search.providers.openai_provider import OpenAIQueryExpansionProvider  # noqa: PLC0415

    provider = OpenAIQueryExpansionProvider(model="gpt-4o-mini")
    result = await provider.decompose_query("search query", num_queries=3)

    assert isinstance(result, list), f"expected list, got {type(result)}"
    assert len(result) >= 1, "expected at least one variant"
    for item in result:
        assert isinstance(item, str), f"expected str items, got: {type(item)}"
    assert len(result) == 3, f"expected 3 variants, got {len(result)}"


@pytest.mark.asyncio
async def test_openai_generate_arbitrary_exception_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock AsyncOpenAI.chat.completions.create raises RuntimeError → returns None.

    Proves the 'never raises' C1 contract for non-timeout errors (DA-TEST-C1-I-6).
    """
    mock_client = _make_mock_client_raising(RuntimeError("sdk error"))
    mock_openai = _make_mock_openai_with_client(mock_client)
    monkeypatch.setitem(sys.modules, "openai", mock_openai)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    if "archon_search.providers.openai_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.openai_provider")

    from archon_search.providers.openai_provider import OpenAIQueryExpansionProvider  # noqa: PLC0415

    provider = OpenAIQueryExpansionProvider(model="gpt-4o-mini")
    result = await provider.generate_hypothetical_doc("test query")

    assert result is None, f"expected None on RuntimeError, got: {result!r}"
    mock_client.chat.completions.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_openai_decompose_arbitrary_exception_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock raises RuntimeError → decompose_query returns [].

    Proves the 'never raises' C1 contract for non-timeout errors (DA-TEST-C1-I-6).
    """
    mock_client = _make_mock_client_raising(RuntimeError("network error"))
    mock_openai = _make_mock_openai_with_client(mock_client)
    monkeypatch.setitem(sys.modules, "openai", mock_openai)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    if "archon_search.providers.openai_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.openai_provider")

    from archon_search.providers.openai_provider import OpenAIQueryExpansionProvider  # noqa: PLC0415

    provider = OpenAIQueryExpansionProvider(model="gpt-4o-mini")
    result = await provider.decompose_query("test query")

    assert result == [], f"expected [] on RuntimeError, got: {result!r}"
    mock_client.chat.completions.create.assert_awaited_once()


# ---------------------------------------------------------------------------
# Rate limit tests (S12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_adapter_has_no_rate_limiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter has no built-in rate limiting — all calls succeed regardless of call frequency.

    Rate limiting (token bucket, RPM enforcement) is in the generator's call site (Root-4),
    NOT in the adapter. This test verifies OpenAIQueryExpansionProvider has no ``_rpm_tokens``
    or ``_rpm_lock`` attributes and that all calls return a result without being throttled.
    """
    mock_openai = _make_mock_openai("some response")
    monkeypatch.setitem(sys.modules, "openai", mock_openai)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    if "archon_search.providers.openai_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.openai_provider")

    from archon_search.providers.openai_provider import OpenAIQueryExpansionProvider  # noqa: PLC0415

    provider = OpenAIQueryExpansionProvider(model="gpt-4o-mini")

    # Adapter must NOT have rate-limit state (Root-4: bucket is in the generator)
    assert not hasattr(provider, "_rpm_tokens"), (
        "OpenAIQueryExpansionProvider must not have _rpm_tokens (rate limiting is in the generator)"
    )
    assert not hasattr(provider, "_rpm_lock"), (
        "OpenAIQueryExpansionProvider must not have _rpm_lock (rate limiting is in the generator)"
    )

    # Many calls — all must succeed (return a str, not None)
    successes = 0
    for _ in range(10):
        result = await provider.generate_hypothetical_doc("test query")
        if result is not None:
            successes += 1

    assert successes == 10, (
        f"Expected all 10 calls to succeed (no adapter-level rate limiting), got {successes} successes"
    )


# ---------------------------------------------------------------------------
# Package-absent tests (S10)
# ---------------------------------------------------------------------------


def test_openai_package_absent_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """S10: sys.modules['openai'] = None + provider='openai' → create_app raises ConfigError.

    Requires lazy import — DA-TEST-C1-I-4.
    """
    import os

    monkeypatch.setitem(sys.modules, "openai", None)
    monkeypatch.delitem(sys.modules, "archon_search.providers.openai_provider", raising=False)

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "test-key-for-unit-test")

    from archon_search.config import ConfigError, SearchConfig, HyDEConfig  # noqa: PLC0415

    config = SearchConfig()
    config.hyde = HyDEConfig(provider="openai", model="gpt-4o-mini")

    # _check_provider_deps must raise ConfigError when openai package absent
    from archon_search.server.app import _check_provider_deps  # noqa: PLC0415

    with pytest.raises(ConfigError, match="openai"):
        _check_provider_deps(config)


# ---------------------------------------------------------------------------
# Privacy invariant tests (S14)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_error_path_uses_query_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S14: Error-path logs contain _query_fingerprint(query) and NOT the raw query string.

    Both conditions required per DA-TEST-C1-I-5.
    """
    raw_query = "this is the raw secret query text that must not appear in logs"
    expected_fingerprint = _query_fingerprint(raw_query)

    mock_client = _make_mock_client_raising(RuntimeError("simulated error"))
    mock_openai = _make_mock_openai_with_client(mock_client)
    monkeypatch.setitem(sys.modules, "openai", mock_openai)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    if "archon_search.providers.openai_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.openai_provider")

    from archon_search.providers.openai_provider import OpenAIQueryExpansionProvider  # noqa: PLC0415

    provider = OpenAIQueryExpansionProvider(model="gpt-4o-mini")

    with caplog.at_level(logging.WARNING, logger="archon_search.providers.openai_provider"):
        result = await provider.generate_hypothetical_doc(raw_query)

    assert result is None, "expected None on error"
    mock_client.chat.completions.create.assert_awaited_once()
    assert caplog.text, "expected at least one warning log message"
    assert expected_fingerprint in caplog.text, (
        f"Expected fingerprint {expected_fingerprint!r} in log, got: {caplog.text!r}"
    )
    assert raw_query not in caplog.text, (
        f"Raw query must NOT appear in logs (privacy invariant S14), but found it in: {caplog.text!r}"
    )


# ---------------------------------------------------------------------------
# Protocol conformance tests
# ---------------------------------------------------------------------------


def test_openai_provider_satisfies_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAIQueryExpansionProvider is an instance of QueryExpansionProvider (runtime_checkable)."""
    mock_openai = _make_mock_openai("text")
    monkeypatch.setitem(sys.modules, "openai", mock_openai)

    if "archon_search.providers.openai_provider" in sys.modules:
        monkeypatch.delitem(sys.modules, "archon_search.providers.openai_provider")

    from archon_search.providers.openai_provider import OpenAIQueryExpansionProvider  # noqa: PLC0415

    provider = OpenAIQueryExpansionProvider(model="gpt-4o-mini")
    assert isinstance(provider, QueryExpansionProvider), (
        "OpenAIQueryExpansionProvider must satisfy the QueryExpansionProvider protocol"
    )

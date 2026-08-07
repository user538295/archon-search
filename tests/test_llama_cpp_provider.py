"""Unit + integration tests for LlamaCppQueryExpansionProvider (BE-2).

Tests the llama.cpp (llama-server) adapter that implements QueryExpansionProvider
over raw ``httpx`` — httpx is a core dependency, no lazy-import guard needed
(unlike ollama_provider/openai_provider which lazy-import their SDKs).

Mocking convention for this repo (per the task-breakdown "Grounding note"):
patch ``archon_search.providers.llama_cpp_provider.httpx.AsyncClient`` directly
— not ``httpx.MockTransport``.

Privacy invariant (S15a): error-path log messages must contain
``_query_fingerprint(query)`` and must NEVER contain the raw query string —
verified structurally by tests/test_no_query_log_in_llama_cpp_provider.py.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from archon_search.query_expansion_protocol import QueryExpansionProvider

_BASE_URL = "http://localhost:8080"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(json_body: dict, status_code: int = 200) -> MagicMock:
    """Return a MagicMock standing in for an ``httpx.Response``.

    ``raise_for_status`` is a no-op for 2xx; callers needing a failing status
    should set ``response.raise_for_status.side_effect`` themselves.
    """
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=json_body)
    response.raise_for_status = MagicMock()
    return response


def _make_async_client_cls(response: MagicMock | None = None, post_side_effect=None) -> MagicMock:
    """Return a mock class standing in for ``httpx.AsyncClient``.

    The returned mock supports ``async with httpx.AsyncClient(...) as client:``
    and records the constructor kwargs on ``mock_cls.call_args`` so tests can
    assert on ``base_url``/``timeout``.
    """
    mock_client = AsyncMock()
    if post_side_effect is not None:
        mock_client.post = AsyncMock(side_effect=post_side_effect)
    else:
        mock_client.post = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_cls = MagicMock(return_value=mock_client)
    return mock_cls


def _choices_body(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_implements_query_expansion_provider_protocol() -> None:
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    provider = LlamaCppQueryExpansionProvider(model="local-model")
    assert isinstance(provider, QueryExpansionProvider)


def test_is_key_available_always_true() -> None:
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    provider = LlamaCppQueryExpansionProvider(model="local-model")
    assert provider.is_key_available() is True


def test_default_base_url_is_llama_cpp_default() -> None:
    """S11 sibling: constructing without base_url defaults to LLAMA_CPP_BASE_URL_DEFAULT."""
    from archon_search.config import LLAMA_CPP_BASE_URL_DEFAULT
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    provider = LlamaCppQueryExpansionProvider(model="local-model")
    assert provider._base_url == LLAMA_CPP_BASE_URL_DEFAULT  # noqa: SLF001


# ---------------------------------------------------------------------------
# generate_hypothetical_doc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_hypothetical_doc_returns_content() -> None:
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    response = _make_response(_choices_body("A hypothetical passage about the query."))
    mock_cls = _make_async_client_cls(response=response)

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        result = await provider.generate_hypothetical_doc("what is archon search?")

    assert isinstance(result, str)
    assert result == "A hypothetical passage about the query."


@pytest.mark.asyncio
async def test_generate_hypothetical_doc_returns_none_on_connect_error() -> None:
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    mock_cls = _make_async_client_cls(post_side_effect=httpx.ConnectError("connection refused"))

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        result = await provider.generate_hypothetical_doc("what is archon search?")

    assert result is None


@pytest.mark.asyncio
async def test_generate_hypothetical_doc_returns_none_on_503() -> None:
    """S8: llama-server returns 503 'Loading model' when no model is loaded."""
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    request = httpx.Request("POST", f"{_BASE_URL}/v1/chat/completions")
    response = _make_response({"error": {"code": 503, "message": "Loading model"}}, status_code=503)
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Server error '503 Service Unavailable'", request=request, response=httpx.Response(503, request=request)
    )
    mock_cls = _make_async_client_cls(response=response)

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        result = await provider.generate_hypothetical_doc("what is archon search?")

    assert result is None


@pytest.mark.asyncio
async def test_generate_hypothetical_doc_returns_none_on_timeout() -> None:
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    mock_cls = _make_async_client_cls(post_side_effect=httpx.TimeoutException("timed out"))

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        result = await provider.generate_hypothetical_doc("what is archon search?")

    assert result is None


@pytest.mark.asyncio
async def test_generate_hypothetical_doc_returns_none_on_json_decode_error() -> None:
    """A 200 response with a malformed (non-JSON) body must not raise json.JSONDecodeError."""
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json = MagicMock(side_effect=json.JSONDecodeError("Expecting value", "", 0))
    mock_cls = _make_async_client_cls(response=response)

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        result = await provider.generate_hypothetical_doc("what is archon search?")

    assert result is None


@pytest.mark.asyncio
async def test_decompose_query_returns_empty_on_json_decode_error() -> None:
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    response = MagicMock()
    response.status_code = 200
    response.raise_for_status = MagicMock()
    response.json = MagicMock(side_effect=json.JSONDecodeError("Expecting value", "", 0))
    mock_cls = _make_async_client_cls(response=response)

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        result = await provider.decompose_query("what is archon search?")

    assert result == []


# ---------------------------------------------------------------------------
# decompose_query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_query_returns_list() -> None:
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    response = _make_response(_choices_body("variant one\nvariant two\nvariant three"))
    mock_cls = _make_async_client_cls(response=response)

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        result = await provider.decompose_query("what is archon search?", num_queries=3)

    assert isinstance(result, list)
    assert result == ["variant one", "variant two", "variant three"]


@pytest.mark.asyncio
async def test_decompose_query_returns_empty_on_timeout() -> None:
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    mock_cls = _make_async_client_cls(post_side_effect=httpx.TimeoutException("timed out"))

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        result = await provider.decompose_query("what is archon search?")

    assert result == []


@pytest.mark.asyncio
async def test_decompose_query_returns_empty_on_connect_error() -> None:
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    mock_cls = _make_async_client_cls(post_side_effect=httpx.ConnectError("connection refused"))

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        result = await provider.decompose_query("what is archon search?")

    assert result == []


# ---------------------------------------------------------------------------
# Normalisation guard (dict access, not SDK attribute access)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normalise_guard_on_missing_choices_key_hyde() -> None:
    """Absent 'choices' key must raise neither KeyError nor IndexError — returns None."""
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    response = _make_response({"unexpected": "shape"})
    mock_cls = _make_async_client_cls(response=response)

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        result = await provider.generate_hypothetical_doc("what is archon search?")

    assert result is None


@pytest.mark.asyncio
async def test_normalise_guard_on_missing_choices_key_rag_fusion() -> None:
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    response = _make_response({"unexpected": "shape"})
    mock_cls = _make_async_client_cls(response=response)

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        result = await provider.decompose_query("what is archon search?")

    assert result == []


@pytest.mark.asyncio
async def test_normalise_guard_on_empty_choices_list() -> None:
    """Empty 'choices' list must trigger the IndexError branch, not raise."""
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    response = _make_response({"choices": []})
    mock_cls = _make_async_client_cls(response=response)

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        result = await provider.generate_hypothetical_doc("what is archon search?")

    assert result is None


@pytest.mark.asyncio
async def test_normalise_guard_on_non_str_content() -> None:
    """Non-str 'content' (e.g. int) must trigger the TypeError branch, not raise."""
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    response = _make_response({"choices": [{"message": {"content": 12345}}]})
    mock_cls = _make_async_client_cls(response=response)

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        result = await provider.generate_hypothetical_doc("what is archon search?")

    assert result is None


# ---------------------------------------------------------------------------
# S13 — no key checks, no external SDK instantiation, all traffic to localhost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_anthropic_openai_client_instantiated() -> None:
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    response = _make_response(_choices_body("a hypothesis"))
    mock_cls = _make_async_client_cls(response=response)

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)

    with (
        patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls),
        patch("anthropic.AsyncAnthropic") as mock_anthropic,
        patch("openai.AsyncOpenAI") as mock_openai,
    ):
        result = await provider.generate_hypothetical_doc("what is archon search?")

    assert result == "a hypothesis"
    mock_anthropic.assert_not_called()
    mock_openai.assert_not_called()


@pytest.mark.asyncio
async def test_httpx_target_base_url_matches_configured_llama_cpp_base_url() -> None:
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    custom_base_url = "http://localhost:9999"
    response = _make_response(_choices_body("a hypothesis"))
    mock_cls = _make_async_client_cls(response=response)

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=custom_base_url)
    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        await provider.generate_hypothetical_doc("what is archon search?")

    assert mock_cls.call_args is not None
    _, kwargs = mock_cls.call_args
    assert kwargs.get("base_url") == custom_base_url


# ---------------------------------------------------------------------------
# End-to-end via HyDEGenerator / RAGFusionGenerator (S1, S2)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hyde_end_to_end_with_llama_cpp() -> None:
    """S1: HyDEGenerator wired with LlamaCppQueryExpansionProvider produces a vector, hyde_applied=True."""
    from archon_search.config import HyDEConfig
    from archon_search.hyde import HyDEGenerator, resolve_hyde_vector
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    response = _make_response(_choices_body("A hypothetical passage about archon search."))
    mock_cls = _make_async_client_cls(response=response)

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    config = HyDEConfig(enabled=True, provider="llama_cpp", llama_cpp_base_url=_BASE_URL)

    fake_embedder = MagicMock()
    fake_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3])

    generator = HyDEGenerator(embedder=fake_embedder, config=config, provider=provider)

    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        vector, hyde_applied = await resolve_hyde_vector("archon search query", True, generator, config)

    assert hyde_applied is True
    assert vector == [0.1, 0.2, 0.3]
    fake_embedder.embed_one.assert_awaited_once_with("A hypothetical passage about archon search.")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rag_fusion_end_to_end_with_llama_cpp() -> None:
    """S2: RAGFusionGenerator wired with LlamaCppQueryExpansionProvider decomposes the query."""
    from archon_search.config import RAGFusionConfig
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider
    from archon_search.rag_fusion import RAGFusionGenerator

    response = _make_response(_choices_body("variant one\nvariant two"))
    mock_cls = _make_async_client_cls(response=response)

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    config = RAGFusionConfig(
        enabled=True, provider="llama_cpp", llama_cpp_base_url=_BASE_URL, num_queries=2
    )

    generator = RAGFusionGenerator(config=config, provider=provider)

    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        variants = await generator.generate_variants("archon search query")

    assert variants == ["variant one", "variant two"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_llama_cpp_fallback_on_unreachable() -> None:
    """S6: llama-server unreachable → provider returns None, HyDE falls back with no exception."""
    from archon_search.config import HyDEConfig
    from archon_search.hyde import HyDEGenerator, resolve_hyde_vector
    from archon_search.providers.llama_cpp_provider import LlamaCppQueryExpansionProvider

    mock_cls = _make_async_client_cls(post_side_effect=httpx.ConnectError("connection refused"))

    provider = LlamaCppQueryExpansionProvider(model="local-model", base_url=_BASE_URL)
    config = HyDEConfig(enabled=True, provider="llama_cpp", llama_cpp_base_url=_BASE_URL)

    fake_embedder = MagicMock()
    fake_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3])

    generator = HyDEGenerator(embedder=fake_embedder, config=config, provider=provider)

    with patch("archon_search.providers.llama_cpp_provider.httpx.AsyncClient", mock_cls):
        vector, hyde_applied = await resolve_hyde_vector("archon search query", True, generator, config)

    assert vector is None
    assert hyde_applied is False
    fake_embedder.embed_one.assert_not_awaited()

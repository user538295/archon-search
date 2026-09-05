"""Unit tests for OllamaEnrichmentClient (LLCP BE-6).

Tests the Ollama enrichment adapter that implements LLMEnrichmentClientProtocol
over raw ``httpx`` — deliberately NOT the ``ollama`` SDK used by
``providers/ollama_provider.py`` (query-expansion adapter). Narrowed to
community summarisation only (BE-17).

Mocking convention: patch ``archon_search.enrichment.ollama.httpx.AsyncClient``
directly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from archon_search.graph_enrichment_protocol import LLMEnrichmentClientProtocol

_BASE_URL = "http://localhost:11434"


def _make_config() -> MagicMock:
    config = MagicMock()
    config.ollama_base_url = _BASE_URL
    config.extraction_timeout_seconds = 30.0
    config.extraction_token_budget = 1024
    return config


def _make_client(config: MagicMock | None = None):
    from archon_search.enrichment.ollama import OllamaEnrichmentClient

    return OllamaEnrichmentClient(model="local-model", config=config or _make_config())


def _make_response(json_body: dict, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=json_body)
    response.raise_for_status = MagicMock()
    return response


def _make_async_client_cls(response: MagicMock | None = None, post_side_effect=None) -> MagicMock:
    mock_client = AsyncMock()
    if post_side_effect is not None:
        mock_client.post = AsyncMock(side_effect=post_side_effect)
    else:
        mock_client.post = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=mock_client)


def _choices_body(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_implements_llm_enrichment_client_protocol() -> None:
    client = _make_client()
    assert isinstance(client, LLMEnrichmentClientProtocol)


@pytest.mark.asyncio
async def test_ollama_enrichment_summarize_happy_path() -> None:
    response = _make_response(_choices_body("A community summary."))
    mock_cls = _make_async_client_cls(response=response)

    client = _make_client()
    with patch("archon_search.enrichment.ollama.httpx.AsyncClient", mock_cls):
        result = await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])

    assert result == "A community summary."


@pytest.mark.asyncio
async def test_ollama_enrichment_transport_raises() -> None:
    mock_cls = _make_async_client_cls(post_side_effect=httpx.ConnectError("connection refused"))

    client = _make_client()
    with (
        patch("archon_search.enrichment.ollama.httpx.AsyncClient", mock_cls),
        pytest.raises(httpx.ConnectError),
    ):
        await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])


@pytest.mark.asyncio
async def test_ollama_enrichment_summarize_returns_none_on_malformed_body() -> None:
    response = _make_response({"unexpected": "shape"})
    mock_cls = _make_async_client_cls(response=response)

    client = _make_client()
    with patch("archon_search.enrichment.ollama.httpx.AsyncClient", mock_cls):
        result = await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])

    assert result is None


@pytest.mark.asyncio
async def test_ollama_enrichment_extract_content_non_str_returns_none() -> None:
    response = _make_response({"choices": [{"message": {"content": 12345}}]})
    mock_cls = _make_async_client_cls(response=response)

    client = _make_client()
    with patch("archon_search.enrichment.ollama.httpx.AsyncClient", mock_cls):
        result = await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])

    assert result is None


@pytest.mark.asyncio
async def test_ollama_enrichment_summarize_empty_content_logs_warning(caplog) -> None:
    response = _make_response(_choices_body(""))
    mock_cls = _make_async_client_cls(response=response)

    client = _make_client()
    with (
        patch("archon_search.enrichment.ollama.httpx.AsyncClient", mock_cls),
        caplog.at_level("WARNING"),
    ):
        result = await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])

    assert result is None
    assert len(caplog.records) == 1
    assert "no usable content" in caplog.records[0].message

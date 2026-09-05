"""Unit tests for LlamaCppEnrichmentClient (LLCP BE-6).

Tests the llama.cpp (llama-server) enrichment adapter that implements
LLMEnrichmentClientProtocol over raw ``httpx`` — httpx is a core dependency,
no lazy-import guard needed.

Mocking convention for this repo (per the task-breakdown "Grounding note"):
patch ``archon_search.enrichment.llama_cpp.httpx.AsyncClient`` directly —
not ``httpx.MockTransport``.

C2 contract (inverse of C1 query-expansion): summarize_community MAY raise on
transport failure; callers catch and substitute None. Narrowed to community
summarisation only (BE-17).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from archon_search.graph_enrichment_protocol import LLMEnrichmentClientProtocol

_BASE_URL = "http://localhost:8080"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config() -> MagicMock:
    config = MagicMock()
    config.llama_cpp_base_url = _BASE_URL
    config.extraction_timeout_seconds = 30.0
    config.extraction_token_budget = 1024
    return config


def _make_client(config: MagicMock | None = None):
    from archon_search.enrichment.llama_cpp import LlamaCppEnrichmentClient

    return LlamaCppEnrichmentClient(model="local-model", config=config or _make_config())


def _make_response(json_body: dict, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=json_body)
    response.raise_for_status = MagicMock()
    return response


def _make_async_client_cls(*responses: MagicMock, post_side_effect=None) -> MagicMock:
    """Return a mock class standing in for ``httpx.AsyncClient``."""
    mock_client = AsyncMock()
    if post_side_effect is not None:
        mock_client.post = AsyncMock(side_effect=post_side_effect)
    else:
        mock_client.post = AsyncMock(return_value=responses[0] if responses else None)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_cls = MagicMock(return_value=mock_client)
    return mock_cls


def _choices_body(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_implements_llm_enrichment_client_protocol() -> None:
    client = _make_client()
    assert isinstance(client, LLMEnrichmentClientProtocol)


# ---------------------------------------------------------------------------
# summarize_community
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llama_cpp_summarize_community_happy_path() -> None:
    response = _make_response(_choices_body("A summary of the community."))
    mock_cls = _make_async_client_cls(response)

    client = _make_client()
    with patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls):
        result = await client.summarize_community(
            chunk_texts=["chunk one", "chunk two"], entity_names=["Entity A", "Entity B"]
        )

    assert result == "A summary of the community."


@pytest.mark.asyncio
async def test_llama_cpp_summarize_community_transport_raises() -> None:
    """C2 raise-on-failure contract: transport failure must propagate, not be swallowed."""
    mock_cls = _make_async_client_cls(post_side_effect=httpx.ConnectError("connection refused"))

    client = _make_client()
    with (
        patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls),
        pytest.raises(httpx.ConnectError),
    ):
        await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])


@pytest.mark.asyncio
async def test_llama_cpp_summarize_community_returns_none_on_malformed_body() -> None:
    response = _make_response({"unexpected": "shape"})
    mock_cls = _make_async_client_cls(response)

    client = _make_client()
    with patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls):
        result = await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])

    assert result is None


@pytest.mark.asyncio
async def test_llama_cpp_extract_content_non_str_returns_none() -> None:
    response = _make_response({"choices": [{"message": {"content": 12345}}]})
    mock_cls = _make_async_client_cls(response)

    client = _make_client()
    with patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls):
        result = await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])

    assert result is None


@pytest.mark.asyncio
async def test_llama_cpp_enrichment_summarize_empty_content_logs_warning(caplog) -> None:
    response = _make_response(_choices_body(""))
    mock_cls = _make_async_client_cls(response)

    client = _make_client()
    with (
        patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls),
        caplog.at_level("WARNING"),
    ):
        result = await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])

    assert result is None
    assert len(caplog.records) == 1
    assert "no usable content" in caplog.records[0].message


# ---------------------------------------------------------------------------
# S26 — no rate limiting
# ---------------------------------------------------------------------------


def test_llama_cpp_no_rate_limit_check() -> None:
    """LlamaCppEnrichmentClient must not implement a _check_rate_limit method at all."""
    client = _make_client()
    assert not hasattr(client, "_check_rate_limit")

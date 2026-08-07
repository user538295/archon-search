"""Unit tests for OpenAIEnrichmentClient (LLCP BE-6).

Tests the OpenAI enrichment adapter that implements LLMEnrichmentClientProtocol
over raw ``httpx`` — deliberately NOT the ``openai`` SDK used by
``providers/openai_provider.py`` (query-expansion adapter).

Mocking convention: patch ``archon_search.enrichment.openai.httpx.AsyncClient``
directly.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from archon_search.graph_enrichment_protocol import (
    LabeledRelationship,
    LLMEnrichmentClientProtocol,
)


def _make_config() -> MagicMock:
    config = MagicMock()
    config.extraction_timeout_seconds = 30.0
    config.extraction_token_budget = 1024
    return config


def _make_client(config: MagicMock | None = None):
    from archon_search.enrichment.openai import OpenAIEnrichmentClient

    return OpenAIEnrichmentClient(model="gpt-4o-mini", config=config or _make_config())


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


def test_implements_llm_enrichment_client_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = _make_client()
    assert isinstance(client, LLMEnrichmentClientProtocol)


@pytest.mark.asyncio
async def test_openai_enrichment_summarize_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    response = _make_response(_choices_body("A community summary."))
    mock_cls = _make_async_client_cls(response=response)

    client = _make_client()
    with patch("archon_search.enrichment.openai.httpx.AsyncClient", mock_cls):
        result = await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])

    assert result == "A community summary."

    mock_client = mock_cls.return_value
    _, kwargs = mock_client.post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_openai_enrichment_transport_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mock_cls = _make_async_client_cls(post_side_effect=httpx.ConnectError("connection refused"))

    client = _make_client()
    with (
        patch("archon_search.enrichment.openai.httpx.AsyncClient", mock_cls),
        pytest.raises(httpx.ConnectError),
    ):
        await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])


@pytest.mark.asyncio
async def test_openai_enrichment_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    client = _make_client()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])


@pytest.mark.asyncio
async def test_openai_enrichment_label_relationships_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    body = json.dumps([{"source_entity": "A", "target_entity": "B", "relationship_type": "implements"}])
    response = _make_response(_choices_body(body))
    mock_cls = _make_async_client_cls(response=response)

    client = _make_client()
    with patch("archon_search.enrichment.openai.httpx.AsyncClient", mock_cls):
        result = await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="A implements B.")

    assert result == [
        LabeledRelationship(source_entity="A", target_entity="B", relationship_type="implements")
    ]


@pytest.mark.asyncio
async def test_openai_enrichment_summarize_returns_none_on_malformed_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    response = _make_response({"unexpected": "shape"})
    mock_cls = _make_async_client_cls(response=response)

    client = _make_client()
    with patch("archon_search.enrichment.openai.httpx.AsyncClient", mock_cls):
        result = await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])

    assert result is None


@pytest.mark.asyncio
async def test_openai_enrichment_label_relationships_empty_response_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    response = _make_response({"unexpected": "shape"})
    mock_cls = _make_async_client_cls(response=response)

    client = _make_client()
    with patch("archon_search.enrichment.openai.httpx.AsyncClient", mock_cls):
        result = await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="text")

    assert result == []


@pytest.mark.asyncio
async def test_openai_enrichment_label_relationships_non_list_json_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    body = json.dumps({"not": "a list"})
    response = _make_response(_choices_body(body))
    mock_cls = _make_async_client_cls(response=response)

    client = _make_client()
    with (
        patch("archon_search.enrichment.openai.httpx.AsyncClient", mock_cls),
        pytest.raises(ValueError, match="Expected JSON array"),
    ):
        await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="text")


@pytest.mark.asyncio
async def test_openai_enrichment_label_relationships_key_error_item_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    body = json.dumps([{"relationship_type": "uses"}])
    response = _make_response(_choices_body(body))
    mock_cls = _make_async_client_cls(response=response)

    client = _make_client()
    with (
        patch("archon_search.enrichment.openai.httpx.AsyncClient", mock_cls),
        caplog.at_level("WARNING"),
    ):
        result = await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="text")

    assert result == []
    assert len(caplog.records) == 1


@pytest.mark.asyncio
async def test_openai_enrichment_extract_content_non_str_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    response = _make_response({"choices": [{"message": {"content": 12345}}]})
    mock_cls = _make_async_client_cls(response=response)

    client = _make_client()
    with patch("archon_search.enrichment.openai.httpx.AsyncClient", mock_cls):
        result = await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])

    assert result is None


@pytest.mark.asyncio
async def test_openai_enrichment_label_relationships_transport_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mock_cls = _make_async_client_cls(post_side_effect=httpx.TimeoutException("timed out"))

    client = _make_client()
    with (
        patch("archon_search.enrichment.openai.httpx.AsyncClient", mock_cls),
        pytest.raises(httpx.TimeoutException),
    ):
        await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="text")

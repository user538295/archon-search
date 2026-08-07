"""Unit tests for LlamaCppEnrichmentClient (LLCP BE-6).

Tests the llama.cpp (llama-server) enrichment adapter that implements
LLMEnrichmentClientProtocol over raw ``httpx`` — httpx is a core dependency,
no lazy-import guard needed.

Mocking convention for this repo (per the task-breakdown "Grounding note"):
patch ``archon_search.enrichment.llama_cpp.httpx.AsyncClient`` directly —
not ``httpx.MockTransport``.

C2 contract (inverse of C1 query-expansion): both methods MAY raise on
transport failure; callers catch and substitute None/[]. Per-item skip vs
whole-call raise: individual unparseable relationship items are skipped
with a WARNING; the call raises only on transport failure or a whole-body
JSON parse failure.
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


def _make_error_response(status_code: int, json_body: dict | None = None) -> MagicMock:
    request = httpx.Request("POST", f"{_BASE_URL}/v1/chat/completions")
    response = MagicMock()
    response.status_code = status_code
    response.json = MagicMock(return_value=json_body or {})
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            f"Error {status_code}",
            request=request,
            response=httpx.Response(status_code, request=request),
        )
    )
    return response


def _make_async_client_cls(*responses: MagicMock, post_side_effect=None) -> MagicMock:
    """Return a mock class standing in for ``httpx.AsyncClient``.

    When multiple ``responses`` are given, successive ``client.post()`` calls
    return them in order (used for the 422-fallback path).
    """
    mock_client = AsyncMock()
    if post_side_effect is not None:
        mock_client.post = AsyncMock(side_effect=post_side_effect)
    elif len(responses) > 1:
        mock_client.post = AsyncMock(side_effect=list(responses))
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


# ---------------------------------------------------------------------------
# label_relationships
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llama_cpp_label_relationships_json_schema_path() -> None:
    body = json.dumps(
        [{"source_entity": "A", "target_entity": "B", "relationship_type": "uses"}]
    )
    response = _make_response(_choices_body(body))
    mock_cls = _make_async_client_cls(response)

    client = _make_client()
    with patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls):
        result = await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="A uses B.")

    assert result == [LabeledRelationship(source_entity="A", target_entity="B", relationship_type="uses")]

    # Confirm the json_schema response_format was sent on the (only) call.
    mock_client = mock_cls.return_value
    _, kwargs = mock_client.post.call_args
    assert kwargs["json"]["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_llama_cpp_label_relationships_422_fallback() -> None:
    """S24a: HTTP 422 on json_schema request → prompt-only fallback, no whole-call raise."""
    error_response = _make_error_response(422)
    body = json.dumps(
        [{"source_entity": "A", "target_entity": "B", "relationship_type": "implements"}]
    )
    fallback_response = _make_response(_choices_body(body))
    mock_cls = _make_async_client_cls(error_response, fallback_response)

    client = _make_client()
    with patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls):
        result = await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="A implements B.")

    assert result == [
        LabeledRelationship(source_entity="A", target_entity="B", relationship_type="implements")
    ]

    mock_client = mock_cls.return_value
    assert mock_client.post.await_count == 2
    first_call, second_call = mock_client.post.await_args_list
    assert "response_format" in first_call.kwargs["json"]
    assert "response_format" not in second_call.kwargs["json"]


@pytest.mark.asyncio
async def test_llama_cpp_label_relationships_non_422_http_error_raises() -> None:
    error_response = _make_error_response(500)
    mock_cls = _make_async_client_cls(error_response)

    client = _make_client()
    with (
        patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="text")


@pytest.mark.asyncio
async def test_llama_cpp_label_relationships_partial_parse(caplog) -> None:
    """S24b / S19: mixed parseable/unparseable items → valid items returned, WARNING per skip."""
    body = json.dumps(
        [
            {"source_entity": "A", "target_entity": "B", "relationship_type": "uses"},
            {"source_entity": "C", "target_entity": "D", "relationship_type": "unknown_type"},
            {"source_entity": "E"},  # missing target_entity/relationship_type
        ]
    )
    response = _make_response(_choices_body(body))
    mock_cls = _make_async_client_cls(response)

    client = _make_client()
    with (
        patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls),
        caplog.at_level("WARNING"),
    ):
        result = await client.label_relationships(
            entity_pairs=[("A", "B"), ("C", "D"), ("E", "F")], chunk_text="text"
        )

    assert result == [LabeledRelationship(source_entity="A", target_entity="B", relationship_type="uses")]
    assert len(caplog.records) >= 2


@pytest.mark.asyncio
async def test_llama_cpp_label_relationships_non_list_json_raises() -> None:
    """A parsed JSON body that is not a list (e.g. a dict) must raise ValueError."""
    body = json.dumps({"not": "a list"})
    response = _make_response(_choices_body(body))
    mock_cls = _make_async_client_cls(response)

    client = _make_client()
    with (
        patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls),
        pytest.raises(ValueError, match="Expected JSON array"),
    ):
        await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="text")


@pytest.mark.asyncio
async def test_llama_cpp_label_relationships_key_error_item_skipped(caplog) -> None:
    """A valid relationship_type but missing source_entity/target_entity keys is skipped."""
    body = json.dumps([{"relationship_type": "uses"}])
    response = _make_response(_choices_body(body))
    mock_cls = _make_async_client_cls(response)

    client = _make_client()
    with (
        patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls),
        caplog.at_level("WARNING"),
    ):
        result = await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="text")

    assert result == []
    assert len(caplog.records) == 1


@pytest.mark.asyncio
async def test_llama_cpp_extract_content_non_str_returns_none() -> None:
    response = _make_response({"choices": [{"message": {"content": 12345}}]})
    mock_cls = _make_async_client_cls(response)

    client = _make_client()
    with patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls):
        result = await client.summarize_community(chunk_texts=["chunk"], entity_names=["Entity A"])

    assert result is None


@pytest.mark.asyncio
async def test_llama_cpp_label_relationships_whole_body_parse_failure_raises() -> None:
    response = _make_response(_choices_body("not json at all"))
    mock_cls = _make_async_client_cls(response)

    client = _make_client()
    with (
        patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls),
        pytest.raises(json.JSONDecodeError),
    ):
        await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="text")


@pytest.mark.asyncio
async def test_llama_cpp_label_relationships_empty_response_returns_empty_list() -> None:
    response = _make_response({"unexpected": "shape"})
    mock_cls = _make_async_client_cls(response)

    client = _make_client()
    with patch("archon_search.enrichment.llama_cpp.httpx.AsyncClient", mock_cls):
        result = await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="text")

    assert result == []


# ---------------------------------------------------------------------------
# S26 — no rate limiting
# ---------------------------------------------------------------------------


def test_llama_cpp_no_rate_limit_check() -> None:
    """LlamaCppEnrichmentClient must not implement a _check_rate_limit method at all."""
    client = _make_client()
    assert not hasattr(client, "_check_rate_limit")

"""Tests for BE-0: LLMEnrichmentClientProtocol and AnthropicEnrichmentClient.

TDD test suite — written before production code exists.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import fields
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(rpm: int = 60, extra_rpm: int | None = None) -> "AnthropicEnrichmentClient":
    from archon_search.enrichment.anthropic import AnthropicEnrichmentClient

    config = MagicMock()
    config.extraction_timeout_seconds = 30.0
    config.extraction_rate_limit_rpm = extra_rpm if extra_rpm is not None else rpm
    config.extraction_token_budget = 1024
    return AnthropicEnrichmentClient(model="claude-haiku-4-5-20251001", config=config)


def _make_ready_client(
    mock_response_text: str = "summary",
) -> tuple["AnthropicEnrichmentClient", "MagicMock"]:
    """Return a client with _anthropic_available=True and a wired mock Anthropic client."""
    client = _make_client()
    mock_content = MagicMock()
    mock_content.text = mock_response_text
    mock_response = MagicMock()
    mock_response.content = [mock_content]
    mock_messages = MagicMock()
    mock_messages.create = AsyncMock(return_value=mock_response)
    mock_anthropic = MagicMock()
    mock_anthropic.messages = mock_messages
    client._client = mock_anthropic
    client._anthropic_available = True
    return client, mock_messages


# ---------------------------------------------------------------------------
# 1. LabeledRelationship dataclass
# ---------------------------------------------------------------------------


def test_label_relationship_dataclass() -> None:
    """LabeledRelationship must expose source_entity, target_entity, relationship_type fields."""
    from archon_search.graph_enrichment_protocol import LabeledRelationship

    field_names = {f.name for f in fields(LabeledRelationship)}
    assert field_names == {"source_entity", "target_entity", "relationship_type"}

    # Constructible with positional args
    lr = LabeledRelationship(
        source_entity="A",
        target_entity="B",
        relationship_type="uses",
    )
    assert lr.source_entity == "A"
    assert lr.target_entity == "B"
    assert lr.relationship_type == "uses"


# ---------------------------------------------------------------------------
# 2. Protocol structural typing
# ---------------------------------------------------------------------------


def test_protocol_method_signatures() -> None:
    """AnthropicEnrichmentClient must be a structural subtype of LLMEnrichmentClientProtocol."""
    from archon_search.graph_enrichment_protocol import LLMEnrichmentClientProtocol

    client = _make_client()

    # Runtime-checkable protocol check
    assert isinstance(client, LLMEnrichmentClientProtocol), (
        "AnthropicEnrichmentClient must satisfy LLMEnrichmentClientProtocol structurally"
    )


# ---------------------------------------------------------------------------
# 3. summarize_community raises on API error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_raises_on_api_error() -> None:
    """AnthropicEnrichmentClient.summarize_community must propagate (not swallow) API errors."""
    client = _make_client()

    class _FakeAPIError(Exception):
        pass

    fake_messages = MagicMock()
    fake_messages.create = AsyncMock(side_effect=_FakeAPIError("api down"))
    fake_anthropic_client = MagicMock()
    fake_anthropic_client.messages = fake_messages
    client._client = fake_anthropic_client
    client._anthropic_available = True

    client._check_rate_limit = AsyncMock(return_value=None)

    with pytest.raises(_FakeAPIError):
        await client.summarize_community(
            chunk_texts=["some text"],
            entity_names=["Entity A"],
        )


# ---------------------------------------------------------------------------
# 4. label_relationships raises on error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_raises_on_label_relationships_error() -> None:
    """AnthropicEnrichmentClient.label_relationships must propagate (not swallow) errors."""
    client = _make_client()

    class _FakeAPIError(Exception):
        pass

    fake_messages = MagicMock()
    fake_messages.create = AsyncMock(side_effect=_FakeAPIError("network error"))
    fake_anthropic_client = MagicMock()
    fake_anthropic_client.messages = fake_messages
    client._client = fake_anthropic_client
    client._anthropic_available = True

    client._check_rate_limit = AsyncMock(return_value=None)

    with pytest.raises(_FakeAPIError):
        await client.label_relationships(
            entity_pairs=[("A", "B")],
            chunk_text="A uses B",
        )


# ---------------------------------------------------------------------------
# 5. summarize_community happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_community_returns_text() -> None:
    """summarize_community returns the text from the LLM response."""
    client, mock_messages = _make_ready_client(mock_response_text="This is the summary.")
    client._check_rate_limit = AsyncMock(return_value=None)

    result = await client.summarize_community(
        chunk_texts=["passage one", "passage two"],
        entity_names=["EntityA", "EntityB"],
    )

    assert result == "This is the summary."
    mock_messages.create.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. label_relationships happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_relationships_returns_labeled_relationships() -> None:
    """label_relationships parses the JSON array into LabeledRelationship objects."""
    from archon_search.graph_enrichment_protocol import LabeledRelationship

    payload = json.dumps([
        {"source_entity": "A", "target_entity": "B", "relationship_type": "uses"},
        {"source_entity": "C", "target_entity": "D", "relationship_type": "depends_on"},
    ])
    client, _ = _make_ready_client(mock_response_text=payload)
    client._check_rate_limit = AsyncMock(return_value=None)

    results = await client.label_relationships(
        entity_pairs=[("A", "B"), ("C", "D")],
        chunk_text="A uses B. C depends on D.",
    )

    assert len(results) == 2
    assert results[0] == LabeledRelationship(
        source_entity="A", target_entity="B", relationship_type="uses"
    )
    assert results[1] == LabeledRelationship(
        source_entity="C", target_entity="D", relationship_type="depends_on"
    )


# ---------------------------------------------------------------------------
# 7. label_relationships skips unknown relationship types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_relationships_unknown_type_skipped() -> None:
    """label_relationships drops items with unknown relationship_type."""
    from archon_search.graph_enrichment_protocol import LabeledRelationship

    payload = json.dumps([
        {"source_entity": "A", "target_entity": "B", "relationship_type": "uses"},
        {"source_entity": "X", "target_entity": "Y", "relationship_type": "knows_about"},
    ])
    client, _ = _make_ready_client(mock_response_text=payload)
    client._check_rate_limit = AsyncMock(return_value=None)

    results = await client.label_relationships(
        entity_pairs=[("A", "B"), ("X", "Y")],
        chunk_text="A uses B. X knows about Y.",
    )

    assert len(results) == 1
    assert results[0] == LabeledRelationship(
        source_entity="A", target_entity="B", relationship_type="uses"
    )


# ---------------------------------------------------------------------------
# 8. label_relationships: malformed item (KeyError path) skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_relationships_malformed_item_skipped() -> None:
    """Items missing source_entity/target_entity are skipped; good items in the same batch survive."""
    client, _ = _make_ready_client(
        mock_response_text='[{"relationship_type": "uses"}, {"source_entity": "A", "target_entity": "B", "relationship_type": "implements"}]'
    )
    client._check_rate_limit = AsyncMock(return_value=None)
    result = await client.label_relationships(
        entity_pairs=[("A", "B")],
        chunk_text="A implements B",
    )
    # The first item has a valid type but missing source/target → skipped
    # The second item is complete → kept
    assert len(result) == 1
    assert result[0].relationship_type == "implements"
    assert result[0].source_entity == "A"
    assert result[0].target_entity == "B"


# ---------------------------------------------------------------------------
# 9. label_relationships: non-dict item (AttributeError path) skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_relationships_non_dict_item_skipped() -> None:
    """Non-dict items in the JSON array are skipped (AttributeError path)."""
    client, _ = _make_ready_client(
        mock_response_text='["not a dict", {"source_entity": "A", "target_entity": "B", "relationship_type": "uses"}]'
    )
    client._check_rate_limit = AsyncMock(return_value=None)
    result = await client.label_relationships(
        entity_pairs=[("A", "B")],
        chunk_text="A uses B",
    )
    assert len(result) == 1
    assert result[0].relationship_type == "uses"


# ---------------------------------------------------------------------------
# 10. label_relationships: non-list JSON raises ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_relationships_non_list_json_raises() -> None:
    """Non-list JSON (e.g. dict) from LLM raises ValueError."""
    client, _ = _make_ready_client(
        mock_response_text='{"relationships": []}'
    )
    client._check_rate_limit = AsyncMock(return_value=None)
    with pytest.raises(ValueError, match="Expected JSON array"):
        await client.label_relationships(
            entity_pairs=[("A", "B")],
            chunk_text="A uses B",
        )


# ---------------------------------------------------------------------------
# 11. label_relationships: malformed JSON raises ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_relationships_malformed_json_raises() -> None:
    """Malformed JSON from LLM raises ValueError/JSONDecodeError."""
    client, _ = _make_ready_client(mock_response_text="not json at all")
    client._check_rate_limit = AsyncMock(return_value=None)
    with pytest.raises(ValueError):
        await client.label_relationships(
            entity_pairs=[("A", "B")],
            chunk_text="A uses B",
        )


# ---------------------------------------------------------------------------
# 12. Rate-limit exhaustion and refill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_rate_limit_exhaustion_raises() -> None:
    """_check_rate_limit raises RuntimeError when token bucket is exhausted."""
    client = _make_client(extra_rpm=1)  # only 1 token
    # Use one token
    await client._check_rate_limit()
    # Next call should raise
    with pytest.raises(RuntimeError, match="rate limit exhausted"):
        await client._check_rate_limit()


@pytest.mark.asyncio
async def test_check_rate_limit_refills_after_window() -> None:
    """Fixed-window rate limiter refills to full capacity after the 60-second window."""
    client = _make_client(extra_rpm=1)
    await client._check_rate_limit()  # consume the single token
    assert client._rpm_tokens == 0
    # Simulate time passing past refill window
    client._rpm_refill_at = time.monotonic() - 1.0  # set to past
    # Should succeed now (refilled to full capacity and one token consumed)
    await client._check_rate_limit()
    # After refill+consume, tokens == capacity - 1
    assert client._rpm_tokens == client._rpm_capacity - 1
    # Refill window must have been advanced (not stuck in the past)
    assert client._rpm_refill_at > time.monotonic()


# ---------------------------------------------------------------------------
# 13. RuntimeError when anthropic unavailable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_community_raises_when_anthropic_unavailable() -> None:
    """summarize_community raises RuntimeError when anthropic is not available."""
    client = _make_client()
    client._anthropic_available = False
    with pytest.raises(RuntimeError, match="anthropic"):
        await client.summarize_community(chunk_texts=["text"], entity_names=["Entity"])


@pytest.mark.asyncio
async def test_label_relationships_raises_when_anthropic_unavailable() -> None:
    """label_relationships raises RuntimeError when anthropic is not available."""
    client = _make_client()
    client._anthropic_available = False
    with pytest.raises(RuntimeError, match="anthropic"):
        await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="A uses B")


# ---------------------------------------------------------------------------
# 14. Empty content branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_community_returns_none_on_empty_content() -> None:
    """summarize_community returns None when LLM response has empty content list."""
    client = _make_client()
    mock_response = MagicMock()
    mock_response.content = []
    mock_messages = MagicMock()
    mock_messages.create = AsyncMock(return_value=mock_response)
    mock_anthropic = MagicMock()
    mock_anthropic.messages = mock_messages
    client._client = mock_anthropic
    client._anthropic_available = True
    client._check_rate_limit = AsyncMock(return_value=None)
    result = await client.summarize_community(chunk_texts=["text"], entity_names=["Entity"])
    assert result is None


@pytest.mark.asyncio
async def test_label_relationships_returns_empty_on_empty_content() -> None:
    """label_relationships returns [] when LLM response has empty content list."""
    client = _make_client()
    mock_response = MagicMock()
    mock_response.content = []
    mock_messages = MagicMock()
    mock_messages.create = AsyncMock(return_value=mock_response)
    mock_anthropic = MagicMock()
    mock_anthropic.messages = mock_messages
    client._client = mock_anthropic
    client._anthropic_available = True
    client._check_rate_limit = AsyncMock(return_value=None)
    result = await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="A uses B")
    assert result == []


# ---------------------------------------------------------------------------
# 15. summarize_community propagates asyncio.TimeoutError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_community_raises_on_timeout() -> None:
    """summarize_community must propagate asyncio.TimeoutError from asyncio.wait_for."""
    client = _make_client()
    client._anthropic_available = True

    async def _slow_create(**kwargs):
        await asyncio.sleep(10)

    mock_anthropic = MagicMock()
    mock_anthropic.messages.create = _slow_create
    client._client = mock_anthropic
    client._timeout = 0.01  # 10ms timeout
    client._check_rate_limit = AsyncMock(return_value=None)
    with pytest.raises(asyncio.TimeoutError):
        await client.summarize_community(chunk_texts=["text"], entity_names=["A"])


# ---------------------------------------------------------------------------
# 16. summarize_community passes max_tokens to API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_community_passes_token_budget_to_api() -> None:
    """summarize_community must pass extraction_token_budget as max_tokens to the API."""
    client, mock_messages = _make_ready_client(mock_response_text="summary")
    client._check_rate_limit = AsyncMock(return_value=None)
    # _token_budget is set to 1024 by _make_ready_client via _make_client
    await client.summarize_community(chunk_texts=["text"], entity_names=["A"])
    call_kwargs = mock_messages.create.call_args.kwargs
    assert call_kwargs["max_tokens"] == 1024
    assert call_kwargs["model"] == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# 17. summarize_community returns None on whitespace-only response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_community_returns_none_on_whitespace_response() -> None:
    """summarize_community returns None when LLM response is whitespace-only."""
    client, _ = _make_ready_client(mock_response_text="   \n  ")
    client._check_rate_limit = AsyncMock(return_value=None)
    result = await client.summarize_community(chunk_texts=["text"], entity_names=["A"])
    assert result is None


# ---------------------------------------------------------------------------
# 18. Rate limit exhaustion prevents API call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_exhausted_prevents_api_call() -> None:
    """When rate limit is exhausted, summarize_community raises before calling the API."""
    client = _make_client(rpm=1)  # 1 token only
    client._anthropic_available = True
    mock_messages = MagicMock()
    mock_messages.create = AsyncMock(return_value=MagicMock(content=[]))
    mock_anthropic = MagicMock()
    mock_anthropic.messages = mock_messages
    client._client = mock_anthropic
    # Consume the single token
    await client._check_rate_limit()
    # Now summarize_community should raise RuntimeError (rate limit), NOT call the API
    with pytest.raises(RuntimeError, match="rate limit exhausted"):
        await client.summarize_community(chunk_texts=["text"], entity_names=["A"])
    # Verify the API was never called
    mock_messages.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# 19. Silent-drop WARNING is emitted for unknown relationship type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_relationships_logs_warning_on_unknown_type(caplog) -> None:
    """label_relationships logs a WARNING when an item has an unknown relationship_type."""
    import logging

    payload = json.dumps([
        {"source_entity": "X", "target_entity": "Y", "relationship_type": "knows_about"},
    ])
    client, _ = _make_ready_client(mock_response_text=payload)
    client._check_rate_limit = AsyncMock(return_value=None)

    with caplog.at_level(logging.WARNING, logger="archon_search.enrichment.anthropic"):
        results = await client.label_relationships(
            entity_pairs=[("X", "Y")], chunk_text="X knows about Y."
        )

    assert results == []
    assert any("knows_about" in record.message for record in caplog.records), (
        "Expected WARNING mentioning the unknown relationship_type 'knows_about'"
    )


# ---------------------------------------------------------------------------
# 20. Silent-drop WARNING is emitted for malformed item (missing keys)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_relationships_logs_warning_on_malformed_item(caplog) -> None:
    """label_relationships logs a WARNING when an item is missing source_entity/target_entity."""
    import logging

    payload = json.dumps([{"relationship_type": "uses"}])  # missing source/target
    client, _ = _make_ready_client(mock_response_text=payload)
    client._check_rate_limit = AsyncMock(return_value=None)

    with caplog.at_level(logging.WARNING, logger="archon_search.enrichment.anthropic"):
        results = await client.label_relationships(
            entity_pairs=[("A", "B")], chunk_text="A uses B."
        )

    assert results == []
    assert any("malformed" in record.message for record in caplog.records), (
        "Expected WARNING mentioning malformed relationship item"
    )


# ---------------------------------------------------------------------------
# 21. LLCP BE-5 — constructible from a real (non-MagicMock) GraphConfig
# ---------------------------------------------------------------------------


def test_anthropic_client_constructible_from_real_graphconfig() -> None:
    """AnthropicEnrichmentClient initialises without error from a real GraphConfig.

    Regression gate for the BE-5 module move: archon_search.enrichment.anthropic
    must still consume all six GraphConfig enrichment fields correctly.
    """
    from archon_search.config import GraphConfig
    from archon_search.enrichment.anthropic import AnthropicEnrichmentClient

    cfg = GraphConfig(
        provider="anthropic",
        extraction_model="claude-haiku-4-5",
        llama_cpp_base_url="http://localhost:8080",
        ollama_base_url="http://localhost:11434",
        extraction_timeout_seconds=30.0,
        extraction_rate_limit_rpm=60,
        extraction_token_budget=1024,
    )
    client = AnthropicEnrichmentClient(model="claude-haiku-4-5", config=cfg)
    assert client is not None

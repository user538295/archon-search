"""Tests for BE-24: shared markdown-code-fence stripping before JSON parsing.

Covers the shared helper (archon_search.enrichment.strip_json_code_fences) and,
for each of the four enrichment clients, that label_relationships correctly
parses a reply whose JSON array is wrapped in a markdown code fence.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from archon_search.enrichment import strip_json_code_fences
from archon_search.graph_enrichment_protocol import LabeledRelationship

_REL_PAYLOAD = json.dumps(
    [{"source_entity": "A", "target_entity": "B", "relationship_type": "depends_on"}]
)
_EXPECTED_RELATIONSHIPS = [
    LabeledRelationship(source_entity="A", target_entity="B", relationship_type="depends_on")
]


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def test_strip_json_code_fences_json_labeled_fence() -> None:
    fenced = f"```json\n{_REL_PAYLOAD}\n```"
    assert strip_json_code_fences(fenced) == _REL_PAYLOAD


def test_strip_json_code_fences_plain_fence() -> None:
    fenced = f"```\n{_REL_PAYLOAD}\n```"
    assert strip_json_code_fences(fenced) == _REL_PAYLOAD


def test_strip_json_code_fences_no_fence_passthrough() -> None:
    assert strip_json_code_fences(_REL_PAYLOAD) == _REL_PAYLOAD


def test_strip_json_code_fences_unclosed_fence_passthrough() -> None:
    unclosed = f"```json\n{_REL_PAYLOAD}"
    assert strip_json_code_fences(unclosed) == unclosed


def test_strip_json_code_fences_leading_trailing_whitespace() -> None:
    fenced = f"  \n```json\n{_REL_PAYLOAD}\n```\n  "
    assert strip_json_code_fences(fenced) == _REL_PAYLOAD


# ---------------------------------------------------------------------------
# Ollama / llama.cpp / OpenAI — same OpenAI-compatible chat-completions shape
# ---------------------------------------------------------------------------


def _make_ollama_client() -> Any:
    from archon_search.enrichment.ollama import OllamaEnrichmentClient

    config = MagicMock()
    config.ollama_base_url = "http://localhost:11434"
    config.extraction_timeout_seconds = 30.0
    config.extraction_token_budget = 1024
    return OllamaEnrichmentClient(model="local-model", config=config)


def _make_llama_cpp_client() -> Any:
    from archon_search.enrichment.llama_cpp import LlamaCppEnrichmentClient

    config = MagicMock()
    config.llama_cpp_base_url = "http://localhost:8080"
    config.extraction_timeout_seconds = 30.0
    config.extraction_token_budget = 1024
    return LlamaCppEnrichmentClient(model="local-model", config=config)


def _make_openai_client() -> Any:
    from archon_search.enrichment.openai import OpenAIEnrichmentClient

    config = MagicMock()
    config.extraction_timeout_seconds = 30.0
    config.extraction_token_budget = 1024
    return OpenAIEnrichmentClient(model="gpt-test", config=config)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_factory", "patch_target", "extra_patches"),
    [
        (_make_ollama_client, "archon_search.enrichment.ollama.httpx.AsyncClient", {}),
        (_make_llama_cpp_client, "archon_search.enrichment.llama_cpp.httpx.AsyncClient", {}),
        (
            _make_openai_client,
            "archon_search.enrichment.openai.httpx.AsyncClient",
            {"OPENAI_API_KEY": "test-key"},
        ),
    ],
    ids=["ollama", "llama_cpp", "openai"],
)
async def test_openai_compatible_client_label_relationships_strips_fence(
    client_factory: Any, patch_target: str, extra_patches: dict[str, str]
) -> None:
    client = client_factory()

    body = {"choices": [{"message": {"content": f"```json\n{_REL_PAYLOAD}\n```"}}]}
    response = MagicMock()
    response.status_code = 200
    response.json = MagicMock(return_value=body)
    response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(patch_target, MagicMock(return_value=mock_client)),
        patch.dict("os.environ", extra_patches),
    ):
        result = await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="A depends on B.")

    assert result == _EXPECTED_RELATIONSHIPS


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_label_relationships_strips_fence() -> None:
    from archon_search.enrichment.anthropic import AnthropicEnrichmentClient

    config = MagicMock()
    config.extraction_timeout_seconds = 30.0
    config.extraction_rate_limit_rpm = 60
    config.extraction_token_budget = 1024
    client = AnthropicEnrichmentClient(model="claude-haiku-4-5-20251001", config=config)

    mock_content = MagicMock()
    mock_content.text = f"```json\n{_REL_PAYLOAD}\n```"
    mock_response = MagicMock()
    mock_response.content = [mock_content]
    mock_messages = MagicMock()
    mock_messages.create = AsyncMock(return_value=mock_response)
    mock_anthropic = MagicMock()
    mock_anthropic.messages = mock_messages
    client._client = mock_anthropic
    client._anthropic_available = True
    client._check_rate_limit = AsyncMock(return_value=None)

    result = await client.label_relationships(entity_pairs=[("A", "B")], chunk_text="A depends on B.")

    assert result == _EXPECTED_RELATIONSHIPS

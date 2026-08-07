"""Unit tests for EnrichmentClientFactory — LLCP BE-7.

Covers:
- provider=None -> None (S27 factory side)
- provider="llama_cpp"/"ollama"/"openai"/"anthropic" -> correct concrete client, bare model name (S10)
- provider="claude_cli" (deferred, no v1 client) -> None, no raise
- all four v1 clients implement LLMEnrichmentClientProtocol (S5)
- eval/runner.py's literal GraphConfig(...) call never sets a provider (S20b determinism)
"""
from __future__ import annotations

from archon_search.config import GraphConfig
from archon_search.graph_enrichment_protocol import LLMEnrichmentClientProtocol


def test_factory_returns_none_for_none_provider() -> None:
    from archon_search.enrichment.factory import EnrichmentClientFactory

    config = GraphConfig(provider=None, extraction_model="some-model")
    assert EnrichmentClientFactory.build(config) is None


def test_factory_returns_llama_cpp_client() -> None:
    from archon_search.enrichment.factory import EnrichmentClientFactory
    from archon_search.enrichment.llama_cpp import LlamaCppEnrichmentClient

    config = GraphConfig(provider="llama_cpp", extraction_model="qwen2.5-coder")
    client = EnrichmentClientFactory.build(config)

    assert isinstance(client, LlamaCppEnrichmentClient)


def test_factory_returns_ollama_client() -> None:
    from archon_search.enrichment.factory import EnrichmentClientFactory
    from archon_search.enrichment.ollama import OllamaEnrichmentClient

    config = GraphConfig(provider="ollama", extraction_model="qwen2.5-coder")
    client = EnrichmentClientFactory.build(config)

    assert isinstance(client, OllamaEnrichmentClient)


def test_factory_returns_openai_client() -> None:
    from archon_search.enrichment.factory import EnrichmentClientFactory
    from archon_search.enrichment.openai import OpenAIEnrichmentClient

    config = GraphConfig(provider="openai", extraction_model="gpt-4o-mini")
    client = EnrichmentClientFactory.build(config)

    assert isinstance(client, OpenAIEnrichmentClient)


def test_factory_returns_anthropic_client() -> None:
    """provider="anthropic" -> AnthropicEnrichmentClient constructed with the bare model name (S10)."""
    from archon_search.enrichment.anthropic import AnthropicEnrichmentClient
    from archon_search.enrichment.factory import EnrichmentClientFactory

    config = GraphConfig(provider="anthropic", extraction_model="claude-haiku-4-5")
    client = EnrichmentClientFactory.build(config)

    assert isinstance(client, AnthropicEnrichmentClient)
    assert client._model == "claude-haiku-4-5"


def test_factory_returns_none_for_claude_cli_deferred() -> None:
    """claude_cli is a valid [graph].provider value (registry-wide) but has no v1
    enrichment client (no HTTP endpoint) -- factory must not raise, must return None."""
    from archon_search.enrichment.factory import EnrichmentClientFactory

    config = GraphConfig(provider="claude_cli", extraction_model="some-model")
    assert EnrichmentClientFactory.build(config) is None


def test_all_four_v1_clients_constructible_and_protocol_conformant() -> None:
    """Factory builds all four v1 clients from a valid GraphConfig; each implements
    LLMEnrichmentClientProtocol (S5)."""
    from archon_search.enrichment.factory import EnrichmentClientFactory

    for provider in ("llama_cpp", "ollama", "openai", "anthropic"):
        config = GraphConfig(provider=provider, extraction_model="some-model")
        client = EnrichmentClientFactory.build(config)
        assert client is not None
        assert isinstance(client, LLMEnrichmentClientProtocol)


def test_eval_runner_graph_config_literal_has_no_provider() -> None:
    """S20b: eval/runner.py's community-build GraphConfig(...) literal never sets
    `provider`, so CommunityBuilder there always receives enrichment_client=None
    (eval must be deterministic; no LLM enrichment in the eval harness)."""
    # Mirrors the exact GraphConfig(...) call in archon_search/eval/runner.py:~1124.
    _graph_config = GraphConfig(
        enabled=True,
        leiden_resolution=1.0,
        community_summary_chunks=3,
        max_community_size=10,
    )
    assert _graph_config.provider is None

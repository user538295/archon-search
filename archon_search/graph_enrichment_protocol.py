"""LLMEnrichmentClientProtocol — Use Cases ↔ Interface Adapters boundary (E2i BE-0).

Defines the interface that Use Cases (CommunityBuilder) depend on for LLM-powered
graph enrichment. The concrete adapter (AnthropicEnrichmentClient) lives in the
Interface Adapters layer (archon_search/enrichment/anthropic.py).

Narrowed to community summarisation only (BE-17, ADR 12): relationship labelling
is now produced locally by the prose extraction engine, so the protocol — and all
four adapters — no longer carry a ``label_relationships`` method.

Pattern mirrors graph_store_protocol.py: the protocol is consumer-owned in Use Cases.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMEnrichmentClientProtocol(Protocol):
    """Structural protocol for LLM-powered graph enrichment adapters.

    Use Cases (CommunityBuilder._generate_llm_summary) depend on this interface,
    not on the concrete AnthropicEnrichmentClient.

    Adapter contract: the method raises on any failure (API error, timeout, parse
    error). Callers are responsible for catching all exceptions and substituting None.
    """

    async def summarize_community(
        self,
        chunk_texts: list[str],
        entity_names: list[str],
    ) -> str | None:
        """Generate an abstractive summary for a community of entities.

        Args:
            chunk_texts: Representative text chunks for the community.
            entity_names: Entity names present in the community.

        Returns:
            A summary string, or None if the LLM returns an empty response.

        Raises:
            Any exception on API error, timeout, or any other failure.
            Callers must catch all exceptions.
        """
        ...

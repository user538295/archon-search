"""LLMEnrichmentClientProtocol — Use Cases ↔ Interface Adapters boundary (E2i BE-0).

Defines the interface that Use Cases (CommunityBuilder, GraphExtractor) depend on
for LLM-powered graph enrichment. The concrete adapter (AnthropicEnrichmentClient)
lives in the Interface Adapters layer (archon_search/enrichment/anthropic.py).

Pattern mirrors graph_store_protocol.py: the protocol is consumer-owned in Use Cases.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class LabeledRelationship:
    """A typed relationship between two entities, produced by an LLM.

    Attributes:
        source_entity: Name of the source entity.
        target_entity: Name of the target entity.
        relationship_type: One of "uses", "implements", "depends_on".
    """

    source_entity: str
    target_entity: str
    relationship_type: str


@runtime_checkable
class LLMEnrichmentClientProtocol(Protocol):
    """Structural protocol for LLM-powered graph enrichment adapters.

    Use Cases (CommunityBuilder._generate_llm_summary, GraphExtractor.extract) depend
    on this interface, not on the concrete AnthropicEnrichmentClient.

    Adapter contract: both methods raise on any failure (API error, timeout, parse error).
    Callers are responsible for catching all exceptions and substituting None / [].
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

    async def label_relationships(
        self,
        entity_pairs: list[tuple[str, str]],
        chunk_text: str,
    ) -> list[LabeledRelationship]:
        """Label relationships between entity pairs given a shared chunk of text.

        Args:
            entity_pairs: Pairs of entity names to classify.
            chunk_text: The text chunk in which both entities appear.

        Returns:
            A list of LabeledRelationship with relationship_type in
            {"uses", "implements", "depends_on"}.

        Raises:
            Any exception on API error, timeout, parse error, or any other failure.
            Callers must catch all exceptions.
        """
        ...

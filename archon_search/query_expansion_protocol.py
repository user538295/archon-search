"""QueryExpansionProvider protocol — Use Cases ↔ Interface Adapters boundary (G10 BE-1).

Defines the interface that Use Cases (HyDEGenerator, RAGFusionGenerator) depend on
for LLM-powered query expansion. Concrete adapters (AnthropicQueryExpansionProvider,
OllamaQueryExpansionProvider, OpenAIQueryExpansionProvider) live in the Interface
Adapters layer (archon_search/providers/).

Pattern mirrors graph_enrichment_protocol.py: the protocol is consumer-owned
at the Use Cases layer.
"""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


def provider_key_available(provider: str) -> bool:
    """Return True when the given provider's required API key is set at call time.

    Provider semantics (checked at call time):
    - ``"ollama"``     → always ``True`` (local inference; no key required)
    - ``"claude_cli"`` → always ``True`` (uses Claude Code's login; no key required)
    - ``"openai"``     → ``OPENAI_API_KEY`` must be set
    - ``"anthropic"``  → ``ANTHROPIC_API_KEY`` must be set (default)
    """
    if provider in ("ollama", "claude_cli"):
        return True
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


@runtime_checkable
class QueryExpansionProvider(Protocol):
    """Structural protocol for LLM-powered query expansion adapters.

    Use Cases (HyDEGenerator, RAGFusionGenerator) depend on this interface,
    not on any concrete provider.

    Adapter contract: both methods must NEVER raise — surface errors via
    ``None`` / ``[]`` return only. Callers fall back to plain search on
    ``None`` / ``[]``.
    """

    async def generate_hypothetical_doc(
        self,
        query: str,
        *,
        max_tokens: int = 200,
        timeout_seconds: float = 10.0,
    ) -> str | None:
        """Generate a short hypothetical passage that would answer the query.

        Args:
            query: The (already-truncated) user query.
            max_tokens: Maximum tokens to generate.
            timeout_seconds: Per-call timeout.

        Returns:
            Hypothesis text as a plain string, or ``None`` on any failure.
            Must never raise.
        """
        ...

    async def decompose_query(
        self,
        query: str,
        *,
        num_queries: int = 3,
        max_tokens: int = 450,
        timeout_seconds: float = 10.0,
    ) -> list[str]:
        """Decompose the query into ``num_queries`` semantic variant strings.

        Args:
            query: The (already-truncated) user query.
            num_queries: Target number of variant queries.
            max_tokens: Maximum tokens to generate.
            timeout_seconds: Per-call timeout.

        Returns:
            List of variant query strings (may be fewer than ``num_queries``),
            or ``[]`` on any failure. Must never raise.
        """
        ...

"""EnrichmentClientFactory — Interface Adapters layer, composition root (LLCP BE-7).

Routes ``GraphConfig.provider`` to a concrete ``LLMEnrichmentClientProtocol``
implementation, mirroring ``_build_query_expansion_provider`` in
``archon_search/server/app.py`` (C3 contract).

``provider=None`` (the default, air-gap-safe) returns ``None`` -- callers
(``CommunityBuilder``, ``GraphExtractor``) treat a ``None`` client as
"enrichment disabled" and never invoke it. ``provider="claude_cli"`` is a
valid ``[graph].provider`` registry value but has no v1 enrichment client (no
HTTP endpoint) -- deferred post-v1; the factory logs a WARNING and returns
``None`` rather than raising, preserving "boot never blocked".
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archon_search.config import GraphConfig
    from archon_search.graph_enrichment_protocol import LLMEnrichmentClientProtocol

_logger = logging.getLogger(__name__)


class EnrichmentClientFactory:
    """Builds the concrete ``LLMEnrichmentClientProtocol`` adapter for ``[graph].provider``."""

    @staticmethod
    def build(config: "GraphConfig") -> "LLMEnrichmentClientProtocol | None":
        """Return the enrichment client for ``config.provider``, or ``None``.

        Args:
            config: The server's ``GraphConfig``. ``config.extraction_model``
                (bare model name, no "provider:" prefix) is passed through to
                the concrete client unchanged.

        Returns:
            A concrete ``LLMEnrichmentClientProtocol`` implementation, or
            ``None`` when ``config.provider`` is ``None`` or ``"claude_cli"``
            (deferred).
        """
        if config.provider is None:
            return None

        model = config.extraction_model or ""

        if config.provider == "llama_cpp":
            from archon_search.enrichment.llama_cpp import (  # noqa: PLC0415
                LlamaCppEnrichmentClient,
            )
            return LlamaCppEnrichmentClient(model=model, config=config)
        if config.provider == "ollama":
            from archon_search.enrichment.ollama import (  # noqa: PLC0415
                OllamaEnrichmentClient,
            )
            return OllamaEnrichmentClient(model=model, config=config)
        if config.provider == "openai":
            from archon_search.enrichment.openai import (  # noqa: PLC0415
                OpenAIEnrichmentClient,
            )
            return OpenAIEnrichmentClient(model=model, config=config)
        if config.provider == "anthropic":
            from archon_search.enrichment.anthropic import (  # noqa: PLC0415
                AnthropicEnrichmentClient,
            )
            return AnthropicEnrichmentClient(model=model, config=config)

        # config.provider == "claude_cli" (the only remaining _VALID_PROVIDERS
        # member): deferred post-v1, no HTTP enrichment endpoint exists yet.
        _logger.warning(
            "[graph] provider=%r has no v1 enrichment client (deferred); "
            "enrichment disabled for this collection.",
            config.provider,
        )
        return None

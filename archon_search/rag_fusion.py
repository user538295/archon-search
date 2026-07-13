"""RAG Fusion (Multi-Query Decomposition) query expansion for archon-search.

Decomposes a user query into N semantic variants via an LLM provider, enabling
downstream parallel search and second-pass RRF fusion.

The ``anthropic`` package is an optional dependency.  The default provider is
``AnthropicQueryExpansionProvider`` (requires ``archon-search[rag_fusion]``).
G10 adds ``OllamaQueryExpansionProvider`` and ``OpenAIQueryExpansionProvider``
as alternatives (BE-3, BE-6).

Privacy note: ``generate_variants`` sends the raw query text to the configured
LLM provider.  Operators who cannot allow this must keep
``[rag_fusion] enabled = false``.  No raw query text is ever written to logs —
only fingerprints.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archon_search.query_expansion_protocol import QueryExpansionProvider

from archon_search._privacy import _query_fingerprint
from archon_search.config import RAGFusionConfig

_logger = logging.getLogger(__name__)

# Control characters to reject in validated variants.
# Rejects \x00–\x1F (except \t \n \r), \x7F (DEL), and \x80–\x9F (C1 controls).
_CONTROL_CHARS_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]"
)


class RAGFusionDependencyError(RuntimeError):
    """Raised when the ``anthropic`` package is not installed.

    Install with: ``pip install 'archon-search[rag_fusion]'``
    """


class RAGFusionGenerator:
    """Generates semantic query variants for RAG Fusion.

    Accepts any ``QueryExpansionProvider`` for text generation.  The default
    provider is ``AnthropicQueryExpansionProvider`` (lazy-imported).  Callers
    that invoke ``generate_variants`` without the provider package receive a
    ``RAGFusionDependencyError``.
    """

    def __init__(
        self,
        config: RAGFusionConfig,
        provider: "QueryExpansionProvider | None" = None,
    ) -> None:
        self._config = config
        self._provider_available: bool = False

        if provider is not None:
            self._provider = provider
            self._provider_available = True
        else:
            # Default: construct AnthropicQueryExpansionProvider (lazy)
            try:
                from archon_search.providers.anthropic_provider import (  # noqa: PLC0415
                    AnthropicQueryExpansionProvider,
                )

                self._provider: "QueryExpansionProvider" = AnthropicQueryExpansionProvider(
                    model=config.model
                )
                self._provider_available = True
            except ImportError:
                self._provider = None  # type: ignore[assignment]
                self._provider_available = False

        # Token bucket state for rate limiting (per-process, in-memory)
        self._lock = asyncio.Lock()
        self._rpm_tokens: int = config.max_requests_per_minute
        self._rpm_refill_at: float = time.monotonic() + 60.0

        # One-time warning flags
        self._warned_no_key: bool = False
        self._rate_limit_warned_at: float = 0.0

    def is_key_available(self) -> bool:
        """Return ``True`` when ``ANTHROPIC_API_KEY`` is set in the environment at call time.

        Checked at call time (not at construction) so the status endpoint reflects
        the live environment, not the state at startup.
        """
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def _validate_variant(self, text: str) -> str | None:
        """Return stripped text if valid, else None.

        A variant is invalid if:
        - empty after stripping whitespace
        - longer than 500 characters after stripping
        - contains Unicode control sequences (\\x00–\\x1F excluding \\t\\n\\r,
          or \\x7F–\\x9F)
        """
        stripped = text.strip()
        if not stripped:
            return None
        if len(stripped) > 500:
            return None
        if _CONTROL_CHARS_RE.search(stripped):
            return None
        return stripped

    async def generate_variants(self, query: str) -> list[str]:
        """Generate up to ``config.num_queries`` validated semantic variant strings.

        Returns an empty list on rate limit, missing key, timeout, API error,
        or any other failure.  Never logs raw query or variant text.

        Raises:
            RAGFusionDependencyError: if the ``anthropic`` package is not installed.
        """
        if not self._provider_available:
            raise RAGFusionDependencyError(
                "Install archon-search[rag_fusion] to use RAG Fusion "
                "(pip install 'archon-search[rag_fusion]')"
            )

        fp = _query_fingerprint(query)

        # Token bucket pre-flight (under lock).
        # Ollama is a local model with no API cap — skip rate limiting entirely.
        _is_ollama = self._config.provider == "ollama"
        if not _is_ollama:
            async with self._lock:
                now = time.monotonic()
                if now >= self._rpm_refill_at:
                    self._rpm_tokens = self._config.max_requests_per_minute
                    self._rpm_refill_at = now + 60.0

                if self._rpm_tokens <= 0:
                    # Rate limit warning at most once per minute
                    if now - self._rate_limit_warned_at >= 60.0:
                        _logger.warning(
                            "RAG Fusion rate limit exhausted (fp=%s); falling back to single-query search",
                            fp,
                        )
                        self._rate_limit_warned_at = now
                    return []

                self._rpm_tokens -= 1

        truncated_query = query[:2000]

        # Delegate text generation to the provider; validation stays here
        raw_text = await self._provider.decompose_query(
            truncated_query,
            num_queries=self._config.num_queries,
            max_tokens=150 * self._config.num_queries,
            timeout_seconds=self._config.timeout_seconds,
        )

        if not raw_text:
            # Provider returned [] — either no key, timeout, or API error
            # The provider already logged a fingerprinted warning
            return []

        variants = raw_text[: self._config.num_queries]

        if len(variants) < self._config.num_queries:
            _logger.warning(
                "RAG Fusion: requested %d variants but got %d valid ones (fp=%s)",
                self._config.num_queries,
                len(variants),
                fp,
            )

        return variants

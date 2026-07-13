"""HyDE (Hypothetical Document Embeddings) query expansion for archon-search.

Generates a short hypothetical answer passage via an LLM provider and uses its
embedding as the ANN lookup vector in place of the original query embedding.

The ``anthropic`` package is an optional dependency.  The default provider is
``AnthropicQueryExpansionProvider`` (requires ``archon-search[hyde]``).  G10
adds ``OllamaQueryExpansionProvider`` and ``OpenAIQueryExpansionProvider`` as
alternatives (BE-3, BE-6).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archon_search.embedder import Embedder
    from archon_search.query_expansion_protocol import QueryExpansionProvider

from archon_search._privacy import _query_fingerprint
from archon_search.config import HyDEConfig

_logger = logging.getLogger(__name__)


class HyDEGenerator:
    """Generates hypothetical document embeddings for HyDE query expansion.

    Accepts any ``QueryExpansionProvider`` for text generation; the embedding
    step stays inside this generator.  The default provider is
    ``AnthropicQueryExpansionProvider`` (lazy-imported from
    ``archon_search.providers.anthropic_provider``).

    Callers that invoke ``generate()`` with a missing provider package receive
    a ``RuntimeError``.
    """

    def __init__(
        self,
        embedder: "Embedder",
        config: HyDEConfig,
        provider: "QueryExpansionProvider | None" = None,
    ) -> None:
        self._hyde_embedder = embedder
        self._config = config

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
                self._provider_available: bool = True
            except ImportError:
                self._provider = None  # type: ignore[assignment]
                self._provider_available = False

        # Token bucket state for rate limiting (per-process, in-memory)
        self._lock = asyncio.Lock()
        self._rpm_tokens: int = config.max_requests_per_minute
        self._rpm_refill_at: float = time.monotonic() + 60.0

        # One-time warning flags
        self._rate_limit_warned_at: float = 0.0

    def is_key_available(self) -> bool:
        """Return ``True`` when ``ANTHROPIC_API_KEY`` is set in the environment at call time.

        Checked at call time (not at construction) so the status endpoint reflects
        the live environment, not the state at startup.
        """
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    async def generate(self, query: str) -> list[float] | None:
        """Generate a hypothetical document embedding for the given query.

        Returns a vector (list of floats) on success, or ``None`` on any failure
        (timeout, API error, rate limit, missing key, empty response).

        Raises:
            RuntimeError: if the ``anthropic`` package is not installed.
        """
        if not self._provider_available:
            raise RuntimeError(
                "Install archon-search[hyde] to use HyDE (pip install 'archon-search[hyde]')"
            )

        # Token bucket pre-flight (under lock)
        async with self._lock:
            now = time.monotonic()
            if now >= self._rpm_refill_at:
                self._rpm_tokens = self._config.max_requests_per_minute
                self._rpm_refill_at = now + 60.0

            if self._rpm_tokens <= 0:
                # Rate limit warning at most once per minute
                if now - self._rate_limit_warned_at >= 60.0:
                    _logger.warning(
                        "HyDE rate limit exhausted (fp=%s); falling back to query embedding",
                        _query_fingerprint(query),
                    )
                    self._rate_limit_warned_at = now
                return None

            self._rpm_tokens -= 1

        truncated_query = query[:2000]

        # Delegate text generation to the provider; embedding stays here
        hypothesis_text = await self._provider.generate_hypothetical_doc(
            truncated_query,
            max_tokens=200,
            timeout_seconds=self._config.timeout_seconds,
        )

        if hypothesis_text is None:
            return None

        # Embed hypothesis and return vector
        # Hypothesis text is untrusted — never logged verbatim
        return await self._hyde_embedder.embed_one(hypothesis_text)


async def resolve_hyde_vector(
    query: str,
    hyde: bool,
    generator: "HyDEGenerator | None",
    config: HyDEConfig,
) -> tuple[list[float] | None, bool]:
    """Resolve a HyDE query vector.

    Returns ``(hyde_vector, hyde_applied)``.

    ``hyde_vector`` is ``None`` when:
    - ``hyde`` is ``False``
    - ``generator`` is ``None``
    - ``config.enabled`` is ``False`` (operator kill switch)
    - generation fails for any reason

    ``hyde_applied`` is ``True`` only when a non-None vector is returned.
    """
    if not hyde or generator is None or not config.enabled:
        return (None, False)

    vector = await generator.generate(query)
    if vector is not None:
        return (vector, True)
    return (None, False)

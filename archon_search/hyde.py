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
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archon_search.embedder import Embedder
    from archon_search.query_expansion_protocol import QueryExpansionProvider

from archon_search._privacy import _query_fingerprint
from archon_search.config import HyDEConfig
from archon_search.query_expansion_protocol import provider_key_available
from archon_search.uv_tool import extra_install_hint

_logger = logging.getLogger(__name__)

# Wire-safe pair for HyDEDependencyError. The exception message also carries the
# install command for this interpreter (``extra_install_hint``), which describes the
# server's own install — it is logged, never sent to the client.
HYDE_DEPENDENCY_DETAIL = "Install archon-search[hyde] to use HyDE"
HYDE_DEPENDENCY_CODE = "validation_error"


class HyDEDependencyError(RuntimeError):
    """Raised when HyDE is requested but its default provider package is not installed.

    Install with: ``pip install 'archon-search[hyde]'``. Transports answer it with
    :data:`HYDE_DEPENDENCY_DETAIL` / :data:`HYDE_DEPENDENCY_CODE`, never ``str(exc)``.
    """


class HyDEGenerator:
    """Generates hypothetical document embeddings for HyDE query expansion.

    Accepts any ``QueryExpansionProvider`` for text generation; the embedding
    step stays inside this generator, on the embedder the caller passes to
    ``generate()`` — the one serving the searched collection, so the hypothesis
    lands in that collection's vector space.  The default provider is
    ``AnthropicQueryExpansionProvider`` (lazy-imported from
    ``archon_search.providers.anthropic_provider``).

    Callers that invoke ``generate()`` with a missing provider package receive
    a ``HyDEDependencyError``.
    """

    def __init__(
        self,
        config: HyDEConfig,
        provider: QueryExpansionProvider | None = None,
    ) -> None:
        self._config = config

        if provider is not None:
            self._provider = provider
            self._provider_available = True
        else:
            # Default: construct AnthropicQueryExpansionProvider (lazy)
            try:
                from archon_search.providers.anthropic_provider import (
                    AnthropicQueryExpansionProvider,
                )

                self._provider: QueryExpansionProvider = AnthropicQueryExpansionProvider(
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
        """Return ``True`` when the provider's required API key is set at call time (G10 BE-5 Root-2)."""
        return provider_key_available(self._config.provider)

    async def generate(self, query: str, *, embedder: Embedder) -> list[float] | None:
        """Generate a hypothetical document embedding for the given query.

        The hypothesis is embedded with *embedder*, which must be the embedder
        serving the searched collection — any other model yields a vector in the
        wrong space (or the wrong dimension).

        Returns a vector (list of floats) on success, or ``None`` on any failure
        (timeout, API error, rate limit, missing key, empty response).

        Raises:
            HyDEDependencyError: if the ``anthropic`` package is not installed.
        """
        if not self._provider_available:
            raise HyDEDependencyError(f"{HYDE_DEPENDENCY_DETAIL} ({extra_install_hint('hyde')})")

        # Token bucket pre-flight (under lock).
        # Ollama, claude_cli, and llama_cpp are local/free paths with no API cap —
        # skip rate limiting entirely.
        _skip_rate_limit = self._config.provider in ("ollama", "claude_cli", "llama_cpp")
        if not _skip_rate_limit:
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
        return await embedder.embed_one(hypothesis_text)

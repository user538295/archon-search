"""HyDE (Hypothetical Document Embeddings) query expansion for archon-search.

Generates a short hypothetical answer passage via Claude and uses its embedding
as the ANN lookup vector in place of the original query embedding.

Module has no top-level ``import anthropic`` — all anthropic imports happen
lazily inside ``__init__`` under a try/except guard to keep the package optional.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archon_search.embedder import Embedder

from archon_search._privacy import _query_fingerprint
from archon_search.config import HyDEConfig

_logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """\
Write a short passage that would directly answer the following question.
Output only the passage — no preamble, no explanation.

---
{query}
---"""


class HyDEGenerator:
    """Generates hypothetical document embeddings for HyDE query expansion.

    The ``anthropic`` package is an optional dependency; the generator
    initialises without raising even when it is absent.  Callers that
    invoke ``generate()`` without the package receive a ``RuntimeError``.
    """

    def __init__(self, embedder: "Embedder", config: HyDEConfig) -> None:
        self._hyde_embedder = embedder
        self._config = config
        self._anthropic_available: bool = False
        self._client: object | None = None

        try:
            import anthropic  # noqa: PLC0415

            self._anthropic_available = True
            self._client = anthropic.AsyncAnthropic()
            # Store the APIError class for except clauses in generate()
            self._APIError: type[Exception] = anthropic.APIError
        except ImportError:
            self._APIError = Exception  # fallback — never reached in generate()

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

    async def generate(self, query: str) -> list[float] | None:
        """Generate a hypothetical document embedding for the given query.

        Returns a vector (list of floats) on success, or ``None`` on any failure
        (timeout, API error, rate limit, missing key, empty response).

        Raises:
            RuntimeError: if the ``anthropic`` package is not installed.
        """
        if not self._anthropic_available:
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

        # API key check (one-time warning)
        if not self.is_key_available():
            if not self._warned_no_key:
                _logger.warning(
                    "ANTHROPIC_API_KEY is not set; HyDE will not run (fp=%s)",
                    _query_fingerprint(query),
                )
                self._warned_no_key = True
            return None

        truncated_query = query[:2000]
        prompt = _PROMPT_TEMPLATE.format(query=truncated_query)

        try:
            response = await asyncio.wait_for(
                self._client.messages.create(  # type: ignore[union-attr]
                    model=self._config.model,
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=self._config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            _logger.warning(
                "HyDE: LLM call timed out after %.1fs (fp=%s)",
                self._config.timeout_seconds,
                _query_fingerprint(query),
            )
            return None
        except Exception as exc:  # noqa: BLE001
            # Catch anthropic.APIError (and anything else) without importing the class
            # at module level.  We check against self._APIError when it is available.
            if isinstance(exc, self._APIError):
                _logger.warning(
                    "HyDE: Anthropic API error (fp=%s): %s",
                    _query_fingerprint(query),
                    type(exc).__name__,
                )
            else:
                _logger.warning(
                    "HyDE: unexpected error during generation (fp=%s): %s",
                    _query_fingerprint(query),
                    type(exc).__name__,
                )
            return None

        # Extract hypothesis text
        if not response.content:
            _logger.warning(
                "HyDE: empty response content (fp=%s)", _query_fingerprint(query)
            )
            return None

        hypothesis_text = response.content[0].text.strip()
        if not hypothesis_text:
            _logger.warning(
                "HyDE: empty hypothesis text after strip (fp=%s)", _query_fingerprint(query)
            )
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

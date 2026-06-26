"""RAG Fusion (Multi-Query Decomposition) query expansion for archon-search.

Decomposes a user query into N semantic variants via an LLM, enabling
downstream parallel search and second-pass RRF fusion.

Module has no top-level ``import anthropic`` — all anthropic imports happen
lazily inside ``__init__`` under a try/except guard to keep the package optional.

Privacy note: ``generate_variants`` sends the raw query text to Anthropic's API.
Operators who cannot allow this must keep ``[rag_fusion] enabled = false``.
No raw query text is ever written to logs — only fingerprints.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

from archon_search._privacy import _query_fingerprint
from archon_search.config import RAGFusionConfig

_logger = logging.getLogger(__name__)

# Control characters to reject in validated variants.
# Rejects \x00–\x1F (except \t \n \r), \x7F (DEL), and \x80–\x9F (C1 controls).
_CONTROL_CHARS_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]"
)

_PROMPT_TEMPLATE = """\
You are a search query decomposer. Given a user query, generate {num_queries} alternative \
search queries that capture different facets of the same information need.
Rules: each query on its own line, plain text, under 500 characters.
Output exactly {num_queries} queries, one per line.

---
{query}
---"""


class RAGFusionDependencyError(RuntimeError):
    """Raised when the ``anthropic`` package is not installed.

    Install with: ``pip install 'archon-search[rag_fusion]'``
    """


class RAGFusionGenerator:
    """Generates semantic query variants for RAG Fusion.

    The ``anthropic`` package is an optional dependency; the generator
    initialises without raising even when it is absent.  Callers that
    invoke ``generate_variants`` without the package receive a
    ``RAGFusionDependencyError``.
    """

    def __init__(self, config: RAGFusionConfig) -> None:
        self._config = config
        self._anthropic_available: bool = False
        self._client: object | None = None

        try:
            import anthropic  # noqa: PLC0415

            self._anthropic_available = True
            self._client = anthropic.AsyncAnthropic()
            # Store the APIError class for except clauses in generate_variants()
            self._APIError: type[Exception] = anthropic.APIError
        except ImportError:
            self._APIError = Exception  # fallback — never reached in generate_variants()

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
        if not self._anthropic_available:
            raise RAGFusionDependencyError(
                "Install archon-search[rag_fusion] to use RAG Fusion "
                "(pip install 'archon-search[rag_fusion]')"
            )

        fp = _query_fingerprint(query)

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
                        "RAG Fusion rate limit exhausted (fp=%s); falling back to single-query search",
                        fp,
                    )
                    self._rate_limit_warned_at = now
                return []

            self._rpm_tokens -= 1

        # API key check (one-time warning)
        if not self.is_key_available():
            if not self._warned_no_key:
                _logger.warning(
                    "ANTHROPIC_API_KEY is not set; RAG Fusion will not run (fp=%s)",
                    fp,
                )
                self._warned_no_key = True
            return []

        truncated_query = query[:2000]
        prompt = _PROMPT_TEMPLATE.format(
            num_queries=self._config.num_queries,
            query=truncated_query,
        )

        try:
            response = await asyncio.wait_for(
                self._client.messages.create(  # type: ignore[union-attr]
                    model=self._config.model,
                    max_tokens=150 * self._config.num_queries,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=self._config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            _logger.warning(
                "RAG Fusion: LLM call timed out after %.1fs (fp=%s)",
                self._config.timeout_seconds,
                fp,
            )
            raise
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, self._APIError):
                _logger.warning(
                    "RAG Fusion: Anthropic API error (fp=%s): %s",
                    fp,
                    type(exc).__name__,
                )
            else:
                _logger.warning(
                    "RAG Fusion: unexpected error during variant generation (fp=%s): %s",
                    fp,
                    type(exc).__name__,
                )
            raise

        if not response.content:
            _logger.warning("RAG Fusion: empty response content (fp=%s)", fp)
            return []

        raw_text = response.content[0].text

        # Parse: one variant per line, validate each, truncate to num_queries
        lines = raw_text.split("\n")
        variants: list[str] = []
        for line in lines:
            validated = self._validate_variant(line)
            if validated is not None:
                variants.append(validated)
            if len(variants) >= self._config.num_queries:
                break

        if len(variants) < self._config.num_queries:
            _logger.warning(
                "RAG Fusion: requested %d variants but got %d valid ones (fp=%s)",
                self._config.num_queries,
                len(variants),
                fp,
            )

        return variants

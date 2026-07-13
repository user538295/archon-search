"""AnthropicQueryExpansionProvider — Interface Adapter for Anthropic (G10 BE-1).

Implements the QueryExpansionProvider protocol using the Anthropic API.
The ``anthropic`` package is imported lazily inside ``__init__`` so that the
provider can be instantiated even when the package is absent — a
``RuntimeError`` is raised at that point (caught by the generator).

Privacy: raw query text is never logged; only ``_query_fingerprint()`` tokens
appear in log messages.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

from archon_search._privacy import _query_fingerprint

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_logger = logging.getLogger(__name__)

_HYDE_PROMPT_TEMPLATE = """\
Write a short passage that would directly answer the following question.
Output only the passage — no preamble, no explanation.

---
{query}
---"""

_RAG_FUSION_PROMPT_TEMPLATE = """\
You are a search query decomposer. Given a user query, generate {num_queries} alternative \
search queries that capture different facets of the same information need.
Rules: each query on its own line, plain text, under 500 characters.
Output exactly {num_queries} queries, one per line.

---
{query}
---"""


class AnthropicQueryExpansionProvider:
    """Anthropic implementation of the QueryExpansionProvider protocol.

    Lazy-imports ``anthropic`` in ``__init__`` to check availability; the
    client is constructed on first use (not at ``__init__`` time) so that test
    fixtures that patch ``AsyncAnthropic`` after construction do not conflict.

    A ``RuntimeError`` is raised at construction time only if the package is
    absent (caught by the generator's ``except ImportError`` guard).
    """

    def __init__(self, model: str) -> None:
        self._model = model
        self._client: object | None = None
        self._anthropic_available: bool = False
        self._APIError: type[Exception] = Exception  # fallback

        try:
            import anthropic  # noqa: PLC0415

            self._anthropic_available = True
            # Store the module reference for lazy client construction on first use.
            self._anthropic_module = anthropic
            self._APIError = anthropic.APIError
        except ImportError:
            pass

        # One-time API-key warning flag
        self._warned_no_key: bool = False

    def _get_client(self) -> object:
        """Return (and lazily construct) the AsyncAnthropic client."""
        if self._client is None:
            self._client = self._anthropic_module.AsyncAnthropic()
        return self._client

    def is_key_available(self) -> bool:
        """Return ``True`` when ``ANTHROPIC_API_KEY`` is set in the environment."""
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        return True

    async def generate_hypothetical_doc(
        self,
        query: str,
        *,
        max_tokens: int = 200,
        timeout_seconds: float = 10.0,
    ) -> str | None:
        """Generate a short hypothetical passage that would answer the query.

        Returns hypothesis text as a plain ``str``, or ``None`` on any failure.
        Never raises.
        """
        if not self._anthropic_available:
            _logger.warning(
                "AnthropicQueryExpansionProvider: anthropic package not available (fp=%s)",
                _query_fingerprint(query),
            )
            return None

        if not self.is_key_available():
            if not self._warned_no_key:
                _logger.warning(
                    "ANTHROPIC_API_KEY is not set; HyDE provider will not run (fp=%s)",
                    _query_fingerprint(query),
                )
                self._warned_no_key = True
            return None

        prompt = _HYDE_PROMPT_TEMPLATE.format(query=query)

        try:
            response = await asyncio.wait_for(
                self._get_client().messages.create(  # type: ignore[union-attr]
                    model=self._model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            _logger.warning(
                "AnthropicQueryExpansionProvider: HyDE call timed out after %.1fs (fp=%s)",
                timeout_seconds,
                _query_fingerprint(query),
            )
            return None
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, self._APIError):
                _logger.warning(
                    "AnthropicQueryExpansionProvider: Anthropic API error for HyDE (fp=%s): %s",
                    _query_fingerprint(query),
                    type(exc).__name__,
                )
            else:
                _logger.warning(
                    "AnthropicQueryExpansionProvider: unexpected error for HyDE (fp=%s): %s",
                    _query_fingerprint(query),
                    type(exc).__name__,
                )
            return None

        if not response.content:
            _logger.warning(
                "AnthropicQueryExpansionProvider: empty HyDE response content (fp=%s)",
                _query_fingerprint(query),
            )
            return None

        hypothesis_text = response.content[0].text.strip()
        if not hypothesis_text:
            _logger.warning(
                "AnthropicQueryExpansionProvider: empty hypothesis text after strip (fp=%s)",
                _query_fingerprint(query),
            )
            return None

        return hypothesis_text

    async def decompose_query(
        self,
        query: str,
        *,
        num_queries: int = 3,
        max_tokens: int = 450,
        timeout_seconds: float = 10.0,
    ) -> list[str]:
        """Decompose the query into semantic variant strings.

        Returns a list of variant strings, or ``[]`` on any failure.
        Never raises.
        """
        if not self._anthropic_available:
            _logger.warning(
                "AnthropicQueryExpansionProvider: anthropic package not available (fp=%s)",
                _query_fingerprint(query),
            )
            return []

        if not self.is_key_available():
            if not self._warned_no_key:
                _logger.warning(
                    "ANTHROPIC_API_KEY is not set; RAG Fusion provider will not run (fp=%s)",
                    _query_fingerprint(query),
                )
                self._warned_no_key = True
            return []

        prompt = _RAG_FUSION_PROMPT_TEMPLATE.format(
            num_queries=num_queries,
            query=query,
        )

        try:
            response = await asyncio.wait_for(
                self._get_client().messages.create(  # type: ignore[union-attr]
                    model=self._model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            _logger.warning(
                "AnthropicQueryExpansionProvider: RAG Fusion call timed out after %.1fs (fp=%s)",
                timeout_seconds,
                _query_fingerprint(query),
            )
            return []
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, self._APIError):
                _logger.warning(
                    "AnthropicQueryExpansionProvider: Anthropic API error for RAG Fusion (fp=%s): %s",
                    _query_fingerprint(query),
                    type(exc).__name__,
                )
            else:
                _logger.warning(
                    "AnthropicQueryExpansionProvider: unexpected error for RAG Fusion (fp=%s): %s",
                    _query_fingerprint(query),
                    type(exc).__name__,
                )
            return []

        if not response.content:
            _logger.warning(
                "AnthropicQueryExpansionProvider: empty RAG Fusion response content (fp=%s)",
                _query_fingerprint(query),
            )
            return []

        raw_text = response.content[0].text

        # Parse: one variant per line, validate each, truncate to num_queries
        lines = raw_text.split("\n")
        variants: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if len(stripped) > 500:
                continue
            if _CONTROL_CHARS_RE.search(stripped):
                continue
            variants.append(stripped)
            if len(variants) >= num_queries:
                break

        if len(variants) < num_queries:
            _logger.warning(
                "AnthropicQueryExpansionProvider: requested %d variants but got %d valid ones (fp=%s)",
                num_queries,
                len(variants),
                _query_fingerprint(query),
            )

        return variants

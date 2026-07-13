"""OpenAIQueryExpansionProvider — Interface Adapter for OpenAI (G10 BE-6).

Implements the QueryExpansionProvider protocol using the OpenAI Python SDK.
The ``openai`` package is imported lazily inside ``__init__`` so that the
provider can be instantiated even when the package is absent — callers
check availability via the ``_openai_available`` flag.

Privacy: raw query text is never logged; only ``_query_fingerprint()`` tokens
appear in log messages.

Rate limiting: NOT implemented — the token bucket is at the generator's call
site (HyDEGenerator / RAGFusionGenerator), not in the adapter (Root-4).
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


class OpenAIQueryExpansionProvider:
    """OpenAI implementation of the QueryExpansionProvider protocol.

    Lazy-imports ``openai`` in ``__init__`` to check availability.
    Uses ``AsyncOpenAI.chat.completions.create()`` so the response shape is
    ``response.choices[0].message.content`` (DA-ARCH-C1-I-8 normalization).

    Errors are silenced internally — returns ``None``/``[]`` on any failure.
    Never raises to callers (C1 adapter contract).

    Rate limiting is NOT implemented here. The token bucket lives in the
    generator's call site (Root-4).
    """

    def __init__(
        self,
        model: str,
    ) -> None:
        self._model = model
        self._openai_available: bool = False
        self._openai_module: object | None = None
        self._client: object | None = None

        try:
            import openai  # noqa: PLC0415

            self._openai_available = True
            self._openai_module = openai
        except ImportError:
            pass

        self._warned_no_key: bool = False

    def _get_client(self) -> object:
        """Return (and lazily construct) the AsyncOpenAI client."""
        assert self._openai_module is not None  # guarded by _openai_available
        if self._client is None:
            self._client = self._openai_module.AsyncOpenAI()  # type: ignore[union-attr]
        return self._client

    def is_key_available(self) -> bool:
        """Return ``True`` when ``OPENAI_API_KEY`` is set in the environment."""
        return bool(os.environ.get("OPENAI_API_KEY"))

    async def generate_hypothetical_doc(
        self,
        query: str,
        *,
        max_tokens: int = 200,
        timeout_seconds: float = 10.0,
    ) -> str | None:
        """Generate a short hypothetical passage that would answer the query.

        Returns hypothesis text as a plain ``str`` (normalized from
        ``response.choices[0].message.content``), or ``None`` on any failure.
        Never raises.
        """
        if not self._openai_available:
            _logger.warning(
                "OpenAIQueryExpansionProvider: openai package not available (fp=%s)",
                _query_fingerprint(query),
            )
            return None

        if not self.is_key_available():
            if not self._warned_no_key:
                _logger.warning(
                    "OPENAI_API_KEY is not set; HyDE provider will not run (fp=%s)",
                    _query_fingerprint(query),
                )
                self._warned_no_key = True
            return None

        prompt = _HYDE_PROMPT_TEMPLATE.format(query=query)
        client = self._get_client()

        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(  # type: ignore[union-attr]
                    model=self._model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            _logger.warning(
                "OpenAIQueryExpansionProvider: HyDE call timed out after %.1fs (fp=%s)",
                timeout_seconds,
                _query_fingerprint(query),
            )
            return None
        except Exception:  # noqa: BLE001
            _logger.warning(
                "OpenAIQueryExpansionProvider: error during HyDE generation (fp=%s)",
                _query_fingerprint(query),
            )
            return None

        # Normalize: OpenAI chat response shape is response.choices[0].message.content
        text: str | None = None
        try:
            text = response.choices[0].message.content  # type: ignore[union-attr]
        except (AttributeError, IndexError):
            pass

        if text is not None and not isinstance(text, str):
            _logger.warning(
                "OpenAIQueryExpansionProvider: unexpected non-str content in HyDE response (fp=%s)",
                _query_fingerprint(query),
            )
            return None

        if not text or not text.strip():
            _logger.warning(
                "OpenAIQueryExpansionProvider: empty HyDE response (fp=%s)",
                _query_fingerprint(query),
            )
            return None

        return text.strip()

    async def decompose_query(
        self,
        query: str,
        *,
        num_queries: int = 3,
        max_tokens: int = 450,
        timeout_seconds: float = 10.0,
    ) -> list[str]:
        """Decompose the query into semantic variant strings.

        Returns a list of variant strings (normalized from
        ``response.choices[0].message.content``), or ``[]`` on any failure.
        Never raises.
        """
        if not self._openai_available:
            _logger.warning(
                "OpenAIQueryExpansionProvider: openai package not available (fp=%s)",
                _query_fingerprint(query),
            )
            return []

        if not self.is_key_available():
            if not self._warned_no_key:
                _logger.warning(
                    "OPENAI_API_KEY is not set; RAG Fusion provider will not run (fp=%s)",
                    _query_fingerprint(query),
                )
                self._warned_no_key = True
            return []

        prompt = _RAG_FUSION_PROMPT_TEMPLATE.format(
            num_queries=num_queries,
            query=query,
        )
        client = self._get_client()

        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(  # type: ignore[union-attr]
                    model=self._model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            _logger.warning(
                "OpenAIQueryExpansionProvider: RAG Fusion call timed out after %.1fs (fp=%s)",
                timeout_seconds,
                _query_fingerprint(query),
            )
            return []
        except Exception:  # noqa: BLE001
            _logger.warning(
                "OpenAIQueryExpansionProvider: error during RAG Fusion decomposition (fp=%s)",
                _query_fingerprint(query),
            )
            return []

        raw_text: str | None = None
        try:
            raw_text = response.choices[0].message.content  # type: ignore[union-attr]
        except (AttributeError, IndexError):
            pass

        if raw_text is not None and not isinstance(raw_text, str):
            _logger.warning(
                "OpenAIQueryExpansionProvider: unexpected non-str content in RAG Fusion response (fp=%s)",
                _query_fingerprint(query),
            )
            return []

        if not raw_text:
            _logger.warning(
                "OpenAIQueryExpansionProvider: empty RAG Fusion response (fp=%s)",
                _query_fingerprint(query),
            )
            return []

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
                "OpenAIQueryExpansionProvider: requested %d variants but got %d valid ones (fp=%s)",
                num_queries,
                len(variants),
                _query_fingerprint(query),
            )

        return variants

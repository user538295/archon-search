"""OllamaQueryExpansionProvider — Interface Adapter for Ollama (G10 BE-3).

Implements the QueryExpansionProvider protocol using the Ollama Python SDK.
The ``ollama`` package is imported lazily inside ``__init__`` so that the
provider can be instantiated even when the package is absent — callers
check availability via the ``_ollama_available`` flag.

Privacy: raw query text is never logged; only ``_query_fingerprint()`` tokens
appear in log messages.

Rate limiting: NOT implemented — Ollama is a local model with no API cap.
Skipping the token bucket for Ollama at the generator's call site is BE-4's
responsibility (Root-4) and is not yet wired up.
"""
from __future__ import annotations

import asyncio
import logging
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


class OllamaQueryExpansionProvider:
    """Ollama implementation of the QueryExpansionProvider protocol.

    Lazy-imports ``ollama`` in ``__init__`` to check availability.
    Uses ``AsyncClient.chat()`` so the response shape is ``response.message.content``
    (DA-ARCH-C1-I-8 normalization).

    Errors are silenced internally — returns ``None``/``[]`` on any failure.
    Never raises to callers (C1 adapter contract).

    Rate limiting is NOT implemented here. Skipping the token bucket at the
    generator's call site is BE-4's responsibility (Root-4) and is not yet
    wired up.
    """

    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
    ) -> None:
        self._model = model
        self._base_url = base_url
        self._ollama_available: bool = False
        self._ollama_module: object | None = None

        try:
            import ollama  # noqa: PLC0415

            self._ollama_available = True
            self._ollama_module = ollama
        except ImportError:
            pass

    def _get_client(self) -> object:
        """Return a new AsyncClient pointing at the configured base URL."""
        assert self._ollama_module is not None  # guarded by _ollama_available
        return self._ollama_module.AsyncClient(host=self._base_url)  # type: ignore[union-attr]

    def is_key_available(self) -> bool:
        """Ollama has no API key — always returns ``True``."""
        return True

    async def generate_hypothetical_doc(
        self,
        query: str,
        *,
        max_tokens: int = 200,
        timeout_seconds: float = 10.0,
    ) -> str | None:
        """Generate a short hypothetical passage that would answer the query.

        Returns hypothesis text as a plain ``str`` (normalized from
        ``response.message.content``), or ``None`` on any failure. Never raises.
        """
        if not self._ollama_available:
            _logger.warning(
                "OllamaQueryExpansionProvider: ollama package not available (fp=%s)",
                _query_fingerprint(query),
            )
            return None

        prompt = _HYDE_PROMPT_TEMPLATE.format(query=query)
        client = self._get_client()

        try:
            response = await asyncio.wait_for(
                client.chat(  # type: ignore[union-attr]
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    options={"num_predict": max_tokens},
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            _logger.warning(
                "OllamaQueryExpansionProvider: HyDE call timed out after %.1fs (fp=%s)",
                timeout_seconds,
                _query_fingerprint(query),
            )
            return None
        except Exception:  # noqa: BLE001
            _logger.warning(
                "OllamaQueryExpansionProvider: error during HyDE generation (fp=%s)",
                _query_fingerprint(query),
            )
            return None

        # Normalize: Ollama chat response shape is response.message.content
        text: str | None = None
        try:
            text = response.message.content  # type: ignore[union-attr]
        except AttributeError:
            pass

        if text is not None and not isinstance(text, str):
            _logger.warning(
                "OllamaQueryExpansionProvider: unexpected non-str content in HyDE response (fp=%s)",
                _query_fingerprint(query),
            )
            return None

        if not text or not text.strip():
            _logger.warning(
                "OllamaQueryExpansionProvider: empty HyDE response (fp=%s)",
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
        ``response.message.content``), or ``[]`` on any failure. Never raises.
        """
        if not self._ollama_available:
            _logger.warning(
                "OllamaQueryExpansionProvider: ollama package not available (fp=%s)",
                _query_fingerprint(query),
            )
            return []

        prompt = _RAG_FUSION_PROMPT_TEMPLATE.format(
            num_queries=num_queries,
            query=query,
        )
        client = self._get_client()

        try:
            response = await asyncio.wait_for(
                client.chat(  # type: ignore[union-attr]
                    model=self._model,
                    messages=[{"role": "user", "content": prompt}],
                    options={"num_predict": max_tokens},
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            _logger.warning(
                "OllamaQueryExpansionProvider: RAG Fusion call timed out after %.1fs (fp=%s)",
                timeout_seconds,
                _query_fingerprint(query),
            )
            return []
        except Exception:  # noqa: BLE001
            _logger.warning(
                "OllamaQueryExpansionProvider: error during RAG Fusion decomposition (fp=%s)",
                _query_fingerprint(query),
            )
            return []

        raw_text: str | None = None
        try:
            raw_text = response.message.content  # type: ignore[union-attr]
        except AttributeError:
            pass

        if raw_text is not None and not isinstance(raw_text, str):
            _logger.warning(
                "OllamaQueryExpansionProvider: unexpected non-str content in RAG Fusion response (fp=%s)",
                _query_fingerprint(query),
            )
            return []

        if not raw_text:
            _logger.warning(
                "OllamaQueryExpansionProvider: empty RAG Fusion response (fp=%s)",
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
                "OllamaQueryExpansionProvider: requested %d variants but got %d valid ones (fp=%s)",
                num_queries,
                len(variants),
                _query_fingerprint(query),
            )

        return variants

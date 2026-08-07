"""LlamaCppQueryExpansionProvider — Interface Adapter for llama.cpp (BE-2).

httpx-based OpenAI-compatible client for a locally running llama-server
(``POST /v1/chat/completions``). ``httpx`` is a core dependency, so unlike the
``openai``/``ollama`` adapters there is no lazy-import availability guard.

The response body is a plain ``dict`` (``response.json()``), not an SDK
object with attribute access — normalisation is guarded with
``(KeyError, IndexError, TypeError)``, not the SDK adapters'
``(AttributeError, IndexError)``.

Privacy: raw query text is never logged; only ``_query_fingerprint()`` tokens
appear in log messages.

Rate limiting: NOT implemented — llama-server is local/unthrottled, mirroring
the Ollama local-mode skip at the generator's call site.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from archon_search._privacy import _query_fingerprint
from archon_search.config import LLAMA_CPP_BASE_URL_DEFAULT

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


class LlamaCppQueryExpansionProvider:
    """llama.cpp implementation of the QueryExpansionProvider protocol.

    Talks to a local llama-server's OpenAI-compatible
    ``POST /v1/chat/completions`` endpoint. The response shape is
    ``data["choices"][0]["message"]["content"]``.

    Errors are silenced internally — returns ``None``/``[]`` on any failure.
    Never raises to callers (C1 adapter contract).

    Rate limiting is NOT implemented here — llama-server is local/unthrottled.
    """

    def __init__(
        self,
        model: str,
        base_url: str = LLAMA_CPP_BASE_URL_DEFAULT,
    ) -> None:
        self._model = model
        self._base_url = base_url

    def is_key_available(self) -> bool:
        """llama.cpp has no API key — always returns ``True``."""
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
        ``data["choices"][0]["message"]["content"]``), or ``None`` on any
        failure. Never raises.
        """
        prompt = _HYDE_PROMPT_TEMPLATE.format(query=query)
        data = await self._post_chat_completion(query, prompt, max_tokens, timeout_seconds)
        if data is None:
            return None

        text = self._extract_content(query, data)
        if text is not None and not isinstance(text, str):
            _logger.warning(
                "LlamaCppQueryExpansionProvider: unexpected non-str content in HyDE response (fp=%s)",
                _query_fingerprint(query),
            )
            return None

        if not text or not text.strip():
            _logger.warning(
                "LlamaCppQueryExpansionProvider: empty HyDE response (fp=%s)",
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
        ``data["choices"][0]["message"]["content"]``), or ``[]`` on any
        failure. Never raises.
        """
        prompt = _RAG_FUSION_PROMPT_TEMPLATE.format(num_queries=num_queries, query=query)
        data = await self._post_chat_completion(query, prompt, max_tokens, timeout_seconds)
        if data is None:
            return []

        raw_text = self._extract_content(query, data)
        if raw_text is not None and not isinstance(raw_text, str):
            _logger.warning(
                "LlamaCppQueryExpansionProvider: unexpected non-str content in RAG Fusion response (fp=%s)",
                _query_fingerprint(query),
            )
            return []

        if not raw_text:
            _logger.warning(
                "LlamaCppQueryExpansionProvider: empty RAG Fusion response (fp=%s)",
                _query_fingerprint(query),
            )
            return []

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
                "LlamaCppQueryExpansionProvider: requested %d variants but got %d valid ones (fp=%s)",
                num_queries,
                len(variants),
                _query_fingerprint(query),
            )

        return variants

    async def _post_chat_completion(
        self,
        query: str,
        prompt: str,
        max_tokens: int,
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        """POST to llama-server's OpenAI-compatible chat completions endpoint.

        Returns the parsed JSON body, or ``None`` on any transport failure
        (connection refused, timeout, non-2xx status — including 503 for a
        loading/absent model). Never raises.
        """
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=timeout_seconds
            ) as client:
                response = await client.post("/v1/chat/completions", json=payload)
                response.raise_for_status()
                return response.json()
        except httpx.ConnectError:
            _logger.warning(
                "LlamaCppQueryExpansionProvider: connection error (fp=%s)",
                _query_fingerprint(query),
            )
            return None
        except httpx.TimeoutException:
            _logger.warning(
                "LlamaCppQueryExpansionProvider: call timed out after %.1fs (fp=%s)",
                timeout_seconds,
                _query_fingerprint(query),
            )
            return None
        except httpx.HTTPStatusError as exc:
            _logger.warning(
                "LlamaCppQueryExpansionProvider: HTTP error %s (fp=%s)",
                exc.response.status_code,
                _query_fingerprint(query),
            )
            return None
        except json.JSONDecodeError:
            _logger.warning(
                "LlamaCppQueryExpansionProvider: malformed JSON response body (fp=%s)",
                _query_fingerprint(query),
            )
            return None

    @staticmethod
    def _extract_content(query: str, data: dict[str, Any]) -> str | None:
        """Normalize ``data["choices"][0]["message"]["content"]``.

        Guards ``(KeyError, IndexError, TypeError)`` — the response is a
        plain dict from ``response.json()``, not an SDK object with
        attribute access.
        """
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            _logger.warning(
                "LlamaCppQueryExpansionProvider: malformed response body (fp=%s)",
                _query_fingerprint(query),
            )
            return None

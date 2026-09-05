"""OllamaEnrichmentClient — Interface Adapters layer (LLCP BE-6).

Concrete implementation of LLMEnrichmentClientProtocol backed by a local
Ollama server's OpenAI-compatible ``POST /v1/chat/completions`` endpoint,
over raw ``httpx`` — deliberately **not** the ``ollama`` SDK used by
``providers/ollama_provider.py`` (query-expansion adapter); httpx is a core
dependency, so enrichment needs no lazy-import availability guard.

Narrowed to community summarisation only (BE-17, ADR 12).

Design decisions (C2 contract):
- Raises on any transport failure (connection error, timeout, non-2xx
  status, whole-body JSON parse failure). Callers (CommunityBuilder) catch
  and substitute None.
- Model string format: bare model id (caller has already parsed any
  "provider:model" prefix).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

_SUMMARIZE_PROMPT_TEMPLATE = """\
You are a knowledge-graph summariser. Given a set of representative text passages and \
the entity names extracted from them, write a single concise paragraph that describes \
what this cluster of entities represents and how they relate.

Output only the paragraph — no preamble, no bullet points, no headings.

Entities: {entity_names}

Passages:
{chunk_texts}
"""


class OllamaEnrichmentClient:
    """Ollama implementation of LLMEnrichmentClientProtocol.

    Talks to a local Ollama server's OpenAI-compatible
    ``POST /v1/chat/completions`` endpoint via raw ``httpx``. The response
    shape is ``data["choices"][0]["message"]["content"]`` (plain dict).

    Raises on any transport failure. Callers must catch all exceptions and
    substitute None / empty list.
    """

    def __init__(self, model: str, config: Any) -> None:
        """Initialise the client.

        Args:
            model: Bare model ID.
            config: Any object with attributes:
                - ``ollama_base_url: str``
                - ``extraction_timeout_seconds: float``
                - ``extraction_token_budget: int``
        """
        self._model = model
        self._base_url = config.ollama_base_url
        self._timeout = config.extraction_timeout_seconds
        self._token_budget = config.extraction_token_budget

    async def summarize_community(
        self,
        chunk_texts: list[str],
        entity_names: list[str],
    ) -> str | None:
        """Generate an abstractive summary for a community.

        Raises on any transport failure. Returns None only when the LLM
        response is empty or malformed.
        """
        prompt = _SUMMARIZE_PROMPT_TEMPLATE.format(
            entity_names=", ".join(entity_names),
            chunk_texts="\n\n---\n\n".join(chunk_texts),
        )

        data = await self._post_chat_completion(prompt)
        text = self._extract_content(data)
        if text is None:
            return None

        # _extract_content already filters empty/whitespace content and returns
        # None for it, so `text` here is always non-empty after strip().
        return text.strip()

    async def _post_chat_completion(self, prompt: str) -> dict[str, Any]:
        """POST to Ollama's OpenAI-compatible chat completions endpoint.

        Raises on any transport failure (connection error, timeout, non-2xx
        status, malformed JSON response body). Never swallows errors.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._token_budget,
            "messages": [{"role": "user", "content": prompt}],
        }

        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            response = await client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str | None:
        """Normalize ``data["choices"][0]["message"]["content"]``.

        Returns None on a missing/malformed shape, non-str content, or
        empty/whitespace-only content — all treated the same as an
        unusable reply, not a transport failure; a WARNING is logged in
        every case.
        """
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            _logger.warning("OllamaEnrichmentClient: malformed response body")
            return None

        if not isinstance(content, str):
            _logger.warning("OllamaEnrichmentClient: unexpected non-str content in response")
            return None

        if not content.strip():
            finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
            if finish_reason:
                _logger.warning(
                    "OllamaEnrichmentClient: reply had no usable content "
                    "(finish_reason=%r); treating as an unusable reply, not a "
                    "genuine empty result",
                    finish_reason,
                )
            else:
                _logger.warning(
                    "OllamaEnrichmentClient: reply had no usable content; treating "
                    "as an unusable reply, not a genuine empty result"
                )
            return None

        return content

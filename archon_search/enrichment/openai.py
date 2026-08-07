"""OpenAIEnrichmentClient — Interface Adapters layer (LLCP BE-6).

Concrete implementation of LLMEnrichmentClientProtocol backed by OpenAI's
``POST /v1/chat/completions`` endpoint, over raw ``httpx`` — deliberately
**not** the ``openai`` SDK used by ``providers/openai_provider.py``
(query-expansion adapter); httpx is a core dependency, so enrichment needs
no lazy-import availability guard.

Design decisions (C2 contract):
- Raises on any transport failure (connection error, timeout, non-2xx
  status, whole-body JSON parse failure) — including a missing
  ``OPENAI_API_KEY``, surfaced as a ``RuntimeError``. Callers
  (CommunityBuilder, GraphExtractor) catch and substitute None / [].
- Per-item skip vs whole-call raise: individual unparseable relationship
  items are skipped with a WARNING; the call raises only on transport
  failure or a whole-body JSON parse failure (S19).
- Model string format: bare model id (caller has already parsed any
  "provider:model" prefix).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from archon_search.enrichment import _VALID_RELATIONSHIP_TYPES
from archon_search.graph_enrichment_protocol import LabeledRelationship

_logger = logging.getLogger(__name__)

_OPENAI_BASE_URL = "https://api.openai.com"

_SUMMARIZE_PROMPT_TEMPLATE = """\
You are a knowledge-graph summariser. Given a set of representative text passages and \
the entity names extracted from them, write a single concise paragraph that describes \
what this cluster of entities represents and how they relate.

Output only the paragraph — no preamble, no bullet points, no headings.

Entities: {entity_names}

Passages:
{chunk_texts}
"""

_LABEL_PROMPT_TEMPLATE = """\
You are a relationship classifier. Given a text passage and a list of entity pairs, \
classify the relationship between each pair. Use exactly one of these types:
- uses
- implements
- depends_on

Respond with a JSON array. Each element must have exactly these keys:
  "source_entity", "target_entity", "relationship_type"

Only include pairs where the text clearly supports a relationship. \
Omit pairs where the relationship is unclear.

Text:
{chunk_text}

Entity pairs to classify:
{entity_pairs}
"""


class OpenAIEnrichmentClient:
    """OpenAI implementation of LLMEnrichmentClientProtocol.

    Talks to OpenAI's ``POST /v1/chat/completions`` endpoint via raw
    ``httpx``. The response shape is
    ``data["choices"][0]["message"]["content"]`` (plain dict).

    Raises on any transport failure, including a missing API key. Callers
    must catch all exceptions and substitute None / empty list.
    """

    def __init__(self, model: str, config: Any) -> None:
        """Initialise the client.

        Args:
            model: Bare model ID (e.g. ``"gpt-4o-mini"``).
            config: Any object with attributes:
                - ``extraction_timeout_seconds: float``
                - ``extraction_token_budget: int``
        """
        self._model = model
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

        text = text.strip()
        return text if text else None

    async def label_relationships(
        self,
        entity_pairs: list[tuple[str, str]],
        chunk_text: str,
    ) -> list[LabeledRelationship]:
        """Label relationships between entity pairs using the chunk text as context.

        Raises on transport failure or a whole-body JSON parse failure.
        Individual unparseable items are skipped with a WARNING.
        """
        pairs_text = "\n".join(f"- {a} / {b}" for a, b in entity_pairs)
        prompt = _LABEL_PROMPT_TEMPLATE.format(
            chunk_text=chunk_text,
            entity_pairs=pairs_text,
        )

        data = await self._post_chat_completion(prompt)
        raw_text = self._extract_content(data)
        if not raw_text:
            return []

        # Whole-body JSON parse failure raises (C2 contract) — not caught here.
        parsed = json.loads(raw_text)

        if not isinstance(parsed, list):
            raise ValueError(f"Expected JSON array from LLM, got {type(parsed).__name__}")

        results: list[LabeledRelationship] = []
        for item in parsed:
            try:
                rel_type = item.get("relationship_type", "")
                if rel_type not in _VALID_RELATIONSHIP_TYPES:
                    _logger.warning(
                        "OpenAIEnrichmentClient: unknown relationship_type %r; "
                        "skipping pair (%r, %r)",
                        rel_type,
                        item.get("source_entity"),
                        item.get("target_entity"),
                    )
                    continue
                results.append(
                    LabeledRelationship(
                        source_entity=item["source_entity"],
                        target_entity=item["target_entity"],
                        relationship_type=rel_type,
                    )
                )
            except (KeyError, AttributeError) as exc:
                _logger.warning(
                    "OpenAIEnrichmentClient: malformed relationship item %r: %s; skipping",
                    item,
                    exc,
                )
                continue

        return results

    async def _post_chat_completion(self, prompt: str) -> dict[str, Any]:
        """POST to OpenAI's chat completions endpoint.

        Raises ``RuntimeError`` when ``OPENAI_API_KEY`` is unset, and
        propagates any transport failure (connection error, timeout,
        non-2xx status, malformed JSON response body). Never swallows
        errors.
        """
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set; OpenAI enrichment cannot run")

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._token_budget,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {"Authorization": f"Bearer {api_key}"}

        async with httpx.AsyncClient(
            base_url=_OPENAI_BASE_URL, timeout=self._timeout
        ) as client:
            response = await client.post(
                "/v1/chat/completions", json=payload, headers=headers
            )
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str | None:
        """Normalize ``data["choices"][0]["message"]["content"]``.

        Returns None on a missing/malformed shape or non-str content —
        treated the same as an empty response, not a transport failure.
        """
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            _logger.warning("OpenAIEnrichmentClient: malformed response body")
            return None

        if not isinstance(content, str):
            _logger.warning("OpenAIEnrichmentClient: unexpected non-str content in response")
            return None

        return content

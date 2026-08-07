"""LlamaCppEnrichmentClient — Interface Adapters layer (LLCP BE-6).

Concrete implementation of LLMEnrichmentClientProtocol backed by a local
llama-server's OpenAI-compatible ``POST /v1/chat/completions`` endpoint,
over raw ``httpx`` (httpx is a core dependency — no lazy-import guard,
mirroring ``providers/llama_cpp_provider.py``).

Design decisions (C2 contract):
- Raises on any transport failure (connection error, timeout, non-2xx status,
  whole-body JSON parse failure) — the inverse of the C1 query-expansion
  contract. Callers (CommunityBuilder, GraphExtractor) catch and substitute
  None / [].
- No rate limiting — llama-server is local/unthrottled; unlike
  AnthropicEnrichmentClient, this client never calls ``_check_rate_limit``
  and ignores ``GraphConfig.extraction_rate_limit_rpm`` (S26).
- ``label_relationships`` constrains output via
  ``response_format: {"type": "json_schema", ...}`` limited to the narrowed
  3-value ``_VALID_RELATIONSHIP_TYPES`` subset — local small models do not
  reliably produce structured output unconstrained. HTTP 422 is the
  canonical signal that the server rejects the ``json_schema`` format; on
  422 the client falls back to a prompt-only request (S24a).
- Per-item skip vs whole-call raise: individual unparseable relationship
  items are skipped with a WARNING; the call raises only on transport
  failure or a whole-body JSON parse failure (S19, S24b).
- Model string format: bare model id (caller has already parsed any
  "provider:model" prefix).
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from archon_search.enrichment import _VALID_RELATIONSHIP_TYPES
from archon_search.graph_enrichment_protocol import LabeledRelationship

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

# json_schema response_format constraining label_relationships output to the
# narrowed 3-value relationship-type subset (see ADR C6-local-llm-provider.md).
_LABEL_RELATIONSHIPS_JSON_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "labeled_relationships",
        "schema": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_entity": {"type": "string"},
                    "target_entity": {"type": "string"},
                    "relationship_type": {
                        "type": "string",
                        "enum": sorted(_VALID_RELATIONSHIP_TYPES),
                    },
                },
                "required": ["source_entity", "target_entity", "relationship_type"],
            },
        },
    },
}


class LlamaCppEnrichmentClient:
    """llama.cpp (llama-server) implementation of LLMEnrichmentClientProtocol.

    Talks to a local llama-server's OpenAI-compatible
    ``POST /v1/chat/completions`` endpoint. The response shape is
    ``data["choices"][0]["message"]["content"]`` (plain dict, not SDK object).

    Raises on any transport failure. Callers must catch all exceptions and
    substitute None / empty list.
    """

    def __init__(self, model: str, config: Any) -> None:
        """Initialise the client.

        Args:
            model: Bare model ID.
            config: Any object with attributes:
                - ``llama_cpp_base_url: str``
                - ``extraction_timeout_seconds: float``
                - ``extraction_token_budget: int``
        """
        self._model = model
        self._base_url = config.llama_cpp_base_url
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

        data = await self._post_chat_completion(prompt, response_format=None)
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

        try:
            data = await self._post_chat_completion(
                prompt, response_format=_LABEL_RELATIONSHIPS_JSON_SCHEMA
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 422:
                _logger.warning(
                    "LlamaCppEnrichmentClient: json_schema response_format rejected "
                    "(HTTP 422); falling back to prompt-only extraction"
                )
                data = await self._post_chat_completion(prompt, response_format=None)
            else:
                raise

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
                        "LlamaCppEnrichmentClient: unknown relationship_type %r; "
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
                    "LlamaCppEnrichmentClient: malformed relationship item %r: %s; skipping",
                    item,
                    exc,
                )
                continue

        return results

    async def _post_chat_completion(
        self,
        prompt: str,
        response_format: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """POST to llama-server's OpenAI-compatible chat completions endpoint.

        Raises on any transport failure (connection error, timeout, non-2xx
        status, malformed JSON response body). Never swallows errors.
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._token_budget,
            "messages": [{"role": "user", "content": prompt}],
        }
        if response_format is not None:
            payload["response_format"] = response_format

        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            response = await client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str | None:
        """Normalize ``data["choices"][0]["message"]["content"]``.

        Returns None on a missing/malformed shape or non-str content —
        this is treated the same as an empty response (C2: "returns
        null/[] on an entirely empty/missing response"), not a transport
        failure.
        """
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            _logger.warning("LlamaCppEnrichmentClient: malformed response body")
            return None

        if not isinstance(content, str):
            _logger.warning("LlamaCppEnrichmentClient: unexpected non-str content in response")
            return None

        return content

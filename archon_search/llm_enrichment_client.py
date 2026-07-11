"""AnthropicEnrichmentClient — Interface Adapters layer (E2i BE-0).

Concrete implementation of LLMEnrichmentClientProtocol backed by the Anthropic API.

Design decisions:
- Lazy Anthropic import — no top-level ``import anthropic``; keeps the package optional.
- In-process fixed-window rate limiter (per-process, in-memory; N requests per 60-second window).
- asyncio.wait_for for timeout enforcement.
- Raises on any failure — callers (CommunityBuilder, GraphExtractor) catch all exceptions
  and substitute None / empty list. This is the inverse of HyDE/RAGFusion which swallow
  errors internally; here the adapter is dumb and callers decide the fallback.
- Config fields consumed: extraction_timeout_seconds, extraction_rate_limit_rpm,
  extraction_token_budget.
- Model string format: bare model id (caller has already parsed the "provider:model" prefix).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from archon_search.graph_enrichment_protocol import LabeledRelationship

_logger = logging.getLogger(__name__)

_VALID_RELATIONSHIP_TYPES: frozenset[str] = frozenset({"uses", "implements", "depends_on"})

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


class AnthropicEnrichmentClient:
    """Anthropic-backed implementation of LLMEnrichmentClientProtocol.

    The ``anthropic`` package is an optional dependency — it is imported lazily
    in ``__init__``. If it is absent, both public methods will raise ``RuntimeError``.

    Raises on any failure (API error, timeout, JSON parse error, etc.).
    Callers must catch all exceptions and substitute None / empty list.
    """

    def __init__(self, model: str, config: Any) -> None:
        """Initialise the client.

        Args:
            model: Bare model ID (e.g. ``"claude-haiku-4-5-20251001"``).
                   Callers that receive a ``"provider:model"`` string must strip
                   the ``"anthropic:"`` prefix before passing it here.
            config: Any object with attributes:
                - ``extraction_timeout_seconds: float``
                - ``extraction_rate_limit_rpm: int``
                - ``extraction_token_budget: int``
        """
        self._model = model
        self._timeout = config.extraction_timeout_seconds
        self._token_budget = config.extraction_token_budget

        self._anthropic_available: bool = False
        self._client: object | None = None

        try:
            import anthropic  # noqa: PLC0415

            self._client = anthropic.AsyncAnthropic()
            self._anthropic_available = True
        except Exception as exc:  # noqa: BLE001
            # Covers ImportError (package absent) and any construction failure
            # (e.g. test fixture blocking AsyncAnthropic() instantiation).
            _logger.debug(
                "LLM enrichment client unavailable: %s", exc,
            )

        # In-process fixed-window rate limiter (N requests per 60s window; burst possible at window boundary)
        self._lock = asyncio.Lock()
        self._rpm_tokens: int = config.extraction_rate_limit_rpm
        self._rpm_capacity: int = config.extraction_rate_limit_rpm
        self._rpm_refill_at: float = time.monotonic() + 60.0

    async def _check_rate_limit(self) -> None:
        """Enforce the per-minute rate limit via a fixed-window counter.

        Raises:
            RuntimeError: when the token bucket is exhausted.
        """
        async with self._lock:
            now = time.monotonic()
            if now >= self._rpm_refill_at:
                self._rpm_tokens = self._rpm_capacity
                self._rpm_refill_at = now + 60.0

            if self._rpm_tokens <= 0:
                raise RuntimeError(
                    "LLM enrichment rate limit exhausted "
                    f"({self._rpm_capacity} rpm); retry later"
                )

            self._rpm_tokens -= 1

    async def summarize_community(
        self,
        chunk_texts: list[str],
        entity_names: list[str],
    ) -> str | None:
        """Generate an abstractive summary for a community.

        Raises on any failure. Returns None only when the LLM response is empty.
        """
        if not self._anthropic_available:
            raise RuntimeError(
                "The 'anthropic' package is required for LLM enrichment. "
                "Install it with: pip install anthropic"
            )

        await self._check_rate_limit()

        prompt = _SUMMARIZE_PROMPT_TEMPLATE.format(
            entity_names=", ".join(entity_names),
            chunk_texts="\n\n---\n\n".join(chunk_texts),
        )

        response = await asyncio.wait_for(
            self._client.messages.create(  # type: ignore[union-attr]
                model=self._model,
                max_tokens=self._token_budget,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=self._timeout,
        )

        if not response.content:
            return None

        text = response.content[0].text.strip()
        return text if text else None

    async def label_relationships(
        self,
        entity_pairs: list[tuple[str, str]],
        chunk_text: str,
    ) -> list[LabeledRelationship]:
        """Label relationships between entity pairs using the chunk text as context.

        Raises on any failure including JSON parse errors.
        """
        if not self._anthropic_available:
            raise RuntimeError(
                "The 'anthropic' package is required for LLM enrichment. "
                "Install it with: pip install anthropic"
            )

        await self._check_rate_limit()

        pairs_text = "\n".join(f"- {a} / {b}" for a, b in entity_pairs)
        prompt = _LABEL_PROMPT_TEMPLATE.format(
            chunk_text=chunk_text,
            entity_pairs=pairs_text,
        )

        response = await asyncio.wait_for(
            self._client.messages.create(  # type: ignore[union-attr]
                model=self._model,
                max_tokens=self._token_budget,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=self._timeout,
        )

        if not response.content:
            return []

        raw_text = response.content[0].text.strip()

        # Parse JSON — raises ValueError (a subclass of Exception) on bad JSON
        parsed = json.loads(raw_text)

        if not isinstance(parsed, list):
            raise ValueError(f"Expected JSON array from LLM, got {type(parsed).__name__}")

        results: list[LabeledRelationship] = []
        for item in parsed:
            try:
                rel_type = item.get("relationship_type", "")
                if rel_type not in _VALID_RELATIONSHIP_TYPES:
                    _logger.warning(
                        "LLM returned unknown relationship_type %r; skipping pair (%r, %r)",
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
                    "LLM returned malformed relationship item %r: %s; skipping", item, exc
                )
                continue

        return results

"""LLM enrichment clients — Interface Adapters layer (E2i / LLCP BE-5).

Concrete LLMEnrichmentClientProtocol implementations live in this package,
one module per provider (``anthropic.py``, and the v1 siblings added in BE-6:
``llama_cpp.py``, ``ollama.py``, ``openai.py``).

``_VALID_RELATIONSHIP_TYPES`` is hoisted here (moved from the former
``archon_search/llm_enrichment_client.py``) so every client shares the same
narrowed 3-value subset when constraining ``label_relationships`` output —
distinct from the full 9-member ``archon_search.graph_types.RelationshipType``
enum, which includes code-symbol-only values.

``strip_json_code_fences`` (BE-24) is shared for the same reason: LLM replies
sometimes wrap JSON in a markdown code fence (```json ... ``` or ``` ... ```),
which breaks ``json.loads``. All four clients strip fences through this one
helper before parsing.
"""
from __future__ import annotations

import re

_VALID_RELATIONSHIP_TYPES: frozenset[str] = frozenset({"uses", "implements", "depends_on"})

_CODE_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", re.DOTALL)


def strip_json_code_fences(text: str) -> str:
    """Strip a wrapping markdown code fence (```json ... ``` or ``` ... ```) from text.

    Text with no fence is returned unchanged.
    """
    match = _CODE_FENCE_RE.match(text.strip())
    return match.group(1) if match else text

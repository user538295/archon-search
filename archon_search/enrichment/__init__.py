"""LLM enrichment clients — Interface Adapters layer (E2i / LLCP BE-5).

Concrete LLMEnrichmentClientProtocol implementations live in this package,
one module per provider (``anthropic.py``, and the v1 siblings added in BE-6:
``llama_cpp.py``, ``ollama.py``, ``openai.py``).

``_VALID_RELATIONSHIP_TYPES`` is hoisted here (moved from the former
``archon_search/llm_enrichment_client.py``) so every client shares the same
narrowed 3-value subset when constraining ``label_relationships`` output —
distinct from the full 9-member ``archon_search.graph_types.RelationshipType``
enum, which includes code-symbol-only values.
"""
from __future__ import annotations

_VALID_RELATIONSHIP_TYPES: frozenset[str] = frozenset({"uses", "implements", "depends_on"})

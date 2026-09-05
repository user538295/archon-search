"""LLM enrichment clients — Interface Adapters layer (E2i / LLCP BE-5).

Concrete LLMEnrichmentClientProtocol implementations live in this package,
one module per provider (``anthropic.py``, and the v1 siblings added in BE-6:
``llama_cpp.py``, ``ollama.py``, ``openai.py``).

Narrowed to community summarisation only (BE-17, ADR 12): relationship labelling
is now produced locally by the prose extraction engine, so the narrowed
relationship-type subset and the JSON-fence stripping helper that only that path
needed are gone.
"""
from __future__ import annotations

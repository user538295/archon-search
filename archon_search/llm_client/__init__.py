"""Shared LLM client layer (collection-descriptions plan, contract C1).

One consumer-owned interface (:mod:`archon_search.llm_client.protocol`) serves HyDE, RAG Fusion,
graph enrichment and description generation; each provider has a thin wrapper that implements it.

Nothing in this ``__init__`` imports a submodule: the modules here are stdlib-only and every
provider SDK is imported inside the wrapper that needs it, so a command that never calls an LLM
(``archon-search config show``) pays nothing for this package. Import the submodule you need.
"""

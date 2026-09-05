"""BE-17: the enrichment protocol narrows to community summarisation only.

``label_relationships``, ``LabeledRelationship``, the narrowed relationship
enum (``_VALID_RELATIONSHIP_TYPES``) and the JSON-schema response constraint are
removed from the protocol and from all four adapters (C3, S24, S25). The single
surviving method keeps its raise-on-failure semantics.
"""
from __future__ import annotations

import inspect

from archon_search.graph_enrichment_protocol import LLMEnrichmentClientProtocol


def _collect_public_methods(cls: type) -> set[str]:
    return {
        name
        for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    }


def test_protocol_declares_only_summarisation() -> None:
    # Exactly one method survives the narrowing.
    assert _collect_public_methods(LLMEnrichmentClientProtocol) == {"summarize_community"}
    # The surviving method is an async coroutine (the shape callers await).
    assert inspect.iscoroutinefunction(LLMEnrichmentClientProtocol.summarize_community)
    # The relationship method and its DTO are gone from the protocol module.
    import archon_search.graph_enrichment_protocol as proto_mod

    assert not hasattr(proto_mod, "LabeledRelationship")
    assert not hasattr(LLMEnrichmentClientProtocol, "label_relationships")


def test_no_adapter_declares_relationship_labelling() -> None:
    from archon_search.config import GraphConfig
    from archon_search.enrichment.anthropic import AnthropicEnrichmentClient
    from archon_search.enrichment.llama_cpp import LlamaCppEnrichmentClient
    from archon_search.enrichment.ollama import OllamaEnrichmentClient
    from archon_search.enrichment.openai import OpenAIEnrichmentClient

    config = GraphConfig(enabled=True)
    for cls in (
        AnthropicEnrichmentClient,
        LlamaCppEnrichmentClient,
        OllamaEnrichmentClient,
        OpenAIEnrichmentClient,
    ):
        assert hasattr(cls, "summarize_community"), cls.__name__
        assert not hasattr(cls, "label_relationships"), cls.__name__
        # Each adapter still structurally conforms to the runtime_checkable protocol.
        instance = cls("model-x", config)
        assert isinstance(instance, LLMEnrichmentClientProtocol), cls.__name__

    # The narrowed relationship-type subset is gone from the package and from the
    # extractor / domain-types modules that once referenced it.
    import archon_search.enrichment as enrichment_pkg
    import archon_search.graph_extractor as graph_extractor
    import archon_search.graph_types as graph_types

    assert not hasattr(enrichment_pkg, "_VALID_RELATIONSHIP_TYPES")
    assert not hasattr(graph_extractor, "_VALID_RELATIONSHIP_TYPES")
    assert not hasattr(graph_types, "_VALID_RELATIONSHIP_TYPES")

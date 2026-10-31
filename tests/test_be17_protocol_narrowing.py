"""BE-17 (re-pointed by LCD BE-16): graph LLM use narrows to community summarisation only.

``label_relationships``, ``LabeledRelationship``, the narrowed relationship enum (``_VALID_RELATIONSHIP_TYPES``) and the
JSON-schema response constraint are gone (C3, S24, S25). The graph path now runs on the shared ``LlmClient``, whose
surface is one generic call plus window and budget queries: none of the five wrappers carries a relationship-labelling
method, and the only graph consumer is the community builder's summary.
"""
from __future__ import annotations

import inspect

import pytest

from archon_search import community_builder, graph_extractor, graph_types
from archon_search.config import GraphConfig
from archon_search.llm_client import protocol
from archon_search.llm_client.protocol import LlmClient
from archon_search.server import app as app_module

_PROVIDERS = ("anthropic", "openai", "ollama", "llama_cpp", "claude_cli")


def _public_members(cls: type) -> set[str]:
    return {name for name, _ in inspect.getmembers(cls) if not name.startswith("_")}


def test_shared_client_surface_has_no_relationship_labelling() -> None:
    assert _public_members(LlmClient) == {"call", "context_window", "input_budget"}
    assert inspect.iscoroutinefunction(LlmClient.call)  # the shape callers await
    assert not hasattr(protocol, "LabeledRelationship")


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_no_graph_client_declares_relationship_labelling(provider: str) -> None:
    config = GraphConfig(enabled=False, provider=provider, extraction_model="model-x")
    client = app_module._build_graph_client(config, rate_limit_rpm=None)
    assert isinstance(client, LlmClient)
    assert not hasattr(client, "label_relationships")
    assert not hasattr(client, "summarize_community")


def test_relationship_type_subset_is_gone_from_every_module_that_once_had_it() -> None:
    for module in (graph_extractor, graph_types, community_builder):
        assert not hasattr(module, "_VALID_RELATIONSHIP_TYPES"), module.__name__
        assert not hasattr(module, "LabeledRelationship"), module.__name__
        assert not hasattr(module, "label_relationships"), module.__name__

"""The services container: one factory, one ``SearchService`` class, whichever adapter calls it."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from archon_search.config import SearchConfig
from archon_search.services.container import (
    build_search_service,
    build_search_service_from_state,
)
from archon_search.services.search_service import SearchService


def _collaborators() -> dict[str, MagicMock]:
    return {
        "embedder_cache": MagicMock(name="embedder_cache"),
        "hyde_generator": MagicMock(name="hyde_generator"),
        "rag_fusion_generator": MagicMock(name="rag_fusion_generator"),
        "telemetry_writer": MagicMock(name="telemetry_writer"),
        "doc_id_hasher": MagicMock(name="doc_id_hasher"),
    }


def test_container_builds_the_same_service_class_whatever_the_callers_arguments() -> None:
    """Minimal (MCP/shim-style), full and state-based wiring all construct ``SearchService`` with the same arguments."""
    pipeline, config = MagicMock(), SearchConfig()
    collaborators = _collaborators()
    state = SimpleNamespace(pipeline=pipeline, config=config, **collaborators)
    with patch("archon_search.services.container.SearchService") as service_class:
        build_search_service(pipeline, config)
        build_search_service(pipeline, config, **collaborators)
        build_search_service_from_state(state)
    minimal_call, full_call, state_call = service_class.call_args_list
    assert minimal_call.args == (pipeline, config)
    assert minimal_call.kwargs == dict.fromkeys(collaborators)
    assert full_call.args == (pipeline, config)
    assert full_call.kwargs == collaborators
    assert state_call == full_call


def test_container_returns_a_real_search_service_carrying_the_collaborators() -> None:
    """Unpatched, the factory returns the class itself, not a subclass or wrapper."""
    collaborators = _collaborators()
    service = build_search_service(MagicMock(), SearchConfig(), **collaborators)
    assert type(service) is SearchService
    assert service._embedder_cache is collaborators["embedder_cache"]
    assert service._doc_id_hasher is collaborators["doc_id_hasher"]


def test_container_reads_app_state_per_call_so_swapped_objects_are_seen() -> None:
    """A pipeline, config or writer swapped on the state after the first build is used by the next build."""
    state = SimpleNamespace(pipeline=MagicMock(name="first"), config=SearchConfig())
    first = build_search_service_from_state(state)
    swapped_pipeline, swapped_config, writer = MagicMock(name="second"), SearchConfig(), MagicMock()
    state.pipeline = swapped_pipeline
    state.config = swapped_config
    state.telemetry_writer = writer
    second = build_search_service_from_state(state)
    assert first._pipeline is not swapped_pipeline
    assert first._telemetry_writer is None
    assert second._pipeline is swapped_pipeline
    assert second._config is swapped_config
    assert second._telemetry_writer is writer


def test_state_without_optional_collaborators_builds_with_none() -> None:
    """Absent optional attributes (cache, generators, writer, hasher) are ``None``, not an error."""
    service = build_search_service_from_state(SimpleNamespace(pipeline=MagicMock(), config=SearchConfig()))
    assert service._embedder_cache is None
    assert service._hyde_generator is None
    assert service._rag_fusion_generator is None
    assert service._telemetry_writer is None
    assert service._doc_id_hasher is None


@pytest.mark.parametrize("missing", ["pipeline", "config"])
def test_state_without_pipeline_or_config_fails_loudly(missing: str) -> None:
    """The two required collaborators are never defaulted."""
    attributes = {"pipeline": MagicMock(), "config": SearchConfig()}
    del attributes[missing]
    with pytest.raises(AttributeError):
        build_search_service_from_state(SimpleNamespace(**attributes))

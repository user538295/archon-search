"""The services container: the one factory that builds a :class:`SearchService` for every adapter.

There is no lifespan instance.  REST and the OpenAI shim build the service per request from ``request.app.state``
(:func:`build_search_service_from_state`, so an object swapped on the state is seen by the next request).  The MCP
factory (lazy build on its first ``search`` call, BE-15) calls the same factory from its own arguments
(:func:`build_search_service`).  Every caller builds the same class, so there is one code path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from archon_search.services.search_service import SearchService

if TYPE_CHECKING:
    from collections.abc import Callable

    from archon_search.config import SearchConfig
    from archon_search.embedder_cache import EmbedderCache
    from archon_search.hyde import HyDEGenerator
    from archon_search.pipeline import SearchPipeline
    from archon_search.rag_fusion import RAGFusionGenerator
    from archon_search.telemetry.writer import TelemetryWriter


def build_search_service(
    pipeline: SearchPipeline,
    config: SearchConfig,
    *,
    embedder_cache: EmbedderCache | None = None,
    hyde_generator: HyDEGenerator | None = None,
    rag_fusion_generator: RAGFusionGenerator | None = None,
    telemetry_writer: TelemetryWriter | None = None,
    doc_id_hasher: Callable[[str], str] | None = None,
) -> SearchService:
    """Build a :class:`SearchService` from its collaborators."""
    return SearchService(
        pipeline,
        config,
        embedder_cache=embedder_cache,
        hyde_generator=hyde_generator,
        rag_fusion_generator=rag_fusion_generator,
        telemetry_writer=telemetry_writer,
        doc_id_hasher=doc_id_hasher,
    )


def build_search_service_from_state(state: Any) -> SearchService:
    """Build a :class:`SearchService` from an application state object (``request.app.state``), read afresh on every call.

    ``pipeline`` and ``config`` are required; the cache, the generators, the writer and the hasher are optional and
    default to ``None`` when absent from *state*.
    """
    return build_search_service(
        state.pipeline,
        state.config,
        embedder_cache=getattr(state, "embedder_cache", None),
        hyde_generator=getattr(state, "hyde_generator", None),
        rag_fusion_generator=getattr(state, "rag_fusion_generator", None),
        telemetry_writer=getattr(state, "telemetry_writer", None),
        doc_id_hasher=getattr(state, "doc_id_hasher", None),
    )

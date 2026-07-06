"""GraphStoreProtocol — structural typing interface for graph storage (E2f BE-4).

Defines the minimum surface that ``SynonymDetector`` and ``AliasLoader`` depend on.
Both components depend on this protocol, not on the concrete ``GraphStore``.

All async methods follow the project invariant: ``ns`` is the LAST parameter.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from archon_search.graph_types import GraphEdge, GraphNode


@runtime_checkable
class GraphStoreProtocol(Protocol):
    """Structural protocol for graph storage backends.

    ``SynonymDetector`` and ``AliasLoader`` depend on this interface, not on the
    concrete ``GraphStore``.  Any object that satisfies these method signatures
    may be used as a graph store for synonym detection.

    Project invariant (enforced here): ``ns`` is the LAST positional parameter
    in every method signature.
    """

    async def get_all_nodes(self, collection: str, ns: str) -> list[GraphNode]:
        """Return all nodes for *collection*; empty list if table absent."""
        ...

    async def vector_search_nodes(
        self,
        collection: str,
        query_embedding: list[float],
        entity_type: str | None,
        limit: int,
        metric: str = "cosine",
        *,
        ns: str,
    ) -> list[GraphNode]:
        """Return the *limit* nearest nodes by ANN search on ``name_embedding``.

        ``ns`` is keyword-only per the project invariant.
        """
        ...

    async def write_graph(
        self,
        collection: str,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        ns: str,
    ) -> None:
        """Upsert *nodes* and *edges* into the collection's graph tables."""
        ...

    async def find_nodes_by_name(
        self, collection: str, names: list[str], ns: str
    ) -> list[GraphNode]:
        """Return nodes whose ``entity_name`` matches any of *names* (case-insensitive)."""
        ...

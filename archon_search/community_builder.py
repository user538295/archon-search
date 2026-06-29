"""CommunityBuilder — Use Cases layer for E1b Leiden community detection.

Builds Leiden communities from the entity graph stored in ``GraphStore``.
``leidenalg`` and ``igraph`` are imported lazily inside
``_run_leiden_partition_sync`` so the module can be imported without the
``[graph]`` extras installed.

Typical usage::

    builder = CommunityBuilder(graph_store, config)
    communities = await builder.build(collection)

See E1b brief for the full design rationale and recursion policy.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archon_search.config import GraphConfig
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import Community, GraphEdge, GraphNode

_logger = logging.getLogger(__name__)

# Maximum recursion depth for oversized-community splitting.
_MAX_SPLIT_DEPTH: int = 5

# Leiden resolution is multiplied by this factor at each recursion level.
_RESOLUTION_GROWTH_FACTOR: int = 2

_LEIDENALG_INSTALL_HINT = (
    "leidenalg is required for community detection. "
    "Install it with: pip install archon-search[graph]"
)


# ---------------------------------------------------------------------------
# Synchronous helpers (called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _run_leiden_partition_sync(
    nodes: list["GraphNode"],
    edges: list["GraphEdge"],
    resolution: float,
) -> list[list[str]]:
    """Run the Leiden algorithm and return a list of entity_id groups.

    Lazy-imports ``leidenalg`` and ``igraph`` inside the function body.
    Raises ``ImportError`` with an install hint when the libraries are absent.

    Edges are treated as undirected; duplicate (src, tgt) pairs are collapsed
    before graph construction.

    Args:
        nodes: All graph nodes for the collection.
        edges: All graph edges for the collection.
        resolution: Leiden resolution_parameter (higher → more communities).

    Returns:
        A list of groups, where each group is a list of entity IDs belonging
        to the same community.
    """
    try:
        import igraph as ig  # noqa: PLC0415
        import leidenalg  # noqa: PLC0415
    except (ImportError, TypeError) as exc:
        raise ImportError(_LEIDENALG_INSTALL_HINT) from exc

    if not nodes:
        return []

    # Build vertex index
    node_ids = [n.id for n in nodes]
    id_to_idx: dict[str, int] = {nid: i for i, nid in enumerate(node_ids)}

    # Deduplicate undirected edges
    edge_pairs: set[tuple[int, int]] = set()
    for e in edges:
        src_idx = id_to_idx.get(e.source_node_id)
        tgt_idx = id_to_idx.get(e.target_node_id)
        if src_idx is None or tgt_idx is None:
            continue
        if src_idx == tgt_idx:  # Skip self-loops
            continue
        pair = (min(src_idx, tgt_idx), max(src_idx, tgt_idx))
        edge_pairs.add(pair)

    g = ig.Graph(n=len(node_ids), edges=list(edge_pairs), directed=False)
    partition = leidenalg.find_partition(
        g,
        leidenalg.RBConfigurationVertexPartition,
        resolution_parameter=resolution,
    )

    groups: list[list[str]] = []
    for community in partition:
        group = [node_ids[idx] for idx in community]
        if group:
            groups.append(group)

    return groups


def _split_oversized_communities(
    entity_id_groups: list[list[str]],
    nodes_by_id: dict[str, "GraphNode"],
    edges: list["GraphEdge"],
    max_size: int,
    resolution: float,
    depth: int,
) -> list[list[str]]:
    """Recursively split communities that exceed *max_size*.

    For each group that is larger than *max_size*:
    - If ``depth >= _MAX_SPLIT_DEPTH``: accept as-is with a WARNING log.
    - Otherwise: extract the subgraph, re-run Leiden at ``resolution * _RESOLUTION_GROWTH_FACTOR``
      (doubled each level).
      - If Leiden returns a single group (cannot split further): accept with WARNING.
      - Otherwise: recurse on each resulting sub-group.

    Args:
        entity_id_groups: Current list of entity_id groups from a Leiden run.
        nodes_by_id: Map of entity_id → GraphNode for subgraph extraction.
        edges: Full edge list for the collection (filtered per subgraph).
        max_size: Maximum allowed community size.
        resolution: Current resolution parameter.
        depth: Current recursion depth (0-indexed from the caller).

    Returns:
        Flattened list of groups, all with ``len <= max_size`` (or accepted
        oversized groups when the recursion limit is reached).
    """
    result: list[list[str]] = []
    for group in entity_id_groups:
        if len(group) <= max_size:
            result.append(group)
            continue

        if depth >= _MAX_SPLIT_DEPTH:
            _logger.warning(
                "community_builder: group of size %d exceeds max_size=%d "
                "at depth=%d (limit); accepting oversized community",
                len(group),
                max_size,
                depth,
            )
            result.append(group)
            continue

        # Extract subgraph
        group_set = set(group)
        sub_nodes = [nodes_by_id[nid] for nid in group if nid in nodes_by_id]
        missing = [nid for nid in group if nid not in nodes_by_id]
        if missing:
            _logger.warning(
                "community_builder: %d node ID(s) in group not found in nodes_by_id; "
                "they will be excluded from the sub-community",
                len(missing),
            )
        sub_edges = [
            e for e in edges
            if e.source_node_id in group_set and e.target_node_id in group_set
        ]

        new_resolution = resolution * _RESOLUTION_GROWTH_FACTOR
        sub_groups = _run_leiden_partition_sync(sub_nodes, sub_edges, new_resolution)

        if len(sub_groups) <= 1:
            # Cannot split further
            _logger.warning(
                "community_builder: Leiden could not split group of size %d "
                "at resolution=%.4f depth=%d; accepting oversized community",
                len(group),
                new_resolution,
                depth,
            )
            result.append(group)
            continue

        # Recurse on sub-groups
        result.extend(
            _split_oversized_communities(
                sub_groups, nodes_by_id, edges, max_size, new_resolution, depth + 1
            )
        )

    return result


def _cluster_with_size_limit(
    nodes: list["GraphNode"],
    edges: list["GraphEdge"],
    resolution: float,
    max_size: int,
) -> list[list[str]]:
    """Run Leiden and split oversized communities as needed.

    Calls ``_run_leiden_partition_sync`` once, then delegates to
    ``_split_oversized_communities`` for any groups that exceed *max_size*.

    Args:
        nodes: All graph nodes for the collection.
        edges: All graph edges for the collection.
        resolution: Initial Leiden resolution parameter.
        max_size: Maximum allowed community size.

    Returns:
        List of entity_id groups with all sizes ``<= max_size`` (or accepted
        oversized when the recursion limit is reached).
    """
    groups = _run_leiden_partition_sync(nodes, edges, resolution)
    if not any(len(g) > max_size for g in groups):
        return groups
    nodes_by_id = {n.id: n for n in nodes}
    return _split_oversized_communities(groups, nodes_by_id, edges, max_size, resolution, depth=0)


# ---------------------------------------------------------------------------
# CommunityBuilder
# ---------------------------------------------------------------------------


class CommunityBuilder:
    """Use Cases layer — builds Leiden communities from the entity graph (E1b).

    Lazy-imports ``leidenalg`` and ``igraph`` inside ``_run_leiden_partition_sync``
    so the module can be imported without the ``[graph]`` extras installed.

    ``build()`` orchestrates:
    1. Load all nodes from ``GraphStore.get_all_nodes``.
    2. Short-circuit to a single community when fewer than 2 nodes exist.
    3. Load all edges from ``GraphStore.get_all_edges``.
    4. Run ``_cluster_with_size_limit`` via ``asyncio.to_thread``.
    5. Return one ``Community`` per group with a fresh UUID.
    """

    def __init__(self, graph_store: "GraphStore", config: "GraphConfig") -> None:
        self._store = graph_store
        self._config = config

    async def build(self, collection: str) -> list["Community"]:
        """Build Leiden communities for *collection*.

        ``representative_chunk_ids`` is empty on all returned communities;
        BE-3b extends this method to fill them via MMR and persist results.

        Args:
            collection: Name of the collection whose entity graph to cluster.

        Returns:
            List of ``Community`` objects — one per detected community.
            May be empty when Leiden produces no communities on a valid graph.

        Raises:
            ValueError: When no graph nodes exist for *collection* (ingest with
                ``graph.enabled=true`` must be run first).
            ImportError: When ``leidenalg``/``igraph`` are not installed
                (``pip install archon-search[graph]``).
            RuntimeError: When the graph store encounters an unexpected I/O or
                storage error while loading nodes or edges.
        """
        from archon_search.graph_types import Community  # noqa: PLC0415

        nodes = await self._store.get_all_nodes(collection)
        if not nodes:
            raise ValueError(
                f"No entity graph nodes found for collection {collection!r}. "
                "Run ingest with graph.enabled=true first."
            )

        now = datetime.now(tz=timezone.utc)

        if len(nodes) < 2:
            _logger.warning(
                "community_builder: collection %r has only %d node(s); "
                "returning single community without running Leiden",
                collection,
                len(nodes),
            )
            return [
                Community(
                    community_id=str(uuid.uuid4()),
                    entity_ids=[n.id for n in nodes],
                    representative_chunk_ids=[],
                    built_at=now,
                    summary_text=None,
                )
            ]

        edges = await self._store.get_all_edges(collection)

        resolution = self._config.leiden_resolution
        max_size = self._config.max_community_size

        groups = await asyncio.to_thread(
            _cluster_with_size_limit, nodes, edges, resolution, max_size
        )

        return [
            Community(
                community_id=str(uuid.uuid4()),
                entity_ids=group,
                representative_chunk_ids=[],
                built_at=now,
                summary_text=None,
            )
            for group in groups
        ]

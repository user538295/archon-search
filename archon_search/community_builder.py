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
import math
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archon_search.config import GraphConfig
    from archon_search.graph_store import GraphStore
    from archon_search.graph_types import Community, GraphEdge, GraphNode
    from archon_search.store import SearchStore

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
# MMR helpers
# ---------------------------------------------------------------------------


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors. Returns 0.0 for zero vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _mmr_select(chunks: list[dict], k: int) -> list[str]:
    """Greedy MMR diversity selection. Returns up to *k* chunk_id strings.

    Filters to chunks with a non-None, non-empty ``vector`` field. Picks the
    chunk closest to the centroid of all valid vectors as the first
    representative, then iteratively picks the chunk with the minimum
    max-cosine-similarity to already-selected chunks.

    Returns ``[]`` when *chunks* is empty or no chunks have valid vectors.

    Note: This is diversity-only selection — there is no relevance/λ trade-off term.
    The centroid serves as a weak relevance anchor (most "central" chunk is picked
    first), but subsequent picks maximise diversity only (λ=0 in classical MMR).
    """
    valid_chunks = [c for c in chunks if c.get("vector")]
    if not valid_chunks:
        return []
    k = min(k, len(valid_chunks))
    if k <= 0:
        return []

    dim = len(valid_chunks[0]["vector"])
    centroid = [0.0] * dim
    for c in valid_chunks:
        for i, v in enumerate(c["vector"]):
            centroid[i] += v
    n = len(valid_chunks)
    centroid = [v / n for v in centroid]

    selected_ids: list[str] = []
    selected_vectors: list[list[float]] = []
    remaining = list(valid_chunks)

    # First pick: chunk closest to centroid
    best_idx = max(range(len(remaining)), key=lambda i: _cosine_similarity(remaining[i]["vector"], centroid))
    chosen = remaining.pop(best_idx)
    selected_ids.append(str(chosen["chunk_id"]))
    selected_vectors.append(chosen["vector"])

    # Subsequent picks: minimum max-similarity to already selected
    while len(selected_ids) < k and remaining:
        best_score = float("inf")
        best_idx = 0
        for i, candidate in enumerate(remaining):
            max_sim = max(_cosine_similarity(candidate["vector"], sv) for sv in selected_vectors)
            if max_sim < best_score:
                best_score = max_sim
                best_idx = i
        chosen = remaining.pop(best_idx)
        selected_ids.append(str(chosen["chunk_id"]))
        selected_vectors.append(chosen["vector"])

    return selected_ids


# ---------------------------------------------------------------------------
# Synchronous helpers (called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _run_leiden_partition_sync(
    nodes: list["GraphNode"],
    edges: list["GraphEdge"],
    resolution: float,
    seed: int | None = None,
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
        seed: Random seed for deterministic Leiden partitioning. When None,
            uses non-deterministic behaviour (existing default). When set to
            an integer (e.g., 42), produces reproducible communities across runs.

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
        seed=seed,
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
    seed: int | None = None,
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
        seed: Random seed for deterministic Leiden partitioning (forwarded to all
            _run_leiden_partition_sync calls during recursion).

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
        sub_groups = _run_leiden_partition_sync(sub_nodes, sub_edges, new_resolution, seed=seed)

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
                sub_groups, nodes_by_id, edges, max_size, new_resolution, depth + 1, seed=seed
            )
        )

    return result


def _cluster_with_size_limit(
    nodes: list["GraphNode"],
    edges: list["GraphEdge"],
    resolution: float,
    max_size: int,
    seed: int | None = None,
) -> list[list[str]]:
    """Run Leiden and split oversized communities as needed.

    Calls ``_run_leiden_partition_sync`` once, then delegates to
    ``_split_oversized_communities`` for any groups that exceed *max_size*.

    Args:
        nodes: All graph nodes for the collection.
        edges: All graph edges for the collection.
        resolution: Initial Leiden resolution parameter.
        max_size: Maximum allowed community size.
        seed: Random seed for deterministic Leiden partitioning (forwarded to
            _run_leiden_partition_sync and _split_oversized_communities).

    Returns:
        List of entity_id groups with all sizes ``<= max_size`` (or accepted
        oversized when the recursion limit is reached).
    """
    groups = _run_leiden_partition_sync(nodes, edges, resolution, seed=seed)
    if not any(len(g) > max_size for g in groups):
        return groups
    nodes_by_id = {n.id: n for n in nodes}
    return _split_oversized_communities(groups, nodes_by_id, edges, max_size, resolution, depth=0, seed=seed)


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
    5. Fill representative_chunk_ids via MMR (when search_store is provided).
    6. Optionally generate LLM summary (stub; falls back to None on failure).
    7. Persist communities via ``GraphStore.write_communities``.
    8. Return one ``Community`` per group with a fresh UUID.
    """

    def __init__(
        self,
        graph_store: "GraphStore",
        config: "GraphConfig",
        *,
        search_store: "SearchStore | None" = None,
    ) -> None:
        self._store = graph_store
        self._config = config
        self._search_store = search_store

    async def _generate_llm_summary(
        self, community_id: str, chunk_texts: list[str]
    ) -> str:
        """LLM summarisation stub — not yet implemented (E1b scope).

        When extraction_model is set, a WARNING is logged here and
        NotImplementedError is raised so that build() falls back to MMR.
        """
        raise NotImplementedError(
            f"LLM community summary for extraction_model={self._config.extraction_model!r} "
            "is not yet implemented"
        )

    async def _select_representative_chunk_ids(
        self,
        collection: str,
        entity_ids: list[str],
        nodes_by_id: "dict[str, GraphNode]",
    ) -> tuple[list[str], list[dict]]:
        """Select representative chunk IDs for a community via MMR.

        When search_store is None, returns ([], []) (no MMR candidates available).
        Gathers all candidate chunks for entities in this community (deduplicated
        by source_doc_id to avoid redundant store queries), then runs MMR to
        select up to ``config.community_summary_chunks`` diverse IDs.

        Returns:
            (selected_chunk_ids, all_candidate_chunks)
        """
        if self._search_store is None:
            return [], []

        seen_chunk_ids: set[str] = set()
        candidate_chunks: list[dict] = []

        # Deduplicate source_doc_ids to avoid redundant store queries
        unique_doc_ids: set[str] = set()
        for eid in entity_ids:
            node = nodes_by_id.get(eid)
            if node is not None:
                unique_doc_ids.add(node.source_doc_id)

        for doc_id in unique_doc_ids:
            doc_chunks = await self._search_store.get_chunks_for_doc(collection, doc_id)
            for chunk in doc_chunks:
                cid = chunk.get("chunk_id")
                if cid and cid not in seen_chunk_ids:
                    seen_chunk_ids.add(cid)
                    candidate_chunks.append(chunk)

        return _mmr_select(candidate_chunks, self._config.community_summary_chunks), candidate_chunks

    async def build(self, collection: str, ns: str, *, seed: int | None = None) -> list["Community"]:
        """Build Leiden communities for *collection*.

        Fills ``representative_chunk_ids`` via MMR when a ``search_store`` is
        provided. Attempts optional LLM summarisation when
        ``config.extraction_model`` is set; falls back to ``summary_text=None``
        on any exception. Persists results via ``GraphStore.write_communities``
        before returning.

        Args:
            collection: Name of the collection whose entity graph to cluster.
            ns: Namespace for the collection.
            seed: Random seed for deterministic Leiden partitioning. When None
                (default), uses non-deterministic behaviour. When set to an integer
                (e.g., 42), produces reproducible communities across runs.

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

        nodes = await self._store.get_all_nodes(collection, ns=ns)
        if not nodes:
            raise ValueError(
                f"No entity graph nodes found for collection {collection!r}. "
                "Run ingest with graph.enabled=true first."
            )

        now = datetime.now(tz=timezone.utc)
        nodes_by_id = {n.id: n for n in nodes}

        if len(nodes) < 2:
            _logger.warning(
                "community_builder: collection %r has only %d node(s); "
                "returning single community without running Leiden",
                collection,
                len(nodes),
            )
            entity_ids = [n.id for n in nodes]
            rep_chunk_ids, _ = await self._select_representative_chunk_ids(
                collection, entity_ids, nodes_by_id
            )
            result = [
                Community(
                    community_id=str(uuid.uuid4()),
                    entity_ids=entity_ids,
                    representative_chunk_ids=rep_chunk_ids,
                    built_at=now,
                    summary_text=None,
                )
            ]
            await self._store.write_communities(collection, result, ns=ns)
            return result

        edges = await self._store.get_all_edges(collection, ns=ns)

        resolution = self._config.leiden_resolution
        max_size = self._config.max_community_size

        groups = await asyncio.to_thread(
            _cluster_with_size_limit, nodes, edges, resolution, max_size, seed
        )

        final_communities: list[Community] = []
        for group in groups:
            community_id = str(uuid.uuid4())

            rep_chunk_ids, candidate_chunks = await self._select_representative_chunk_ids(
                collection, group, nodes_by_id
            )

            summary_text: str | None = None
            if self._config.extraction_model is not None:
                chunk_texts: list[str] = []
                if rep_chunk_ids:
                    rep_ids_set = set(rep_chunk_ids)
                    for chunk in candidate_chunks:
                        cid = chunk.get("chunk_id")
                        if cid in rep_ids_set:
                            text = chunk.get("text") or ""
                            if text:
                                chunk_texts.append(text)

                try:
                    summary_text = await self._generate_llm_summary(community_id, chunk_texts)
                except Exception as exc:
                    _logger.warning(
                        "community_builder: LLM summary failed for community %s "
                        "(extraction_model=%r): %s; falling back to MMR representatives only",
                        community_id,
                        self._config.extraction_model,
                        exc,
                    )
                    summary_text = None

            final_communities.append(
                Community(
                    community_id=community_id,
                    entity_ids=group,
                    representative_chunk_ids=rep_chunk_ids,
                    built_at=now,
                    summary_text=summary_text,
                )
            )

        await self._store.write_communities(collection, final_communities, ns=ns)
        return final_communities

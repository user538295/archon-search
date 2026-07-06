"""SynonymDetector — embedding-based automatic synonym edge detection (E2f BE-4).

For each collection, groups nodes by entity_type and uses all-pairs Python cosine
similarity to find near-duplicate entity names.  Pairs with cosine similarity >=
``config.graph.synonym_threshold`` are returned as ``synonym_of`` edges.

Callers are responsible for writing the resulting edges via
``graph_store.write_graph(collection, nodes=[], edges=edges, ns=ns)``.

The detector depends on ``GraphStoreProtocol`` and ``Embedder`` only — no
concrete ``GraphStore`` import.  ``ns`` is last in all public method signatures
per project invariant.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict

from archon_search.config import SearchConfig
from archon_search.embedder import Embedder
from archon_search.graph_store_protocol import GraphStoreProtocol
from archon_search.graph_types import (
    GraphEdge,
    GraphNode,
    RelationshipType,
    make_stable_edge_id,
)

logger = logging.getLogger(__name__)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return cosine similarity between two non-zero vectors.

    Returns 0.0 when either vector has zero magnitude (guards against
    zero-padded embeddings from stubs or uninitialized models), or when
    vectors have mismatched lengths (guards against partial embeddings).
    """
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


class SynonymDetector:
    """Detect synonym entity pairs via embedding similarity and produce ``synonym_of`` edges.

    Usage::

        detector = SynonymDetector(graph_store=gs, embedder=embedder, config=cfg)
        edges = await detector.detect(collection, ns=namespace)
        await graph_store.write_graph(collection, nodes=[], edges=edges, ns=namespace)

    ``SynonymDetector`` never calls ``write_graph`` itself — the caller is responsible
    for persisting the returned edges.
    """

    def __init__(
        self,
        graph_store: GraphStoreProtocol,
        embedder: Embedder,
        config: SearchConfig,
    ) -> None:
        self._graph_store = graph_store
        self._emb = embedder
        self._config = config

    async def detect(
        self,
        collection: str,
        ns: str,
        skip_pairs: frozenset[tuple[str, str]] | set[tuple[str, str]] = frozenset(),
    ) -> list[GraphEdge]:
        """Detect synonym pairs for *collection* and return ``synonym_of`` edges.

        Args:
            collection: Name of the collection to scan.
            ns: Namespace — LAST per project invariant.
            skip_pairs: Set of ``(id_a, id_b)`` tuples in ANY order that should be
                excluded from the result regardless of similarity.  Both orderings are
                checked via canonical normalization.

        Returns:
            A list of ``GraphEdge`` objects with ``relationship_type=synonym_of`` and
            ``extraction_method="embedding"``.  Source/target IDs are in lexicographic
            order so ``make_stable_edge_id`` is deterministic across traversal directions.
        """
        # Normalise skip_pairs to canonical (min, max) ordering so both orderings match.
        canonical_skip: set[tuple[str, str]] = {
            (min(a, b), max(a, b)) for a, b in skip_pairs
        }

        nodes: list[GraphNode] = await self._graph_store.get_all_nodes(collection, ns)
        if not nodes:
            return []

        # Group nodes by entity_type.  All-pairs cosine is computed within each group,
        # so cross-type pairs are structurally impossible — no explicit guard needed.
        by_type: dict[str, list[GraphNode]] = defaultdict(list)
        for node in nodes:
            by_type[node.entity_type.value].append(node)

        threshold = self._config.graph.synonym_threshold
        result_edges: list[GraphEdge] = []

        for type_nodes in by_type.values():
            if len(type_nodes) < 2:
                # Need at least 2 nodes for a pair; skip singletons.
                continue

            # Use stored name_embedding only; skip nodes without one.
            # Note: name_embedding is populated once BE-5 wires the synonym enrichment callback.
            # Until then, all nodes have name_embedding=None from graph_extractor.py, so this
            # list will be empty and detect() returns [].  This is expected behavior for the
            # BE-4 slice — synonym detection becomes active when BE-5 is implemented.
            # Zero-vector embeddings are retained — _cosine_similarity handles them.
            paired = [
                (n, n.name_embedding)
                for n in type_nodes
                if n.name_embedding is not None
            ]
            if len(paired) < 2:
                continue

            # Index-based all-pairs iteration: (i, i+1), (i, i+2), … naturally
            # avoids self-pairs and duplicate pairs without an explicit seen set.
            for i, (node_a, emb_a) in enumerate(paired):
                for node_b, emb_b in paired[i + 1:]:
                    src_id = min(node_a.id, node_b.id)
                    tgt_id = max(node_a.id, node_b.id)
                    canonical_pair = (src_id, tgt_id)

                    if canonical_pair in canonical_skip:
                        continue

                    sim = _cosine_similarity(emb_a, emb_b)
                    if sim >= threshold:
                        edge_id = make_stable_edge_id(
                            src_id, tgt_id, RelationshipType.synonym_of.value
                        )
                        result_edges.append(
                            GraphEdge(
                                id=edge_id,
                                source_node_id=src_id,
                                target_node_id=tgt_id,
                                relationship_type=RelationshipType.synonym_of,
                                source_doc_id="synonym-detector",
                                extraction_method="embedding",
                            )
                        )

        return result_edges

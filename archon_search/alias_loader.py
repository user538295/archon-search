"""AliasLoader — manual alias (synonym) edge loader from a TOML file (E2f BE-6).

Reads TOML file at ``config.graph.alias_file`` using stdlib ``tomllib``.
For each ``"name_a" = "name_b"`` alias pair, resolves both names via
``graph_store.find_nodes_by_name`` and produces ``synonym_of`` edges with
``extraction_method="manual"``.

The caller (``_run_synonym_enrichment``) invokes ``AliasLoader.load()`` first,
passes the returned ``skip_pairs`` set to ``SynonymDetector.detect()``, then
writes all edges (alias + ANN) in one ``write_graph()`` call.

Project invariants observed:
- ``ns`` is the LAST positional parameter in ``load()``.
- Depends on ``GraphStoreProtocol``, not the concrete ``GraphStore``.
- Source/target IDs are sorted lexicographically before constructing each
  ``GraphEdge`` and before adding to ``skip_pairs``.
"""
from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from archon_search.config import GraphConfig
from archon_search.graph_store_protocol import GraphStoreProtocol
from archon_search.graph_types import (
    GraphEdge,
    RelationshipType,
    make_stable_edge_id,
)

logger = logging.getLogger(__name__)


class AliasLoader:
    """Load manual synonym edges from a TOML alias file.

    Usage::

        loader = AliasLoader(config=graph_cfg, graph_store=gs)
        alias_edges, skip_pairs = await loader.load(collection, ns)
        ann_edges = await detector.detect(collection, ns=ns, skip_pairs=skip_pairs)
        await gs.write_graph(collection, [], alias_edges + ann_edges, ns=ns)

    ``AliasLoader`` never calls ``write_graph`` itself — the orchestrator is
    responsible for persisting edges.
    """

    def __init__(
        self,
        config: GraphConfig,
        graph_store: GraphStoreProtocol,
    ) -> None:
        self._config = config
        self._graph_store = graph_store

    async def load(
        self,
        collection: str,
        ns: str,
    ) -> tuple[list[GraphEdge], set[tuple[str, str]]]:
        """Load alias pairs and return synonym edges with a skip-set.

        Args:
            collection: Name of the collection to resolve names against.
            ns: Namespace — LAST per project invariant.

        Returns:
            A tuple of:
            - ``list[GraphEdge]``: synonym_of edges with ``extraction_method="manual"``.
            - ``set[tuple[str, str]]``: canonical ``(min_id, max_id)`` pairs to pass
              to ``SynonymDetector.detect()`` as ``skip_pairs``.

        Missing or unreadable file logs WARNING and returns ``([], set())``.
        Either name resolving to zero nodes logs WARNING and skips the pair.
        When a name resolves to multiple nodes, edges are created for each
        matching type-pair that shares the same ``entity_type``.
        """
        alias_file = self._config.alias_file
        if not alias_file:
            return ([], set())

        path = Path(alias_file)

        # Read and parse the TOML file.
        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.warning(
                "AliasLoader: alias_file %r is missing or unreadable (%s); skipping",
                alias_file,
                exc,
            )
            return ([], set())

        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            logger.warning(
                "AliasLoader: alias_file %r has invalid TOML (%s); skipping",
                alias_file,
                exc,
            )
            return ([], set())

        edges: list[GraphEdge] = []
        skip_pairs: set[tuple[str, str]] = set()

        for name_a, name_b in data.items():
            if not isinstance(name_b, str):
                logger.warning(
                    "AliasLoader: alias entry %r = %r is not a string pair; skipping",
                    name_a,
                    name_b,
                )
                continue

            # Resolve both names in a single call per project invariant (ns last).
            nodes = await self._graph_store.find_nodes_by_name(
                collection, [name_a, name_b], ns
            )

            # Partition resolved nodes by their original names (case-insensitive).
            nodes_a = [n for n in nodes if n.entity_name.lower() == name_a.lower()]
            nodes_b = [n for n in nodes if n.entity_name.lower() == name_b.lower()]

            if not nodes_a:
                logger.warning(
                    "AliasLoader: alias pair (%r, %r) — %r resolved to zero nodes in %s/%s; skipping",
                    name_a,
                    name_b,
                    name_a,
                    ns,
                    collection,
                )
                continue

            if not nodes_b:
                logger.warning(
                    "AliasLoader: alias pair (%r, %r) — %r resolved to zero nodes in %s/%s; skipping",
                    name_a,
                    name_b,
                    name_b,
                    ns,
                    collection,
                )
                continue

            # Create edges for each same-type pair across nodes_a × nodes_b.
            for node_a in nodes_a:
                for node_b in nodes_b:
                    if node_a.id == node_b.id:
                        continue
                    if node_a.entity_type != node_b.entity_type:
                        continue

                    # Canonical lexicographic ordering of IDs.
                    src_id = min(node_a.id, node_b.id)
                    tgt_id = max(node_a.id, node_b.id)

                    edge_id = make_stable_edge_id(
                        src_id, tgt_id, RelationshipType.synonym_of.value
                    )
                    edge = GraphEdge(
                        id=edge_id,
                        source_node_id=src_id,
                        target_node_id=tgt_id,
                        relationship_type=RelationshipType.synonym_of,
                        source_doc_id="alias-loader",
                        extraction_method="manual",
                    )
                    edges.append(edge)
                    skip_pairs.add((src_id, tgt_id))

        return (edges, skip_pairs)

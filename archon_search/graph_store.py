"""GraphStore — LanceDB-backed graph node and edge storage for GraphRAG (E1a).

Wraps two per-collection LanceDB tables:
  _archon_graph_{collection}_nodes
  _archon_graph_{collection}_edges

All SQL predicates MUST go through ``_where_eq`` / ``_where_in`` helpers
(from ``archon_search.store_filters``), never f-strings passed directly to
``.where()``, ``.delete()``, or ``.count_rows()``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from archon_search.graph_types import EntityType, GraphEdge, GraphNode, RelationshipType
from archon_search.store_filters import _sql_quote_str

if TYPE_CHECKING:
    import lancedb

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COLLECTION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_ARCHON_PREFIX = "_archon_"

# ---------------------------------------------------------------------------
# SQL helpers (same pattern as store.py)
# ---------------------------------------------------------------------------


def _where_eq(col: str, value: str) -> str:
    """Return ``col = 'value'`` (SQL-safe via _sql_quote_str)."""
    return f"{col} = {_sql_quote_str(value)}"


def _where_in(col: str, values: list[str]) -> str:
    """Return ``col IN ('a', 'b', ...)``. Empty list yields ``1=0`` (always-false)."""
    items = ", ".join(_sql_quote_str(v) for v in values)
    return f"{col} IN ({items})" if items else "1=0"


# ---------------------------------------------------------------------------
# GraphStore
# ---------------------------------------------------------------------------


class GraphStore:
    """Async LanceDB store for graph nodes and edges (E1a GraphRAG)."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser()
        self._db: Optional[lancedb.db.AsyncConnection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the LanceDB async connection."""
        import lancedb  # noqa: PLC0415

        self._db_path.mkdir(parents=True, exist_ok=True)
        self._db = await lancedb.connect_async(str(self._db_path))

    async def disconnect(self) -> None:
        """Close the connection (noop if already disconnected)."""
        db = self._db
        self._db = None
        if db is not None:
            db.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_db(self) -> "lancedb.db.AsyncConnection":
        if self._db is None:
            raise RuntimeError("GraphStore not connected — call connect() first")
        return self._db

    def _validate_collection(self, collection: str) -> None:
        """Raise ValueError for collection names that fail _COLLECTION_RE."""
        if not _COLLECTION_RE.match(collection):
            raise ValueError(
                f"Invalid collection name {collection!r}: must match "
                r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$"
            )

    def _nodes_table_name(self, collection: str) -> str:
        return _ARCHON_PREFIX + "graph_" + collection + "_nodes"

    def _edges_table_name(self, collection: str) -> str:
        return _ARCHON_PREFIX + "graph_" + collection + "_edges"

    @staticmethod
    def _nodes_schema():  # type: ignore[return]
        """Return the PyArrow schema for the nodes table."""
        import pyarrow as pa  # noqa: PLC0415

        return pa.schema([
            pa.field("id", pa.utf8()),
            pa.field("entity_name", pa.utf8()),
            pa.field("entity_type", pa.utf8()),
            pa.field("source_doc_id", pa.utf8()),
            pa.field("collection_name", pa.utf8()),
            pa.field("entity_subtype", pa.utf8()),
        ])

    @staticmethod
    def _edges_schema():  # type: ignore[return]
        """Return the PyArrow schema for the edges table."""
        import pyarrow as pa  # noqa: PLC0415

        return pa.schema([
            pa.field("id", pa.utf8()),
            pa.field("source_node_id", pa.utf8()),
            pa.field("target_node_id", pa.utf8()),
            pa.field("relationship_type", pa.utf8()),
            pa.field("source_doc_id", pa.utf8()),
        ])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ensure_graph_tables(self, collection: str) -> None:
        """Create nodes and edges tables for *collection* if they don't already exist.

        Idempotent — safe to call multiple times. Raises ``ValueError`` for
        collection names that fail the naming regex.
        """
        self._validate_collection(collection)
        db = self._require_db()

        await db.create_table(
            self._nodes_table_name(collection),
            schema=self._nodes_schema(),
            exist_ok=True,
        )
        await db.create_table(
            self._edges_table_name(collection),
            schema=self._edges_schema(),
            exist_ok=True,
        )

    async def write_graph(
        self,
        collection: str,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        """Upsert *nodes* and *edges* into the collection's graph tables.

        Uses ``merge_insert`` keyed on ``id`` so that re-ingesting the same
        document replaces existing rows rather than duplicating them.
        """
        import pyarrow as pa  # noqa: PLC0415

        self._validate_collection(collection)
        db = self._require_db()

        if nodes:
            nodes_table = await db.open_table(self._nodes_table_name(collection))
            nodes_data = pa.table(
                {
                    "id": [n.id for n in nodes],
                    "entity_name": [n.entity_name for n in nodes],
                    "entity_type": [n.entity_type.value for n in nodes],
                    "source_doc_id": [n.source_doc_id for n in nodes],
                    "collection_name": [n.collection_name for n in nodes],
                    "entity_subtype": [n.entity_subtype for n in nodes],
                },
                schema=self._nodes_schema(),
            )
            await (
                nodes_table.merge_insert("id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(nodes_data)
            )

        if edges:
            edges_table = await db.open_table(self._edges_table_name(collection))
            edges_data = pa.table(
                {
                    "id": [e.id for e in edges],
                    "source_node_id": [e.source_node_id for e in edges],
                    "target_node_id": [e.target_node_id for e in edges],
                    "relationship_type": [e.relationship_type.value for e in edges],
                    "source_doc_id": [e.source_doc_id for e in edges],
                },
                schema=self._edges_schema(),
            )
            await (
                edges_table.merge_insert("id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(edges_data)
            )

    async def get_neighbours(
        self, collection: str, entity_ids: list[str]
    ) -> list[GraphNode]:
        """Return GraphNode objects that are first-degree neighbours of *entity_ids*.

        Queries both ``source_node_id`` and ``target_node_id`` columns so edges
        in either direction are followed.  Returns unique neighbour nodes only.
        """
        if not entity_ids:
            return []

        self._validate_collection(collection)
        db = self._require_db()

        edges_table = await db.open_table(self._edges_table_name(collection))

        # Build safe predicate: source_node_id IN (...) OR target_node_id IN (...)
        src_pred = _where_in("source_node_id", entity_ids)
        tgt_pred = _where_in("target_node_id", entity_ids)
        edge_pred = "(" + src_pred + " OR " + tgt_pred + ")"

        edges_arrow = await edges_table.query().where(edge_pred).to_arrow()

        # Collect neighbour IDs (nodes that are NOT in entity_ids)
        entity_ids_set = set(entity_ids)
        neighbour_ids: list[str] = []
        seen: set[str] = set()

        for src_id, tgt_id in zip(
            edges_arrow["source_node_id"].to_pylist(),
            edges_arrow["target_node_id"].to_pylist(),
        ):
            for nid in (src_id, tgt_id):
                if nid not in entity_ids_set and nid not in seen:
                    neighbour_ids.append(nid)
                    seen.add(nid)

        if not neighbour_ids:
            return []

        nodes_table = await db.open_table(self._nodes_table_name(collection))
        node_pred = _where_in("id", neighbour_ids)
        nodes_arrow = await nodes_table.query().where(node_pred).to_arrow()

        return self._arrow_to_nodes(nodes_arrow)

    async def edge_count(self, collection: str) -> int:
        """Return the number of edges in *collection*'s graph table; 0 if table absent."""
        self._validate_collection(collection)
        db = self._require_db()
        try:
            edges_table = await db.open_table(self._edges_table_name(collection))
            return await edges_table.count_rows()
        except FileNotFoundError:
            return 0
        except Exception:
            logger.warning(
                "edge_count: unexpected error for collection %r; reporting 0", collection, exc_info=True
            )
            return 0

    async def node_count(self, collection: str) -> int:
        """Return the number of nodes in *collection*'s graph table; 0 if table absent."""
        self._validate_collection(collection)
        db = self._require_db()
        try:
            nodes_table = await db.open_table(self._nodes_table_name(collection))
            return await nodes_table.count_rows()
        except FileNotFoundError:
            return 0
        except Exception:
            logger.warning(
                "node_count: unexpected error for collection %r; reporting 0", collection, exc_info=True
            )
            return 0

    async def find_nodes_by_name(
        self, collection: str, names: list[str]
    ) -> list[GraphNode]:
        """Return nodes whose ``entity_name`` matches any of *names* (case-insensitive).

        Empty *names* list returns ``[]`` immediately.  The predicate uses
        ``lower(entity_name)`` so stored names like ``"AuthService"`` match
        a query for ``"authservice"`` or ``"AUTHSERVICE"``.
        """
        if not names:
            return []

        self._validate_collection(collection)
        db = self._require_db()

        lower_names = [n.lower() for n in names]
        # Build safe SQL: lower(entity_name) IN ('name1', 'name2', ...)
        items = ", ".join(_sql_quote_str(n) for n in lower_names)
        predicate = "lower(entity_name) IN (" + items + ")"

        nodes_table = await db.open_table(self._nodes_table_name(collection))
        nodes_arrow = await nodes_table.query().where(predicate).to_arrow()

        return self._arrow_to_nodes(nodes_arrow)

    # ------------------------------------------------------------------
    # Internal conversion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _arrow_to_nodes(arrow_table) -> list[GraphNode]:  # type: ignore[return]
        """Convert a PyArrow table of node rows into ``GraphNode`` dataclass objects."""
        results: list[GraphNode] = []
        ids = arrow_table["id"].to_pylist()
        names = arrow_table["entity_name"].to_pylist()
        types = arrow_table["entity_type"].to_pylist()
        source_docs = arrow_table["source_doc_id"].to_pylist()
        collections = arrow_table["collection_name"].to_pylist()
        subtypes = arrow_table["entity_subtype"].to_pylist()

        for nid, name, etype, sdoc, col, subtype in zip(
            ids, names, types, source_docs, collections, subtypes
        ):
            results.append(
                GraphNode(
                    id=nid,
                    entity_name=name,
                    entity_type=EntityType(etype),
                    source_doc_id=sdoc,
                    collection_name=col,
                    entity_subtype=subtype,
                )
            )
        return results

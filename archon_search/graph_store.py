"""GraphStore — LanceDB-backed graph node and edge storage for GraphRAG (E1a/E1b).

Wraps per-collection LanceDB tables:
  _archon_graph_{collection}_nodes
  _archon_graph_{collection}_edges
  _archon_graph_{collection}_communities   (E1b)

All SQL predicates MUST go through ``_where_eq`` / ``_where_in`` helpers
(from ``archon_search.store_filters``), never f-strings passed directly to
``.where()``, ``.delete()``, or ``.count_rows()``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from archon_search.graph_types import Community, EntityType, GraphEdge, GraphNode, RelationshipType
from archon_search.store_filters import _sql_quote_str

if TYPE_CHECKING:
    import lancedb
    from datetime import datetime

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

    def _communities_table_name(self, collection: str) -> str:
        return _ARCHON_PREFIX + "graph_" + collection + "_communities"

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

    @staticmethod
    def _communities_schema():  # type: ignore[return]
        """Return the PyArrow schema for the communities table (E1b).

        ``entity_ids`` and ``representative_chunk_ids`` are ``list_(utf8)`` columns.
        ``summary_text`` is nullable utf8 — null when no LLM summary was generated.
        ``built_at`` is stored as ISO 8601 UTC string (same convention as the rest of
        the codebase; no timezone coercion enforced at the storage layer).
        """
        import pyarrow as pa  # noqa: PLC0415

        return pa.schema([
            pa.field("community_id", pa.utf8()),
            pa.field("entity_ids", pa.list_(pa.utf8())),
            pa.field("representative_chunk_ids", pa.list_(pa.utf8())),
            pa.field("summary_text", pa.utf8(), nullable=True),
            pa.field("built_at", pa.utf8()),
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
    # Communities table (E1b)
    # ------------------------------------------------------------------

    async def ensure_communities_table(self, collection: str) -> None:
        """Create the communities table for *collection* if it doesn't already exist.

        Idempotent — safe to call multiple times.
        """
        self._validate_collection(collection)
        db = self._require_db()
        await db.create_table(
            self._communities_table_name(collection),
            schema=self._communities_schema(),
            exist_ok=True,
        )

    async def write_communities(
        self,
        collection: str,
        communities: list[Community],
    ) -> None:
        """Persist *communities* for *collection*, replacing any existing data.

        Deletes all existing rows first, then inserts the new set.  This makes the
        operation idempotent — a second call to ``build-communities`` fully replaces
        the previous run without duplication.

        Note: The delete + add sequence is not atomic. A concurrent reader between
        the two operations will see zero communities for this collection. This is
        acceptable because ``write_communities`` is called exclusively from the
        ``build-communities`` CLI batch operation, and communities are derived
        (rebuildable) data. The failure mode is: re-run ``build-communities``.
        """
        import pyarrow as pa  # noqa: PLC0415

        self._validate_collection(collection)
        db = self._require_db()
        await self.ensure_communities_table(collection)
        table = await db.open_table(self._communities_table_name(collection))

        # Clear existing communities before inserting the new set
        await table.delete("1=1")

        if not communities:
            return

        data = pa.table(
            {
                "community_id": [c.community_id for c in communities],
                "entity_ids": [c.entity_ids for c in communities],
                "representative_chunk_ids": [c.representative_chunk_ids for c in communities],
                "summary_text": [c.summary_text for c in communities],
                "built_at": [c.built_at.isoformat() for c in communities],
            },
            schema=self._communities_schema(),
        )
        await table.add(data)

    # ponytail: LanceDB's _where_in helper generates simple equality predicates and cannot filter
    # list-typed columns. This method performs a full table scan and applies the intersection
    # filter in-process. Acceptable for < 1000 communities per collection; revisit if community
    # counts grow significantly.
    async def get_communities_for_entities(
        self,
        collection: str,
        entity_ids: list[str],
    ) -> list[Community]:
        """Return all communities whose ``entity_ids`` list intersects *entity_ids*."""
        if not entity_ids:
            return []

        self._validate_collection(collection)
        db = self._require_db()

        try:
            table = await db.open_table(self._communities_table_name(collection))
        except (FileNotFoundError, ValueError):
            return []

        rows = await table.query().to_list()
        entity_id_set = set(entity_ids)
        return [
            self._row_to_community(row)
            for row in rows
            if any(eid in entity_id_set for eid in (row.get("entity_ids") or []))
        ]

    async def list_community_representatives(
        self,
        collection: str,
    ) -> list[Community]:
        """Return all communities stored for *collection*.

        Returns an empty list if the communities table has not been created yet.
        All returned ``Community`` objects have ``representative_chunk_ids`` populated.
        """
        self._validate_collection(collection)
        db = self._require_db()

        try:
            table = await db.open_table(self._communities_table_name(collection))
        except (FileNotFoundError, ValueError):
            return []

        rows = await table.query().to_list()
        return [self._row_to_community(row) for row in rows]

    async def get_community_stats(
        self,
        collection: str,
    ) -> tuple[int, "datetime | None"]:
        """Return ``(community_count, last_built_at)`` for *collection*.

        Returns ``(0, None)`` when the communities table has not been created or is
        empty.  ``last_built_at`` is the maximum ``built_at`` across all communities,
        returned as a :class:`datetime` (UTC).
        """
        from datetime import datetime  # noqa: PLC0415

        self._validate_collection(collection)
        db = self._require_db()

        try:
            table = await db.open_table(self._communities_table_name(collection))
        except (FileNotFoundError, ValueError):
            return 0, None

        rows = await table.query().select(["built_at"]).to_list()
        if not rows:
            return 0, None

        count = len(rows)
        # Find the most-recent built_at across all communities
        max_ts: datetime | None = None
        for row in rows:
            raw = row.get("built_at")
            if not raw:
                continue
            try:
                ts = datetime.fromisoformat(raw)
            except (ValueError, TypeError):
                logger.warning(
                    "get_community_stats: unparseable built_at %r; skipping for max calculation", raw
                )
                continue
            if max_ts is None or ts > max_ts:
                max_ts = ts

        return count, max_ts

    # ------------------------------------------------------------------
    # Internal conversion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_community(row: dict) -> Community:
        """Convert a raw LanceDB row dict to a :class:`Community` dataclass."""
        from datetime import datetime, timezone  # noqa: PLC0415

        raw_ts = row.get("built_at") or ""
        try:
            built_at = datetime.fromisoformat(raw_ts)
        except (ValueError, TypeError):
            logger.warning("_row_to_community: unparseable built_at %r; using epoch sentinel", raw_ts)
            built_at = datetime(1970, 1, 1, tzinfo=timezone.utc)

        entity_ids = list(row.get("entity_ids") or [])
        representative_chunk_ids = list(row.get("representative_chunk_ids") or [])
        summary_text = row.get("summary_text") or None  # coerce empty string → None

        return Community(
            community_id=row["community_id"],
            entity_ids=entity_ids,
            representative_chunk_ids=representative_chunk_ids,
            built_at=built_at,
            summary_text=summary_text,
        )

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

    @staticmethod
    def _arrow_to_edges(arrow_table) -> list[GraphEdge]:  # type: ignore[return]
        """Convert a PyArrow table of edge rows into ``GraphEdge`` dataclass objects."""
        results: list[GraphEdge] = []
        ids = arrow_table["id"].to_pylist()
        source_node_ids = arrow_table["source_node_id"].to_pylist()
        target_node_ids = arrow_table["target_node_id"].to_pylist()
        rel_types = arrow_table["relationship_type"].to_pylist()
        source_docs = arrow_table["source_doc_id"].to_pylist()

        for eid, src_id, tgt_id, rtype, sdoc in zip(
            ids, source_node_ids, target_node_ids, rel_types, source_docs
        ):
            results.append(
                GraphEdge(
                    id=eid,
                    source_node_id=src_id,
                    target_node_id=tgt_id,
                    relationship_type=RelationshipType(rtype),
                    source_doc_id=sdoc,
                )
            )
        return results

    async def _load_all_from_table(self, table_name: str, context_label: str):  # type: ignore[return]
        """Open *table_name* and fetch all rows as an Arrow table.

        *context_label* is used only in log/error messages for diagnostics.

        Returns ``None`` if the table does not exist (``FileNotFoundError`` /
        ``ValueError``). Raises ``RuntimeError`` (chaining the original exception)
        for any other failure (I/O error, Arrow query error, etc.).
        """
        db = self._require_db()
        try:
            table = await db.open_table(table_name)
            return await table.query().to_arrow()
        except (FileNotFoundError, ValueError):
            return None
        except Exception as exc:
            logger.warning(
                "_load_all_from_table: unexpected error for table %r (%s)",
                table_name,
                context_label,
                exc_info=True,
            )
            raise RuntimeError(
                f"Failed to load table {table_name!r} ({context_label}): {exc}"
            ) from exc

    async def get_all_nodes(self, collection: str) -> list[GraphNode]:
        """Return all nodes for *collection*; empty list if table absent.

        Raises:
            RuntimeError: On unexpected storage / I/O errors (table absent
                returns ``[]``, not an error).
        """
        self._validate_collection(collection)
        arrow = await self._load_all_from_table(
            self._nodes_table_name(collection), f"collection={collection!r}"
        )
        if arrow is None:
            return []
        return self._arrow_to_nodes(arrow)

    async def get_all_edges(self, collection: str) -> list[GraphEdge]:
        """Return all edges for *collection*; empty list if table absent.

        Raises:
            RuntimeError: On unexpected storage / I/O errors (table absent
                returns ``[]``, not an error).
        """
        self._validate_collection(collection)
        arrow = await self._load_all_from_table(
            self._edges_table_name(collection), f"collection={collection!r}"
        )
        if arrow is None:
            return []
        return self._arrow_to_edges(arrow)

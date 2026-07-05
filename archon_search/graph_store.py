"""GraphStore — LanceDB-backed graph node and edge storage for GraphRAG (E1a/E1b/E2b).

Wraps per-collection, per-namespace LanceDB tables (E2d naming scheme):
  _archon_graph_{ns}__{col}_nodes
  _archon_graph_{ns}__{col}_edges
  _archon_graph_{ns}__{col}_communities   (E1b)
  _archon_graph_{ns}__{col}_mentions      (E2b)

All SQL predicates MUST go through ``_where_eq`` / ``_where_in`` helpers
(from ``archon_search.store_filters``), never f-strings passed directly to
``.where()``, ``.delete()``, or ``.count_rows()``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from archon_search.constants import _validate_namespace, _validate_segment_safe
from archon_search.graph_types import Community, EntityType, GraphEdge, GraphMention, GraphNode, RelationshipType
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
        _validate_segment_safe(collection, "collection name")

    def _table_name(self, collection: str, ns: str, suffix: str) -> str:
        return _ARCHON_PREFIX + "graph_" + ns + "__" + collection + "_" + suffix

    def _nodes_table_name(self, collection: str, ns: str) -> str:
        return self._table_name(collection, ns, "nodes")

    def _edges_table_name(self, collection: str, ns: str) -> str:
        return self._table_name(collection, ns, "edges")

    def _communities_table_name(self, collection: str, ns: str) -> str:
        return self._table_name(collection, ns, "communities")

    def _mentions_table_name(self, collection: str, ns: str) -> str:
        return self._table_name(collection, ns, "mentions")

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

    @staticmethod
    def _mentions_schema():  # type: ignore[return]
        """Return the PyArrow schema for the mentions table (E2b).

        Stores incidence records: entity_id, chunk_id, doc_id. All fields are utf8.
        The mentions table allows duplicate rows with the same (entity_id, chunk_id)
        if an extractor produces the same entity twice in one chunk.
        """
        import pyarrow as pa  # noqa: PLC0415

        return pa.schema([
            pa.field("entity_id", pa.utf8()),
            pa.field("chunk_id", pa.utf8()),
            pa.field("doc_id", pa.utf8()),
        ])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ensure_graph_tables(self, collection: str, ns: str) -> None:
        """Create nodes, edges, and mentions tables for *collection* if they don't already exist.

        Idempotent — safe to call multiple times. Raises ``ValueError`` for
        collection names that fail the naming regex.
        """
        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()

        await db.create_table(
            self._nodes_table_name(collection, ns),
            schema=self._nodes_schema(),
            exist_ok=True,
        )
        await db.create_table(
            self._edges_table_name(collection, ns),
            schema=self._edges_schema(),
            exist_ok=True,
        )
        await db.create_table(
            self._mentions_table_name(collection, ns),
            schema=self._mentions_schema(),
            exist_ok=True,
        )

    async def write_graph(
        self,
        collection: str,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        ns: str,
    ) -> None:
        """Upsert *nodes* and *edges* into the collection's graph tables.

        Uses ``merge_insert`` keyed on ``id`` so that re-ingesting the same
        document replaces existing rows rather than duplicating them.
        """
        import pyarrow as pa  # noqa: PLC0415

        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()

        if nodes:
            nodes_table = await db.open_table(self._nodes_table_name(collection, ns))
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
            edges_table = await db.open_table(self._edges_table_name(collection, ns))
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
        self, collection: str, entity_ids: list[str], ns: str
    ) -> list[GraphNode]:
        """Return GraphNode objects that are first-degree neighbours of *entity_ids*.

        Queries both ``source_node_id`` and ``target_node_id`` columns so edges
        in either direction are followed.  Returns unique neighbour nodes only.
        """
        if not entity_ids:
            return []

        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()

        edges_table = await db.open_table(self._edges_table_name(collection, ns))

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

        nodes_table = await db.open_table(self._nodes_table_name(collection, ns))
        node_pred = _where_in("id", neighbour_ids)
        nodes_arrow = await nodes_table.query().where(node_pred).to_arrow()

        return self._arrow_to_nodes(nodes_arrow)

    async def get_edges_for_nodes(
        self, collection: str, entity_ids: list[str], ns: str
    ) -> list[GraphEdge]:
        """Return edges where any of *entity_ids* appears as source or target node.

        Uses the same predicate as ``get_neighbours`` so edges in either direction
        are returned.  Useful for building ``TraversalStep`` objects that require
        the edge ``relationship_type`` (E1c naive-mode provenance).

        Empty *entity_ids* returns ``[]`` immediately.
        """
        if not entity_ids:
            return []

        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()

        edges_table = await db.open_table(self._edges_table_name(collection, ns))
        src_pred = _where_in("source_node_id", entity_ids)
        tgt_pred = _where_in("target_node_id", entity_ids)
        pred = "(" + src_pred + " OR " + tgt_pred + ")"

        arrow = await edges_table.query().where(pred).to_arrow()
        return self._arrow_to_edges(arrow)

    async def edge_count(self, collection: str, ns: str) -> int:
        """Return the number of edges in *collection*'s graph table; 0 if table absent."""
        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()
        try:
            edges_table = await db.open_table(self._edges_table_name(collection, ns))
            return await edges_table.count_rows()
        except FileNotFoundError:
            return 0
        except Exception:
            logger.warning(
                "edge_count: unexpected error for collection %r; reporting 0", collection, exc_info=True
            )
            return 0

    async def node_count(self, collection: str, ns: str) -> int:
        """Return the number of nodes in *collection*'s graph table; 0 if table absent."""
        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()
        try:
            nodes_table = await db.open_table(self._nodes_table_name(collection, ns))
            return await nodes_table.count_rows()
        except FileNotFoundError:
            return 0
        except Exception:
            logger.warning(
                "node_count: unexpected error for collection %r; reporting 0", collection, exc_info=True
            )
            return 0

    async def find_nodes_by_name(
        self, collection: str, names: list[str], ns: str
    ) -> list[GraphNode]:
        """Return nodes whose ``entity_name`` matches any of *names* (case-insensitive).

        Empty *names* list returns ``[]`` immediately.  The predicate uses
        ``lower(entity_name)`` so stored names like ``"AuthService"`` match
        a query for ``"authservice"`` or ``"AUTHSERVICE"``.
        """
        if not names:
            return []

        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()

        lower_names = [n.lower() for n in names]
        # Build safe SQL: lower(entity_name) IN ('name1', 'name2', ...)
        items = ", ".join(_sql_quote_str(n) for n in lower_names)
        predicate = "lower(entity_name) IN (" + items + ")"

        nodes_table = await db.open_table(self._nodes_table_name(collection, ns))
        nodes_arrow = await nodes_table.query().where(predicate).to_arrow()

        return self._arrow_to_nodes(nodes_arrow)

    # ------------------------------------------------------------------
    # Communities table (E1b)
    # ------------------------------------------------------------------

    async def communities_table_exists(self, collection: str, ns: str) -> bool:
        """Return ``True`` if the communities table exists for *collection*.

        Does NOT indicate whether any communities have been written — only
        whether ``ensure_communities_table`` (or ``write_communities``) has
        been called at least once for this collection.
        """
        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()
        try:
            await db.open_table(self._communities_table_name(collection, ns))
            return True
        except (FileNotFoundError, ValueError):
            return False

    async def ensure_communities_table(self, collection: str, ns: str) -> None:
        """Create the communities table for *collection* if it doesn't already exist.

        Idempotent — safe to call multiple times.
        """
        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()
        await db.create_table(
            self._communities_table_name(collection, ns),
            schema=self._communities_schema(),
            exist_ok=True,
        )

    async def write_communities(
        self,
        collection: str,
        communities: list[Community],
        ns: str,
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
        _validate_namespace(ns)
        db = self._require_db()
        await self.ensure_communities_table(collection, ns)
        table = await db.open_table(self._communities_table_name(collection, ns))

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

    # ------------------------------------------------------------------
    # Mentions table (E2b)
    # ------------------------------------------------------------------

    async def write_mentions(
        self,
        collection: str,
        mentions: list[GraphMention],
        ns: str,
    ) -> None:
        """Append *mentions* into the collection's mentions table (E2b).

        This is an append-only operation; the mentions table has no upsert key.
        The pipeline uses delete-then-add per doc_id for idempotency on re-ingest:
        ``delete_mentions_by_doc(doc_id)`` followed by ``write_mentions(new_mentions)``.
        """
        import pyarrow as pa  # noqa: PLC0415

        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()
        await self.ensure_graph_tables(collection, ns)

        if not mentions:
            return

        table = await db.open_table(self._mentions_table_name(collection, ns))
        data = pa.table(
            {
                "entity_id": [m.entity_id for m in mentions],
                "chunk_id": [m.chunk_id for m in mentions],
                "doc_id": [m.doc_id for m in mentions],
            },
            schema=self._mentions_schema(),
        )
        await table.add(data)

    async def delete_mentions_by_doc(
        self,
        collection: str,
        doc_id: str,
        ns: str,
    ) -> None:
        """Delete all mentions for a given *doc_id* from the collection's mentions table.

        Uses ``_where_eq`` for SQL-safe predicate construction. Idempotent — deleting
        a non-existent doc_id is a noop.
        """
        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()

        try:
            table = await db.open_table(self._mentions_table_name(collection, ns))
        except (FileNotFoundError, ValueError):
            # Table doesn't exist; nothing to delete
            return

        predicate = _where_eq("doc_id", doc_id)
        await table.delete(predicate)

    async def get_all_mentions(
        self,
        collection: str,
        limit: int | None = None,
        *,
        ns: str,
    ) -> list[GraphMention]:
        """Return mentions for *collection*; optionally limited to first *limit* rows.

        When *limit* is None, returns all rows. When *limit* is provided, returns
        at most *limit* rows (storage order, not sorted). Returns empty list if
        the mentions table does not exist.

        Note: The mentions table allows duplicate rows with the same (entity_id, chunk_id)
        if the extractor produces the same entity twice in one chunk. Deduplication is
        the responsibility of the caller (e.g., ``graph_inspector.py``).
        """
        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()

        try:
            table = await db.open_table(self._mentions_table_name(collection, ns))
        except (FileNotFoundError, ValueError):
            return []

        query = table.query()
        if limit is not None:
            query = query.limit(limit)
        arrow = await query.to_arrow()

        # Convert PyArrow table to GraphMention objects
        results: list[GraphMention] = []
        entity_ids = arrow["entity_id"].to_pylist()
        chunk_ids = arrow["chunk_id"].to_pylist()
        doc_ids = arrow["doc_id"].to_pylist()

        for entity_id, chunk_id, doc_id in zip(entity_ids, chunk_ids, doc_ids):
            results.append(
                GraphMention(
                    entity_id=entity_id,
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                )
            )
        return results

    # ponytail: LanceDB's _where_in helper generates simple equality predicates and cannot filter
    # list-typed columns. This method performs a full table scan and applies the intersection
    # filter in-process. Acceptable for < 1000 communities per collection; revisit if community
    # counts grow significantly.
    async def get_communities_for_entities(
        self,
        collection: str,
        entity_ids: list[str],
        ns: str,
    ) -> list[Community]:
        """Return all communities whose ``entity_ids`` list intersects *entity_ids*."""
        if not entity_ids:
            return []

        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()

        try:
            table = await db.open_table(self._communities_table_name(collection, ns))
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
        ns: str,
    ) -> list[Community]:
        """Return all communities stored for *collection*.

        Returns an empty list if the communities table has not been created yet.
        All returned ``Community`` objects have ``representative_chunk_ids`` populated.
        """
        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()

        try:
            table = await db.open_table(self._communities_table_name(collection, ns))
        except (FileNotFoundError, ValueError):
            return []

        rows = await table.query().to_list()
        return [self._row_to_community(row) for row in rows]

    async def get_community_stats(
        self,
        collection: str,
        ns: str,
    ) -> tuple[int, "datetime | None"]:
        """Return ``(community_count, last_built_at)`` for *collection*.

        Returns ``(0, None)`` when the communities table has not been created or is
        empty.  ``last_built_at`` is the maximum ``built_at`` across all communities,
        returned as a :class:`datetime` (UTC).
        """
        from datetime import datetime  # noqa: PLC0415

        self._validate_collection(collection)
        _validate_namespace(ns)
        db = self._require_db()

        try:
            table = await db.open_table(self._communities_table_name(collection, ns))
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

    async def get_all_nodes(self, collection: str, ns: str) -> list[GraphNode]:
        """Return all nodes for *collection*; empty list if table absent.

        Raises:
            RuntimeError: On unexpected storage / I/O errors (table absent
                returns ``[]``, not an error).
        """
        self._validate_collection(collection)
        _validate_namespace(ns)
        arrow = await self._load_all_from_table(
            self._nodes_table_name(collection, ns), f"collection={collection!r} ns={ns!r}"
        )
        if arrow is None:
            return []
        return self._arrow_to_nodes(arrow)

    async def get_entity_presence_across_collections(
        self, collection_names: list[str], ns: str
    ) -> dict[str, int]:
        """Return entity_id → count of distinct collections containing that entity.

        For each collection in *collection_names*, scans the nodes table and counts
        how many distinct collections each entity appears in. An entity is counted
        at most once per collection even if it has duplicate rows.

        Returns ``{}`` if *collection_names* is empty. Collections whose node
        tables don't exist are skipped — absent tables contribute 0 to entity counts.
        Unreadable tables are skipped with a WARNING log.
        """
        if not collection_names:
            return {}

        _validate_namespace(ns)
        # Deduplicate while preserving order — duplicate names would double-count entities
        collection_names = list(dict.fromkeys(collection_names))

        # Verify store is connected before the loop — a disconnected store must fail
        # loudly, not silently return {}, because an empty presence map causes every
        # entity to receive df=1 (max IDF boost), corrupting TF-IDF salience scores.
        self._require_db()

        presence: dict[str, int] = {}
        for collection in collection_names:
            try:
                node_ids = await self._fetch_collection_entity_ids(collection, ns)
            except RuntimeError as e:
                logger.debug(
                    "get_entity_presence_across_collections: skipping collection %r — read failed: %s",
                    collection,
                    e,
                )
                continue
            seen_in_collection: set[str] = set()
            for entity_id in node_ids:
                if entity_id not in seen_in_collection:
                    seen_in_collection.add(entity_id)
                    presence[entity_id] = presence.get(entity_id, 0) + 1

        return presence

    async def _fetch_collection_entity_ids(self, collection: str, ns: str) -> list[str]:
        """Return all entity IDs from *collection*'s node table; ``[]`` if absent.

        Fetches only the ``id`` column — avoids loading all 6 node columns and
        constructing ``GraphNode`` objects when only IDs are needed.

        Raises:
            RuntimeError: On unexpected storage / I/O errors (table absent
                returns ``[]``, not an error).
        """
        self._validate_collection(collection)
        db = self._require_db()
        # Block 1: open the table — FileNotFoundError/ValueError = absent, silent skip
        try:
            table = await db.open_table(self._nodes_table_name(collection, ns))
        except (FileNotFoundError, ValueError):
            return []
        except Exception as exc:  # unexpected open failure — log + raise
            logger.warning(
                "_fetch_collection_entity_ids: failed to open node table for %r: %s",
                collection,
                exc,
                exc_info=True,
            )
            raise RuntimeError(
                f"Failed to open node table for collection {collection!r}"
            ) from exc
        # Block 2: read IDs — any failure = corrupt/unreadable, log + raise
        try:
            arrow = await table.query().select(["id"]).to_arrow()
            return arrow["id"].to_pylist()
        except Exception as exc:
            logger.warning(
                "_fetch_collection_entity_ids: failed to read node ids for %r: %s",
                collection,
                exc,
                exc_info=True,
            )
            raise RuntimeError(
                f"Failed to read entity ids from {collection}"
            ) from exc

    async def get_all_edges(self, collection: str, ns: str) -> list[GraphEdge]:
        """Return all edges for *collection*; empty list if table absent.

        Raises:
            RuntimeError: On unexpected storage / I/O errors (table absent
                returns ``[]``, not an error).
        """
        self._validate_collection(collection)
        _validate_namespace(ns)
        arrow = await self._load_all_from_table(
            self._edges_table_name(collection, ns), f"collection={collection!r} ns={ns!r}"
        )
        if arrow is None:
            return []
        return self._arrow_to_edges(arrow)

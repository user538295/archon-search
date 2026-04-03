"""RagStore — LanceDB-backed vector + FTS store for Archon RAG (FEAT-019)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from archon.rag._types import ChunkRecord, CollectionInfo, DocumentInfo, SearchResult

if TYPE_CHECKING:
    import lancedb
    import pyarrow as pa

logger = logging.getLogger("archon")

_CHUNK_ID_RE = re.compile(r"^[a-f0-9]{64}-\d{6}$")
_DOC_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_COLLECTION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_ARCHON_PREFIX = "_archon_"
_META_TABLE = "_archon_collection_meta"

_RRF_K = 60  # RRF constant


def _rrf_score(rank: int) -> float:
    return 1.0 / (_RRF_K + rank + 1)


class RagStore:
    """Async LanceDB store for chunked document embeddings."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db: Optional[lancedb.db.AsyncConnection] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        import lancedb  # noqa: PLC0415

        self._db_path.mkdir(parents=True, exist_ok=True)
        self._db = await lancedb.connect_async(str(self._db_path))

    async def disconnect(self) -> None:
        db = self._db
        self._db = None
        if db is not None:
            db.close()

    # ------------------------------------------------------------------
    # Guard
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_collection(collection: str) -> None:
        if not _COLLECTION_RE.match(collection):
            raise ValueError(
                f"Invalid collection name: {collection!r} — "
                "must start with alphanumeric, contain only [a-zA-Z0-9_-], max 64 chars"
            )

    def _require_connected(self) -> lancedb.db.AsyncConnection:
        if self._db is None:
            raise RuntimeError("RagStore not connected")
        return self._db

    # ------------------------------------------------------------------
    # Schema helper
    # ------------------------------------------------------------------

    @staticmethod
    def _schema(embedding_dim: int) -> pa.Schema:
        import pyarrow as pa  # noqa: PLC0415

        return pa.schema(
            [
                pa.field("doc_id", pa.utf8()),
                pa.field("chunk_id", pa.utf8()),
                pa.field("text", pa.utf8()),
                pa.field("vector", pa.list_(pa.float32(), embedding_dim)),
                pa.field("source_path", pa.utf8()),
                pa.field("indexed_at", pa.utf8()),
            ]
        )

    @staticmethod
    def _meta_schema() -> pa.Schema:
        import pyarrow as pa  # noqa: PLC0415

        return pa.schema(
            [
                pa.field("name", pa.utf8()),
                pa.field("description", pa.utf8()),
                pa.field("centroid_json", pa.utf8()),
                pa.field("doc_count", pa.int64()),
                pa.field("chunk_count", pa.int64()),
                pa.field("embedding_model", pa.utf8()),
                pa.field("last_indexed", pa.utf8()),
                pa.field("last_described", pa.utf8()),
                pa.field("described_at_doc_count", pa.int64()),
            ]
        )

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    async def ensure_collection(self, collection: str, embedding_dim: int) -> None:
        self._validate_collection(collection)
        db = self._require_connected()
        await db.create_table(
            collection,
            schema=self._schema(embedding_dim),
            exist_ok=True,
        )

    async def drop_collection(self, name: str) -> None:
        """Drop a LanceDB table by name.

        Raises:
            RuntimeError: if the store is not connected.
            KeyError: if *name* does not exist in LanceDB.
        """
        db = self._require_connected()
        names: list[str] = (await db.list_tables()).tables
        if name not in names:
            raise KeyError(name)
        await db.drop_table(name)

    async def rename_collection(self, old: str, new: str) -> None:
        """Rename a LanceDB table from *old* to *new*.

        The caller is responsible for ensuring *new* does not conflict with an
        existing collection before calling this method.

        Raises:
            RuntimeError: if the store is not connected.
            KeyError: if *old* does not exist in LanceDB.
            ValueError: if *new* already exists in LanceDB, or if *new* is not
                a valid collection name.
            NotImplementedError: if the installed LanceDB version lacks ``rename_table``.
        """
        self._validate_collection(new)
        db = self._require_connected()
        names: list[str] = (await db.list_tables()).tables
        if old not in names:
            raise KeyError(old)
        if new in names:
            raise ValueError(f"Target collection already exists: {new!r}")
        try:
            await db.rename_table(old, new)
        except (AttributeError, NotImplementedError) as exc:
            raise NotImplementedError(
                "rename_table not available; use copy-ingest + drop"
            ) from exc

    async def list_collections(self) -> list[CollectionInfo]:
        db = self._require_connected()
        all_names: list[str] = (await db.list_tables()).tables
        names = [n for n in all_names if not n.startswith(_ARCHON_PREFIX)]
        result: list[CollectionInfo] = []
        for name in names:
            try:
                table = await db.open_table(name)
                chunk_count = await table.count_rows()
                # count distinct doc_ids via Arrow column (avoids materializing dicts)
                arrow_table = await table.query().select(["doc_id"]).to_arrow()
                doc_count = len(arrow_table.column("doc_id").unique())
                result.append(CollectionInfo(name=name, doc_count=doc_count, chunk_count=chunk_count))
            except (RuntimeError, ValueError, OSError) as exc:
                logger.warning("Could not inspect collection %s: %s", name, exc)
        return result

    # ------------------------------------------------------------------
    # Collection metadata (FEAT-022)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_meta(row: "dict[str, Any]") -> "CollectionMeta":
        from archon.rag.collection_meta import CollectionMeta  # noqa: PLC0415

        try:
            centroid = json.loads(row["centroid_json"]) if row["centroid_json"] else None
        except json.JSONDecodeError:
            logger.warning("Malformed centroid_json for collection %r — centroid set to None", row.get("name"))
            centroid = None
        last_indexed = datetime.fromisoformat(row["last_indexed"]) if row["last_indexed"] else None
        last_described = datetime.fromisoformat(row["last_described"]) if row["last_described"] else None
        raw_described_at: int = row["described_at_doc_count"]
        described_at = None if raw_described_at < 0 else raw_described_at
        return CollectionMeta(
            name=row["name"],
            description=row["description"] if row["description"] else None,
            centroid=centroid,
            doc_count=row["doc_count"],
            chunk_count=row["chunk_count"],
            embedding_model=row["embedding_model"],
            last_indexed=last_indexed,
            last_described=last_described,
            described_at_doc_count=described_at,
        )

    async def get_collection_meta(self, name: str) -> "CollectionMeta | None":
        self._validate_collection(name)
        db = self._require_connected()
        all_names: list[str] = (await db.list_tables()).tables
        if _META_TABLE not in all_names:
            return None
        table = await db.open_table(_META_TABLE)
        # Fetch all rows and filter in Python to avoid SQL injection concerns
        rows = await table.query().to_list()
        matching = [r for r in rows if r["name"] == name]
        if not matching:
            return None
        return self._row_to_meta(matching[0])

    async def get_all_collections_meta(self) -> "list[CollectionMeta]":
        db = self._require_connected()
        all_names: list[str] = (await db.list_tables()).tables
        if _META_TABLE not in all_names:
            return []
        table = await db.open_table(_META_TABLE)
        rows = await table.query().to_list()
        return [self._row_to_meta(row) for row in rows]

    async def update_collection_meta(self, meta: "CollectionMeta") -> None:
        self._validate_collection(meta.name)
        db = self._require_connected()
        all_names: list[str] = (await db.list_tables()).tables
        if _META_TABLE not in all_names:
            table = await db.create_table(_META_TABLE, schema=self._meta_schema())
        else:
            table = await db.open_table(_META_TABLE)
            # Upsert = delete existing row by name, then insert.
            # name is validated against _COLLECTION_RE (alphanumeric + underscore/dash),
            # so it is safe to use directly in the SQL filter expression.
            rows = await table.query().to_list()
            if any(r["name"] == meta.name for r in rows):
                await table.delete(f"name = '{meta.name}'")

        centroid_json = json.dumps(meta.centroid) if meta.centroid is not None else ""
        last_indexed_str = meta.last_indexed.isoformat() if meta.last_indexed else ""
        last_described_str = meta.last_described.isoformat() if meta.last_described else ""
        described_at = meta.described_at_doc_count if meta.described_at_doc_count is not None else -1

        await table.add(
            [
                {
                    "name": meta.name,
                    "description": meta.description or "",
                    "centroid_json": centroid_json,
                    "doc_count": meta.doc_count,
                    "chunk_count": meta.chunk_count,
                    "embedding_model": meta.embedding_model,
                    "last_indexed": last_indexed_str,
                    "last_described": last_described_str,
                    "described_at_doc_count": described_at,
                }
            ]
        )

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    async def ingest_chunks(self, collection: str, chunks: list[ChunkRecord]) -> int:
        self._validate_collection(collection)
        db = self._require_connected()
        for chunk in chunks:
            if not _CHUNK_ID_RE.match(chunk.chunk_id):
                raise ValueError(f"malformed chunk_id: {chunk.chunk_id!r}")

        if not chunks:
            return 0

        table = await db.open_table(collection)
        rows = [
            {
                "doc_id": c.doc_id,
                "chunk_id": c.chunk_id,
                "text": c.text,
                "vector": [float(v) for v in c.vector],
                "source_path": c.source_path,
                "indexed_at": c.indexed_at,
            }
            for c in chunks
        ]
        await table.add(rows)
        return len(chunks)

    # ------------------------------------------------------------------
    # FTS index
    # ------------------------------------------------------------------

    async def rebuild_fts_index(self, collection: str) -> None:
        self._validate_collection(collection)
        db = self._require_connected()
        from lancedb.index import FTS  # noqa: PLC0415

        table = await db.open_table(collection)
        await table.create_index("text", config=FTS(), replace=True)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def hybrid_search(
        self,
        collection: str,
        query_vector: list[float],
        query_text: str,
        top_k: int,
    ) -> list[SearchResult]:
        self._validate_collection(collection)
        db = self._require_connected()
        try:
            table = await db.open_table(collection)
        except ValueError:
            return []

        fetch = max(top_k * 3, 20)

        # Vector search
        vec_rows = await table.vector_search(query_vector).limit(fetch).to_list()
        vec_rank: dict[str, int] = {r["chunk_id"]: i for i, r in enumerate(vec_rows)}

        # FTS search (may fail if no index)
        fts_rows: list[dict[str, Any]] = []
        fts_rank: dict[str, int] = {}
        try:
            fts_q = await table.search(query_text, query_type="fts")
            fts_rows = await fts_q.limit(fetch).to_list()
            fts_rank = {r["chunk_id"]: i for i, r in enumerate(fts_rows)}
        except Exception as exc:
            exc_str = str(exc).lower()
            if "index" in exc_str or "fts" in exc_str:
                logger.warning("FTS index not available for collection %r, using vector-only results", collection)
            else:
                raise

        # Build combined row lookup
        all_rows: dict[str, dict[str, Any]] = {r["chunk_id"]: r for r in vec_rows}
        for r in fts_rows:
            all_rows.setdefault(r["chunk_id"], r)

        # RRF scoring
        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk_id, row in all_rows.items():
            score = 0.0
            if chunk_id in vec_rank:
                score += _rrf_score(vec_rank[chunk_id])
            if chunk_id in fts_rank:
                score += _rrf_score(fts_rank[chunk_id])
            scored.append((score, row))

        scored.sort(key=lambda x: x[0], reverse=True)

        return [
            SearchResult(
                doc_id=row["doc_id"],
                chunk_id=row["chunk_id"],
                text=row["text"],
                score=score,
                source_path=row["source_path"],
            )
            for score, row in scored[:top_k]
        ]

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete_document(self, collection: str, doc_id: str) -> int:
        self._validate_collection(collection)
        db = self._require_connected()
        if not _DOC_ID_RE.match(doc_id):
            raise ValueError(f"Invalid doc_id: {doc_id!r} — must be 64 hex chars")
        try:
            table = await db.open_table(collection)
        except ValueError:
            return 0
        count: int = await table.count_rows(f"doc_id = '{doc_id}'")
        if count == 0:
            return 0
        await table.delete(f"doc_id = '{doc_id}'")
        return count

    async def delete_by_source_path(self, collection: str, source_path: str) -> int:
        """Delete all chunks for a source file by computing its doc_id.

        ``source_path`` must be an absolute, resolved path — the same form
        produced by ``str(path.resolve())`` at ingest time.  Relative paths
        will resolve against the current working directory at call time and
        may not match the stored doc_id.
        """
        doc_id = hashlib.sha256(str(Path(source_path).resolve()).encode()).hexdigest()
        return await self.delete_document(collection, doc_id)

    # ------------------------------------------------------------------
    # List documents
    # ------------------------------------------------------------------

    async def list_documents(
        self, collection: str, limit: int = 100
    ) -> list[DocumentInfo]:
        self._validate_collection(collection)
        limit = min(limit, 1000)  # cap to prevent unbounded memory consumption
        db = self._require_connected()
        try:
            table = await db.open_table(collection)
        except ValueError:
            return []

        rows = (
            await table.query()
            .select(["doc_id", "source_path", "indexed_at"])
            .limit(limit * 50)
            .to_list()
        )

        # Aggregate per doc_id
        docs: dict[str, dict[str, Any]] = {}
        for r in rows:
            doc_id = r["doc_id"]
            if doc_id not in docs:
                docs[doc_id] = {
                    "source_path": r["source_path"],
                    "indexed_at": r["indexed_at"],
                    "chunk_count": 0,
                }
            docs[doc_id]["chunk_count"] += 1

        result = [
            DocumentInfo(
                doc_id=doc_id,
                source_path=info["source_path"],
                chunk_count=info["chunk_count"],
                indexed_at=info["indexed_at"],
            )
            for doc_id, info in docs.items()
        ]
        return result[:limit]

    # ------------------------------------------------------------------
    # Fetch adjacent chunks
    # ------------------------------------------------------------------

    async def fetch_adjacent_chunks(
        self,
        collection: str,
        doc_id: str,
        center_idx: int,
        window: int,
    ) -> list[ChunkRecord]:
        self._validate_collection(collection)
        db = self._require_connected()
        if not _DOC_ID_RE.match(doc_id):
            raise ValueError(f"Invalid doc_id: {doc_id!r}")
        target_ids = [
            f"{doc_id}-{i:06d}"
            for i in range(max(0, center_idx - window), center_idx + window + 1)
            if i != center_idx
        ]

        if not target_ids:
            return []

        try:
            table = await db.open_table(collection)
        except ValueError:
            return []

        # Build SQL IN clause
        id_list = ", ".join(f"'{cid}'" for cid in target_ids)
        rows = (
            await table.query()
            .where(f"chunk_id IN ({id_list})")
            .to_list()
        )

        result = [
            ChunkRecord(
                doc_id=r["doc_id"],
                chunk_id=r["chunk_id"],
                text=r["text"],
                vector=list(r["vector"]),
                source_path=r["source_path"],
                indexed_at=r["indexed_at"],
            )
            for r in rows
        ]
        result.sort(key=lambda c: c.chunk_id)
        return result

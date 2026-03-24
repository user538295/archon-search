"""RagStore — LanceDB-backed vector + FTS store for Archon RAG (FEAT-019)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

import lancedb
import pyarrow as pa

from archon.rag._types import ChunkRecord, CollectionInfo, DocumentInfo, SearchResult

logger = logging.getLogger("archon")

_CHUNK_ID_RE = re.compile(r"^[a-f0-9]{64}-\d{6}$")
_DOC_ID_RE = re.compile(r"^[a-f0-9]{64}$")

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
        self._db_path.mkdir(parents=True, exist_ok=True)
        self._db = await lancedb.connect_async(str(self._db_path))

    async def disconnect(self) -> None:
        self._db = None

    # ------------------------------------------------------------------
    # Guard
    # ------------------------------------------------------------------

    def _require_connected(self) -> lancedb.db.AsyncConnection:
        if self._db is None:
            raise RuntimeError("RagStore not connected")
        return self._db

    # ------------------------------------------------------------------
    # Schema helper
    # ------------------------------------------------------------------

    @staticmethod
    def _schema(embedding_dim: int) -> pa.Schema:
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

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    async def ensure_collection(self, collection: str, embedding_dim: int) -> None:
        db = self._require_connected()
        await db.create_table(
            collection,
            schema=self._schema(embedding_dim),
            exist_ok=True,
        )

    async def list_collections(self) -> list[CollectionInfo]:
        db = self._require_connected()
        # list_tables() returns a response object with .tables attribute
        response = await db.list_tables()
        names: list[str] = response.tables
        result: list[CollectionInfo] = []
        for name in names:
            try:
                table = await db.open_table(name)
                chunk_count = await table.count_rows()
                # count distinct doc_ids
                rows = await table.query().select(["doc_id"]).limit(chunk_count + 1).to_list()
                doc_count = len({r["doc_id"] for r in rows})
                result.append(CollectionInfo(name=name, doc_count=doc_count, chunk_count=chunk_count))
            except Exception:
                logger.warning("Could not inspect collection %s", name)
        return result

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    async def ingest_chunks(self, collection: str, chunks: list[ChunkRecord]) -> int:
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
        db = self._require_connected()
        table = await db.open_table(collection)
        await table.create_index("text", config=lancedb.index.FTS(), replace=True)

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
        fts_rank: dict[str, int] = {}
        try:
            fts_q = await table.search(query_text, query_type="fts")
            fts_rows = await fts_q.limit(fetch).to_list()
            fts_rank = {r["chunk_id"]: i for i, r in enumerate(fts_rows)}
        except Exception:
            logger.warning("FTS search failed for collection %r, using vector-only", collection)

        # Build combined row lookup
        all_rows: dict[str, dict[str, Any]] = {r["chunk_id"]: r for r in vec_rows}
        try:
            for r in fts_rows:  # noqa: F821  # type: ignore[name-defined]
                all_rows.setdefault(r["chunk_id"], r)
        except NameError:
            pass

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

    # ------------------------------------------------------------------
    # List documents
    # ------------------------------------------------------------------

    async def list_documents(
        self, collection: str, limit: int = 100
    ) -> list[DocumentInfo]:
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
        db = self._require_connected()
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

        return [
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

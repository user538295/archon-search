"""SearchStore — LanceDB-backed vector + FTS store for Archon RAG (FEAT-019)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search._types import ChunkRecord, CollectionInfo, DocumentInfo, SearchResult
from archon_search.constants import DEFAULT_NAMESPACE, _validate_namespace

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

_META_MAX_FIELDS = 50
_META_MAX_KEY_LEN = 256
_META_MAX_VAL_LEN = 4096


def _rrf_score(rank: int) -> float:
    return 1.0 / (_RRF_K + rank + 1)


def validate_metadata(metadata: dict[str, str]) -> None:
    """Validate metadata dict against size constraints.

    Raises ValueError if any constraint is violated.
    """
    if len(metadata) > _META_MAX_FIELDS:
        raise ValueError(f"metadata exceeds max {_META_MAX_FIELDS} fields (got {len(metadata)})")
    for key, value in metadata.items():
        if not isinstance(key, str):
            raise ValueError(f"metadata key must be a string (got {type(key).__name__})")
        if not isinstance(value, str):
            raise ValueError(f"metadata value must be a string (got {type(value).__name__})")
        if len(key) > _META_MAX_KEY_LEN:
            raise ValueError(f"metadata key too long: max {_META_MAX_KEY_LEN} chars (got {len(key)})")
        if len(value) > _META_MAX_VAL_LEN:
            raise ValueError(f"metadata value too long: max {_META_MAX_VAL_LEN} chars (got {len(value)})")


def parse_metadata(raw: str) -> dict[str, str]:
    """Parse a JSON string into a metadata dict. Returns empty dict on error.

    Coerces non-string values to strings and skips non-string keys to guard
    against corrupted stored data.
    """
    if not raw:
        return {}
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            return {}
        return {str(k): str(v) for k, v in result.items() if isinstance(k, str)}
    except json.JSONDecodeError:
        return {}


class SearchStore:
    """Async LanceDB store for chunked document embeddings."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser()
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
            raise RuntimeError("SearchStore not connected")
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
                # Extended metadata fields (FEAT-038 Task 6.1)
                pa.field("file_type", pa.utf8()),
                pa.field("language", pa.utf8()),  # nullable via None → ""
                pa.field("metadata", pa.utf8()),   # JSON string
                pa.field("custom_score", pa.float32()),  # nullable
                pa.field("ingested_by", pa.utf8()),
                pa.field("updated_at", pa.utf8()),
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
                pa.field("namespace", pa.utf8()),
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
                result.append(CollectionInfo(name=name, doc_count=doc_count, chunk_count=chunk_count, namespace=DEFAULT_NAMESPACE))
            except (RuntimeError, ValueError, OSError) as exc:
                logger.warning("Could not inspect collection %s: %s", name, exc)
        return result

    # ------------------------------------------------------------------
    # Collection metadata (FEAT-022)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_meta(row: "dict[str, Any]") -> "CollectionMeta":
        from archon_search.collection_meta import CollectionMeta  # noqa: PLC0415

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
            namespace=row.get("namespace") or DEFAULT_NAMESPACE,
        )

    async def get_collection_meta(self, name: str, namespace: str = DEFAULT_NAMESPACE) -> "CollectionMeta | None":
        self._validate_collection(name)
        _validate_namespace(namespace)
        db = self._require_connected()
        all_names: list[str] = (await db.list_tables()).tables
        if _META_TABLE not in all_names:
            return None
        table = await db.open_table(_META_TABLE)
        # Fetch all rows and filter in Python to avoid SQL injection concerns
        rows = await table.query().to_list()
        matching = [
            r for r in rows
            if r["name"] == name and (r.get("namespace") or DEFAULT_NAMESPACE) == namespace
        ]
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

    async def migrate_namespace(self) -> None:
        """Idempotent: adds namespace column to _archon_collection_meta if absent."""
        db = self._require_connected()
        all_names: list[str] = (await db.list_tables()).tables
        if _META_TABLE not in all_names:
            return
        table = await db.open_table(_META_TABLE)
        schema_names = (await table.schema()).names
        if "namespace" in schema_names:
            return
        try:
            await table.add_columns({"namespace": f"'{DEFAULT_NAMESPACE}'"})
            logger.info("namespace migration: added namespace column to %s", _META_TABLE)
        except Exception as exc:
            if "already exists" in str(exc).lower():
                logger.warning("Concurrent migration: namespace column already added — %s", exc)
                return
            raise

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
                    "namespace": meta.namespace,
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
            validate_metadata(chunk.metadata)

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
                "file_type": c.file_type or "",
                "language": c.language or "",
                "metadata": json.dumps(c.metadata) if c.metadata else "{}",
                "custom_score": float(c.custom_score) if c.custom_score is not None else None,
                "ingested_by": c.ingested_by or "archon-search-cli",
                "updated_at": c.updated_at or c.indexed_at,
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
                file_type=r.get("file_type") or "",
                language=r.get("language") or None,
                metadata=parse_metadata(r.get("metadata") or "{}"),
                custom_score=r.get("custom_score"),
                ingested_by=r.get("ingested_by") or "archon-search-cli",
                updated_at=r.get("updated_at") or r["indexed_at"],
            )
            for r in rows
        ]
        result.sort(key=lambda c: c.chunk_id)
        return result

    async def get_all_vectors(self, collection: str) -> list[list[float]]:
        """Return all embedding vectors stored in the collection.

        Returns an empty list if the collection does not exist.
        Used by :meth:`SearchPipeline.recompute_collection_meta`.
        """
        self._validate_collection(collection)
        db = self._require_connected()
        try:
            table = await db.open_table(collection)
        except ValueError:
            return []
        rows = await table.query().select(["vector"]).to_list()
        return [list(r["vector"]) for r in rows if r.get("vector") is not None]

    async def count_documents(self, collection: str) -> int:
        """Return the number of distinct documents (by doc_id) in a collection.

        Unlike :meth:`list_documents`, this method has no upper bound and is
        accurate for collections of any size.  Returns 0 if the collection does
        not exist.
        """
        self._validate_collection(collection)
        db = self._require_connected()
        try:
            table = await db.open_table(collection)
        except ValueError:
            return 0
        rows = await table.query().select(["doc_id"]).to_list()
        return len({r["doc_id"] for r in rows})


# ---------------------------------------------------------------------------
# Private eval / diagnostic trace helper
#
# Raw-score semantics (LanceDB backend):
#   - Vector search rows expose ``_distance`` (lower is better; kind="distance").
#   - FTS rows expose ``_score`` (higher is better; BM25; kind="bm25").
#   - RRF score is a separate fused rank score (higher is better).
#
# When a backend result row omits the expected raw-score field, the
# corresponding score and score_kind are set to ``None`` — never fabricated.
# When no FTS index exists the fts_* fields are all ``None``.
# ---------------------------------------------------------------------------


async def _hybrid_search_with_trace(
    store: SearchStore,
    collection: str,
    query_vector: list[float],
    query_text: str,
    candidate_depth: int,
) -> list[ScoredSearchCandidate]:
    """Internal trace helper — returns full score provenance per candidate.

    This is a private, eval/debug-only function.  It must NOT appear in the
    public ``archon_search`` package exports.

    Args:
        store: A connected :class:`SearchStore` instance.
        collection: Collection name (validated by the store).
        query_vector: Embedding vector for vector search.
        query_text: Text query for FTS search.
        candidate_depth: Maximum number of raw candidates to fetch from each
            search leg (analogous to ``fetch`` in :meth:`SearchStore.hybrid_search`).

    Returns:
        List of :class:`ScoredSearchCandidate` ordered by descending RRF score.
        Ties are broken by ``chunk_id`` (ascending) for deterministic ordering.
    """
    store._validate_collection(collection)
    db = store._require_connected()
    try:
        table = await db.open_table(collection)
    except ValueError:
        return []

    # --- Vector search ---
    vec_rows: list[dict[str, Any]] = await (
        table.vector_search(query_vector).limit(candidate_depth).to_list()
    )
    # Map chunk_id → (rank, raw_distance | None)
    vec_rank: dict[str, int] = {}
    vec_raw: dict[str, float | None] = {}
    for i, row in enumerate(vec_rows):
        cid = row["chunk_id"]
        vec_rank[cid] = i
        raw = row.get("_distance")
        vec_raw[cid] = float(raw) if raw is not None else None

    # --- FTS search (degrades gracefully when no index) ---
    fts_rows: list[dict[str, Any]] = []
    fts_rank: dict[str, int] = {}
    fts_raw: dict[str, float | None] = {}
    try:
        fts_q = await table.search(query_text, query_type="fts")
        fts_rows = await fts_q.limit(candidate_depth).to_list()
        for i, row in enumerate(fts_rows):
            cid = row["chunk_id"]
            fts_rank[cid] = i
            raw = row.get("_score")
            fts_raw[cid] = float(raw) if raw is not None else None
    except Exception as exc:
        exc_str = str(exc).lower()
        if "index" in exc_str or "fts" in exc_str:
            logger.warning(
                "FTS index not available for collection %r, trace will have fts_score=None",
                collection,
            )
        else:
            raise

    # --- Merge candidates ---
    all_rows: dict[str, dict[str, Any]] = {r["chunk_id"]: r for r in vec_rows}
    for r in fts_rows:
        all_rows.setdefault(r["chunk_id"], r)

    # --- Build ScoredSearchCandidate list ---
    candidates: list[ScoredSearchCandidate] = []
    for chunk_id, row in all_rows.items():
        in_vec = chunk_id in vec_rank
        in_fts = chunk_id in fts_rank

        v_rank = vec_rank[chunk_id] if in_vec else None
        v_score = vec_raw[chunk_id] if in_vec else None
        # score_kind follows score: if raw score field was absent, kind is also None
        v_kind: str | None = "distance" if (in_vec and v_score is not None) else None

        f_rank = fts_rank[chunk_id] if in_fts else None
        f_score = fts_raw[chunk_id] if in_fts else None
        f_kind: str | None = "bm25" if (in_fts and f_score is not None) else None

        rrf = 0.0
        if in_vec:
            rrf += _rrf_score(vec_rank[chunk_id])
        if in_fts:
            rrf += _rrf_score(fts_rank[chunk_id])

        candidates.append(
            ScoredSearchCandidate(
                doc_id=row["doc_id"],
                chunk_id=chunk_id,
                text=row["text"],
                source_path=row["source_path"],
                score_breakdown=SearchScoreBreakdown(
                    vector_rank=v_rank,
                    vector_score=v_score,
                    vector_score_kind=v_kind,
                    fts_rank=f_rank,
                    fts_score=f_score,
                    fts_score_kind=f_kind,
                    rrf_score=rrf,
                    reranker_score=None,
                ),
                collection=collection,
            )
        )

    # Stable sort: descending RRF, then ascending chunk_id for ties
    candidates.sort(key=lambda c: (-c.score_breakdown.rrf_score, c.chunk_id))
    return candidates

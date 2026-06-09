"""SearchStore — LanceDB-backed vector + FTS store."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from archon_search._diagnostics import ScoredSearchCandidate, SearchScoreBreakdown
from archon_search._types import ChunkRecord, CollectionInfo, DocumentInfo, SearchResult, normalize_iso_utc
from archon_search.constants import DEFAULT_NAMESPACE, INGEST_LOCK_TIMEOUT_S, PING_TIMEOUT_SECONDS, PING_TTL_SECONDS, _validate_namespace
from archon_search.observability import record_stage, _stage_recorder
from archon_search.store_filters import build_where, _compute_fetch, _sql_quote_str


from dataclasses import dataclass, field
from typing import Callable

# Fixed-width UTC timestamp regex: YYYY-MM-DDTHH:MM:SS.ffffffZ
_FIXED_WIDTH_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


@dataclass
class ReindexResult:
    """Outcome of ``SearchStore.reindex_metadata``."""

    processed: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    ts_normalized: int = 0


@dataclass
class ChunkIngestResult:
    """Outcome of ``SearchStore.ingest_chunks``."""

    chunks_ingested: int
    needs_recompute: bool


class StoreBusyError(Exception):
    """Raised when ``SearchStore.ingest_chunks`` cannot acquire the per-collection
    lock within ``INGEST_LOCK_TIMEOUT_S`` seconds (typically because
    ``reindex_metadata`` holds the lock).

    Callers (REST `/ingest`) translate this into HTTP 503 + ``Retry-After``.
    """

    def __init__(self, timeout_s: float) -> None:
        super().__init__(
            f"store busy: lock acquisition exceeded {timeout_s}s"
        )
        self.timeout_s = timeout_s

if TYPE_CHECKING:
    import lancedb
    import pyarrow as pa
    from archon_search.filters import SearchFilters

logger = logging.getLogger(__name__)

_CHUNK_ID_RE = re.compile(r"^[a-f0-9]{64}-\d{6}$")
_DOC_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_COLLECTION_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_ARCHON_PREFIX = "_archon_"
_META_TABLE = "_archon_collection_meta"

_RRF_K = 60  # RRF constant

# Mapping from ISO 639-1 codes (as produced by fasttext / stored in the language column)
# to the capitalized full English names accepted by LanceDB's FTS(language=...) parameter.
# LanceDB uses non-standard internal keys for a few languages (e.g. "du" for Dutch instead
# of ISO "nl", "gr" for Greek instead of ISO "el"). This map bridges those mismatches so
# that archon-search's stored ISO codes translate to valid LanceDB tokenizer names.
# Languages not present in this map fall back to the LanceDB default ("English" stemming).
_LANCEDB_TOKENIZER_MAP: dict[str, str] = {
    "ar": "Arabic",
    "da": "Danish",
    "nl": "Dutch",       # ISO 639-1; LanceDB internal key is "du"
    "en": "English",
    "fi": "Finnish",
    "fr": "French",
    "de": "German",
    "el": "Greek",       # ISO 639-1; LanceDB internal key is "gr"
    "hu": "Hungarian",
    "it": "Italian",
    "no": "Norwegian",
    "nb": "Norwegian",   # fasttext Bokmål → same LanceDB tokenizer
    "nn": "Norwegian",   # fasttext Nynorsk → same LanceDB tokenizer
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "es": "Spanish",
    "sv": "Swedish",
    "ta": "Tamil",
    "tr": "Turkish",
}

# Matches the fixed-width UTC format produced by normalize_iso_utc (_types.py).
# Must stay in sync with: dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
_FIXED_WIDTH_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

_META_MAX_FIELDS = 50
_META_MAX_KEY_LEN = 256
_META_MAX_VAL_LEN = 4096

# Set to False if spike gate (c) failed (optimize() does not remove deleted rows from FTS).
# This constant is set manually after the spike (Task 1.1) and committed as part of C6.
FTS_OPTIMIZE_REMOVES_DELETED: bool = True  # Plan A; change to False if Plan B applies


# ---------------------------------------------------------------------------
# SQL fragment helpers — defense-in-depth behind upstream identifier regexes.
# LanceDB 0.30.2 async delete()/count_rows() accept only str (no bind params),
# so we build safe fragments via _sql_quote_str from store_filters.
# ---------------------------------------------------------------------------


def _where_eq(col: str, value: str) -> str:
    """Return e.g. "name = 'O''Brien'". Callers compose with literal ' AND '."""
    return f"{col} = {_sql_quote_str(value)}"


def _where_in(col: str, values: Iterable[str]) -> str:
    """Return e.g. "chunk_id IN ('a', 'b')". Empty values yield "1=0" (always-false)."""
    items = ", ".join(_sql_quote_str(v) for v in values)
    return f"{col} IN ({items})" if items else "1=0"


def _rrf_score(rank: int) -> float:
    return 1.0 / (_RRF_K + rank + 1)


def _centroid_sum_valid(
    centroid_sum: list[float] | None,
    embedding_dim: int,
    stored_model: str,
    writer_model: str,
) -> bool:
    """Return True iff centroid_sum is safe to use for incremental updates.

    All of the following must hold:
    - centroid_sum is not None
    - embedding_dim > 0
    - len(centroid_sum) == embedding_dim
    - stored_model == writer_model
    - every element is finite (no NaN, no inf)
    """
    if centroid_sum is None:
        return False
    if embedding_dim <= 0:
        return False
    if len(centroid_sum) != embedding_dim:
        return False
    if stored_model != writer_model:
        return False
    return all(math.isfinite(v) for v in centroid_sum)


def _batch_vectors_valid(vectors: list[list[float]]) -> bool:
    """Return True iff every element in every vector is finite (no NaN, no inf).

    Empty vectors list returns True (vacuously all-finite).
    """
    return all(math.isfinite(v) for vec in vectors for v in vec)


def elementwise_sum(vectors: list[list[float]]) -> list[float]:
    """Return element-wise sum of *vectors*.

    Returns [] for an empty list.  Raises ValueError if vectors have mixed
    dimensions.
    """
    if not vectors:
        return []
    dim = len(vectors[0])
    if any(len(v) != dim for v in vectors):
        raise ValueError("mixed-dimension vectors")
    return [sum(v[i] for v in vectors) for i in range(dim)]


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


def _normalize_ingested_by(value: "str | None") -> str:
    """Normalize a stored ``ingested_by`` value at the read boundary.

    None / empty / legacy ``"archon-search-cli"`` → ``"cli"``.
    Canonical members pass through. Unknown values DEBUG-logged and coerced
    to ``"cli"`` defensively.
    """
    from archon_search.constants import INGESTED_BY_VALUES, LEGACY_INGESTED_BY  # noqa: PLC0415

    if not value or value == LEGACY_INGESTED_BY:
        return "cli"
    if value in INGESTED_BY_VALUES:
        return value
    logger.debug("unexpected stored ingested_by %r; coerced to 'cli'", value)
    return "cli"


class SearchStore:
    """Async LanceDB store for chunked document embeddings."""

    def __init__(self, db_path: str | Path, config: "SearchConfig | None" = None) -> None:
        self._db_path = Path(db_path).expanduser()
        self._db: Optional[lancedb.db.AsyncConnection] = None
        # Per-collection ingest/reindex lock map. Reads do not acquire any
        # lock — LanceDB handles read concurrency. Independent from
        # ``SearchCollectionSync._collection_locks`` (same key space, different
        # call paths).
        self._collection_locks: dict[str, asyncio.Lock] = {}
        self._ping_cache: tuple[float, bool] | None = None
        from archon_search.config import SearchConfig as _SearchConfig  # noqa: PLC0415
        self._config: _SearchConfig = config if config is not None else _SearchConfig()

    def _lock_for(self, collection: str) -> asyncio.Lock:
        """Lazily create and return the lock for *collection*."""
        lock = self._collection_locks.get(collection)
        if lock is None:
            lock = asyncio.Lock()
            self._collection_locks[collection] = lock
        return lock

    @property
    def supports_incremental_fts_delete(self) -> bool:
        """True when ``table.optimize()`` removes deleted rows from FTS (Plan A).

        Returns ``False`` when the spike found that deleted rows remain in the FTS
        index after optimize (Plan B), meaning ``rebuild_fts_index`` must be used
        on the delete path to avoid phantom hits.

        Controlled by the module-level ``FTS_OPTIMIZE_REMOVES_DELETED`` constant.
        """
        return FTS_OPTIMIZE_REMOVES_DELETED

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        import lancedb  # noqa: PLC0415

        self._ping_cache = None
        self._db_path.mkdir(parents=True, exist_ok=True)
        self._db = await lancedb.connect_async(str(self._db_path))

    async def disconnect(self) -> None:
        self._ping_cache = None
        db = self._db
        self._db = None
        if db is not None:
            db.close()

    async def ping(self) -> bool:
        """Return True if the store is reachable, False otherwise.

        Uses an instance-level TTL cache (PING_TTL_SECONDS) to avoid hammering
        the storage backend. CancelledError propagates unchanged — cancellation
        does not write a stale False into the cache.
        """
        if self._db is None:
            return False
        now = time.monotonic()
        if self._ping_cache is not None and now - self._ping_cache[0] < PING_TTL_SECONDS:
            return self._ping_cache[1]
        try:
            result = await asyncio.wait_for(self._db.list_tables(), timeout=PING_TIMEOUT_SECONDS)
            _ = result.tables
            self._ping_cache = (time.monotonic(), True)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            self._ping_cache = (time.monotonic(), False)
            return False

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
                # Extended metadata fields
                pa.field("file_type", pa.utf8()),
                pa.field("language", pa.utf8()),  # nullable via None → ""
                pa.field("metadata", pa.utf8()),   # JSON string
                pa.field("custom_score", pa.float32(), nullable=True),
                pa.field("ingested_by", pa.utf8()),
                pa.field("updated_at", pa.utf8()),
                pa.field("acl", pa.list_(pa.utf8()), nullable=True),
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
                pa.field("description_embedding_json", pa.utf8()),
                pa.field("doc_count", pa.int64()),
                pa.field("chunk_count", pa.int64()),
                pa.field("active_embedding_model", pa.utf8()),
                pa.field("pending_embedding_model", pa.utf8(), nullable=True),
                pa.field("needs_reindex", pa.bool_(), nullable=True),
                pa.field("reindex_job_id", pa.utf8(), nullable=True),
                pa.field("last_indexed", pa.utf8()),
                pa.field("last_described", pa.utf8()),
                pa.field("described_at_doc_count", pa.int64()),
                pa.field("namespace", pa.utf8()),
                pa.field("centroid_sum_json", pa.utf8(), nullable=True),
                pa.field("mutations_since_recompute", pa.int64(), nullable=True),
                pa.field("needs_recompute", pa.bool_(), nullable=True),
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
        # Avoid leaking lock entries for dropped collections.
        self._collection_locks.pop(name, None)

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
        all_meta = await self.get_all_collections_meta()
        meta_by_name = {m.name: m for m in all_meta}
        result: list[CollectionInfo] = []
        for name in names:
            try:
                table = await db.open_table(name)
                chunk_count = await table.count_rows()
                # count distinct doc_ids via Arrow column (avoids materializing dicts)
                arrow_table = await table.query().select(["doc_id"]).to_arrow()
                doc_count = len(arrow_table.column("doc_id").unique())
                meta = meta_by_name.get(name)
                namespace = meta.namespace if meta else DEFAULT_NAMESPACE
                result.append(CollectionInfo(name=name, doc_count=doc_count, chunk_count=chunk_count, namespace=namespace))
            except (RuntimeError, ValueError, OSError) as exc:
                logger.warning("Could not inspect collection %s: %s", name, exc)
        return result

    # ------------------------------------------------------------------
    # Collection metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_meta(row: "dict[str, Any]") -> "CollectionMeta":
        from archon_search.collection_meta import CollectionMeta  # noqa: PLC0415

        try:
            centroid = json.loads(row["centroid_json"]) if row["centroid_json"] else None
        except json.JSONDecodeError:
            logger.warning("Malformed centroid_json for collection %r — centroid set to None", row.get("name"))
            centroid = None
        raw_emb = row.get("description_embedding_json", "")
        description_embedding: list[float] | None = None
        if raw_emb:
            try:
                parsed = json.loads(raw_emb)
                if not isinstance(parsed, list) or any(
                    type(x) not in (int, float) or not math.isfinite(x) for x in parsed
                ):
                    logger.warning(
                        "Malformed description_embedding_json for collection %r — description_embedding set to None",
                        row.get("name"),
                    )
                else:
                    description_embedding = [float(x) for x in parsed]
            except json.JSONDecodeError:
                logger.warning(
                    "Malformed description_embedding_json for collection %r — description_embedding set to None",
                    row.get("name"),
                )
        last_indexed = datetime.fromisoformat(row["last_indexed"]) if row["last_indexed"] else None
        last_described = datetime.fromisoformat(row["last_described"]) if row["last_described"] else None
        raw_described_at: int = row["described_at_doc_count"]
        described_at = None if raw_described_at < 0 else raw_described_at
        raw_cs = row.get("centroid_sum_json", "")
        centroid_sum: list[float] | None = None
        if raw_cs:
            try:
                parsed = json.loads(raw_cs)
                if not isinstance(parsed, list) or any(
                    type(x) not in (int, float) or not math.isfinite(x) for x in parsed
                ):
                    logger.warning(
                        "Malformed centroid_sum_json for collection %r — centroid_sum set to None",
                        row.get("name"),
                    )
                else:
                    centroid_sum = [float(x) for x in parsed]
            except json.JSONDecodeError:
                logger.warning(
                    "Malformed centroid_sum_json for collection %r — centroid_sum set to None",
                    row.get("name"),
                )
                centroid_sum = None
        mutations_since_recompute = int(row.get("mutations_since_recompute") or 0)
        needs_recompute = bool(row.get("needs_recompute") or False)
        return CollectionMeta(
            name=row["name"],
            description=row["description"] if row["description"] else None,
            centroid=centroid,
            centroid_sum=centroid_sum,
            mutations_since_recompute=mutations_since_recompute,
            needs_recompute=needs_recompute,
            doc_count=row["doc_count"],
            chunk_count=row["chunk_count"],
            active_embedding_model=row["active_embedding_model"] if row.get("active_embedding_model") is not None else row.get("embedding_model", ""),
            pending_embedding_model=row.get("pending_embedding_model") or None,
            needs_reindex=bool(row.get("needs_reindex") or False),
            reindex_job_id=row.get("reindex_job_id") or None,
            last_indexed=last_indexed,
            last_described=last_described,
            described_at_doc_count=described_at,
            namespace=row.get("namespace") or DEFAULT_NAMESPACE,
            description_embedding=description_embedding,
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

    async def delete_collection_meta(self, name: str, namespace: str) -> None:
        self._validate_collection(name)
        _validate_namespace(namespace)
        db = self._require_connected()
        all_names: list[str] = (await db.list_tables()).tables
        if _META_TABLE not in all_names:
            return
        table = await db.open_table(_META_TABLE)
        # name validated by _COLLECTION_RE, namespace by _validate_namespace; _where_eq is defense-in-depth
        await table.delete(_where_eq("name", name) + " AND " + _where_eq("namespace", namespace))

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

    async def migrate_description_embedding(self) -> None:
        """Idempotent: adds description_embedding_json column to _archon_collection_meta if absent."""
        db = self._require_connected()
        all_names: list[str] = (await db.list_tables()).tables
        if _META_TABLE not in all_names:
            return
        table = await db.open_table(_META_TABLE)
        schema_names = (await table.schema()).names
        if "description_embedding_json" in schema_names:
            return
        try:
            await table.add_columns({"description_embedding_json": "''"})
            logger.info(
                "description_embedding migration: added description_embedding_json column to %s", _META_TABLE
            )
        except Exception as exc:
            if "already exists" in str(exc).lower():
                logger.warning(
                    "Concurrent migration: description_embedding_json column already added — %s", exc
                )
                return
            raise

    async def migrate_centroid_sum(self) -> None:
        """Idempotent: adds centroid_sum_json, mutations_since_recompute, needs_recompute columns to _archon_collection_meta if absent."""
        db = self._require_connected()
        all_names: list[str] = (await db.list_tables()).tables
        if _META_TABLE not in all_names:
            return
        table = await db.open_table(_META_TABLE)
        schema_names = (await table.schema()).names
        _B5_COLUMNS = [
            ("centroid_sum_json", "cast('' as string)"),
            ("mutations_since_recompute", "cast(0 as bigint)"),
            ("needs_recompute", "cast(false as boolean)"),
        ]
        if all(col in schema_names for col, _ in _B5_COLUMNS):
            return
        added = []
        for col, default in _B5_COLUMNS:
            if col in schema_names:
                continue
            try:
                await table.add_columns({col: default})
                added.append(col)
            except RuntimeError as exc:
                if "already exists" in str(exc).lower():
                    logger.warning("Concurrent migration: %s already added — %s", col, exc)
                else:
                    raise
        if added:
            logger.info("centroid_sum migration: added %s to %s", added, _META_TABLE)

    async def migrate_per_collection_model(self) -> None:
        """Idempotent 3-state crash-recovery migration for per-collection embedding model columns.

        State (a): embedding_model present, active_embedding_model absent
            → copy embedding_model values into active_embedding_model, add C1 extra columns.
        State (b): active_embedding_model present, but one or more C1 extra columns absent
            → add only the missing C1 extra columns (no rename).
        State (c): all four columns present → no-op.
        """
        _C1_EXTRA_COLUMNS = [
            ("pending_embedding_model", "cast('' as string)"),
            ("needs_reindex", "cast(false as boolean)"),
            ("reindex_job_id", "cast('' as string)"),
        ]
        db = self._require_connected()
        all_names: list[str] = (await db.list_tables()).tables
        if _META_TABLE not in all_names:
            return
        table = await db.open_table(_META_TABLE)
        schema_names = (await table.schema()).names

        has_active = "active_embedding_model" in schema_names
        has_old = "embedding_model" in schema_names
        c1_extra_present = all(col in schema_names for col, _ in _C1_EXTRA_COLUMNS)

        if has_active and c1_extra_present:
            # State (c): fully migrated
            return

        if not has_active:
            # State (a): copy embedding_model values into active_embedding_model
            # Step 1: add the column with empty default
            try:
                await table.add_columns({"active_embedding_model": "''"})
            except RuntimeError as exc:
                if "already exists" in str(exc).lower():
                    logger.warning("Concurrent migration: active_embedding_model already added — %s", exc)
                else:
                    raise

            # Step 2: read all rows and re-insert with the correct active_embedding_model value
            rows = await table.query().to_list()
            for row in rows:
                original_model = row.get("embedding_model", "")
                name = row["name"]
                namespace_val = row.get("namespace", "")
                # Delete the row (use same delete pattern as _do_write_meta_unlocked)
                ns_predicate = _where_eq("namespace", namespace_val)
                if namespace_val == DEFAULT_NAMESPACE:
                    ns_predicate = f"({ns_predicate} OR namespace IS NULL)"
                await table.delete(_where_eq("name", name) + " AND " + ns_predicate)
                # Re-insert with active_embedding_model set to original value
                new_row = dict(row)
                new_row["active_embedding_model"] = original_model
                await table.add([new_row])

            # Step 3: attempt to drop the old embedding_model column
            if has_old:
                try:
                    await table.drop_columns(["embedding_model"])
                except Exception as exc:
                    logger.warning("migrate_per_collection_model: could not drop embedding_model column — %s", exc)

            logger.info("per_collection_model migration (state a): renamed embedding_model → active_embedding_model in %s", _META_TABLE)

        # States (a) and (b): add missing C1 extra columns
        # Refresh schema after potential state-a changes
        table = await db.open_table(_META_TABLE)
        schema_names = (await table.schema()).names
        added = []
        for col, default in _C1_EXTRA_COLUMNS:
            if col in schema_names:
                continue
            try:
                await table.add_columns({col: default})
                added.append(col)
            except RuntimeError as exc:
                if "already exists" in str(exc).lower():
                    logger.warning("Concurrent migration: %s already added — %s", col, exc)
                else:
                    raise
        if added:
            logger.info("per_collection_model migration: added %s to %s", added, _META_TABLE)

    async def migrate_acl(self) -> None:
        """Idempotent: adds acl column (list<utf8>, nullable) to each chunk table that lacks it.

        Scans _archon_collection_meta to enumerate all known collections, then
        for each one opens the chunk table and adds the acl column if absent.
        Safe to call multiple times (concurrent-startup RuntimeError is caught and logged).
        """
        db = self._require_connected()
        all_names: list[str] = (await db.list_tables()).tables
        if _META_TABLE not in all_names:
            return
        meta_table = await db.open_table(_META_TABLE)
        rows = await meta_table.query().to_list()
        import pyarrow as pa  # noqa: PLC0415

        collection_names = [r["name"] for r in rows]
        acl_field = pa.field("acl", pa.list_(pa.utf8()), nullable=True)
        for name in collection_names:
            if name not in all_names:
                continue
            try:
                table = await db.open_table(name)
                schema_names = (await table.schema()).names
                if "acl" in schema_names:
                    continue
                await table.add_columns(acl_field)
                logger.info("acl migration: added acl column to chunk table %r", name)
            except RuntimeError as exc:
                if "already exists" in str(exc).lower():
                    logger.warning("Concurrent acl migration for %r: column already added — %s", name, exc)
                else:
                    logger.warning("acl migration: skipping %r due to RuntimeError — %s", name, exc)

    async def update_collection_meta(self, meta: "CollectionMeta") -> None:
        _validate_namespace(meta.namespace)
        self._validate_collection(meta.name)
        lock = self._lock_for(meta.name)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise StoreBusyError(timeout_s=INGEST_LOCK_TIMEOUT_S) from e
        try:
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
                existing = next((r for r in rows if r["name"] == meta.name), None)
                if existing is not None:
                    existing_ns = existing.get("namespace") or DEFAULT_NAMESPACE
                    if existing_ns != meta.namespace:
                        logger.error(
                            "update_collection_meta: name %r is registered under namespace %r, "
                            "refusing to overwrite with namespace %r",
                            meta.name, existing_ns, meta.namespace,
                        )
                        raise ValueError(
                            f"Collection {meta.name!r} belongs to namespace {existing_ns!r}; "
                            f"cannot reassign to {meta.namespace!r}"
                        )
                    # _where_eq is defense-in-depth; include namespace to prevent cross-namespace collision.
                    # Legacy rows may have NULL namespace (treated as DEFAULT_NAMESPACE), so match both.
                    ns_predicate = _where_eq("namespace", meta.namespace)
                    if meta.namespace == DEFAULT_NAMESPACE:
                        ns_predicate = f"({ns_predicate} OR namespace IS NULL)"
                    await table.delete(_where_eq("name", meta.name) + " AND " + ns_predicate)

            centroid_json = json.dumps(meta.centroid) if meta.centroid is not None else ""
            description_embedding_json = (
                json.dumps(meta.description_embedding) if meta.description_embedding is not None else ""
            )
            last_indexed_str = meta.last_indexed.isoformat() if meta.last_indexed else ""
            last_described_str = meta.last_described.isoformat() if meta.last_described else ""
            described_at = meta.described_at_doc_count if meta.described_at_doc_count is not None else -1

            await table.add(
                [
                    {
                        "name": meta.name,
                        "description": meta.description or "",
                        "centroid_json": centroid_json,
                        "description_embedding_json": description_embedding_json,
                        "doc_count": meta.doc_count,
                        "chunk_count": meta.chunk_count,
                        "active_embedding_model": meta.active_embedding_model,
                        "pending_embedding_model": meta.pending_embedding_model or "",
                        "needs_reindex": meta.needs_reindex,
                        "reindex_job_id": meta.reindex_job_id or "",
                        "last_indexed": last_indexed_str,
                        "last_described": last_described_str,
                        "described_at_doc_count": described_at,
                        "namespace": meta.namespace,
                        "centroid_sum_json": json.dumps(meta.centroid_sum) if meta.centroid_sum is not None else "",
                        "mutations_since_recompute": meta.mutations_since_recompute,
                        "needs_recompute": meta.needs_recompute,
                    }
                ]
            )
        finally:
            lock.release()

    async def update_description(
        self,
        collection: str,
        description: "str | None",
        last_described: "datetime | None",
        described_at_doc_count: "int | None",
        last_indexed: "datetime | None",
        namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        """Partial-write description/timestamp fields without touching centroid fields.

        On lock timeout, logs a warning and returns silently (no StoreBusyError).
        Description is cosmetic — stalling indefinitely behind reindex_metadata is
        worse than skipping one description update.
        """
        from archon_search.collection_meta import CollectionMeta  # noqa: PLC0415

        self._validate_collection(collection)
        db = self._require_connected()
        lock = self._lock_for(collection)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("Collection %r lock timeout in update_description, skipping", collection)
            return
        try:
            existing = await self._do_read_meta_unlocked(db, collection, namespace=namespace)
            if existing is None:
                return
            updated = CollectionMeta(
                name=existing.name,
                description=description,
                description_embedding=existing.description_embedding,
                centroid=existing.centroid,
                centroid_sum=existing.centroid_sum,
                doc_count=existing.doc_count,
                chunk_count=existing.chunk_count,
                active_embedding_model=existing.active_embedding_model,
                pending_embedding_model=existing.pending_embedding_model,
                needs_reindex=existing.needs_reindex,
                reindex_job_id=existing.reindex_job_id,
                last_indexed=last_indexed,
                last_described=last_described,
                described_at_doc_count=described_at_doc_count,
                namespace=existing.namespace,
                mutations_since_recompute=existing.mutations_since_recompute,
                needs_recompute=existing.needs_recompute,
            )
            await self._do_write_meta_unlocked(db, collection, updated)
        finally:
            lock.release()

    # ------------------------------------------------------------------
    # Private unlocked helpers (caller must hold _lock_for(collection))
    # ------------------------------------------------------------------

    async def _do_read_meta_unlocked(
        self, db: "lancedb.db.AsyncConnection", collection: str, namespace: str = DEFAULT_NAMESPACE
    ) -> "CollectionMeta | None":
        # Caller must hold _lock_for(collection)
        all_names: list[str] = (await db.list_tables()).tables
        if _META_TABLE not in all_names:
            return None
        table = await db.open_table(_META_TABLE)
        rows = await table.query().to_list()
        row = next(
            (
                r for r in rows
                if r["name"] == collection and (r.get("namespace") or DEFAULT_NAMESPACE) == namespace
            ),
            None,
        )
        return self._row_to_meta(row) if row is not None else None

    async def _do_write_meta_unlocked(
        self, db: "lancedb.db.AsyncConnection", collection: str, meta: "CollectionMeta"
    ) -> None:
        # Caller must hold _lock_for(collection)
        if collection != meta.name:
            raise ValueError(f"collection {collection!r} != meta.name {meta.name!r}")
        _validate_namespace(meta.namespace)
        self._validate_collection(meta.name)
        all_names: list[str] = (await db.list_tables()).tables
        if _META_TABLE not in all_names:
            table = await db.create_table(_META_TABLE, schema=self._meta_schema())
        else:
            table = await db.open_table(_META_TABLE)
            rows = await table.query().to_list()
            existing = next((r for r in rows if r["name"] == meta.name), None)
            if existing is not None:
                # Include namespace in delete predicate to avoid cross-namespace collision.
                # Legacy rows may have NULL namespace (treated as DEFAULT_NAMESPACE), so match both.
                ns_predicate = _where_eq("namespace", meta.namespace)
                if meta.namespace == DEFAULT_NAMESPACE:
                    ns_predicate = f"({ns_predicate} OR namespace IS NULL)"
                await table.delete(_where_eq("name", meta.name) + " AND " + ns_predicate)

        centroid_json = json.dumps(meta.centroid) if meta.centroid is not None else ""
        description_embedding_json = (
            json.dumps(meta.description_embedding) if meta.description_embedding is not None else ""
        )
        last_indexed_str = meta.last_indexed.isoformat() if meta.last_indexed else ""
        last_described_str = meta.last_described.isoformat() if meta.last_described else ""
        described_at = meta.described_at_doc_count if meta.described_at_doc_count is not None else -1

        await table.add(
            [
                {
                    "name": meta.name,
                    "description": meta.description or "",
                    "centroid_json": centroid_json,
                    "description_embedding_json": description_embedding_json,
                    "doc_count": meta.doc_count,
                    "chunk_count": meta.chunk_count,
                    "active_embedding_model": meta.active_embedding_model,
                    "pending_embedding_model": meta.pending_embedding_model or "",
                    "needs_reindex": meta.needs_reindex,
                    "reindex_job_id": meta.reindex_job_id or "",
                    "last_indexed": last_indexed_str,
                    "last_described": last_described_str,
                    "described_at_doc_count": described_at,
                    "namespace": meta.namespace,
                    "centroid_sum_json": json.dumps(meta.centroid_sum) if meta.centroid_sum is not None else "",
                    "mutations_since_recompute": meta.mutations_since_recompute,
                    "needs_recompute": meta.needs_recompute,
                }
            ]
        )

    async def _do_fetch_doc_vectors_unlocked(
        self, db: "lancedb.db.AsyncConnection", collection: str, doc_id: str
    ) -> list[list[float]]:
        # Caller must hold _lock_for(collection)
        if not _DOC_ID_RE.match(doc_id):
            raise ValueError(f"Invalid doc_id: {doc_id!r} — must be 64 hex chars")
        self._validate_collection(collection)
        try:
            table = await db.open_table(collection)
        except ValueError:
            return []
        # doc_id validated above by _DOC_ID_RE; _where_eq is defense-in-depth
        rows = await table.query().where(_where_eq("doc_id", doc_id)).select(["vector", "doc_id"]).to_list()
        # Guard against rows where the vector column is unexpectedly absent/null
        # (LanceDB schema enforces non-null, but defensive filter prevents bad list(None) calls)
        return [list(r["vector"]) for r in rows if r.get("vector") is not None]

    async def _do_update_meta_on_add(
        self,
        db: "lancedb.db.AsyncConnection",
        collection: str,
        batch_vectors: "list[list[float]]",
        distinct_doc_count: int,
        embedding_model: "str | None",
        embedding_dim: int,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> bool:
        # Caller must hold _lock_for(collection).
        # Returns True when the caller should invoke recompute_collection_meta.
        from archon_search.collection_meta import CollectionMeta  # noqa: PLC0415
        if embedding_model is None:
            return False

        existing = await self._do_read_meta_unlocked(db, collection, namespace=namespace)

        if existing is not None:
            if not _batch_vectors_valid(batch_vectors):
                logger.warning("Collection %r batch vectors contain NaN/inf; skipping centroid maintenance", collection)
                patched = CollectionMeta(
                    name=existing.name,
                    description=existing.description,
                    description_embedding=existing.description_embedding,
                    centroid=existing.centroid,
                    centroid_sum=existing.centroid_sum,
                    doc_count=existing.doc_count,
                    chunk_count=existing.chunk_count,
                    active_embedding_model=existing.active_embedding_model,
                    pending_embedding_model=existing.pending_embedding_model,
                    needs_reindex=existing.needs_reindex,
                    reindex_job_id=existing.reindex_job_id,
                    last_indexed=existing.last_indexed,
                    last_described=existing.last_described,
                    described_at_doc_count=existing.described_at_doc_count,
                    namespace=existing.namespace,
                    mutations_since_recompute=existing.mutations_since_recompute,
                    needs_recompute=True,
                )
                await self._do_write_meta_unlocked(db, collection, patched)
                return True

            if not _centroid_sum_valid(
                existing.centroid_sum, embedding_dim,
                stored_model=existing.active_embedding_model,
                writer_model=embedding_model,
            ):
                logger.warning("Collection %r centroid stale, recompute queued", collection)
                patched = CollectionMeta(
                    name=existing.name,
                    description=existing.description,
                    description_embedding=existing.description_embedding,
                    centroid=None,
                    centroid_sum=None,
                    doc_count=existing.doc_count,
                    chunk_count=existing.chunk_count,
                    active_embedding_model=existing.active_embedding_model,
                    pending_embedding_model=existing.pending_embedding_model,
                    needs_reindex=existing.needs_reindex,
                    reindex_job_id=existing.reindex_job_id,
                    last_indexed=existing.last_indexed,
                    last_described=existing.last_described,
                    described_at_doc_count=existing.described_at_doc_count,
                    namespace=existing.namespace,
                    mutations_since_recompute=existing.mutations_since_recompute,
                    needs_recompute=True,
                )
                await self._do_write_meta_unlocked(db, collection, patched)
                return True

            new_sum = [a + b for a, b in zip(existing.centroid_sum, elementwise_sum(batch_vectors))]
            new_chunk_count = existing.chunk_count + len(batch_vectors)
            new_doc_count = existing.doc_count + distinct_doc_count
            new_mutations = existing.mutations_since_recompute + len(batch_vectors)
            new_centroid = [v / new_chunk_count for v in new_sum]
            new_meta = CollectionMeta(
                name=existing.name,
                description=existing.description,
                description_embedding=existing.description_embedding,
                centroid=new_centroid,
                centroid_sum=new_sum,
                doc_count=new_doc_count,
                chunk_count=new_chunk_count,
                active_embedding_model=existing.active_embedding_model,
                pending_embedding_model=existing.pending_embedding_model,
                needs_reindex=existing.needs_reindex,
                reindex_job_id=existing.reindex_job_id,
                last_indexed=existing.last_indexed,
                last_described=existing.last_described,
                described_at_doc_count=existing.described_at_doc_count,
                namespace=existing.namespace,
                mutations_since_recompute=new_mutations,
                needs_recompute=existing.needs_recompute,
            )
            await self._do_write_meta_unlocked(db, collection, new_meta)
            return new_meta.mutations_since_recompute >= self._config.centroid_recompute_threshold or new_meta.needs_recompute
        else:
            # Brand-new collection: batch IS the full collection; no recompute needed.
            if not _batch_vectors_valid(batch_vectors):
                logger.warning("Collection %r batch vectors contain NaN/inf; skipping centroid maintenance", collection)
                new_meta = CollectionMeta(
                    name=collection, active_embedding_model=embedding_model,
                    namespace=namespace, needs_recompute=True,
                )
                await self._do_write_meta_unlocked(db, collection, new_meta)
                return True
            batch_sum = elementwise_sum(batch_vectors)
            n = len(batch_vectors)
            new_meta = CollectionMeta(
                name=collection,
                centroid=[v / n for v in batch_sum],
                centroid_sum=batch_sum,
                doc_count=distinct_doc_count,
                chunk_count=n,
                active_embedding_model=embedding_model,
                namespace=namespace,
                mutations_since_recompute=n,
            )
            await self._do_write_meta_unlocked(db, collection, new_meta)
            return False

    async def _do_subtract_meta_on_delete(
        self,
        db: "lancedb.db.AsyncConnection",
        collection: str,
        del_vectors: "list[list[float]]",
        namespace: str = DEFAULT_NAMESPACE,
    ) -> None:
        # Caller must hold _lock_for(collection).
        from archon_search.collection_meta import CollectionMeta  # noqa: PLC0415
        from datetime import timezone  # noqa: PLC0415

        if not del_vectors:
            return

        existing = await self._do_read_meta_unlocked(db, collection, namespace=namespace)
        if existing is None:
            logger.warning("Collection %r has no meta row; delete cannot update centroid", collection)
            return

        now = datetime.now(timezone.utc)
        new_mutations = existing.mutations_since_recompute + len(del_vectors)

        embedding_dim = len(existing.centroid_sum) if existing.centroid_sum is not None else 0
        if not _centroid_sum_valid(
            existing.centroid_sum, embedding_dim,
            stored_model=existing.active_embedding_model or "",
            writer_model=existing.active_embedding_model or "",
        ):
            logger.warning("Collection %r centroid stale, recompute queued", collection)
            patched = CollectionMeta(
                name=existing.name,
                description=existing.description,
                description_embedding=existing.description_embedding,
                centroid=None,
                centroid_sum=None,
                doc_count=existing.doc_count,
                chunk_count=existing.chunk_count,
                active_embedding_model=existing.active_embedding_model,
                pending_embedding_model=existing.pending_embedding_model,
                needs_reindex=existing.needs_reindex,
                reindex_job_id=existing.reindex_job_id,
                last_indexed=now,
                last_described=existing.last_described,
                described_at_doc_count=existing.described_at_doc_count,
                namespace=existing.namespace,
                mutations_since_recompute=new_mutations,
                needs_recompute=True,
            )
            await self._do_write_meta_unlocked(db, collection, patched)
            return

        del_sum = elementwise_sum(del_vectors)
        new_sum = [a - b for a, b in zip(existing.centroid_sum, del_sum)]
        new_chunk_count = max(0, existing.chunk_count - len(del_vectors))
        new_doc_count = max(0, existing.doc_count - 1)

        if new_chunk_count == 0:
            new_centroid = None
            new_sum = None
            new_doc_count = 0
        else:
            new_centroid = [v / new_chunk_count for v in new_sum]

        new_meta = CollectionMeta(
            name=existing.name,
            description=existing.description,
            description_embedding=existing.description_embedding,
            centroid=new_centroid,
            centroid_sum=new_sum,
            doc_count=new_doc_count,
            chunk_count=new_chunk_count,
            active_embedding_model=existing.active_embedding_model,
            pending_embedding_model=existing.pending_embedding_model,
            needs_reindex=existing.needs_reindex,
            reindex_job_id=existing.reindex_job_id,
            last_indexed=now,
            last_described=existing.last_described,
            described_at_doc_count=existing.described_at_doc_count,
            namespace=existing.namespace,
            mutations_since_recompute=new_mutations,
            needs_recompute=existing.needs_recompute,
        )
        await self._do_write_meta_unlocked(db, collection, new_meta)

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    async def ingest_chunks(
        self,
        collection: str,
        chunks: list[ChunkRecord],
        *,
        _locked_by_caller: bool = False,
        embedding_model: str | None = None,
        namespace: str = DEFAULT_NAMESPACE,
    ) -> ChunkIngestResult:
        self._validate_collection(collection)
        db = self._require_connected()
        for chunk in chunks:
            if not _CHUNK_ID_RE.match(chunk.chunk_id):
                raise ValueError(f"malformed chunk_id: {chunk.chunk_id!r}")
            validate_metadata(chunk.metadata)

        if not chunks:
            return ChunkIngestResult(chunks_ingested=0, needs_recompute=False)

        lock = None if _locked_by_caller else self._lock_for(collection)
        if lock is not None:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)
            except asyncio.TimeoutError as e:
                raise StoreBusyError(timeout_s=INGEST_LOCK_TIMEOUT_S) from e
        try:
            chunks_ingested = await self._do_ingest(db, collection, chunks)
            needs_recompute = False

            if self._config.centroid_incremental_enabled:
                batch_vectors = [list(c.vector) for c in chunks]
                if batch_vectors:
                    distinct_doc_count = len({c.doc_id for c in chunks})
                    needs_recompute = await self._do_update_meta_on_add(
                        db, collection, batch_vectors, distinct_doc_count,
                        embedding_model=embedding_model,
                        embedding_dim=len(batch_vectors[0]),
                    )

            return ChunkIngestResult(chunks_ingested=chunks_ingested, needs_recompute=needs_recompute)
        finally:
            if lock is not None:
                lock.release()

    async def _do_ingest(self, db, collection: str, chunks: list[ChunkRecord]) -> int:
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
                "ingested_by": c.ingested_by,
                "updated_at": c.updated_at or c.indexed_at,
                "acl": c.acl,
            }
            for c in chunks
        ]
        await table.add(rows)
        return len(chunks)

    # ------------------------------------------------------------------
    # FTS index
    # ------------------------------------------------------------------

    async def rebuild_fts_index(self, collection: str, *, language: str = "") -> None:
        """Rebuild the FTS index for *collection*.

        Parameters
        ----------
        collection:
            Name of the collection to rebuild the index for.
        language:
            ISO 639-1 code (e.g. ``"fr"``, ``"de"``) of the dominant language in the
            collection.  When a matching entry is found in ``_LANCEDB_TOKENIZER_MAP``,
            the corresponding LanceDB tokenizer name is passed to ``FTS(language=...)``,
            enabling language-appropriate stemming and stop-word removal.  Unknown or
            empty codes fall back to the LanceDB default (``"English"``).
        """
        self._validate_collection(collection)
        db = self._require_connected()
        from lancedb.index import FTS  # noqa: PLC0415

        tokenizer_name = _LANCEDB_TOKENIZER_MAP.get(language, "English")
        if language and language not in _LANCEDB_TOKENIZER_MAP:
            logger.warning(
                "rebuild_fts_index: unrecognized language code %r; falling back to English tokenizer",
                language,
            )
        table = await db.open_table(collection)
        await table.create_index("text", config=FTS(language=tokenizer_name), replace=True)

    async def optimize_fts(self, collection: str) -> None:
        """Incrementally update the FTS index for *collection* via ``table.optimize()``.

        Incorporates rows added or deleted since the last index creation or
        optimize call without rebuilding the full index.  The tokenizer language
        is embedded in the index at creation time and is not reconfigured here.

        **Lock scope**: this method does NOT acquire the per-collection lock.
        Callers are responsible for concurrency.  In ``delete_document`` the
        call is placed AFTER ``lock.release()`` to avoid holding the lock during
        a potentially long optimize operation (matching the existing
        ``rebuild_fts_index`` convention).
        """
        self._validate_collection(collection)
        db = self._require_connected()
        logger.debug("optimize_fts: collection=%s", collection)
        table = await db.open_table(collection)
        await table.optimize()

    # ------------------------------------------------------------------
    # Reindex (metadata backfill for pre-A1 collections)
    # ------------------------------------------------------------------

    async def reindex_metadata(
        self,
        collection: str,
        *,
        dry_run: bool = False,
        normalize_timestamps: bool = True,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> ReindexResult:
        """Refresh metadata fields on every row in *collection*.

        - ``file_type`` re-derived from ``Path(source_path).suffix.lower().lstrip('.')``.
        - ``updated_at`` set to the source file's mtime in UTC ISO 8601 if the
          file still exists; otherwise preserved (with a warning).
        - Legacy ``ingested_by == "archon-search-cli"`` is rewritten to
          ``"reindex"``. Canonical members are preserved.

        Reads RAW LanceDB rows — does NOT route through ``_normalize_ingested_by``
        (Task 6.2 requirement), so legacy values are visible and rewriteable.

        Holds the per-collection lock for the full duration (no timeout — the
        reindex is the holder).
        """
        from archon_search.constants import LEGACY_INGESTED_BY  # noqa: PLC0415

        self._validate_collection(collection)
        db = self._require_connected()
        result = ReindexResult()

        lock = self._lock_for(collection)
        await lock.acquire()
        try:
            table = await db.open_table(collection)
            rows = await table.query().to_list()
            total = len(rows)
            updates: list[tuple[str, dict[str, str]]] = []

            for row in rows:
                result.processed += 1
                source_path = row.get("source_path") or ""
                stored_file_type = row.get("file_type") or ""
                stored_updated_at = row.get("updated_at") or ""
                stored_ingested_by = row.get("ingested_by") or ""

                new_file_type = (
                    Path(source_path).suffix.lower().lstrip(".") if source_path else ""
                )

                new_updated_at = stored_updated_at
                if source_path:
                    try:
                        mtime = Path(source_path).stat().st_mtime
                        mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
                        new_updated_at = (
                            normalize_iso_utc(mtime_dt)
                            if normalize_timestamps
                            else mtime_dt.isoformat()
                        )
                    except OSError:
                        result.warnings.append(f"missing-source: {source_path}")

                if stored_ingested_by == LEGACY_INGESTED_BY:
                    new_ingested_by = "reindex"
                else:
                    new_ingested_by = stored_ingested_by

                # Timestamp normalization pass
                stored_indexed_at = row.get("indexed_at") or ""
                new_indexed_at = stored_indexed_at
                ts_row_changed = False
                if normalize_timestamps:
                    if stored_indexed_at and not _FIXED_WIDTH_TS_RE.match(stored_indexed_at):
                        try:
                            new_indexed_at = normalize_iso_utc(stored_indexed_at)
                            ts_row_changed = True
                        except Exception:  # noqa: BLE001
                            result.warnings.append(f"bad-indexed_at: {stored_indexed_at}")
                    if new_updated_at and not _FIXED_WIDTH_TS_RE.match(new_updated_at):
                        try:
                            new_updated_at = normalize_iso_utc(new_updated_at)
                            ts_row_changed = True
                        except Exception:  # noqa: BLE001
                            result.warnings.append(f"bad-updated_at: {new_updated_at}")
                if ts_row_changed:
                    result.ts_normalized += 1

                differs = (
                    new_file_type != stored_file_type
                    or new_updated_at != stored_updated_at
                    or new_ingested_by != stored_ingested_by
                    or new_indexed_at != stored_indexed_at
                )
                if not differs:
                    continue

                updates.append((
                    row["chunk_id"],
                    {
                        "file_type": new_file_type,
                        "updated_at": new_updated_at,
                        "ingested_by": new_ingested_by,
                        "indexed_at": new_indexed_at,
                    },
                ))

                if progress_cb is not None and result.processed % 200 == 0:
                    progress_cb(result.processed, total)

            if not dry_run:
                for chunk_id, vals in updates:
                    await table.update(
                        where=f"chunk_id = '{chunk_id}'",
                        updates=vals,
                    )
                    result.updated += 1
                if updates:
                    try:
                        await self.rebuild_fts_index(collection)
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "rebuild_fts_index after reindex failed", exc_info=True
                        )

            if progress_cb is not None:
                progress_cb(result.processed, total)
        finally:
            lock.release()
        return result

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def hybrid_search(
        self,
        collection: str,
        query_vector: list[float],
        query_text: str,
        top_k: int,
        filters: "SearchFilters | None" = None,
    ) -> list[SearchResult]:
        self._validate_collection(collection)
        db = self._require_connected()
        try:
            table = await db.open_table(collection)
        except ValueError:
            return []

        has_glob = bool(filters and filters.source_path_glob)
        fetch = _compute_fetch(top_k, has_glob=has_glob)
        pred = build_where(filters) if filters else ""

        # Vector search
        with record_stage("vector"):
            vec_q = table.vector_search(query_vector)
            if pred:
                vec_q = vec_q.where(pred)
            vec_rows = await vec_q.limit(fetch).to_list()
            vec_rank: dict[str, int] = {r["chunk_id"]: i for i, r in enumerate(vec_rows)}

        # FTS search (may fail if no index); record only on success
        fts_rows: list[dict[str, Any]] = []
        fts_rank: dict[str, int] = {}
        _fts_t0 = time.perf_counter()
        try:
            fts_q = await table.search(query_text, query_type="fts")
            if pred:
                fts_q = fts_q.where(pred)
            fts_rows = await fts_q.limit(fetch).to_list()
            fts_rank = {r["chunk_id"]: i for i, r in enumerate(fts_rows)}
            _fts_recorder = _stage_recorder.get()
            if _fts_recorder is not None:
                _fts_recorder.record("fts", (time.perf_counter() - _fts_t0) * 1000.0)
        except Exception as exc:
            exc_str = str(exc).lower()
            if "index" in exc_str or "fts" in exc_str:
                logger.warning("FTS index not available for collection %r, using vector-only results", collection)
            else:
                raise

        # Build combined row lookup and RRF scoring
        with record_stage("fuse"):
            all_rows: dict[str, dict[str, Any]] = {r["chunk_id"]: r for r in vec_rows}
            for r in fts_rows:
                all_rows.setdefault(r["chunk_id"], r)

            scored: list[tuple[float, dict[str, Any]]] = []
            for chunk_id, row in all_rows.items():
                score = 0.0
                if chunk_id in vec_rank:
                    score += _rrf_score(vec_rank[chunk_id])
                if chunk_id in fts_rank:
                    score += _rrf_score(fts_rank[chunk_id])
                scored.append((score, row))

            # Tie-break on chunk_id (ascending) to match the trace path's
            # (-rrf_score, chunk_id) order; deterministic on exact score ties.
            scored.sort(key=lambda x: (-x[0], x[1]["chunk_id"]))

        # fnmatch has no path semantics: * matches / and ** is identical to *;
        # source_path_glob matches the full source_path; combine with source_path_prefix for
        # prefix-anchored narrowing before this post-filter runs.
        if filters and filters.source_path_glob:
            glob_pattern = filters.source_path_glob
            scored = [
                (score, row) for score, row in scored
                if fnmatch.fnmatchcase(row["source_path"], glob_pattern)
            ]
            if len(scored) < top_k:
                logger.warning(
                    "glob post-filter shrank pool below top_k: %d/%d",
                    len(scored), top_k,
                )

        results = []
        for score, row in scored[:top_k]:
            raw_acl = row.get("acl")
            row_acl: list[str] | None = list(raw_acl) if isinstance(raw_acl, list) else None
            indexed_at = row.get("indexed_at") or ""
            results.append(
                SearchResult(
                    doc_id=row["doc_id"],
                    chunk_id=row["chunk_id"],
                    text=row["text"],
                    score=score,
                    source_path=row["source_path"],
                    file_type=row.get("file_type") or "",
                    language=row.get("language") or "",
                    indexed_at=indexed_at,
                    updated_at=row.get("updated_at") or indexed_at,
                    ingested_by=_normalize_ingested_by(row.get("ingested_by")),  # type: ignore[arg-type]
                    metadata=parse_metadata(row.get("metadata") or "{}"),
                    acl=row_acl,
                    collection=collection,
                )
            )

        if filters and (filters.indexed_after or filters.indexed_before):
            legacy_count = sum(
                1 for r in results
                if not _FIXED_WIDTH_PATTERN.match(r.indexed_at or "")
            )
            if legacy_count:
                logger.warning(
                    "date filter applied to %d legacy-format rows in collection %s; "
                    "re-ingest these documents to normalize indexed_at format "
                    "and avoid silent boundary errors",
                    legacy_count, collection,
                )

        return results

    async def hybrid_search_with_trace(
        self,
        collection: str,
        query_vector: list[float],
        query_text: str,
        candidate_depth: int,
        filters: "SearchFilters | None" = None,
    ) -> list[ScoredSearchCandidate]:
        """Thin instance-method delegate to module-level _hybrid_search_with_trace.

        Used both for eval/debug observability and as the production search backend
        for the RAG Fusion path (Task 2.2, C5).  The *filters* parameter applies
        the same field-predicate logic as :meth:`hybrid_search`.
        """
        return await _hybrid_search_with_trace(
            self, collection, query_vector, query_text, candidate_depth, filters=filters
        )

    async def has_vector_index(self, collection: str) -> bool:
        """Return True if *collection* has a vector column in its LanceDB schema.

        Forward-compatibility guard: all current collections always have a vector
        column (created via ``ensure_collection``), but FTS-only collections
        created in future may not.  Returns False when the collection does not
        exist.  O(1) — reads Arrow schema metadata only.
        """
        self._validate_collection(collection)
        db = self._require_connected()
        try:
            table = await db.open_table(collection)
        except ValueError:
            return False
        schema = await table.schema()
        try:
            schema.field("vector")
            return True
        except KeyError:
            return False

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------


    async def delete_document(
        self,
        collection: str,
        doc_id: str,
        namespace: str = DEFAULT_NAMESPACE,
        *,
        skip_fts_optimize: bool = False,
    ) -> int:
        """Delete all chunks for *doc_id* from *collection*.

        Parameters
        ----------
        collection:
            Name of the collection to delete from.
        doc_id:
            64-hex-char document identifier.
        namespace:
            Namespace used for centroid bookkeeping (ignored when centroid
            incremental updates are disabled).
        skip_fts_optimize:
            When ``True``, suppress the post-delete FTS maintenance call.
            Pass ``True`` from ingest paths that will call ``optimize_fts``
            (or ``rebuild_fts_index`` under Plan B) separately at batch end,
            to avoid redundant per-file FTS operations.  Default ``False``
            maintains FTS coherence on standalone deletes.
        """
        self._validate_collection(collection)
        db = self._require_connected()
        if not _DOC_ID_RE.match(doc_id):
            raise ValueError(f"Invalid doc_id: {doc_id!r} — must be 64 hex chars")
        lock = self._lock_for(collection)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=INGEST_LOCK_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise StoreBusyError(timeout_s=INGEST_LOCK_TIMEOUT_S) from e
        count: int = 0
        try:
            try:
                table = await db.open_table(collection)
            except ValueError:
                return 0
            del_vectors = await self._do_fetch_doc_vectors_unlocked(db, collection, doc_id)
            # doc_id validated upstream by _DOC_ID_RE; _where_eq is defense-in-depth
            count = await table.count_rows(_where_eq("doc_id", doc_id))
            if count == 0:
                return 0
            # doc_id validated upstream by _DOC_ID_RE; _where_eq is defense-in-depth
            await table.delete(_where_eq("doc_id", doc_id))
            if self._config.centroid_incremental_enabled:
                await self._do_subtract_meta_on_delete(db, collection, del_vectors, namespace=namespace)
        finally:
            lock.release()
        # FTS maintenance is performed AFTER lock release to avoid holding the lock
        # during a potentially long optimize/rebuild operation.
        if count > 0 and not skip_fts_optimize:
            if self.supports_incremental_fts_delete:
                await self.optimize_fts(collection)
            else:
                dominant_lang = await self.get_dominant_language(collection)
                await self.rebuild_fts_index(collection, language=dominant_lang)
        return count

    async def delete_by_source_path(
        self,
        collection: str,
        source_path: str,
        namespace: str = DEFAULT_NAMESPACE,
        *,
        skip_fts_optimize: bool = False,
    ) -> int:
        """Delete all chunks for a source file by computing its doc_id.

        ``source_path`` must be an absolute, resolved path — the same form
        produced by ``str(path.resolve())`` at ingest time.  Relative paths
        will resolve against the current working directory at call time and
        may not match the stored doc_id.

        ``skip_fts_optimize`` is forwarded to ``delete_document``; pass
        ``True`` from batch callers (e.g. sync.py delete loop) to suppress
        per-file FTS maintenance in favour of a single batch-end call.
        """
        doc_id = hashlib.sha256(str(Path(source_path).resolve()).encode()).hexdigest()
        return await self.delete_document(
            collection, doc_id, namespace=namespace, skip_fts_optimize=skip_fts_optimize
        )

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

        # chunk_ids are constructed from doc_id (validated by _DOC_ID_RE); _where_in is defense-in-depth
        rows = (
            await table.query()
            .where(_where_in("chunk_id", target_ids))
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
                language=r.get("language") or "",
                metadata=parse_metadata(r.get("metadata") or "{}"),
                custom_score=r.get("custom_score"),
                ingested_by=_normalize_ingested_by(r.get("ingested_by")),
                updated_at=r.get("updated_at") or r["indexed_at"],
                acl=list(r.get("acl")) if isinstance(r.get("acl"), list) else None,
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

    async def get_stored_vector_dimension(
        self, collection: str, namespace: str = DEFAULT_NAMESPACE
    ) -> int | None:
        """Return the fixed-size list dimension of the ``vector`` column in *collection*.

        Returns ``None`` if the table does not exist.  This is O(1) — reads Arrow
        schema metadata only, no row scan.

        The *namespace* parameter is accepted for API symmetry but does not affect
        table-name resolution (the collection name is the table name).
        """
        self._validate_collection(collection)
        db = self._require_connected()
        try:
            table = await db.open_table(collection)
        except ValueError:
            return None
        schema = await table.schema()
        try:
            vector_field = schema.field("vector")
        except KeyError:
            return None
        return vector_field.type.list_size

    async def count_chunks(self, collection: str, namespace: str) -> int:
        """Return the total number of chunks (rows) in *collection*.

        Returns ``0`` if the collection does not exist.  This is O(1) —
        uses ``table.count_rows()`` which reads metadata only.

        The *namespace* parameter is accepted for API symmetry but does not
        affect table-name resolution (the collection name is the table name).
        """
        self._validate_collection(collection)
        db = self._require_connected()
        try:
            table = await db.open_table(collection)
        except ValueError:
            return 0
        return await table.count_rows()

    async def count_untagged_language_chunks(self, collection: str) -> int:
        """Return the number of chunks in *collection* where ``language = ''``.

        These are legacy chunks that were ingested before C2 (language detection)
        was active.  Returns ``0`` if the collection does not exist.

        Uses ``_sql_quote_str`` (not an f-string) for the SQL predicate — consistent
        with the CI guard in ``tests/test_no_fstring_sql.py``.
        """
        self._validate_collection(collection)
        db = self._require_connected()
        try:
            table = await db.open_table(collection)
        except ValueError:
            return 0
        predicate = "language = " + _sql_quote_str("")
        return await table.count_rows(predicate)

    async def get_dominant_language(self, collection: str) -> str:
        """Return the most common non-empty language tag across all chunks in *collection*.

        Uses a Python-side ``Counter`` over a full column scan because LanceDB 0.30.2
        does not support ``GROUP BY`` SQL.  Empty-string tags (``""``) are excluded from
        the tally — they represent untagged (legacy) chunks.

        Returns ``""`` if the collection does not exist, is empty, or all chunks have
        ``language=""``.
        """
        self._validate_collection(collection)
        db = self._require_connected()
        try:
            table = await db.open_table(collection)
        except ValueError:
            return ""
        from collections import Counter  # noqa: PLC0415

        arrow_table = await table.query().select(["language"]).to_arrow()
        lang_col = arrow_table.column("language")
        # Filter out "" (untagged/legacy) and "unknown" (below-threshold detection) —
        # neither is a valid ISO language code for FTS tokenizer selection.
        detected = [code for code in lang_col.to_pylist() if code and code != "unknown"]
        if not detected:
            return ""
        counts: Counter[str] = Counter(detected)
        return counts.most_common(1)[0][0]

    async def get_acl_stats(self, collection: str) -> tuple[int, int]:
        """Return (acl_protected_count, acl_open_count) for all chunks in a collection.

        acl_protected_count — rows where acl IS NOT NULL.
        acl_open_count      — rows where acl IS NULL.

        Returns (0, 0) if the collection does not exist.
        No namespace filter — aggregate-only operator.
        """
        self._validate_collection(collection)
        db = self._require_connected()
        try:
            table = await db.open_table(collection)
        except ValueError:
            return (0, 0)
        import pyarrow.compute as pc  # noqa: PLC0415

        arrow_table = await table.query().select(["acl"]).to_arrow()
        acl_col = arrow_table.column("acl")
        acl_open_count = int(pc.sum(pc.is_null(acl_col)).as_py() or 0)
        acl_protected_count = len(acl_col) - acl_open_count
        return (acl_protected_count, acl_open_count)


async def migrate_description_embedding(store: SearchStore) -> None:
    """Module-level delegate for :meth:`SearchStore.migrate_description_embedding`."""
    await store.migrate_description_embedding()


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
    filters: "SearchFilters | None" = None,
) -> list[ScoredSearchCandidate]:
    """Internal trace helper — returns full score provenance per candidate.

    Used both for eval/debug observability and as the production search backend
    for the RAG Fusion path (Task 2.2, C5).  The optional *filters* parameter
    applies the same field-predicate logic as :meth:`SearchStore.hybrid_search`.

    Args:
        store: A connected :class:`SearchStore` instance.
        collection: Collection name (validated by the store).
        query_vector: Embedding vector for vector search.
        query_text: Text query for FTS search.
        candidate_depth: Maximum number of raw candidates to fetch from each
            search leg (analogous to ``fetch`` in :meth:`SearchStore.hybrid_search`).
        filters: Optional field filters applied to both the vector and FTS legs.

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

    pred = build_where(filters) if filters else ""

    # --- Vector search ---
    with record_stage("vector"):
        vec_q = table.vector_search(query_vector)
        if pred:
            vec_q = vec_q.where(pred)
        vec_rows: list[dict[str, Any]] = await vec_q.limit(candidate_depth).to_list()
        # Map chunk_id → (rank, raw_distance | None)
        vec_rank: dict[str, int] = {}
        vec_raw: dict[str, float | None] = {}
        for i, row in enumerate(vec_rows):
            cid = row["chunk_id"]
            vec_rank[cid] = i
            raw = row.get("_distance")
            vec_raw[cid] = float(raw) if raw is not None else None

    # --- FTS search (degrades gracefully when no index); record only on success ---
    fts_rows: list[dict[str, Any]] = []
    fts_rank: dict[str, int] = {}
    fts_raw: dict[str, float | None] = {}
    _fts_t0 = time.perf_counter()
    try:
        fts_q = await table.search(query_text, query_type="fts")
        if pred:
            fts_q = fts_q.where(pred)
        fts_rows = await fts_q.limit(candidate_depth).to_list()
        for i, row in enumerate(fts_rows):
            cid = row["chunk_id"]
            fts_rank[cid] = i
            raw = row.get("_score")
            fts_raw[cid] = float(raw) if raw is not None else None
        _fts_recorder = _stage_recorder.get()
        if _fts_recorder is not None:
            _fts_recorder.record("fts", (time.perf_counter() - _fts_t0) * 1000.0)
    except Exception as exc:
        exc_str = str(exc).lower()
        if "index" in exc_str or "fts" in exc_str:
            logger.warning(
                "FTS index not available for collection %r, trace will have fts_score=None",
                collection,
            )
        else:
            raise

    # --- Merge candidates and build ScoredSearchCandidate list ---
    with record_stage("fuse"):
        all_rows: dict[str, dict[str, Any]] = {r["chunk_id"]: r for r in vec_rows}
        for r in fts_rows:
            all_rows.setdefault(r["chunk_id"], r)

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

            raw_acl = row.get("acl")
            row_acl: list[str] | None = list(raw_acl) if isinstance(raw_acl, list) else None
            indexed_at = row.get("indexed_at") or ""

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
                    acl=row_acl,
                    file_type=row.get("file_type") or "",
                    indexed_at=indexed_at,
                    updated_at=row.get("updated_at") or indexed_at,
                    ingested_by=_normalize_ingested_by(row.get("ingested_by")),  # type: ignore[arg-type]
                    language=row.get("language") or "",
                    metadata=parse_metadata(row.get("metadata") or "{}"),
                )
            )

        # Stable sort: descending RRF, then ascending chunk_id for ties
        candidates.sort(key=lambda c: (-c.score_breakdown.rrf_score, c.chunk_id))
    return candidates

/**
 * E2a — TTL and Scoping: internal chunk schema seam.
 * Core-construct TypeSpec (no HTTP decorators). Internal seam between Use Cases (pipeline.py)
 * and Frameworks & Drivers (store.py / _types.py / collection_meta.py).
 *
 * This is a PARTIAL view — E2a delta fields only.
 * Full ChunkRecord: archon_search/_types.py (doc_id, chunk_id, text, vector, source_path,
 *   indexed_at, file_type, language, metadata, custom_score, ingested_by, updated_at, acl,
 *   start_offset, end_offset) — plus these two new fields.
 * Full CollectionMeta: archon_search/collection_meta.py (name, namespace, active/pending
 *   embedding_model, doc_count, chunk_count, schema_version, etc.) — plus one new field.
 * Full SearchFilters: archon_search/store_filters.py — scope_filter is NOT added to SearchFilters;
 *   it is passed as a separate parameter to hybrid_search_with_trace.
 */

namespace ArchonSearch.E2a.Internal;

/** E2a delta fields added to ChunkRecord dataclass (_types.py). */
model ChunkRecordE2aFields {
  /**
   * ISO 8601 UTC expiry timestamp, e.g. "2025-07-01T12:00:00+00:00".
   * Null = chunk never expires.
   * Computed at ingest time: expires_at = now_utc + ttl_seconds.
   * Precedence: request-level chunk_ttl_seconds > CollectionMeta.default_ttl_seconds > null.
   * (Per-chunk metadata dict ttl_seconds is deferred to v2 — no pipeline mechanism exists yet.)
   * Stored as pa.utf8() | null in LanceDB chunk table (consistent with indexed_at, updated_at).
   */
  expires_at: string | null;

  /**
   * Scope tags assigned to this chunk at ingest time.
   * List of 0–100 UTF-8 strings, each 1–255 chars.
   * Empty list or null = unscoped (no scope restriction applies to this chunk).
   * Stored as pa.list_(pa.utf8()) | null in LanceDB chunk table (same pattern as acl).
   */
  scopes: string[] | null;
}

/** E2a delta field added to CollectionMeta dataclass (collection_meta.py). */
model CollectionMetaE2aFields {
  /**
   * Default TTL in seconds for new chunks in this collection. Range: [1, 2^31-1].
   * Null = no collection-level default; only chunk-level or request-level TTL applies.
   * Forward-only: changing this value does NOT retroactively update existing chunks.
   * Persisted in LanceDB meta table as pa.int64() | null.
   */
  default_ttl_seconds: int32 | null;
}

/**
 * Store migration interface — two MigrationSpec entries, both with introduced_at = 1,
 * in one STORE_SCHEMA_VERSION bump (0 → 1).
 */
/** Adds expires_at (utf8|null) and scopes (list<utf8>|null) to every collection chunk table. */
op migrate_expires_at_and_scopes(): void;

/** Adds default_ttl_seconds (int64|null) to the _archon_collection_meta table. */
op migrate_default_ttl_seconds(): void;

/**
 * Store prune interface — queries expired chunk doc_ids FIRST, then deletes, returns doc_ids.
 * len(result) = chunk count deleted. doc_ids may contain duplicates (multiple chunks per doc).
 */
op prune_expired_chunks(collection: string, ns: string): string[];

/** Record returned by query_expiring_chunks — includes full chunk identifiers needed to build ExpiringChunkItem. */
model ExpiringChunkRecord {
  chunk_id: string;
  doc_id: string;
  source_path: string;
  /** ISO 8601 UTC expiry timestamp. */
  expires_at: string;
  scopes: string[] | null;
}

/** Store expiring-chunks query interface. */
op query_expiring_chunks(
  collection: string,
  ns: string,
  /** Upper bound: now_utc + within_seconds. Chunks at or beyond this cutoff are excluded. */
  within_seconds: int32,
): ExpiringChunkRecord[];

/**
 * Returns the count of chunks with expires_at < now_utc in the named collection and namespace.
 * Pre-migration guard: returns 0 immediately if expires_at column is absent.
 * Used by GET /status to populate expired_chunk_count.
 */
op count_expired_chunks(collection: string, ns: string): int32;

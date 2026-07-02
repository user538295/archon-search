import "@typespec/http";
using Http;

@service(#{ title: "Collections API — TTL default, expiring chunks, scopes in documents (E2a)" })
namespace CollectionsTtlContract;

/**
 * E2a additions to PATCH /collections/{name}, new GET /collections/{name}/expiring,
 * and scopes field on GET /collections/{name}/documents items.
 */

/** E2a addition to PatchCollectionBody. Existing field: embedding_model. */
model PatchCollectionBodyE2aExtension {
  /**
   * Default TTL in seconds [1, 2^31-1] applied to all new chunks in this collection.
   * Only affects new ingests — does NOT retroactively update existing chunks.
   * Request-level chunk_ttl_seconds overrides this value. (Per-chunk override is deferred to v2.)
   * Set to null to disable the collection default (new chunks have no expiry unless specified at ingest).
   */
  default_ttl_seconds?: int32 | null;
}

/** One expiring chunk entry returned by GET /collections/{name}/expiring. */
model ExpiringChunkItem {
  chunk_id: string;
  doc_id: string;
  source_path: string;
  /** ISO 8601 UTC timestamp when this chunk expires. */
  expires_at: string;
}

/** Response for GET /collections/{name}/expiring (cursor-paginated). */
model ExpiringChunksResponse {
  items: ExpiringChunkItem[];
  next_cursor: string | null;
  total: int32;
}

/**
 * E2a addition to DocumentInfoItem (GET /collections/{name}/documents response items).
 * Existing fields: doc_id, source_path, chunk_count, indexed_at.
 * The full DocumentInfoItem lives in archon_search/server/schemas.py.
 */
model DocumentInfoItemE2aExtension {
  /**
   * Union of scope tags assigned to chunks under this document.
   * Empty list when no chunks carry scopes.
   */
  scopes: string[];
}

@route("/collections/{name}")
interface Collections {
  @patch patch(@path name: string, @body body: PatchCollectionBodyE2aExtension): void;

  @route("/expiring")
  @get
  listExpiring(
    @path name: string,
    /** Hours ahead to look for expiring chunks. Range: [1, 8760]. */
    @query within_hours: int32,
    @query limit?: int32,
    @query cursor?: string,
  ): ExpiringChunksResponse;
}

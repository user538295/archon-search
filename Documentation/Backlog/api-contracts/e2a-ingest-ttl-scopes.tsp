import "@typespec/http";
using Http;

@service(#{ title: "Ingest API — TTL and scopes (E2a)" })
namespace IngestTtlScopesContract;

/**
 * E2a additions to IngestRequest (POST /ingest and POST /ingest/directory).
 * Partial model — only the fields added by E2a are shown.
 * The full IngestRequest lives in archon_search/server/routes_jobs.py.
 * Existing fields (collection, path, documents, ingested_by) are unchanged.
 */
model IngestRequestE2aExtension {
  /**
   * TTL for all chunks in this ingest, in seconds. Range: [1, 2^31-1].
   * Computes expires_at = now_utc + chunk_ttl_seconds on each chunk.
   * Overrides default_ttl_seconds on the collection.
   * (Per-chunk metadata dict ttl_seconds override is deferred to v2.)
   * Null or absent = no expiry (expires_at = null on chunk).
   */
  chunk_ttl_seconds?: int32 | null;

  /**
   * Scope tags applied to all chunks in this ingest.
   * List of 0–100 strings; each string 1–255 UTF-8 chars, no null bytes, no empty strings.
   * Used to filter search results via scope_filter on POST /search and POST /explain.
   * Null or absent = no scope restriction (chunks are unscoped).
   */
  chunk_scopes?: string[] | null;
}

/** Stubbed full ingest request for route reference. */
model IngestRequest {
  collection: string;
  path?: string;
  documents?: Record<unknown>[];
  ingested_by?: string;
  chunk_ttl_seconds?: int32 | null;
  chunk_scopes?: string[] | null;
}

model IngestJobResponse {
  job_id: string;
  status: string;
}

@route("/ingest")
interface Ingest {
  @post ingest(@body body: IngestRequest): IngestJobResponse;
  @route("/directory") @post ingestDirectory(@body body: IngestRequest): IngestJobResponse;
}

import "@typespec/http";
using Http;

// C1 — GET /status per-collection entry: the `path` and `doc_count` seam.
// Logical contract only. `path` is the collection's ABSOLUTE resolved storage
// path (matching GET /collections/{name}); `doc_count` is the CACHED
// CollectionMeta.doc_count (fast, may lag a live recount).
@service(#{ title: "Archon Search Status API" })
namespace ArchonSearchStatusApi;

model StatusCollectionEntry {
  name: string;
  // Absolute resolved storage path (e.g. /home/op/.archon-search/collections/docs).
  // "" only when the collection name is absent from _all_collection_paths(config).
  path: string;
  // Cached CollectionMeta.doc_count for the caller's namespace; 0 when no meta row.
  doc_count?: int32 = 0;
  chunk_count?: int32 = 0;
  status: string;
  watching: boolean;
  eta_seconds?: float64 | null;
  processed_files?: int32 = 0;
  total_files?: int32 = 0;
  error?: string | null;
  error_count?: int32 = 0;
  needs_reindex?: boolean = false;
  warning?: string | null;
  community_count?: int32 = 0;
  last_built_at?: string | null;
}

model StatusResponse {
  running: boolean;
  collections: StatusCollectionEntry[];
}

@route("/status")
interface Status {
  @get status(): StatusResponse;
}

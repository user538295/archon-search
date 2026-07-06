import "@typespec/http";
using Http;

@service(#{ title: "E2d Graph Lifecycle Hygiene — GET /status changes" })
namespace E2dGraphLifecycleStatus;

/** Per-collection graph statistics; gains communities_invalidated in E2d. */
model GraphCollectionStats {
  collection: string;
  node_count: int32;
  edge_count: int32;
  community_count: int32;
  last_built_at: string | null;
  /** True from the moment GC removes nodes in this collection until the async rebuild completes. */
  communities_invalidated: boolean;
}

/** Top-level graph sub-object on GET /status; gains stale_mention_count in E2d. */
model GraphStatusDetail {
  enabled: boolean;
  backend_threshold_edges: int32;
  /** Total orphan-mention count cached from the last GC run. O(1) read from state file. */
  stale_mention_count: int32;
  collections: GraphCollectionStats[];
}

/** Maintenance sub-object gains last_graph_gc_at in E2d (consistent with last_expired_pruned_at from E2a). */
model MaintenanceStatusDetail {
  last_run_at: string | null;
  last_expired_pruned_at: string | null;
  /** ISO-8601 timestamp of the last graph GC pass, or null if GC has never run. */
  last_graph_gc_at: string | null;
}

/** Slim envelope: only the fields relevant to E2d changes. */
model StatusResponseGraphSlice {
  graph: GraphStatusDetail | null;
  maintenance: MaintenanceStatusDetail | null;
}

@route("/status")
interface StatusEndpoint {
  @get get(): StatusResponseGraphSlice;
}

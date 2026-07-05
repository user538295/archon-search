namespace GraphStoreNamespaceSeam;

// CONVENTION: ns is the LAST parameter on all methods, matching the store.py trailing-namespace pattern.
// KEYWORD-ONLY CONVENTION: Any GraphStore method with an optional positional parameter before ns
// (e.g., limit=None) MUST declare ns as keyword-only via * in the Python implementation:
// e.g. def get_all_mentions(self, collection, limit=None, *, ns).
// The * marker makes ns keyword-only and required; Python allows this even though ns has no default
// and comes after limit=None.

/** Result of a single GC pass on one collection. */
model GcPassResult {
  /** Number of orphan nodes deleted (no remaining mention). */
  orphan_nodes_removed: int32;
  /** Number of orphan edges deleted. */
  orphan_edges_removed: int32;
  /** True if any nodes were removed (communities must be rebuilt).
      communities_invalidated is derived (orphan_nodes_removed > 0); not directly settable. */
  communities_invalidated: boolean;
}

/** GraphStore interface as modified by E2d: every method gains ns (namespace) as the LAST parameter. */
interface GraphStore {
  /** Ensure the four graph tables exist for this ns+collection pair.
      Table name pattern: _archon_graph_{ns}__{collection}_* */
  ensureGraphTables(collection: string, ns: string): void;

  /** Remove all mention rows for a specific document. Idempotent. */
  deleteMentionsByDoc(collection: string, doc_id: string, ns: string): void;

  /** Delete nodes with zero remaining mentions, plus all edges referencing them.
      Returns the count of nodes and edges removed.
      communities_invalidated is derived (orphan_nodes_removed > 0); not directly settable. */
  deleteOrphanNodesAndEdges(collection: string, ns: string): GcPassResult;

  /** Delete mention rows whose chunk_id is NOT in liveChunkIds. Returns the count of rows pruned.
      IMPORTANT: Callers (Use Cases layer only) must validate that liveChunkIds is not spuriously empty before invoking this method.
      Implementation should use `frozenset` for O(1) membership lookup. The string[] type here is TypeSpec's representation. */
  pruneStaleMentions(collection: string, liveChunkIds: string[], ns: string): int32;

  /** Count mention rows whose chunk_id is NOT in liveChunkIds.
      Used to pre-measure stale mention count at the START of a GC pass (before prune),
      so the cached stale_mention_count reflects what was stale before cleanup. */
  countStaleMentions(collection: string, liveChunkIds: string[], ns: string): int32;

  /** Replace-all the communities table for a collection. */
  writeCommunities(collection: string, communities: Community[], ns: string): void;
}

model Community {
  community_id: string;
  entity_ids: string[];
  representative_chunk_ids: string[];
  summary_text: string | null;
  built_at: string;
}

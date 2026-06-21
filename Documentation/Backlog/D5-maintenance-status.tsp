namespace ArchonSearch.D5;

/** Per-collection health snapshot written after each maintenance pass. */
model CollectionHealthEntry {
  /** Bare collection name (namespace is implicit from the caller's API key scope). */
  collection: string;

  /** ISO-8601 timestamp of last successful FTS optimise for this collection; null if never run. */
  fts_optimized_at: utcDateTime | null;

  /** Number of orphaned chunks removed in the most recent pass. */
  orphans_removed_last_run: int64;

  /** ISO-8601 timestamp of the last failed-ingest retry enqueue; null if never retried. */
  last_retry_at: utcDateTime | null;

  /** Last pass error for this collection (first error encountered); null if clean. */
  last_error: string | null;

  /** Raw router counter: centroid mutations since last recompute (for D5.1 stale-detection). */
  mutations_since_recompute: int64;

  /** Config value at status-read time: threshold beyond which recompute is advised. */
  centroid_recompute_threshold: int64;

  /** chunk_count stored in collection-meta row (O(1) from get_collection_meta()); D5.1 may add actual_chunk_count via count_rows() for drift detection. */
  meta_chunk_count: int64;
}

/** Maintenance object in GET /status response; null when loop is not wired to app.state. */
model MaintenanceStatusDetail {
  /** True when interval_hours > 0. */
  enabled: boolean;

  /** Configured interval; 0 when disabled. */
  interval_hours: int32;

  /** ISO-8601 timestamp of last completed pass; null if no pass has ever run. */
  last_run_at: utcDateTime | null;

  /** ISO-8601 timestamp of next scheduled pass; null when disabled. */
  next_run_at: utcDateTime | null;

  /** Per-collection health, scoped to the caller's namespace. */
  collection_health: CollectionHealthEntry[];
}

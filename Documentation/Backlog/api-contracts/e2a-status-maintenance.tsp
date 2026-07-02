import "@typespec/http";
using Http;

@service(#{ title: "Status API — maintenance TTL pruning fields (E2a)" })
namespace StatusMaintenanceContract;

/**
 * E2a additions to MaintenanceStatusDetail (nested under GET /status → maintenance field).
 * Partial model — only the fields added by E2a are shown.
 * The full MaintenanceStatusDetail lives in archon_search/server/schemas.py.
 * Existing fields (enabled, interval_hours, last_run_at, next_run_at, collection_health) are unchanged.
 */
model MaintenanceStatusE2aExtension {
  /**
   * Point-in-time count of chunks with expires_at < now_utc across all collections in the
   * caller's namespace. Computed at GET /status call time — not the count from the last prune run.
   * Always a non-null integer (0 or more); returns 0 when no expired chunks exist.
   */
  expired_chunk_count: int32;

  /**
   * ISO 8601 UTC timestamp of the most recent successful prune_expired_chunks completion.
   * Null before the first successful prune run.
   */
  last_expired_pruned_at: string | null;
}

/** Stub for GET /status top-level shape. Only the maintenance-related E2a extension is shown. */
model StatusResponseE2aExtension {
  maintenance: MaintenanceStatusE2aExtension | null;
}

@route("/status")
interface Status {
  @get status(): StatusResponseE2aExtension;
}

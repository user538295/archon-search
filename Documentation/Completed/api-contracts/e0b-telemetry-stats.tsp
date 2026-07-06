import "@typespec/http";
using Http;

@service(#{ title: "Telemetry Stats API — truncation count (E0b L11)" })
namespace TelemetryStatsContract;

/**
 * E0b addition to StatsResponse (L11).
 * This is a partial model — only the field added by E0b is shown.
 * The full StatsResponse lives in archon_search/server/schemas_telemetry.py.
 * Existing fields (schema_version, enabled, total_queries, skipped_lines,
 * since, until, success_rate, latency_ms, etc.) are unchanged.
 * Note: implementers extend StatsResponse, not a new class.
 * Count logic must use `entry.truncated is True` (identity check) —
 * TelemetryEntry.truncated defaults to None (not False), so `if entry.truncated:`
 * would incorrectly skip None entries.
 */
model StatsResponseE0bExtension {
  /** L11: count of entries where result_doc_ids was truncated to fit MAX_ENTRY_BYTES. */
  truncated_count: int32;
}

@route("/telemetry/stats")
interface TelemetryStats {
  @get stats(): StatsResponseE0bExtension;
}

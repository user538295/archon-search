import "@typespec/http";
using Http;

@service(#{ title: "Telemetry Stats API — truncation count (E0b L11)" })
namespace TelemetryStatsContract;

/** StatsResponse shape after E0b L11. Additive — no existing fields removed. Matches Python class StatsResponse in schemas_telemetry.py. */
model StatsResponse {
  schema_version: int32;
  enabled: boolean;
  total_queries: int32;
  skipped_lines: int32;
  /** L11: count of entries where result_doc_ids was truncated to fit MAX_ENTRY_BYTES. */
  truncated_count: int32;
}

@route("/telemetry/stats")
interface TelemetryStats {
  @get stats(): StatsResponse;
}

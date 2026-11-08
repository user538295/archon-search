// C4 — Telemetry label plumbing for the shared service (Use Cases <-> telemetry writer, internal).
// The service records every outcome once for every interface (ADR 20 S1 amendment). This contract parameterises the label that is the constant `_ENDPOINT = "search"` in services/search_service.py (lines 86, 248, 264, 532 today).
// ADR 05: the entry never carries the query, the hypothesis or an exception's text; the factories take no query parameter and this slice adds none.
namespace SearchWithContextTelemetryContract;

// Verified: `EndpointKind.search_with_context` already exists (telemetry/entry.py line 65), `from_search_tool_result` accepts it (lines 154-164) and `from_error` takes an `EndpointKind` (line 249). No entry-model change.
enum EndpointLabel {
  search,
  search_multi,
  search_with_context,
}

// The service passes the label into the shared steps (`_report_failure`, `_record_success`, `_log_stage_timings`) instead of reading the constant. Label values for `search` do not change.
// Recorded for search with context on every interface: success, timeout and internal error. MCP records success and the generic internal error today (`_enqueue_internal_error`, mcp.py line 817); the timeout is new for it, and so are the entries for embedder-not-ready, metadata-store and HyDE failures, which the service records as internal_error / `other` exactly as for `search` (service_error.py `_TELEMETRY`, lines 115-120) and which MCP never recorded. Validation-class kinds and not-found are not recorded.
enum RecordedOutcome {
  success,
  timeout,
  internal_error,
}

// Recorded doc ids are the HITS' doc ids only (verified against the MCP tool today, `result_doc_ids` from `r["result"]`); neighbour doc ids are not recorded. With `[telemetry] hash_doc_ids` on, the same doc_id_hasher hashes them on REST, MCP and CLI-via-REST (the hasher must reach the new service and route).
model SearchWithContextTelemetryEntry {
  endpoint: EndpointLabel;
  collection: string;
  result_doc_ids: string[];
  latency_ms: float64;
  rag_fusion_applied: boolean;
  rag_fusion_queries_used: int32;
}

interface SearchWithContextTelemetry {
  enqueue(entry: SearchWithContextTelemetryEntry): void;
}

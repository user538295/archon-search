// C4 — Search telemetry recorded by the service (Use Cases <-> telemetry writer).
// The service records the outcome of every search once, for every interface (brief Core Flow step 8, drift row 10).
// ADR 05: the entry never carries the query, the hypothesis or an exception's text.
namespace SearchTelemetryContract;

// What the brief names: "success, timeout, internal error". Which further outcomes are recorded is Q9 in the plan.
enum RecordedOutcome {
  success,
  timeout,
  internal_error,
}

// Labels keep today's values (REST and MCP already agree; verified in the plan): single-path success `search`, multi-path success `search_multi`,
// and every error entry `search`, including a multi-path one. There is no per-interface dimension in this slice.
enum EndpointLabel {
  search,
  search_multi,
}

enum TelemetryStatus {
  ok,
  timeout,
  internal_error,
}

model FilterFlags {
  file_type: boolean;
  source_path_prefix: boolean;
  source_path_glob: boolean;
  indexed_after: boolean;
  indexed_before: boolean;
  include_metadata: boolean;
  language_filter_used: boolean;
}

model SearchTelemetryEntry {
  endpoint: EndpointLabel;
  status: TelemetryStatus;
  latency_ms: float64;
  // Single path only.
  collection?: string;
  // Multi path only.
  collections?: string[];
  result_count?: int32;
  // Hashed with the configured doc_id hasher when hashing is on; identical on every interface.
  result_doc_ids?: string[];
  // Set from the resolved command, so include_metadata counts the same on every interface.
  filter_flags?: FilterFlags;
  rag_fusion_applied?: boolean;
  rag_fusion_queries_used?: int32;
  correlation_id?: string;
}

// Built only through the existing entry factories; the writer is fire-and-forget and never fails a search.
interface SearchTelemetryRecorder {
  recordOutcome(outcome: RecordedOutcome, entry: SearchTelemetryEntry): void;
}

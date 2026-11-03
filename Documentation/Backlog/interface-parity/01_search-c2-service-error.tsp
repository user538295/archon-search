// C2 — ServiceError and the per-adapter error tables (Use Cases <-> Interface Adapters).
// One translation inside the service; each adapter owns one table that renders a ServiceError to its wire form.
// Wire text is fixed and never an exception's text (tests/test_no_wire_str_exc.py guards services/).
// A ServiceError carries no exception, so the service (not the adapters) logs the failure events where the exception is in hand, for every interface and both paths: ERROR `search_pipeline_failure` with exc_info for internal and hyde_failed, ERROR `search_timeout` with the collection(s) in extra, the traceback of a metadata_store_error and a warning for embedder_not_ready; never the query. The adapters log nothing for these kinds.
namespace SearchServiceErrorContract;

// Closed set. Each kind keeps the wire code the interfaces use today; kinds with no code today use `validation_error`.
enum ServiceErrorKind {
  // Query, collection selection, fan-out limit, top_k bounds, ppr combined with collections, filter values.
  validation,
  invalid_graph_mode,
  graph_disabled,
  scope_filter_graph_mode_incompatible,
  invalid_scope_filter,
  // The detail is built from the exception's typed `collection` and the message template that GraphCommunitiesNotBuiltError exposes as a class constant (pipeline.py), never retyped in the service.
  graph_communities_not_built,
  // Also a name that is not a valid collection name (is_valid_collection_name, checked first in the service): the real store raises ValueError for it, which must not be taken for a metadata-store failure and is not recorded.
  not_found,
  metadata_store_error,
  embedder_not_ready,
  timeout,
  // A HyDE generation failure. A separate kind so REST keeps its two distinct 500 shapes (JSON `search failed` for this one).
  hyde_failed,
  // Any other failure of the pipeline (REST answers a plain-text 500).
  internal,
}

model ServiceError {
  kind: ServiceErrorKind;
  // The stable machine code (for example "metadata_store_error"); one constant per code, defined once next to this type.
  code: string;
  // Fixed, sanitized text; never `str(exc)`.
  detail: string;
  // Set only for embedder_not_ready: the single constant behind every `Retry-After` header (30 seconds today).
  retry_after_seconds?: int32;
}

// How each kind is recorded (C4, Brief 00 C5). timeout -> status timeout, error kind timeout; internal, hyde_failed, embedder_not_ready and metadata_store_error -> status internal_error, error kind other;
// validation, invalid_graph_mode, graph_disabled, scope_filter_graph_mode_incompatible, invalid_scope_filter, graph_communities_not_built and not_found are not recorded (REST records nothing for graph_communities_not_built today).
enum TelemetryStatus {
  timeout,
  internal_error,
}

enum TelemetryErrorKind {
  timeout,
  other,
}

model ServiceErrorTelemetry {
  kind: ServiceErrorKind;
  recorded: boolean;
  status?: TelemetryStatus;
  error_kind?: TelemetryErrorKind;
}

// How a REST body is shaped today and must stay (brief: existing body shapes are preserved unless the table row says otherwise).
enum RestBodyShape {
  plain_detail,
  coded_detail,
  detail_with_code,
  framework_validation_list,
  plain_text,
}

model RestRendering {
  kind: ServiceErrorKind;
  status: int32;
  body: RestBodyShape;
  retry_after_header: boolean;
}

// MCP renders every ServiceError as {error, code}. A missing identity is answered first, in production, by the API-key middleware (a bodyless 401, middleware_auth.py); the adapter-level refusal (for MCP a fixed ToolError raised before the service is called) is a second line reachable only without the middleware.
model McpRendering {
  kind: ServiceErrorKind;
  error: string;
  code: string;
}

// The shim renders the OpenAI error envelope {error:{message,type,code?}}; type is invalid_request_error or server_error.
model ShimRendering {
  kind: ServiceErrorKind;
  status: int32;
  message: string;
  error_type: string;
  retry_after_header: boolean;
}

// The CLI is an HTTP client and has no service table: it renders REST statuses and the three REST `detail` shapes (string, list, coded object).
interface AdapterErrorTables {
  rest(error: ServiceError): RestRendering;
  mcp(error: ServiceError): McpRendering;
  shim(error: ServiceError): ShimRendering;
}

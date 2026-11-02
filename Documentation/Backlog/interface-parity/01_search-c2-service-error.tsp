// C2 — ServiceError and the per-adapter error tables (Use Cases <-> Interface Adapters).
// One translation inside the service; each adapter owns one table that renders a ServiceError to its wire form.
// Wire text is fixed and never an exception's text (tests/test_no_wire_str_exc.py guards services/).
namespace SearchServiceErrorContract;

// Closed set. Each kind keeps the wire code the interfaces use today; kinds with no code today use `validation_error`.
enum ServiceErrorKind {
  // Query, collection selection, fan-out limit, top_k bounds, ppr combined with collections, filter values.
  validation,
  invalid_graph_mode,
  graph_disabled,
  scope_filter_graph_mode_incompatible,
  invalid_scope_filter,
  graph_communities_not_built,
  not_found,
  metadata_store_error,
  embedder_not_ready,
  timeout,
  // Any other failure of the pipeline or of HyDE / RAG Fusion resolution. Whether a HyDE failure is a separate kind is Q15 in the plan.
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

// MCP renders every ServiceError as {error, code}; a missing identity is a fixed ToolError raised by the adapter before the service is called.
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

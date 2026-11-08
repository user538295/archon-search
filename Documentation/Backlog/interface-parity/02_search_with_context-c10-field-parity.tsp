// C10 — Field-level parity for search with context (test-side). Extends S1's C9 (01_search-c9-field-parity.tsp): the same checker generalised, a second mapping beside the first.
// tests/parity/field_parity.py names `search` in three places only: its imports of the live schema types, the constants `_MCP_SEARCH_TOOL` and `_CLI_SEARCH_COMMAND` (lines 55-56) and the two collectors `collect_request_fields` and `collect_response_fields` (lines 277 and 287); `verify_request` and `verify_response` already take any mapping rows. Decided: the collectors and the two constants are parametrised over the surface set (service types, REST models, MCP tool name, CLI command name) and a second mapping table is added to tests/parity/field_registry.py. Names only; values are compared by the smoke test.
namespace SearchWithContextFieldParityContract;

enum Surface {
  command,
  rest,
  mcp,
  cli,
}

model FieldMapping {
  command_field: string;
  rest_field?: string;
  mcp_param?: string;
  cli_option?: string;
  waived_surfaces?: Surface[];
  waiver_reason?: string;
}

// Command side (SearchWithContextCommand, REST request, MCP input, CLI options): `context_window` maps to REST `context_window`, MCP `context_window`, CLI `--context-window`; filters nested on REST, flat on MCP and CLI; `filters.include_metadata` is REST-only and waived on MCP and CLI as in S1. There is no `graph_mode` and no `collections` row: neither exists on any surface, so no permanent waiver is needed (brief Key Decisions).
// Response side: results[].result.* (hit), results[].context_before[].* and results[].context_after[].* (neighbours, nested dotted leaf paths), and the derived fields; the CLI --json is the REST body, so the CLI is not a response surface. The hit-level raw ACL provenance and the ORed include_metadata flag are waived on REST and MCP as in S1. Neighbours expose `acl` only on both wire surfaces, so every other leaf of the service's neighbour type (C1: vector, custom_score, expires_at, scopes, start_offset, end_offset, acl_source, acl_sidecar_path, acl_warning) carries a waiver on REST and MCP: the vector, scopes, expiry, offsets and custom score are service-internal and never leave the server, and the ACL provenance is shown only through a hit's `acl_gate` (neighbours carry none, brief Key Decisions). Without those waivers the leaf-name check would flag them as missing on the wire surfaces.
model ResponseFieldMapping {
  result_field: string;
  rest_field?: string;
  mcp_field?: string;
  waived_surfaces?: Surface[];
  waiver_reason?: string;
}

enum FieldParityFailure {
  missing_on_surface,
  unmapped_surface_field,
  invalid_waiver,
}

// Registry row (tests/parity/registry.py "S2 Search with context"): service_method set to `search_with_context`, REST identifier `POST /search-with-context`, MCP `search_with_context`, CLI `search-with-context`; the REST and CLI `pending S2` waivers are removed.
interface FieldParity {
  check(rows: FieldMapping[]): FieldParityFailure[];
}

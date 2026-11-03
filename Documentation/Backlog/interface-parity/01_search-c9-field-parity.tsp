// C9 — Field-level parity check (test-side; the first one, brief Q3). Shapes Brief 00's C12 for the remaining slices.
// It extends tests/parity/ (field_parity.py: the checker and the live-schema collectors; field_registry.py: the mapping data) and runs in the default suite. It compares NAMES only: a field is either carried by a surface or not, and no failure kind names a value difference (values are compared by the smoke tests, tests/smoke/test_four_interface_parity.py). The surface-level registry check is unchanged.
namespace FieldParityContract;

enum Surface {
  command,
  rest,
  mcp,
  cli,
}

// A command field and where it lives on each surface. REST filters are nested (`filters.<name>`), MCP and the CLI are flat; the mapping is explicit data, not inferred.
// The command carries two include-metadata flags: the top-level `include_metadata` maps to REST `include_metadata`, MCP `include_metadata` and CLI `--include-metadata`; `filters.include_metadata` maps to REST only and is waived on MCP and the CLI (each carries one flag, which fills the top-level one).
model FieldMapping {
  command_field: string;
  rest_field?: string;
  mcp_param?: string;
  cli_option?: string;
  // Required when a surface is intentionally absent (for example a field a surface keeps out, or one the response shows only through another field): the surfaces the waiver covers and the reason. A waiver is invalid without a non-empty surface list or without a non-blank reason, and for a surface it names that is not a checked surface (`command`; `cli` on a response) or that does carry the field. The shim has no surface and no row: it is waived wholesale as adapter policy (its own protocol envelope; it ignores a request `top_k`).
  waived_surfaces?: Surface[];
  waiver_reason?: string;
}

// Response side: the same check over the response fields (service result `SearchOutcome`, REST SearchResponse, MCP response; the CLI --json is the REST body, so the CLI is not a response surface). Names are dotted leaf paths (`results[].result.doc_id` on the service side, `results[].doc_id` on REST and MCP, `results[].acl_gate.warnings`). The raw ACL provenance (`results[].result.acl_source`, `acl_sidecar_path`, `acl_warning`) and the ORed `include_metadata` flag are waived on REST and MCP: the responses show the first only through `acl_gate`, and the second only by blanking `metadata`.
model ResponseFieldMapping {
  result_field: string;
  rest_field?: string;
  mcp_field?: string;
  waived_surfaces?: Surface[];
  waiver_reason?: string;
}

enum FieldParityFailure {
  // A command or result field that a surface does not carry and no waiver covers; also a mapping row whose command or result field no longer exists (surface `command`).
  missing_on_surface,
  // A surface field (REST property, MCP parameter, CLI option) that maps to no command or result field and is not a listed transport option; also a command or result field with no mapping row (surface `command`).
  unmapped_surface_field,
  // A waiver that names no surface or no reason (surface `command`), or that names a surface that is not checked or that does carry the field.
  invalid_waiver,
}

model FieldParityFinding {
  failure: FieldParityFailure;
  surface: Surface;
  field: string;
}

// Transport options carry no command field: the CLI options `--json`, `--api-url` and `--api-key`. They form an explicit exemption set, not a waiver per field, so `unmapped_surface_field` does not fire for them.
model TransportExemptions {
  cli_options: string[];
}

// Reads the live schemas (`collect_request_fields`, `collect_response_fields`): the REST request model, the registered MCP tool input schema, the Click command parameters, and the `SearchCommand` / `SearchOutcome` definitions; the REST and MCP response models. A malformed mapping (an unknown surface or key, a missing source key, a second row for one field) or a model field with an alias is a ValueError, not a finding.
interface FieldParityCheck {
  verifyRequest(mappings: FieldMapping[], live: Record<string[]>, exemptions: TransportExemptions): FieldParityFinding[];
  verifyResponse(mappings: ResponseFieldMapping[], live: Record<string[]>): FieldParityFinding[];
}

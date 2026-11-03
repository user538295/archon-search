// C9 — Field-level parity check (test-side; the first one, brief Q3). Shapes Brief 00's C12 for the remaining slices.
// It extends tests/parity/ and runs in the default suite; it compares values as well as names where that is cheap. The surface-level registry check is unchanged.
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
  // Required when a surface is intentionally absent (for example the shim, or a field a surface keeps out): the surfaces the waiver covers and the reason. A waiver is invalid without a non-empty surface list or without a reason.
  waived_surfaces?: Surface[];
  waiver_reason?: string;
}

// Response side: the same check over the response fields (service result, REST SearchResponse, MCP response, CLI --json = the REST body).
model ResponseFieldMapping {
  result_field: string;
  rest_field?: string;
  mcp_field?: string;
  waived_surfaces?: Surface[];
  waiver_reason?: string;
}

enum FieldParityFailure {
  // A command or result field that a surface does not carry and no waiver covers.
  missing_on_surface,
  // A surface field (REST property, MCP parameter, CLI option) that maps to no command or result field and is not a listed transport option.
  unmapped_surface_field,
  // A waiver that names no surface or no reason, or that names a surface that does carry the field.
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

// Reads the live schemas: the REST request model, the registered MCP tool input schema, the Click command parameters, and the command / result definitions.
interface FieldParityCheck {
  verifyRequest(mappings: FieldMapping[], exemptions: TransportExemptions): FieldParityFinding[];
  verifyResponse(mappings: ResponseFieldMapping[]): FieldParityFinding[];
}

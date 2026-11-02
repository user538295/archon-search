// C9 — Field-level parity check (test-side; the first one, brief Q3). Shapes Brief 00's C12 for the remaining slices.
// It extends tests/parity/ and runs in the default suite. The surface-level registry check is unchanged.
namespace FieldParityContract;

enum Surface {
  command,
  rest,
  mcp,
  cli,
}

// A command field and where it lives on each surface. REST filters are nested (`filters.<name>`), MCP and the CLI are flat; the mapping is explicit data, not inferred.
model FieldMapping {
  command_field: string;
  rest_field?: string;
  mcp_param?: string;
  cli_option?: string;
  // Required when a surface is intentionally absent (for example the shim, or a field a surface keeps out): the reason.
  waiver_reason?: string;
}

// Response side: the same check over the response fields (service result, REST SearchResponse, MCP response, CLI --json = the REST body).
model ResponseFieldMapping {
  result_field: string;
  rest_field?: string;
  mcp_field?: string;
  waiver_reason?: string;
}

enum FieldParityFailure {
  // A command or result field that a surface does not carry and no waiver covers.
  missing_on_surface,
  // A surface field (REST property, MCP parameter, CLI option) that maps to no command or result field.
  unmapped_surface_field,
  // A waiver naming a surface that does carry the field, or naming no reason.
  invalid_waiver,
}

model FieldParityFinding {
  failure: FieldParityFailure;
  surface: Surface;
  field: string;
}

// Reads the live schemas: the REST request model, the registered MCP tool input schema, the Click command parameters, and the command / result definitions.
interface FieldParityCheck {
  verifyRequest(mappings: FieldMapping[]): FieldParityFinding[];
  verifyResponse(mappings: ResponseFieldMapping[]): FieldParityFinding[];
}

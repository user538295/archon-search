// C4 — Parity registry (brief C12): what the registry claims versus what the three live surfaces expose.
namespace ParityRegistryContract;

enum Surface {
  rest,
  mcp,
  cli,
}

model Waiver {
  reason: string;
  // Names the slice that will close the gap, for pending work.
  pendingSlice: string | null;
}

model SurfaceClaim {
  surface: Surface;
  // REST: method plus path. MCP: tool name. CLI: command path, e.g. "collection list".
  identifier: string | null;
  waiver: Waiver | null;
}

model Capability {
  name: string;
  serviceMethod: string | null;
  claims: SurfaceClaim[];
}

enum ParityFailure {
  unclaimedSurface,
  claimedSurfaceMissing,
  gapWithoutWaiver,
}

model ParityFinding {
  failure: ParityFailure;
  surface: Surface;
  identifier: string;
}

interface LiveSurfaces {
  restRoutes(): string[];
  mcpTools(keyManagementEnabled: boolean): string[];
  // Must resolve lazily registered command groups as well as eagerly registered ones.
  cliCommands(): string[];
}

interface ParityCheck {
  verify(registry: Capability[], live: LiveSurfaces): ParityFinding[];
}

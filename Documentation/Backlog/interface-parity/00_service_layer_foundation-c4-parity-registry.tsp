// C4 — Parity registry (brief C12): what the registry claims versus what the three live surfaces expose.
namespace ParityRegistryContract;

enum Surface {
  rest,
  mcp,
  cli,
}

enum WaiverKind {
  permanent,
  pending,
}

model Waiver {
  kind: WaiverKind;
  reason: string;
  // Required (non-blank) when kind is pending: the roadmap slice that will close the gap, for example "S2".
  pendingSlice: string | null;
}

model SurfaceClaim {
  surface: Surface;
  // REST: `METHOD path` for each method of a route with HEAD dropped (Starlette adds HEAD to plain GET routes such as /openapi.json
  // and the docs routes); a Mount, which has no methods, is its path alone (`/mcp`).
  // MCP: tool name. CLI: the space-joined command path, e.g. "collection list" or "backup status".
  // The conditional /mcp mount, /openapi.json, the docs routes and the conditional /v1 routes are live surfaces claimed with a waiver (identifier plus reason).
  // A Click group is a surface only when it is invokable without a subcommand (today only `backup`, id "backup"); other groups are namespaces.
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
  pendingWaiverWithoutSlice,
}

model ParityFinding {
  failure: ParityFailure;
  // The capability the finding concerns; empty for an unclaimed surface.
  capability: string | null;
  surface: Surface;
  // Null for a gap-level finding (gapWithoutWaiver, pendingWaiverWithoutSlice) that has no identifier.
  identifier: string | null;
}

interface LiveSurfaces {
  // Routes of the app built for the test with docs and the OpenAI shim enabled in its config, so /docs, /redoc, /docs/oauth2-redirect and /v1/* are live.
  restRoutes(): string[];
  // REST surfaces that exist only after the lifespan runs (today `/mcp`, mounted inside the lifespan). The test does not run the lifespan,
  // so a claim of one of these is exempt from claimedSurfaceMissing.
  lifespanOnlyRestSurfaces(): string[];
  // Built with a key store, so the four key-management tools are included; without a key store they are not registered and the test does not check that case.
  mcpTools(): string[];
  // Must resolve lazily registered command groups as well as eagerly registered ones.
  cliCommands(): string[];
}

interface ParityCheck {
  verify(registry: Capability[], live: LiveSurfaces): ParityFinding[];
}

// C1 — Import boundary rule (brief C1 + C11). Logical seam between the layer rules and the AST boundary test.
namespace ImportBoundaryContract;

enum ImportScope {
  moduleLevel,
  functionLocal,
  typeCheckingOnly,
}

// One import edge. A relative import is resolved to its absolute module first. `from pkg import name` targets
// `pkg.name` when that submodule exists, otherwise `pkg`. Several statements with the same key are one edge.
model ImportEdge {
  importingFile: string;
  importedModule: string;
  scope: ImportScope;
}

// A known violation. Keyed by the edge (importingFile, importedModule, scope); the list only shrinks.
model AllowlistEntry {
  edge: ImportEdge;
  reason: string;
}

// A permanent, hard-coded exception. Not an allowlist entry: it never goes stale and never shrinks.
// An importedModulePattern is an exact module name, or a prefix ending in `*`; scopes lists every scope it covers.
// Today: app.py (the composition root) may import `archon_search.server.routes_*` and `archon_search.server.mcp` in all three scopes;
// cli/serve.py may import `archon_search.server.app` function-locally (the lazy run_server import).
model PermanentException {
  importingFile: string;
  importedModulePatterns: string[];
  scopes: ImportScope[];
  reason: string;
}

enum VerdictKind {
  clean,
  newViolation,
  staleAllowlistEntry,
}

model BoundaryFinding {
  kind: VerdictKind;
  edge: ImportEdge;
}

// Rule 1: what a services module must never import. The internal and third-party entries apply in every scope;
// the module-level-only entries (the optional `anthropic` dependency, ADR-C4) allow function-local and type-only imports.
const servicesDeniedInternalPackages = #["archon_search.server", "archon_search.cli"];
const servicesDeniedThirdPartyPackages = #["fastapi", "starlette", "fastmcp", "click", "httpx", "uvicorn"];
const servicesDeniedAtModuleLevel = #["anthropic"];

model ServicesDenyList {
  forbiddenInternalPackages: string[];
  forbiddenThirdPartyPackages: string[];
  forbiddenAtModuleLevelOnly: string[];
}

// Rules 2-4: layering inside server/ and cli/.
model LayeringRules {
  // Rule 2: no file under archon_search/server imports a module matching one of these patterns (a trailing `*` is a prefix match):
  // today `archon_search.server.routes_*` (the OpenAI shim included) and `archon_search.server.mcp`.
  // The composition root app.py is a permanent exception, not an entry here.
  serverForbiddenImportPatterns: string[];
  // Rule 3: the only archon_search.cli module another CLI file may import; the command-registration module is exempt.
  cliSharedModule: string;
  cliRegistrationModule: string;
  // Rule 4: a CLI file imports nothing from these packages (apart from the permanent exceptions).
  cliForbiddenPackages: string[];
}

model BoundaryRules {
  services: ServicesDenyList;
  layering: LayeringRules;
  permanentExceptions: PermanentException[];
}

interface BoundaryCheck {
  // Scans archon_search/services, archon_search/server and archon_search/cli and compares every edge to the rules
  // and the allowlist: a violation not in the allowlist is new, an allowlist entry with no matching edge is stale.
  scan(allowlist: AllowlistEntry[], rules: BoundaryRules): BoundaryFinding[];
}

// A moved authz name still taken from `archon_search.server.authz` (S31).
model MovedAuthzNameFinding {
  file: string;
  name: string;
}

// Companion check in the same test file, not part of the allowlist logic: scans every .py file under archon_search/ and tests/
// and returns each import of a moved authz name from `archon_search.server.authz` (no allowlist, no scope logic).
interface MovedAuthzNameCheck {
  scan(movedNames: string[]): MovedAuthzNameFinding[];
}

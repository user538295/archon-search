// C1 — Import boundary rule (brief C1 + C11). Logical seam between the layer rules and the AST boundary test.
namespace ImportBoundaryContract;

enum ImportScope {
  moduleLevel,
  functionLocal,
  typeCheckingOnly,
}

model ImportEdge {
  importingFile: string;
  importedModule: string;
  scope: ImportScope;
}

// A known violation. Keyed by (importingFile, importedModule); the list only shrinks.
model AllowlistEntry {
  importingFile: string;
  importedModule: string;
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

// Directories a services module must never import from (and the third-party packages it must avoid).
model ServicesDenyList {
  forbiddenInternalPackages: string[];
  forbiddenThirdPartyPackages: string[];
}

interface BoundaryCheck {
  // Scans the guarded source tree and compares every edge to the allowlist.
  scan(allowlist: AllowlistEntry[], denyList: ServicesDenyList): BoundaryFinding[];
}

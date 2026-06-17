// C1 — Store migration API seam (Use Cases ↔ Interface Adapters / Frameworks)
// Logical contract only. Realised by SearchStore in archon_search/store.py.
// Core TypeSpec constructs only; validate with: tsp compile D3-store-migration-api.tsp --no-emit

namespace StoreMigrationContract;

enum MigrationKind {
  in_place,
  rewrite,
  export_rebuild,
}

// Phases a rewrite migration passes through; emitted via the progress callback.
enum MigrationPhase {
  detecting,
  rewriting,
  reindexing,
  finalizing,
}

// One pending migration, classified so operators know its cost/risk upfront.
model MigrationSpec {
  name: string;
  kind: MigrationKind;
  description: string;
}

// Progress checkpoint reported every 100 chunks during a rewrite.
model MigrationProgress {
  processed: int32;
  total: int32;
  phase: MigrationPhase;
}

// Result payload carried on a DONE rewrite migration.
model MigrationResult {
  migratedChunks: int32;
  migrationsApplied: string[];
  kind: MigrationKind;
}

// The SearchStore migration surface. pending_migrations compares the live LanceDB
// schema + the schema_version meta column against the canonical schema target.
interface StoreMigrationApi {
  pendingMigrations(collection: string): MigrationSpec[];
  applyInPlaceMigrations(collection: string, specs: MigrationSpec[]): void;
  applyRewriteMigration(collection: string, spec: MigrationSpec): MigrationResult;
}

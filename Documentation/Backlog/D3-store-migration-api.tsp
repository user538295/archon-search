namespace StoreMigrationContract;

enum MigrationKind {
  in_place,
  rewrite,
  export_rebuild,
}

model MigrationSpec {
  name: string;
  kind: MigrationKind;
  description: string;
  introduced_at: int32;
}

interface SearchStoreMigrationInterface {
  // ns maps to `namespace` in Python signatures; 'namespace' is a reserved keyword in TypeSpec op parameter positions
  op pendingMigrations(collection: string, ns: string): MigrationSpec[];
  // Returns names of applied migrations. Postcondition: schema_version in _archon_collection_meta updated to max(spec.introduced_at)
  // ns maps to `namespace` in Python signatures; 'namespace' is a reserved keyword in TypeSpec op parameter positions
  op applyInPlaceMigrations(collection: string, ns: string, specs: MigrationSpec[]): string[];
  // progress_cb(processed, total, phase) called every 100 chunks; not a TypeSpec-expressible callable type
  // ns maps to `namespace` in Python signatures; 'namespace' is a reserved keyword in TypeSpec op parameter positions
  op applyRewriteMigration(collection: string, ns: string, spec: MigrationSpec): void;
  op runStartupMigrations(): void;
}

// C3 — Migration REST / CLI seam (Presentation ↔ server; CLI ↔ server over HTTP)
// Logical contract only. Realised by Pydantic models in archon_search/server/schemas.py
// and routes in routes_collections.py / routes_status.py; consumed by the CLI in cli/.
// Validate with: tsp compile D3-migration-rest-api.tsp --no-emit

namespace MigrationRestContract;

enum MigrationKind {
  in_place,
  rewrite,
  export_rebuild,
}

model MigrationSpec {
  name: string;
  kind: MigrationKind;
  description: string;
}

// GET /collections/{name}/migrations/pending  -> 200; 404 if collection unknown.
model PendingMigrationsResponse {
  collection: string;
  pending: MigrationSpec[];
  schemaVersion: int32;
}

// POST /collections/{name}/migrate request body.
// dry_run=true returns PendingMigrationsResponse without creating a job.
// backup_confirmed is required for rewrite / export_rebuild kinds (else 422).
model MigrateRequest {
  backupConfirmed?: boolean = false;
  dryRun?: boolean = false;
}

// Additive nullable fields grafted onto the shared JobResponse for migration jobs.
model JobResponseMigrationFields {
  migrationsApplied?: string[];
  backupConfirmed?: boolean;
}

// Additive fields on GET /status.
model StatusMigrationFields {
  storeSchemaVersion: int32;
  collectionsSchemaBehind: int32;
}

interface MigrationApi {
  // 200 dry-run / 200 in_place summary / 202 + job for rewrite; 404 / 409 / 422 on error.
  pending(collection: string): PendingMigrationsResponse;
  migrate(collection: string, body: MigrateRequest): PendingMigrationsResponse;
}

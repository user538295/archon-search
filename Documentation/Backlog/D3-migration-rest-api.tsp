namespace MigrationRestApiContract;

enum MigrationKind {
  in_place,
  rewrite,
  export_rebuild,
}

model MigrationSpec {
  name: string;
  kind: MigrationKind;
  description: string;
  // introduced_at: version at which this migration was introduced; helps operators understand ordering
  introduced_at: int32;
}

model MigrationPendingResponse {
  collection: string;
  pending: MigrationSpec[];
  schema_version: int32;
}

// POST /collections/{name}/migrate request body.
// Response dispatch: dry_run=true → MigrationPendingResponse (alias for GET pending, no side effects);
// in-place only → MigrateInPlaceResponse (200); rewrite/export_rebuild → MigrateResponse (202).
model MigrateRequest {
  backup_confirmed?: boolean;
  dry_run?: boolean;
}

model MigrateResponse {
  job_id: string;
  status: string;
}

// 200 response for in-place-only migrations (no MigrationJob created)
model MigrateInPlaceResponse {
  migrations_applied: string[];
}

model JobResponseMigrationExtension {
  migrations_applied?: string[];
  backup_confirmed?: boolean;
}

model StatusResponseSchemaExtension {
  store_schema_version: int32;
  collections_schema_behind: int32;
}

// Error response body (reuses existing ErrorDetail pattern from schemas.py).
// Used for 404, 409, 422 responses from migration endpoints.
model MigrateErrorResponse {
  detail: string;
}

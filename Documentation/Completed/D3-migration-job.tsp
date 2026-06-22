namespace MigrationContract;

enum MigrationKind {
  in_place,
  rewrite,
  export_rebuild,
}

model MigrationProgress {
  processed: int32;
  total: int32;
  phase: string;
}

// Describes the shape of the `result: dict` stored in IngestJob.result for MigrationJob.
// Stored as a dict (not a separate dataclass) to match the existing job_to_dict() pattern.
model MigrationResult {
  migrated_chunks: int32;
  migrations_applied: string[];
  kind: MigrationKind;
}

model MigrationSpec {
  name: string;
  kind: MigrationKind;
  description: string;
  introduced_at: int32;
}

model MigrationJob {
  job_id: string;
  status: string;
  created_at: string;
  updated_at: string;
  collection: string;
  kind: MigrationKind;
  migrations_applied: string[];
  backup_confirmed?: boolean;
  source: string;
  `namespace`: string; // backtick required: TypeSpec reserves 'namespace' as a keyword; wire name matches IngestJob.namespace
  progress?: MigrationProgress;
  result?: MigrationResult;
  error?: string;
}

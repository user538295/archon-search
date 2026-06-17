// C2 — MigrationJob persistence seam (Entities ↔ Interface Adapters)
// Logical contract only. Realised by MigrationJob in archon_search/types.py and the
// JobStore discriminator/serialization in archon_search/jobs/store.py + jobs/model.py.
// Validate with: tsp compile D3-migration-job.tsp --no-emit

namespace MigrationJobContract;

enum JobStatus {
  pending,
  queued,
  running,
  done,
  failed,
  cancelled,
  cancelling,
}

enum MigrationKind {
  in_place,
  rewrite,
  export_rebuild,
}

enum JobSource {
  user,
  auto,
}

// MigrationJob extends the shared IngestJob shape (job_id, status, timestamps,
// result, error, namespace, progress) with the migration-specific fields below.
// job_type discriminator value is "migration".
model MigrationJob {
  jobId: string;
  status: JobStatus;
  createdAt: string;
  updatedAt: string;
  jobNamespace: string;
  progress?: Progress;
  result?: string;
  error?: string;

  collection: string;
  kind: MigrationKind;
  migrationsApplied: string[];
  backupConfirmed: boolean;
  source: JobSource;
}

model Progress {
  processed: int32;
  total: int32;
  phase: string;
}

// The serialization contract two sides must honour: persist with a job_type tag,
// reload into the correct subclass, and surface the new fields (nullable for other
// job kinds) through job_to_dict.
interface JobPersistence {
  createMigration(
    collection: string,
    kind: MigrationKind,
    migrationsApplied: string[],
    backupConfirmed: boolean,
  ): MigrationJob;
  load(jobType: string): MigrationJob;
  toDict(job: MigrationJob): Record<unknown>;
}

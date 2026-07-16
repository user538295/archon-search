// C2 — JobStore <-> route internal seam (in-process, non-HTTP).
// The route creates a CommunityRebuildJob in the SAME JobStore as ingest and
// migration jobs; it is persisted to disk and surfaced via GET /jobs/{id) only
// — NOT /status (StatusResponse, server/schemas.py, has no generic jobs list;
// its only job-derived field is failed_expired_ingest_count, which counts only
// FAILED_EXPIRED IngestJob instances). A running/DONE rebuild job cannot appear
// in /status. Needed because MaintenanceLoop._spawn_rebuild_task is fire-and-
// forget (no trackable job_id). Logical shape only — no bodies, no persistence
// detail.
//
// IC-4 round-trip requirement (verified against jobs/store.py):
// JobStore serialises jobs to disk via a `job_type` string tag, dispatched on
// WRITE by an ordered isinstance(job, ...) cascade (JobStore._write_atomic) and
// reconstructed on READ by matching that same string (JobStore._load).
// CommunityRebuildJob MUST be added to BOTH dispatch chains (e.g. tag
// "community_rebuild", `isinstance(job, CommunityRebuildJob)` on write,
// `job_type == "community_rebuild" -> CommunityRebuildJob(**item)` on read) —
// if only one side is added, a server restart deserialises the job as a plain
// IngestJob, silently losing `collection`/the community_rebuild_job_id guard's
// identity. Ordering matters on the write side (Mo7 / C2 in the team plan):
// CommunityRebuildJob subclasses IngestJob (types.py), and _write_atomic's
// cascade falls through to the generic "ingest" tag only in its final `else` —
// so the new `elif isinstance(job, CommunityRebuildJob)` branch must be placed
// among the other subclass-specific elif branches, before that final `else`,
// or every CommunityRebuildJob silently serialises as a plain IngestJob. This
// contract's `create_community_rebuild` factory is the write-side entry point;
// the read-side dispatch fix has no contract shape of its own (it's an
// internal JobStore branch) but is listed in Backend Done-when.
//
// IMod-2 result semantics (verified against CommunityBuilder.build,
// community_builder.py):
// - Zero graph nodes for the collection -> CommunityBuilder.build raises
//   ValueError -> job ends FAILED (not a valid DONE result).
// - Exactly 1 graph node -> build returns a single synthetic community without
//   running Leiden -> DONE, communities_built: 1 (valid, not an error case).
// - >= 2 graph nodes but Leiden partitions into zero communities on a given
//   graph -> DONE, communities_built: 0 (valid empty-but-clustered result).
//
// M3 correction: CommunityRebuildJob.result below is typed as a required-shape
// CommunityRebuildResult, which contradicted this file's own disclaimer that
// job.result is runtime-untyped (bare dict | None on the shared IngestJob
// base, types.py) and is None on FAILED. `result` is now `optional + nullable`
// on CommunityRebuildJob so a FAILED job with a null result is representable.
// CommunityRebuildResult remains a documentation-only success shape.
namespace GraphCommunityRebuildJobContract;

enum JobStatus {
  PENDING,
  QUEUED,
  RUNNING,
  DONE,
  FAILED,
  FAILED_EXPIRED,
  CANCELLED,
}

// Minor (documentation-only): this model documents the SHAPE of job.result for
// this job type, but the runtime job.result field itself is untyped — it is a
// bare `dict | None` on the shared IngestJob base (types.py, class IngestJob,
// field `result`). CommunityRebuildResult has no runtime enforcement; it exists
// here only so this contract's readers know what keys to expect.
model CommunityRebuildResult {
  // 0 and 1 are both valid non-error values — see IMod-2 note above.
  communities_built: int32;
}

model CommunityRebuildJob {
  job_id: string;
  collection: string;
  `namespace`: string;
  source: string; // "user" | "backup"
  status: JobStatus;
  result?: CommunityRebuildResult | null;
  error?: string;
  created_at: string;
  updated_at: string;
}

interface JobStore {
  create_community_rebuild(collection: string, `namespace`: string): CommunityRebuildJob;
}

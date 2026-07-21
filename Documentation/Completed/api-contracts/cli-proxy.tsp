import "@typespec/http";
using Http;

// Logical CLI <-> server contract for the write-command proxy feature.
// Captures only what crosses the wire: the two NEW endpoints (sync,
// reindex-metadata), the shared async-job response, the job-poll seam, and the
// existing write routes the CLI must call. Auth is a Bearer header on every op.
@service(#{ title: "archon-search CLI proxy API" })
namespace CliProxyApi;

// Terminal + in-flight job states the CLI polls against.
enum JobStatus {
  PENDING,
  QUEUED,
  RUNNING,
  DONE,
  FAILED,
  FAILED_EXPIRED,
  CANCELLED,
  CANCELLING,
}

// The job envelope returned by every async (202) route and by GET /jobs/{id}.
// Only the fields the CLI reads while proxying / polling are modelled here.
model JobResponse {
  job_id: string;
  status: JobStatus;
  @doc("The collection name this job targets. For POST /sync jobs (whole-server sync), this field is \"\" (empty string) — consumers must treat an empty collection as \"whole-server sync\" rather than a named collection.")
  collection: string;
  created_at: string;
  updated_at: string;
  progress?: Record<unknown> | null;
  result?: Record<unknown> | string | null;
  error?: string | null;
}

// Synchronous delete result (remove is NOT a job).
model DeleteResponse {
  name: string;
  deleted: boolean;
}

model AddCollectionRequest {
  path: string;
  embedding_model?: string | null;
}

model IngestRequest {
  collection: string;
  path?: string | null;
  ingested_by?: string;
}

// NEW — reindex-metadata request body (forwards the CLI flags).
model ReindexMetadataRequest {
  dry_run?: boolean;
  normalize_timestamps?: boolean;
}

// NEW — sync trigger. No body: the server syncs its configured collections,
// mirroring the current in-process CLI behaviour.
@route("/collections")
interface Collections {
  // Existing: add -> async ingest job.
  @post
  add(@body body: AddCollectionRequest): {
    @statusCode statusCode: 202;
    @body body: JobResponse;
  };

  // Existing: remove -> synchronous delete (no job, no --wait).
  @delete
  @route("/{name}")
  remove(@path name: string): DeleteResponse;

  // Existing: reindex -> async reindex job.
  @post
  @route("/{name}/reindex")
  reindex(@path name: string): {
    @statusCode statusCode: 202;
    @body body: JobResponse;
  };

  // NEW: reindex-metadata -> async job (added to support --wait).
  @post
  @route("/{name}/reindex-metadata")
  reindexMetadata(@path name: string, @body body: ReindexMetadataRequest): {
    @statusCode statusCode: 202;
    @body body: JobResponse;
  };
}

// Existing: ingest -> async ingest job.
@route("/ingest")
interface Ingest {
  @post
  ingest(@body body: IngestRequest): {
    @statusCode statusCode: 202;
    @body body: JobResponse;
  };
}

// NEW: sync -> async job over the server's configured collections.
@route("/sync")
interface Sync {
  @post
  sync(): {
    @statusCode statusCode: 202;
    @body body: JobResponse;
  };
}

// Shared poll seam used by every --wait loop.
@route("/jobs")
interface Jobs {
  @get
  @route("/{job_id}")
  get(@path job_id: string): JobResponse;
}

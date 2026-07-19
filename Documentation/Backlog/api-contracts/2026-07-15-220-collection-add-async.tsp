import "@typespec/http";
using Http;

// C1 — POST /collections/ (CLI <-> server seam) for bug-005 Non-Blocking
// Collection Add. This contract is a POINT-IN-TIME mirror of the ALREADY-SHIPPED
// handler (routes_collections.py, add_collection, CSP120 commit 8c36a6f7); the
// plan it accompanies is a verification/gap-closing plan, not a green-field one.
//
// Purpose of authoring the FULL status-code contract here (the sibling
// cli-proxy.tsp only models the 202 happy path): the single real design
// decision in this feature is the duplicate-collection semantics. The brief
// asked for 200 "already up to date"; the shipped server returns 409. Modelling
// every emitted status makes that contract explicit and reviewable.
//
// Auth: Bearer token on every request; namespace is derived server-side from the
// token (ADR-09), never sent by the CLI. Name is derived server-side via
// path_to_collection_name(resolved) — the request body has NO collection_name
// field. The server (not the CLI) writes the path to archon-search.toml via
// _maybe_save_config, with rollback on any downstream failure.
//
// JobResponse below is a field-for-field snapshot of server/schemas.py's
// JobResponse (the route returns job_to_dict(job) unmodified). TypeSpec cannot
// $ref a Python model, so this duplication is a deliberate, acknowledged
// snapshot — regenerate it manually if server/schemas.py's JobResponse changes.
@service(#{ title: "Archon Search Collection Add API" })
namespace ArchonSearchCollectionAddApi;

// Request body — mirrors AddCollectionRequest (routes_collections.py).
// NOTE: no `collection_name` field. Name is always server-derived from `path`.
model AddCollectionRequest {
  path: string;
  embedding_model?: string | null;
}

// Mirrors server/schemas.py's JobResponse class exactly.
model JobResponse {
  job_id: string;
  status: string;
  created_at: string;
  updated_at: string;
  result?: string | Record<unknown> | null;
  error?: string | null;
  `namespace`: string;
  progress?: Record<unknown> | null;
  source: string = "user";
  source_path: string = "";
  collection: string = "";
  retry_count: int32 = 0;
  output_path?: string | null;
  archive_path?: string | null;
  kind?: string | null;
  migrations_applied?: string[] | null;
  backup_confirmed?: boolean | null;
}

model ErrorDetail {
  detail: string;
}

// 503 store-busy body differs from ErrorDetail: it is {"error": "store_busy", ...}
// with a Retry-After header. Modelled separately to keep the mirror honest.
model StoreBusyError {
  error: string;
}

@route("/collections")
interface Collections {
  // Register a path as a collection and enqueue its first ingest as an async job.
  //   202 — job queued (JobResponse; CLI reads job_id + collection).
  //   400 — path unsafe (PathUnsafeError).
  //   401 — missing/invalid Bearer token (APIKeyMiddleware).
  //   409 — duplicate resolved path OR name collision across namespaces OR
  //         TOCTOU race. THERE IS NO 200 "already up to date" PATH — a duplicate
  //         is an error (409), which the CLI surfaces as exit 1. This is the
  //         feature's one contract decision (see the plan's Contradictions + Q2).
  //   422 — invalid embedding_model.
  //   500 — internal (stub-meta write failure; config append is rolled back).
  @post
  add(@body body: AddCollectionRequest): {
    @statusCode statusCode: 202;
    @body body: JobResponse;
  } | {
    @statusCode statusCode: 400 | 401 | 409 | 422 | 500;
    @body error: ErrorDetail;
  } | {
    @statusCode statusCode: 503;
    @header("Retry-After") retryAfter: int32;
    @body error: StoreBusyError;
  };
}

// Shared poll seam used by the `add --wait` loop (GET /jobs/{id} every 2s).
@route("/jobs")
interface Jobs {
  @get
  @route("/{job_id}")
  get(@path job_id: string): {
    @statusCode statusCode: 200;
    @body body: JobResponse;
  } | {
    @statusCode statusCode: 401 | 404;
    @body error: ErrorDetail;
  };
}

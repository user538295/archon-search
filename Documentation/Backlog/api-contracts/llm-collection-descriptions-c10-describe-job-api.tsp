import "@typespec/http";
using Http;

@service(#{ title: "Archon Search Describe Job API (description job kind)" })
namespace DescribeJobApi;

// C10 — `GET /jobs/{id}` and `DELETE /jobs/{id}` for the new description job kind.
//
// HTTP/API seam. `GET /openapi.json` stays the authoritative contract; this file models only what a
// description job adds to the existing job response (`JobResponse`, schemas.py line 542, built by
// `job_to_dict`). The job is created RUNNING by `POST /collections/{name}/describe` (C6, 202 with the
// `JobResponse`) and polled like the reindex and reindex-metadata jobs; `collection describe --wait`
// polls it. The route runs it as a tracked background task, like the community rebuild and the
// metadata reindex: the scheduler promotes only export, import and migration jobs, so there is no
// scheduler dispatch branch. `describe --all` is a CLI loop over the caller's namespace (the list route
// is namespace-scoped), one job per collection; there is no server-side `--all`.
// A job for a collection already being generated (by an ingest or an earlier run) does not start a
// second generation (key: collection name).
//
// Wire vocabulary:
//   - `status` is the existing UPPERCASE `JobStatus` (types.py lines 13-21). `job_to_dict` emits
//     `status.value` and the CLI's `_poll_job` compares against `DONE`. A description job is created
//     RUNNING and ends `DONE` or `FAILED`; `QUEUED`, `PENDING`, `FAILED_EXPIRED`, `CANCELLED` and
//     `CANCELLING` are listed because every job response shares the field. `DONE` means the run
//     settled; the settled outcome is in `result`. `FAILED` means the job itself broke (an unexpected
//     error, or a restart that found it RUNNING).
//   - `job_type` is `description` (a new branch in `_job_type`, jobs/model.py line 40). `kind` stays
//     null: `JobKind` (types.py line 148) has no description member, so a description job persists
//     only the base `IngestJob` fields and an older server that finds it in the jobs file loads it as
//     a plain ingest job instead of resetting the whole file (jobs/store.py lines 626-627 and 671). The
//     list filter `?kind=description` works through `_KIND_TYPE_MAP` (routes_jobs.py line 939), which
//     already holds `ingest` and `reindex` without matching `JobKind` members.
//   - `DELETE /jobs/{id}` on a description job answers 409: the generation is shared by every caller
//     of the collection (single-flight) and cannot be detached, so the job is not cancellable, like
//     a sync job (routes_jobs.py line 1119). A finished job answers 200 as for every kind.

/// Every job response shares this field; a description job is created RUNNING and ends DONE or FAILED.
enum JobStatusValue {
  PENDING,
  QUEUED,
  RUNNING,
  DONE,
  FAILED,
  FAILED_EXPIRED,
  CANCELLED,
  CANCELLING,
}

enum DescriptionJobOutcome {
  generated,
  /// The generation settled as `failed` (unusable output, no unrestricted chunks, call failed, save
  /// failed, including a lock timeout while recording the failure).
  failed,
  /// A generation for this collection was already running; this job did not start another.
  alreadyRunning,
  /// The collection became manual between the route's 409 check and the run: nothing was generated.
  refusedManual,
  /// A manual set, a clear or another change landed while the job ran (C3 `refusedStale`): the
  /// generated text or the recorded failure was discarded and the stored state is the other writer's.
  /// A server shutdown during the run is not an outcome: the job ends `FAILED` ("the server is
  /// shutting down").
  superseded,
}

/// Value of the `result` field of a finished description job.
model DescriptionJobResult {
  outcome: DescriptionJobOutcome;
  /// The collection's `description_status` as read when the job settled. Null when nothing is stored
  /// (a first attempt whose failure could not be saved, or a manual-refused empty row); `pending`
  /// when `alreadyRunning` and the other generation is still in flight.
  description_status: "ok" | "failed" | "pending" | null;
}

model DescriptionJob {
  job_id: string;
  status: JobStatusValue;
  created_at: string;
  updated_at: string;
  `namespace`: string;
  collection: string;
  job_type: "description";
  /// Always null: a description job has no `JobKind` (see above).
  kind: string | null;
  result: DescriptionJobResult | null;
  error: string | null;
}

@error
model NotFound {
  @statusCode statusCode: 404;
  detail: string;
}

@error
model NotCancellable {
  @statusCode statusCode: 409;
  detail: string;
}

@route("/jobs")
interface Jobs {
  @route("/{id}")
  @get get(@path id: string): DescriptionJob | NotFound;

  /// Never cancels a description job: 409. A finished job answers 200 with its body.
  @route("/{id}")
  @delete cancel(@path id: string): DescriptionJob | NotFound | NotCancellable;
}

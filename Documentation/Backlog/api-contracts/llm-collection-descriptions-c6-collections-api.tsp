import "@typespec/http";
using Http;

@service(#{ title: "Archon Search Collections API (description fields)" })
namespace CollectionsDescriptionApi;

// C6 — `/collections` routes, description fields and actions.
//
// HTTP/API seam. `GET /openapi.json` is the authoritative HTTP contract for this project, so this
// file models only what this feature changes on the collection routes, not the whole route set.
// Every change is additive on the wire; the contract changes are recorded in `BREAKING.md` because
// descriptions are now off by default.
//
// What changes:
//   - `CollectionSummary` and `CollectionDetail` gain `description_source` and `description_status`.
//     Stored values are `ok`, `failed` or empty; `pending` is overlaid from memory while a
//     generation runs. A collection with a description from before this feature reads as source
//     `generated`, status `ok`; an empty description (never attempted, or feature off) reads null.
//   - `PUT /collections/{name}/description` sets a manual description (source `manual`, status `ok`)
//     and answers 200 with the updated `CollectionDetail`.
//     `DELETE /collections/{name}/description` clears it (resets source, status, last-described and
//     the baseline; no generation starts) and answers 200 with the updated `CollectionDetail`.
//     Authorization: the key whose namespace owns the
//     collection, no admin; the check is "the collection exists in the caller's namespace", NOT the
//     config-path rule of PATCH, so meta-only collections work. Manual text is one line, at most 600
//     characters (any control character, tab and Unicode line or paragraph separator counts as a line
//     break), and is rejected with 422, never trimmed.
//   - `POST /collections/{name}/describe` forces a fresh generation (it skips the 20% rule and
//     regenerates a generated, failed or empty description) as a job and answers 202 with the same
//     `JobResponse` the sibling job routes return (`reindex`, `reindex-metadata`, community rebuild:
//     `JobResponse(**job_to_dict(job))`, routes_collections.py lines 547 and 1117, and the same dict
//     body at line 1611); the job is
//     polled through `GET /jobs/{id}` (C10). 409 covers exactly two cases, told apart by `detail`:
//     the collection has a MANUAL description (the message says to clear it first), and
//     descriptions are not configured (`[description]` absent or `enabled = false`; the message says
//     how to set a provider up, so the CLI can print it and exit 1 without reading `/status`). A
//     provider that is configured but down is NOT a 409: the job runs and settles `failed`.
//   - `PATCH /collections/{name}` is unchanged (it carries no `description` field).

enum DescriptionSource {
  generated,
  manual,
}

enum DescriptionStatus {
  ok,
  failed,
  /// Held in memory only; overlaid while a generation is in flight.
  pending,
}

model ErrorDetail {
  detail: string;
}

model CollectionSummary {
  name: string;
  path: string;
  description: string;
  description_source: DescriptionSource | null;
  description_status: DescriptionStatus | null;
  doc_count: int32;
  chunk_count: int32;
  `namespace`: string;
  status: string;
  active_embedding_model: string;
  needs_reindex: boolean;
}

model CollectionDetail {
  ...CollectionSummary;
  pending_embedding_model: string | null;
  reindex_job_id: string | null;
  centroid_present: boolean;
  last_indexed: string | null;
  acl_protected_count: int32;
  acl_open_count: int32;
  default_ttl_seconds: int32 | null;
  schema_version: int32;
}

/// Unchanged by this feature (no `description` field).
model PatchCollectionBody {
  embedding_model?: string;
  default_ttl_seconds?: int32 | null;
}

/// One line of plain text, at most 600 characters; otherwise 422.
model SetDescriptionBody {
  description: string;
}

/// The job body every job route returns (schemas.py line 542). A description job has `job_type`
/// `description` and `kind` null; C10 narrows `status` and `result`.
model JobResponse {
  job_id: string;
  status: string;
  created_at: string;
  updated_at: string;
  result: string | Record<unknown> | null;
  error: string | null;
  `namespace`: string;
  progress: Record<unknown> | null;
  source: string;
  source_path: string;
  collection: string;
  retry_count: int32;
  output_path: string | null;
  archive_path: string | null;
  kind: string | null;
  migrations_applied: string[] | null;
  backup_confirmed: boolean | null;
  job_type: string;
}

model DescribeAccepted {
  @statusCode statusCode: 202;
  @body body: JobResponse;
}

@error
model NotFound {
  @statusCode statusCode: 404;
  detail: string;
}

/// On `describe`: a manual description ("clear it first") or descriptions not configured (the setup
/// hint). On `PATCH`: the existing conflicts. Told apart by `detail`.
@error
model Conflict {
  @statusCode statusCode: 409;
  detail: string;
}

@error
model Unprocessable {
  @statusCode statusCode: 422;
  detail: string;
}

@error
model StoreBusy {
  @statusCode statusCode: 503;
  detail: string;
}

@route("/collections")
interface Collections {
  @get list(): CollectionSummary[];

  @route("/{name}")
  @get get(@path name: string): CollectionDetail | NotFound;

  @route("/{name}")
  @patch patch(@path name: string, @body body: PatchCollectionBody): CollectionDetail | NotFound | Conflict | Unprocessable | StoreBusy;

  @route("/{name}/description")
  @put setDescription(@path name: string, @body body: SetDescriptionBody): CollectionDetail | NotFound | Unprocessable | StoreBusy;

  @route("/{name}/description")
  @delete clearDescription(@path name: string): CollectionDetail | NotFound | StoreBusy;

  @route("/{name}/describe")
  @post describe(@path name: string): DescribeAccepted | NotFound | Conflict | StoreBusy;
}

import "@typespec/http";
using Http;

// C1 — POST /graph/{collection}/rebuild-communities (CLI <-> server seam).
// Enqueues an async Leiden community rebuild for ONE collection in the caller's
// namespace (derived from the Bearer token, NOT a path/query/body param).
// Serialised server-side by a dedicated per-(namespace, collection) community-
// rebuild lock, held in a MODULE-LEVEL REGISTRY in community_builder.py and
// resolved from within CommunityBuilder.build (NOT an attribute of the
// CommunityBuilder instance — CommunityBuilder is constructed fresh at every
// call site, so an instance-attribute lock would serialise nothing; NOT the
// chunk-table ingest lock either — see the team plan's C3/Q2/C2-1 for why
// holding the ingest lock self-deadlocks against update_collection_meta, and
// why the lock must be module-level to serialise across the route's task and
// MaintenanceLoop's rebuild path, which each construct their own
// CommunityBuilder). Returns 202 + a job that is then tracked via
// GET /jobs/{id} ONLY — NOT /status. A running/DONE rebuild job cannot appear
// in /status: StatusResponse (server/schemas.py) has no generic jobs list,
// only failed_expired_ingest_count, which counts only FAILED_EXPIRED
// IngestJob instances. Mirrors POST /collections/{name}/migrate.
//
// IC-5 correction: the route returns job_to_dict(job) directly, i.e. the FULL
// serialised job in JobResponse shape (server/schemas.py, class JobResponse),
// not a trimmed subset — matching how POST /collections/{name}/migrate returns
// job_to_dict(running_job) (routes_collections.py, function migrate_collection).
// JobResponse is mirrored below field-for-field (collection/source default to
// "" / "user"; result/error/progress/output_path/archive_path/kind/
// migrations_applied/backup_confirmed are all nullable and unused by this job
// type, present only because they are shared IngestJob-base/JobResponse
// fields). M2 correction: JobResponse.result on the Python model
// (server/schemas.py, class JobResponse) is typed `str | dict | None`, not
// `dict | None` — the mirror below now includes the `string` member so the
// "field-for-field mirror" claim holds.
//
// C2-7 mirror-drift note: the JobResponse model below is a POINT-IN-TIME,
// field-for-field mirror of server/schemas.py's JobResponse. TypeSpec cannot
// $ref a Python model, so this duplication is a deliberate, acknowledged
// snapshot — if server/schemas.py's JobResponse changes, this model must be
// manually regenerated to match.
@service(#{ title: "Archon Search Graph Community Rebuild API" })
namespace ArchonSearchGraphRebuildApi;

// Mirrors server/schemas.py's JobResponse class exactly, since the route
// returns job_to_dict(job) unmodified — see IC-5 above.
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

@route("/graph")
interface GraphCommunityRebuild {
  // 401 unauthorized, 404 collection-not-found, 409 rebuild-already-in-progress,
  // 422 graph-not-enabled, 500 invalid-namespace-sentinel (middleware_auth.py's
  // dispatch path returns 500 on INVALID_NAMESPACE_SENTINEL).
  // C2-9: this route relies SOLELY on APIKeyMiddleware for auth. It must NOT
  // copy routes_graph.py's existing in-handler ?token= validation branch (used
  // by the graph-viewer HTML route only), which returns 401 on the same
  // sentinel — that would turn this route's 500 into a 401.
  @route("/{collection}/rebuild-communities")
  @post
  rebuildCommunities(@path collection: string): {
    @statusCode statusCode: 202;
    @body body: JobResponse;
  } | {
    @statusCode statusCode: 401 | 404 | 409 | 422 | 500;
    @body error: ErrorDetail;
  };
}

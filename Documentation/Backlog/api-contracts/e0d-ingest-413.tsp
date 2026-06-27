import "@typespec/http";

using TypeSpec.Http;

@service(#{title: "Archon Search — Ingest Size Guard"})
namespace IngestSizeGuardApi;

model ErrorDetail {
  detail: string;
}

model JobResponse {
  job_id: string;
  status: string;
  kind: string;
  created_at: string;
  updated_at: string;
}

model IngestRequest {
  path?: string;
  /** Inline document list; when present, no filesystem path exists and the size guard is skipped. */
  documents?: Record<unknown>[];
  collection: string;
  ingested_by?: string;
}

@doc("File exceeds [ingest].max_file_mb; body.detail names the file size, limit, and config key.")
model FileTooLargeResponse {
  @statusCode statusCode: 413;
  @body body: ErrorDetail;
}

@route("/ingest")
interface IngestRoute {
  @post
  @doc("Enqueue an ingest job. Returns 202 on success. Returns 413 when a single-file path exceeds [ingest].max_file_mb (skipped for directory paths and documents payload).")
  ingest(@body body: IngestRequest):
    | {
        @statusCode statusCode: 202;
        @body body: JobResponse;
      }
    | {
        @statusCode statusCode: 400;
        @body body: ErrorDetail;
      }
    | {
        @statusCode statusCode: 401;
        @body body: ErrorDetail;
      }
    | FileTooLargeResponse
    | {
        @statusCode statusCode: 503;
        @body body: ErrorDetail;
      };
}

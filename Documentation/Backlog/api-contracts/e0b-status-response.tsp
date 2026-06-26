import "@typespec/http";
using Http;

@service(#{ title: "Status API — expansion key availability (E0b L6)" })
namespace StatusResponseContract;

/** L6: HyDE expansion availability detail nested under GET /status. */
model HydeStatusDetail {
  /** True when ANTHROPIC_API_KEY is present in the server environment at startup. */
  key_available: boolean;
}

/** L6: RAG Fusion expansion availability detail nested under GET /status. */
model RagFusionStatusDetail {
  /** True when ANTHROPIC_API_KEY is present in the server environment at startup. */
  key_available: boolean;
}

/** Partial StatusResponse showing only E0b L6 additions. */
model StatusResponse {
  running: boolean;
  pid: int32;
  version: string;
  /** Null when [hyde] is not configured. */
  hyde: HydeStatusDetail | null;
  /** Null when [rag_fusion] is not configured. */
  rag_fusion: RagFusionStatusDetail | null;
  /** L10: count of IngestJobs that aged past retry_max_age_hours without completing. */
  failed_expired_ingest_count: int32;
}

@route("/status")
interface Status {
  @get status(): StatusResponse;
}

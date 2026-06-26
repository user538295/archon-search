import "@typespec/http";
using Http;

@service(#{ title: "Status API — expansion key availability (E0b L6)" })
namespace StatusResponseContract;

/** L6: HyDE expansion availability detail nested under GET /status. */
model HydeStatusDetail {
  /** True when ANTHROPIC_API_KEY is present in os.environ at the time GET /status is called (checked at call time, not cached at startup). */
  key_available: boolean;
}

/** L6: RAG Fusion expansion availability detail nested under GET /status. */
model RagFusionStatusDetail {
  /** True when ANTHROPIC_API_KEY is present in os.environ at the time GET /status is called (checked at call time, not cached at startup). */
  key_available: boolean;
}

/**
 * E0b additions to StatusResponse (L6 + L10).
 * This is a partial model — only the fields added by E0b are shown.
 * The full StatusResponse lives in archon_search/server/schemas.py.
 * Existing fields (running, pid, version, backup, maintenance, model_validation, mcp, etc.) are unchanged.
 */
model StatusResponseE0bExtension {
  /** Null when [hyde] is not configured. */
  hyde: HydeStatusDetail | null;
  /** Null when [rag_fusion] is not configured. */
  rag_fusion: RagFusionStatusDetail | null;
  /** L10: count of IngestJobs in FAILED_EXPIRED state (aged past retry_max_age_hours OR exhausted retry_max_attempts). */
  failed_expired_ingest_count: int32;
}

@route("/status")
interface Status {
  @get status(): StatusResponseE0bExtension;
}

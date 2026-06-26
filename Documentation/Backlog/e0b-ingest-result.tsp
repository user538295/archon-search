namespace IngestResultContract;

/** Internal seam: IngestResult additions for E0b L14. */
model IngestResult {
  doc_id: string;
  chunks_created: int32;
  status: string;
  error: string | null;
  needs_recompute: boolean;
  /** L14: non-fatal warnings from ingest (e.g. oversized ACL sidecar skipped). */
  warnings: string[];
}

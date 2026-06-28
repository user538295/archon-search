namespace IngestResultContract;

/**
 * E0b delta — shows only the field ADDED by E0b L14.
 * This is an internal seam contract (--no-emit, validation only).
 * The full IngestResult dataclass lives in archon_search/_types.py.
 * Existing fields (doc_id, chunks_created, status, error, needs_recompute, etc.) are unchanged.
 * Note: IngestResultSchema (mcp_schemas.py) intentionally excludes `needs_recompute`
 * — this contract describes the domain entity, not the MCP-facing adapter.
 */
model IngestResultE0bDelta {
  /** L14: non-fatal warnings from ingest (e.g. oversized ACL sidecar > 64 KB skipped). Default: empty list. */
  warnings: string[];
}

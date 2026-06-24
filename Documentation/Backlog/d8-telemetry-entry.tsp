namespace TelemetryEntryContract;

/**
 * New field added to TelemetryEntry (archon_search/telemetry/entry.py).
 * Also added to DOCUMENTED_SCHEMA_FIELDS frozenset.
 * Default False — backward-compatible with JSONL files written before D8.
 */
model TelemetryEntryDocIdsHashedField {
  /**
   * True when result_doc_ids in this entry are HMAC-SHA256 hashed (hash_doc_ids=true and
   * a valid salt was available at write time).
   * False in all other cases (config off, no result_doc_ids, salt read error).
   */
  doc_ids_hashed: boolean;
}

/**
 * The from_search_tool_result factory is updated to accept an optional hasher.
 * doc_id_hasher absent/null → raw doc_ids stored, doc_ids_hashed=False.
 * doc_id_hasher provided → each doc_id hashed before storage, doc_ids_hashed=True.
 * Only from_search_tool_result is updated; from_explain_result, from_error,
 * from_route_response, and from_search_multi_result are unchanged.
 */
interface TelemetryEntryFactory {
  fromSearchToolResult(
    endpoint: string,
    collection: string,
    resultDocIds: string[],
    latencyMs: float64,
    docIdHasherPresent: boolean,
  ): TelemetryEntryResult;
}

/** The relevant output fields of TelemetryEntry after D8 changes. */
model TelemetryEntryResult {
  resultDocIds: string[] | null;

  /** Set by the factory: True if docIdHasher was provided and applied. */
  docIdsHashed: boolean;
}

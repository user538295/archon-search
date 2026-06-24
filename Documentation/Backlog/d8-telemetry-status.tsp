namespace TelemetryStatusContract;

/**
 * New sub-object added to StatusResponse (archon_search/server/schemas.py).
 * Present whenever telemetry is enabled; null when telemetry is disabled.
 * Surfaced by GET /status under the key "telemetry".
 */
model TelemetryStatusDetail {
  /** Mirrors TelemetryConfig.enabled — true when telemetry writes JSONL logs. */
  enabled: boolean;

  /**
   * True when hash_doc_ids=true in config AND the salt file was loaded successfully at startup.
   * False when config is off, or when the salt file was unreadable (fallback mode).
   */
  hash_doc_ids_enabled: boolean;
}

/**
 * Extension of the existing StatusResponse — D8 additive field.
 * GET /status (routes_status.py) returns this alongside existing fields.
 */
model StatusResponseExtension {
  /** Null when telemetry is disabled; TelemetryStatusDetail when enabled. */
  telemetry: TelemetryStatusDetail | null;
}

interface StatusBuilder {
  /**
   * Builds TelemetryStatusDetail from config and app state (app.state.salt_bytes).
   * Returns null when telemetry.enabled=false.
   * hash_doc_ids_enabled is true only when BOTH config flag is true AND salt is loaded
   * (app.state.salt_bytes is not null) — guards against the read_error fallback case.
   */
  buildTelemetryStatus(config: TelemetryConfig): TelemetryStatusDetail | null;
}

/** Relevant subset of TelemetryConfig consumed by StatusBuilder. */
model TelemetryConfig {
  enabled: boolean;
  hash_doc_ids: boolean;
}

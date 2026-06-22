namespace ReadinessContract;

/** Extended status values for readiness checks (D6 adds PENDING and WARN). */
enum CheckStatus {
  ok: "ok",
  fail: "fail",
  /** Validation is in progress; treat as equivalent to healthy for routing purposes. */
  pending: "pending",
  /** Validation completed with warnings (e.g. provider fallback to CPU). */
  warn: "warn",
}

/** Extended readiness checks including model validation state (D6). */
model ReadinessChecks {
  /** Storage (LanceDB) connectivity. */
  storage: CheckStatus;

  /**
   * Model validation state. PENDING while the background task has not yet completed.
   * OK when both models loaded cleanly. WARN when a provider fallback occurred.
   * FAIL when a model could not be loaded at all.
   * ready: bool is NOT gated on this field — the server always reports ready when storage is OK.
   * IMPLEMENTATION NOTE: the Python field MUST default to PENDING
   * (models: CheckStatus = CheckStatus.PENDING) — routes_ready.py constructs
   * ReadinessChecks(storage=...) with no models kwarg; a missing default crashes at runtime.
   */
  models: CheckStatus;
}

model ReadinessResponse {
  /** True when the server can serve requests (storage OK). Not gated on model validation. */
  ready: boolean;
  checks: ReadinessChecks;
}

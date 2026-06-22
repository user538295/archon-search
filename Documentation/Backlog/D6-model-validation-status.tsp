namespace ModelValidationContract;

/** Result produced by validate_models_async(); stored on app.state.model_validation. */
model ModelValidationResult {
  /** True when the embedder loaded successfully; null while validation is pending. */
  embedder_ok: boolean | null;

  /** True when the reranker loaded successfully; null while pending, true when reranker is disabled. */
  reranker_ok: boolean | null;

  /** Human-readable warning strings emitted during validation (provider missing, timeout, etc.). */
  provider_warnings: string[];

  /** ISO-8601 UTC timestamp of when validation completed; null while pending. */
  validated_at: utcDateTime | null;
}

/** API-facing mirror of ModelValidationResult, nested under GET /status response. */
model ModelValidationStatus {
  embedder_ok: boolean | null;
  reranker_ok: boolean | null;
  provider_warnings: string[];
  validated_at: utcDateTime | null;
}

interface ModelValidator {
  /** Run model validation asynchronously and return the result. Never raises.
   * @param timeoutSeconds Maximum seconds before both probes are marked failed.
   * @param embedderIsWarm When true, skip the embedder probe (caller confirms it is already exercised).
   *        NOTE: eager_load_embedders does NOT set this — it warms per-collection cache instances,
   *        not the global default embedder. Pass app.state.embedder.is_warm from the lifespan.
   */
  validateModels(timeoutSeconds: float64, embedderIsWarm?: boolean): ModelValidationResult;
}

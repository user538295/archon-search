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

/** Subset of SearchConfig consumed by validation (passed opaquely as the Python `config` arg). */
model ModelValidationConfig {
  /** Dense embedding model name. */
  embedding_model: string;

  /** Reranker model name; empty string disables the reranker probe. */
  reranker_model: string;

  /** ONNX execution providers to validate (single key covers both models in v1). */
  providers: string[];
}

interface ModelValidator {
  /** Run model validation asynchronously and return the result. Never raises.
   * Python signature: validate_models_async(config, timeout_seconds, embedder_is_warm=False).
   * Naming: TypeSpec uses camelCase; the Python implementation uses snake_case per PEP 8.
   * @param config Validation inputs (embedding_model, reranker_model, providers) — the Python
   *        SearchConfig is passed opaquely; ModelValidationConfig documents the fields consumed.
   * @param timeoutSeconds Maximum WHOLE seconds before both probes are marked failed
   *        (int32 — mirrors SearchConfig.validation_timeout_seconds: int, default 60).
   * @param embedderIsWarm When true, skip the embedder probe (caller confirms it is already exercised).
   *        NOTE: eager_load_embedders does NOT set this — it warms per-collection cache instances,
   *        not the global default embedder. Pass app.state.embedder.is_warm from the lifespan.
   */
  validateModels(
    config: ModelValidationConfig,
    timeoutSeconds: int32,
    embedderIsWarm?: boolean,
  ): ModelValidationResult;
}

// C4 — Config surface + status schema (INTERNAL DATA SEAM)
// Config dataclasses in the Frameworks & Drivers layer
// (archon_search/config.py), loaded from ~/.archon-search/archon-search.toml,
// PLUS the GET /status ModelValidationStatus/ModelValidationResult schema
// extension that surfaces the llama-server reachability probe.
// This feature is additive: every new field carries a default, so no config
// migration tooling is involved (none exists).

namespace ArchonSearch.Config;

// New module-level default (mirrors OLLAMA_BASE_URL_DEFAULT).
// LLAMA_CPP_BASE_URL_DEFAULT = "http://localhost:8080"
alias LlamaCppBaseUrlDefault = "http://localhost:8080";

// New module-level default (archon_search/config.py:30,
// OLLAMA_BASE_URL_DEFAULT = "http://localhost:11434"). Applied below to
// GraphConfig.ollamaBaseUrl; HyDEConfig.ollamaBaseUrl / RAGFusionConfig.
// ollamaBaseUrl are pre-existing fields already defaulted in the real
// dataclasses (config.py:40, :51) and are not part of this feature's diff.
alias OllamaBaseUrlDefault = "http://localhost:11434";

// Provider allowlist (config.py:_VALID_PROVIDERS, a 5-member frozenset once
// this feature adds "llama_cpp"). `_validate_provider_config()` validates
// `GraphConfig.provider` against this SAME set, "same pattern as
// `[hyde]`/`[rag_fusion]` validators" (team-plan.md Backend "Done when" bullet
// + Scenario S16; tasks.md BE-4) — there is no narrower enrichment-only
// allowlist. Also used for HyDE/RAG-Fusion query-expansion provider
// selection.
// SYNCHRONIZATION: this must be kept identical to the ProviderKind
// definition in llama-cpp-provider-factory.tsp — these four files do not use
// cross-file TypeSpec imports, so the two copies are synchronized by
// convention/comment, not by import.
// Registry shape (documentation-only — no separate model needed): this
// enum's declared member order IS the `_PROVIDER_REGISTRY` ordered
// sequence referenced by team-plan.md C4 ("the registry key shape ... is
// defined here as a C4 invariant"). `_VALID_PROVIDERS` (a frozenset) is
// derived from this ordered sequence. `_PROVIDER_EXTRA.keys()` MUST be a
// SUBSET of these 5 names, never equal to them — some providers (e.g.
// `llama_cpp`, `claude_cli`) have no pip extra at all, while others
// (`ollama`, `openai`) do. `tests/test_provider_registry_sync.py` (BE-10)
// must assert both: registry-derived equality for `_VALID_PROVIDERS`/the
// wizard set/TOML writer branches, and strict-subset (not equality) for
// `_PROVIDER_EXTRA.keys()`.
enum ProviderKind {
  anthropic,
  ollama,
  openai,
  claude_cli,
  llama_cpp,
}

model HyDEConfig {
  enabled: boolean;
  "model": string;
  timeoutSeconds: float64;
  maxRequestsPerMinute: int32;
  provider: ProviderKind = ProviderKind.anthropic;
  ollamaBaseUrl: string;
  // New — additive.
  llamaCppBaseUrl: string = LlamaCppBaseUrlDefault;
}

model RAGFusionConfig {
  enabled: boolean;
  "model": string;
  timeoutSeconds: float64;
  maxRequestsPerMinute: int32;
  numQueries: int32;
  provider: ProviderKind = ProviderKind.anthropic;
  ollamaBaseUrl: string;
  // New — additive.
  llamaCppBaseUrl: string = LlamaCppBaseUrlDefault;
}

model GraphConfig {
  enabled: boolean;
  // New — additive. Discrete enrichment provider selector (Q7=A resolved:
  // no "provider:model" prefix parser — a discrete field was selected
  // instead). Defaults to null: enrichment stays disabled unless an
  // operator explicitly sets it, preserving the air-gap guarantee for
  // operators who configure query-expansion but not enrichment.
  // Typed as the SAME 5-member ProviderKind used by HyDE/RAG-Fusion —
  // `_validate_provider_config()` validates it against `_VALID_PROVIDERS`,
  // "same pattern as `[hyde]`/`[rag_fusion]` validators" (team-plan.md C3;
  // tasks.md BE-4). Neither document restricts `claude_cli` here, so config
  // validation accepts it for `[graph] provider`.
  // UNRESOLVED GAP (flagged, not decided here): `claude_cli` has no v1
  // enrichment client — `ClaudeCLIEnrichmentClient` is deferred (subprocess-
  // based, no HTTP endpoint; see llama-cpp-provider-factory.tsp). So
  // `[graph] provider = "claude_cli"` passes config validation but the
  // enrichment factory (C3) has no concrete client to build for it. Neither
  // the plan nor the tasks file resolves what the factory should do in that
  // case (e.g. return null with a WARNING, or raise) — this needs a team
  // decision before BE-4/BE-7 are implemented. See the corresponding note
  // on `BuildEnrichmentClientInput.provider` in llama-cpp-provider-factory.tsp.
  provider: ProviderKind | null = null;
  // Always a bare model name (Q7=A resolved) — never a "provider:model"
  // prefixed string.
  extractionModel: string | null = null;
  // New — additive. The enrichment client's timeout/rate-limit/token
  // fields that AnthropicEnrichmentClient already reads but GraphConfig
  // never defined. They MUST be added before any enrichment client can be
  // constructed against a real GraphConfig.
  //
  // Defaults are REQUIRED, not optional polish: GraphConfig is a real
  // Python @dataclass (config.py) where every existing field already
  // carries a default — appending these three as non-default fields would
  // raise `TypeError: non-default argument follows default argument` at
  // class-definition time. This is a technical blocker for BE-4, verified
  // against config.py, not merely a documentation gap.
  //
  // extractionTimeoutSeconds matches existing HyDE/RAG-Fusion precedent
  // exactly (HyDEConfig.timeoutSeconds / RAGFusionConfig.timeoutSeconds =
  // 10.0, config.py:37/47).
  extractionTimeoutSeconds: float64 = 10.0;
  // extractionRateLimitRpm matches existing HyDE/RAG-Fusion precedent
  // exactly (HyDEConfig.maxRequestsPerMinute / RAGFusionConfig.
  // maxRequestsPerMinute = 60, config.py:38/48).
  extractionRateLimitRpm: int32 = 60;
  // extractionTokenBudget: PROPOSED default — no existing precedent value
  // anywhere in the codebase for this concept. 4096 is offered as a
  // reasonable common LLM context-budget round number, but this needs
  // explicit team confirmation before BE-4 locks it in — unlike the two
  // fields above, this is not grounded in an existing config value.
  extractionTokenBudget: int32 = 4096;
  // New — additive. Base URL for a local (llama_cpp) enrichment backend.
  // Q2 (resolved): discrete field selected over a "provider:model@base_url"
  // encoding — see the Data table in the team plan ("resolves Q2 — discrete
  // field").
  llamaCppBaseUrl: string = LlamaCppBaseUrlDefault;
  // New — additive. Base URL for the Ollama enrichment backend; default
  // mirrors OLLAMA_BASE_URL_DEFAULT. Distinct from HyDEConfig.ollamaBaseUrl
  // / RAGFusionConfig.ollamaBaseUrl — same default, different config object.
  ollamaBaseUrl: string = OllamaBaseUrlDefault;
}

// --- Status schema extension (C4, second half) --------------------------
// Extends D6-model-validation-status.tsp's `ModelValidationStatus` /
// `ModelValidationResult` (Documentation/Completed/D6-model-validation-
// status.tsp) with the non-blocking llama-server reachability probe.
// C4 text lists FOUR required schema changes; D6's precedent models BOTH
// types separately (the internal dataclass and its API-facing Pydantic
// mirror) — this file mirrors that same relationship for `llama_cpp_ok`.
// Field carries the same snake_case wire convention as the base contract
// (see D6's documented camelCase-vs-snake_case note): this is a wire-facing
// GET /status model, unlike the rest of this file's camelCase internal-seam
// fields.

// Internal dataclass produced by validate_models_async()
// (model_validation.py:33-44); stored on app.state.model_validation.
model ModelValidationResult {
  embedder_ok: boolean | null;
  reranker_ok: boolean | null;
  provider_warnings: string[];
  validated_at: utcDateTime | null;
  // New — additive. Result of the non-blocking llama-server GET /v1/models
  // probe. null = probe pending, true = reachable, false = unreachable.
  // Never blocks boot; must not feed the GET /ready checks.models FAIL
  // condition (S25).
  llama_cpp_ok: boolean | null = null;
}

// API-facing Pydantic mirror of ModelValidationResult (schemas.py:169),
// nested under the GET /status response — same relationship as D6's
// ModelValidationResult / ModelValidationStatus pair.
model ModelValidationStatus {
  embedder_ok: boolean | null;
  reranker_ok: boolean | null;
  provider_warnings: string[];
  validated_at: utcDateTime | null;
  llama_cpp_ok: boolean | null = null;
}

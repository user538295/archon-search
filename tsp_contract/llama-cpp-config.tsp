// C4 — Config surface (INTERNAL DATA SEAM)
// Config dataclasses in the Frameworks & Drivers layer
// (archon_search/config.py), loaded from ~/.archon-search/archon-search.toml.
// This feature is additive: every new field carries a default, so no config
// migration tooling is involved (none exists).

namespace ArchonSearch.Config;

// New module-level default (mirrors OLLAMA_BASE_URL_DEFAULT).
// LLAMA_CPP_BASE_URL_DEFAULT = "http://localhost:8080"
alias LlamaCppBaseUrlDefault = "http://localhost:8080";

model HyDEConfig {
  enabled: boolean;
  "model": string;
  timeoutSeconds: float64;
  maxRequestsPerMinute: int32;
  provider: string = "anthropic";
  ollamaBaseUrl: string;
  // New — additive.
  llamaCppBaseUrl: string;
}

model RAGFusionConfig {
  enabled: boolean;
  "model": string;
  timeoutSeconds: float64;
  maxRequestsPerMinute: int32;
  numQueries: int32;
  provider: string = "anthropic";
  ollamaBaseUrl: string;
  // New — additive.
  llamaCppBaseUrl: string;
}

model GraphConfig {
  enabled: boolean;
  // Carries the enrichment provider as a "provider:model" prefix.
  extractionModel: string | null;
  // New — additive. The enrichment client's timeout/rate-limit/token
  // fields that AnthropicEnrichmentClient already reads but GraphConfig
  // never defined. They MUST be added before any enrichment client can be
  // constructed against a real GraphConfig.
  extractionTimeoutSeconds: float64;
  extractionRateLimitRpm: int32;
  extractionTokenBudget: int32;
  // New — additive. Base URL for a local (llama_cpp) enrichment backend.
  // Open question OQ2: this discrete field vs a "provider:model@base_url"
  // encoding — plan selects the discrete field.
  llamaCppBaseUrl: string;
}

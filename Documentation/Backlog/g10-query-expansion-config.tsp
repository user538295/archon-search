namespace G10QueryExpansionConfig;

// Boundary: Entities layer — HyDEConfig and RAGFusionConfig in archon_search/config.py
// Each feature section ([hyde] and [rag_fusion]) carries its own instance of these fields.
// Config is loaded by _apply_toml; ConfigError raised at startup for invalid provider or missing package.
// The missing-package ConfigError fires regardless of whether the feature is enabled —
// validation happens at startup, not first use (prevents misconfiguration from hiding until runtime).
//
// Cross-field invariant (Q5): when provider != "anthropic" and the pre-existing `model` field
// is empty string (the "unset" sentinel), create_app() raises ConfigError at startup.
// Operators must set an explicit model name when using ollama or openai providers.
// The `model` field is NOT added by G10 — it pre-exists on HyDEConfig and RAGFusionConfig;
// G10 only adds the startup guard for non-anthropic providers with an unset model.

// Valid provider identifiers accepted by the provider field.
union ProviderKind {
  anthropic: "anthropic",
  openai: "openai",
  ollama: "ollama",
}

// Fields added to both HyDEConfig and RAGFusionConfig.
// provider defaults to "anthropic" — existing operators need no config change.
// ollama_base_url is repeated per feature section (consistent with existing per-feature config pattern).
model QueryExpansionConfigExtension {
  provider?: ProviderKind = "anthropic";

  // Used only when provider = "ollama". Ignored for anthropic / openai.
  ollama_base_url?: string = "http://localhost:11434";
}

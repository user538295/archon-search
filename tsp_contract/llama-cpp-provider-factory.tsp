// C3 — Provider factory seams (INTERNAL LOGICAL SEAM)
// Composition-root factories in the Interface Adapters layer
// (archon_search/server/app.py). Two factories:
//   1. _build_query_expansion_provider — EXISTS; gains a `llama_cpp` branch
//      and a llama_cpp base-url input (signature widens).
//   2. Enrichment client factory — DOES NOT EXIST TODAY; greenfield.
//      Routes an enrichment-provider selection to a concrete client and is
//      injected into CommunityBuilder / GraphExtractor at their four
//      construction sites.

namespace ArchonSearch.ProviderFactory;

// Provider allowlist (config.py:_VALID_PROVIDERS). Gains `llama_cpp`.
enum ProviderKind {
  anthropic,
  ollama,
  openai,
  claude_cli,
  llama_cpp,
}

// Seam 1 — query-expansion factory input. The `llamaCppBaseUrl` field is
// the additive signature change; both call sites (HyDE, RAG Fusion) must
// pass it through from their config section.
model BuildQueryExpansionProviderInput {
  provider: ProviderKind;
  "model": string;
  ollamaBaseUrl: string;
  llamaCppBaseUrl: string;
}

// Seam 2 — enrichment factory input (greenfield). Provider + model come
// from parsing GraphConfig.extraction_model's "provider:model" prefix;
// baseUrl comes from GraphConfig.llama_cpp_base_url (or the resolved
// default) for the local providers.
model BuildEnrichmentClientInput {
  provider: ProviderKind;
  "model": string;
  baseUrl?: string;
}

// The "provider:model" parser seam — NEW code, contrary to the brief's
// premise that it "already exists". Splits an extraction_model string into
// its provider prefix and model name; falls back to a default provider
// when no recognised prefix is present.
model ParsedExtractionModel {
  provider: ProviderKind;
  "model": string;
}

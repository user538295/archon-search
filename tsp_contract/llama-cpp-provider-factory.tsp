// C3 — Provider factory seams (INTERNAL LOGICAL SEAM)
// Composition-root factories in the Interface Adapters layer
// (archon_search/server/app.py). A `_enrichment_client` local is built in
// the create_app() body and stored as `app.state.enrichment_client`; all
// five construction sites use it via constructor injection or `app.state`
// access (team-plan.md C3 text). Two factories:
//   1. _build_query_expansion_provider — EXISTS; gains a `llama_cpp` branch
//      and a llama_cpp base-url input (signature widens).
//   2. Enrichment client factory — DOES NOT EXIST TODAY; greenfield.
//      Routes an enrichment-provider selection to a concrete client and is
//      injected into CommunityBuilder / GraphExtractor at construction
//      sites including app.py:639, routes_graph.py:99, and
//      maintenance_loop.py:586 (all receive a real, non-None client when
//      configured), and eval/runner.py:1130 (both team-plan.md and
//      tasks.md agree this always receives None — deterministic eval).
//      pipeline.py:3541 is one of the GraphExtractor/CommunityBuilder
//      construction sites; whether it receives a real client or always
//      None is NOT settled here.
//
// pipeline.py:3541 — evidence-backed default (re-investigated this cycle):
// the contradiction is INTERNAL to team-plan.md, not between team-plan.md
// and tasks.md as a prior cycle's comment here claimed. team-plan.md's own
// Scenario S20a text says pipeline.py:3541 receives "a non-None enrichment
// client", but team-plan.md's OWN Backend "Done when" bullet a few dozen
// lines later says "pipeline.py:3541 ... always receive None". tasks.md's
// BE-7 description agrees with the "Done when" bullet (None), and BE-7's
// own S20a integration test lists only app.py:639, routes_graph.py:99, and
// maintenance_loop.py:586 as construction sites to assert non-None —
// pipeline.py:3541 is explicitly NOT among them. That is a 3-to-1 evidence
// split (team-plan.md's "Done when" bullet + tasks.md's BE-7 description +
// BE-7's own test, vs. team-plan.md's own S20a scenario text) in favor of
// pipeline.py:3541 always receiving None. RECOMMENDATION: implement
// pipeline.py:3541 to always receive None — the well-supported reading.
// team-plan.md's S20a text itself still needs a one-line correction by the
// team to remove the internal inconsistency; that edit is out of scope here
// (team-plan.md is off-limits to this contract file).

namespace ArchonSearch.ProviderFactory;

// Provider allowlist (config.py:_VALID_PROVIDERS). Gains `llama_cpp`. Used
// for HyDE/RAG-Fusion query-expansion provider selection, where
// `claude_cli` IS supported. Also the type of GraphConfig.provider
// (llama-cpp-config.tsp) — `_validate_provider_config()` validates
// `[graph] provider` against this SAME `_VALID_PROVIDERS` set, "same
// pattern as `[hyde]`/`[rag_fusion]` validators" (team-plan.md Backend
// "Done when" bullet + Scenario S16; tasks.md BE-4) — there is no narrower
// enrichment-only allowlist.
// SYNCHRONIZATION: this must be kept identical to the ProviderKind
// definition in llama-cpp-config.tsp — these four files do not use
// cross-file TypeSpec imports, so the two copies are synchronized by
// convention/comment, not by import.
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

// Opaque placeholder standing in for the real `QueryExpansionProvider`
// interface, whose shape is defined in the sibling C1 contract
// (llama-cpp-query-expansion-protocol.tsp) — referenced by name only, since
// these four files do not cross-import. Always constructed, never null.
model QueryExpansionProviderRef {}

op buildQueryExpansionProvider(input: BuildQueryExpansionProviderInput): QueryExpansionProviderRef;

// Seam 2 — enrichment factory input (greenfield). Provider comes from the
// discrete GraphConfig.provider field (Q7=A resolved — no "provider:model"
// prefix parser); "model" is GraphConfig.extraction_model, always a bare
// model name (nullable per S28 — provider set but extraction_model absent
// is valid: the factory still builds, enrichment is skipped at the
// AND-gate). `provider` is nullable per S27 — the enrichment factory
// returns a null client when provider=None, matching
// GraphConfig.provider: ProviderKind | null = null in llama-cpp-config.tsp.
//
// `provider` is typed as the full 5-member ProviderKind (matching
// config-side validation, which accepts `claude_cli` for `[graph] provider`
// per the same-pattern-as-HyDE/RAG-Fusion rule — see the ProviderKind
// comment above and the corresponding note on GraphConfig.provider in
// llama-cpp-config.tsp).
//
// UNRESOLVED GAP (flagged, not decided here): `claude_cli` has no v1
// enrichment client — `ClaudeCLIEnrichmentClient` is deferred from v1 scope
// (subprocess-based, no HTTP endpoint; see llama-cpp-enrichment-protocol.tsp
// and team-plan.md's In Scope section). So `provider: claude_cli` passes
// config validation but this factory currently has no concrete client to
// route to for it. Neither team-plan.md nor tasks.md resolves what
// buildEnrichmentClient should do in that case — possible options include
// returning null with a WARNING (silently disabling enrichment, consistent
// with the provider=null case) or raising a ConfigError-equivalent at
// construction time. This needs a team decision before BE-4/BE-7 are
// implemented; do not pick a resolution here.
//
// Base URL resolution: the factory resolves ONE of two distinct
// GraphConfig base-url fields depending on the selected provider —
// llama_cpp -> GraphConfig.llamaCppBaseUrl, ollama -> GraphConfig.
// ollamaBaseUrl, anthropic/openai/claude_cli -> no base URL needed (cloud
// endpoints / subprocess). `baseUrl` below is the single resolved value the
// factory passes to the concrete client constructor, not a pass-through of
// both config fields.
//
// OpenAI note: like the cloud `anthropic` client, `OpenAIEnrichmentClient`
// needs its own hardcoded default base URL (https://api.openai.com/v1) and
// an OPENAI_API_KEY-sourced bearer token to authenticate. This is
// client-implementation detail owned by BE-6, not a GraphConfig-level
// field — so BuildEnrichmentClientInput correctly has no base-URL field for
// the openai provider, matching the "no base URL needed" line above.
model BuildEnrichmentClientInput {
  provider: ProviderKind | null;
  "model": string | null;
  baseUrl?: string;

  // The three enrichment fields that MUST exist (non-optional on
  // GraphConfig) before any client can be constructed — same names/types
  // AND same defaults as the corresponding GraphConfig fields in
  // llama-cpp-config.tsp (kept in sync by convention/comment, not import,
  // per this file's no-cross-import rule).
  //
  // Defaults are required here for the same reason as llama-cpp-config.tsp:
  // GraphConfig is a real Python @dataclass where every existing field
  // already has a default, so these three cannot be added as non-default
  // fields without a class-definition-time TypeError.
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
}

// Opaque placeholder standing in for the real `LLMEnrichmentClient`
// interface, whose shape is defined in the sibling C2 contract
// (llama-cpp-enrichment-protocol.tsp) — referenced by name only, since
// these four files do not cross-import.
model LLMEnrichmentClientRef {}

// Return type of the enrichment factory — nullable, per S27 (provider=null
// -> null client, enrichment silently disabled).
op buildEnrichmentClient(input: BuildEnrichmentClientInput): LLMEnrichmentClientRef | null;

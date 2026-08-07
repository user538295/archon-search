// C2 — LLMEnrichmentClientProtocol (INTERNAL LOGICAL SEAM)
// Consumer-owned protocol in the Use Cases layer
// (archon_search/graph_enrichment_protocol.py). CommunityBuilder and
// GraphExtractor depend on it; every enrichment adapter implements it:
// AnthropicEnrichmentClient (exists, currently orphaned) plus three new
// v1 clients — LlamaCpp / Ollama / OpenAI. Only these four providers
// (anthropic, ollama, openai, llama_cpp) get enrichment parity in v1;
// ClaudeCLIEnrichmentClient is deferred from v1 scope — claude_cli is
// subprocess-based with no HTTP endpoint, so it needs a different approach.
//
// Load-bearing contract — the INVERSE of the query-expansion seam:
// these operations MAY RAISE on failure. The caller (CommunityBuilder,
// GraphExtractor) catches and substitutes null / empty list. Adapters
// must not swallow errors into a degraded-but-silent success.
//
// Per-item-skip vs whole-call-raise (the load-bearing distinguishing rule
// of this contract): each client skips unparseable *individual* items
// (via continue/WARNING) and returns null/[] on an entirely empty/missing
// response. It raises ONLY on transport failure — network error, non-2xx
// status after all retries, or a whole-body JSON parse failure.
//
// HTTP 422 response-format-rejected fallback: HTTP 422 from llama-server is
// the canonical detection trigger for "the server rejected the `json_schema`
// response_format" — not a version check or pre-flight probe. On 422, the
// client falls back to prompt-only extraction with per-item skip-on-parse-
// failure; this is never a whole-call raise (S24a).
//
// Privacy: no raw chunk/community text is ever logged. This is a related
// but distinct invariant this feature introduces for enrichment content
// (S15b) — not the same rule as ADR 05, which covers raw QUERY text (see
// the C1 contract, llama-cpp-query-expansion-protocol.tsp, where the ADR 05
// attribution is correct). Entity names (e.g. source_entity/target_entity)
// are EXCLUDED from this guard: they are already-abstracted graph metadata
// (extracted by spaCy), not raw input content, and the reference
// AnthropicEnrichmentClient correctly logs them (S15b).

namespace ArchonSearch.GraphEnrichment;

// Entity-layer value object (graph_enrichment_protocol.py:LabeledRelationship).
// Deliberately narrowed 3-value subset used ONLY for LLM-labeled
// relationships — distinct from, and not to be confused with, the full
// 9-member archon_search.graph_types.RelationshipType enum (uses, implements,
// depends_on, related_to, synonym_of, calls, imports, defines, inherits)
// used elsewhere in the graph subsystem. Named EnrichmentRelationshipType to
// avoid the name collision.
enum EnrichmentRelationshipType {
  uses,
  // Wire value is "implements" (verified: _VALID_RELATIONSHIP_TYPES =
  // frozenset({"uses", "implements", "depends_on"})). BE-5 deletes
  // llm_enrichment_client.py and hoists this constant to
  // archon_search/enrichment/__init__.py — all four v1 clients
  // (llama_cpp, ollama, openai, anthropic) must import it from that
  // post-move location. llm_enrichment_client.py:28 is cited here only as
  // historical context (the pre-move location where this was verified),
  // and graph_types.RelationshipType.implements = "implements". `implements`
  // is a legal bare TypeSpec `enum` member identifier (see
  // e2i-llm-enrichment-client.tsp, e1a-graphextractor-contract.tsp, and
  // e1a-graphstore-contract.tsp, which all use it bare, with no underscore
  // workaround, inside an `enum` — not e2f-graph-entity-model.tsp/
  // e2g-defref-edge.tsp, which use `implements: "implements"` inside a
  // `union`, a different construct) — the explicit `: "implements"`
  // wire-value pin below is kept only for clarity/consistency with the
  // other two members, even though it defaults to the same value.
  implements: "implements",
  depends_on,
}

model LabeledRelationship {
  sourceEntity: string;
  targetEntity: string;
  relationshipType: EnrichmentRelationshipType;
}

model SummarizeCommunityParams {
  chunkTexts: string[];
  entityNames: string[];
}

model LabelRelationshipsParams {
  // Tuple syntax preserves the arity-2 constraint of the real
  // list[tuple[str, str]] (entity name pairs) — a plain string[][] would
  // allow any length per element.
  entityPairs: [string, string][];
  chunkText: string;
}

// The seam every enrichment client must satisfy.
//
// Rate-limit divergence (deliberate, per-implementation — not a shape
// difference in this interface): `AnthropicEnrichmentClient` calls
// `_check_rate_limit()` at the top of each method below; `LlamaCppEnrichmentClient`
// omits this entirely and ignores `GraphConfig.extractionRateLimitRpm`
// (llama-server is unthrottled) — S26. Neither the plan nor the tasks file
// states whether `OllamaEnrichmentClient`/`OpenAIEnrichmentClient` call
// `_check_rate_limit()` — only the llama_cpp/anthropic contrast is
// specified. This divergence (wherever it applies) is intentional and does
// not change the method signatures below.
interface LLMEnrichmentClient {
  // Returns an abstractive summary, or null. MAY raise on transport failure.
  summarizeCommunity(params: SummarizeCommunityParams): string | null;

  // Returns typed relationship labels. MAY raise on transport failure.
  labelRelationships(params: LabelRelationshipsParams): LabeledRelationship[];
}

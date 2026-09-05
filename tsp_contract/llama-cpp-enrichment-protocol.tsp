// C3 — LLMEnrichmentClientProtocol, NARROWED (INTERNAL LOGICAL SEAM)
// Consumer-owned protocol in the Use Cases layer
// (archon_search/graph_enrichment_protocol.py). CommunityBuilder depends on
// it; every enrichment adapter implements it: AnthropicEnrichmentClient plus
// three v1 clients — LlamaCpp / Ollama / OpenAI. Only these four providers
// (anthropic, ollama, openai, llama_cpp) get enrichment parity; ClaudeCLI is
// deferred (subprocess-based, no HTTP endpoint).
//
// BE-17 / ADR 12: narrowed to community summarisation ONLY. Typed relationship
// labelling is now produced locally by the gliner prose-extraction engine, so
// `label_relationships`, its `LabeledRelationship` DTO, the narrowed
// relationship enum and the llama.cpp JSON-schema response constraint are
// removed from the protocol and from all four adapters. This file declares the
// single surviving method.
//
// Load-bearing contract — the INVERSE of the query-expansion seam:
// the operation MAY RAISE on failure. The caller (CommunityBuilder) catches
// and substitutes null. Adapters must not swallow errors into a
// degraded-but-silent success. A null return means only an entirely
// empty/missing response, never a swallowed transport failure.
//
// Privacy: no raw chunk/community text is ever logged. This is a related but
// distinct invariant from ADR 05, which covers raw QUERY text (see the C1
// contract, llama-cpp-query-expansion-protocol.tsp, where the ADR 05
// attribution is correct). Entity names are already-abstracted graph metadata,
// not raw input content, and the reference AnthropicEnrichmentClient logs them.

namespace ArchonSearch.GraphEnrichment;

model SummarizeCommunityParams {
  chunkTexts: string[];
  entityNames: string[];
}

// The seam every enrichment client must satisfy.
//
// Rate-limit divergence (deliberate, per-implementation — not a shape
// difference in this interface): `AnthropicEnrichmentClient` calls
// `_check_rate_limit()` at the top of the method below; `LlamaCppEnrichmentClient`
// omits this entirely and ignores `GraphConfig.extractionRateLimitRpm`
// (llama-server is unthrottled) — S26. This divergence does not change the
// method signature below.
interface LLMEnrichmentClient {
  // Returns an abstractive summary, or null. MAY raise on transport failure.
  summarizeCommunity(params: SummarizeCommunityParams): string | null;
}

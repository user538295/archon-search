// C2 — LLMEnrichmentClientProtocol (INTERNAL LOGICAL SEAM)
// Consumer-owned protocol in the Use Cases layer
// (archon_search/graph_enrichment_protocol.py). CommunityBuilder and
// GraphExtractor depend on it; every enrichment adapter implements it:
// AnthropicEnrichmentClient (exists, currently orphaned) plus the new
// LlamaCpp / Ollama / OpenAI / ClaudeCLI enrichment clients.
//
// Load-bearing contract — the INVERSE of the query-expansion seam:
// these operations MAY RAISE on failure. The caller (CommunityBuilder,
// GraphExtractor) catches and substitutes null / empty list. Adapters
// must not swallow errors into a degraded-but-silent success.
//
// Privacy: no raw query / chunk text is ever logged — fingerprint only
// (ADR 05 no-raw-query invariant).

namespace ArchonSearch.GraphEnrichment;

// Entity-layer value object (graph_enrichment_protocol.py:LabeledRelationship).
enum RelationshipType {
  uses,
  implements_,
  depends_on,
}

model LabeledRelationship {
  sourceEntity: string;
  targetEntity: string;
  relationshipType: RelationshipType;
}

model SummarizeCommunityParams {
  chunkTexts: string[];
  entityNames: string[];
}

model LabelRelationshipsParams {
  entityPairs: string[][];
  chunkText: string;
}

// The seam every enrichment client must satisfy.
interface LLMEnrichmentClient {
  // Returns an abstractive summary, or null. MAY raise on transport failure.
  summarizeCommunity(params: SummarizeCommunityParams): string | null;

  // Returns typed relationship labels. MAY raise on transport failure.
  labelRelationships(params: LabelRelationshipsParams): LabeledRelationship[];
}

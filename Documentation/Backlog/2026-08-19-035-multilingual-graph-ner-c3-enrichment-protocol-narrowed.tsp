// C3 — LLM enrichment protocol, narrowed to community summarisation (internal logical seam).
//
// Layer edge: Use Cases (protocol, consumer-owned) -> Interface Adapters (four provider
// adapters). This contract supersedes the relationship-labelling half declared in
// `tsp_contract/llama-cpp-enrichment-protocol.tsp`; the summarisation half is unchanged.
//
// After this feature the protocol has exactly one method. `[graph].provider` /
// `[graph].extraction_model` and the timeout / rate-limit / token-budget fields all survive,
// now gating community summarisation alone.

namespace GraphEnrichmentNarrowed;

/// One community's abstractive summary request payload, as it crosses the protocol.
model CommunitySummaryInput {
  /// Representative chunk texts for the community.
  chunkTexts: string[];

  /// Entity names in the community. Produced by the local extraction engine — no longer
  /// by spaCy, and no longer repaired by any name-splitting heuristic on this path.
  entityNames: string[];
}

/// The surviving protocol. Raise-on-failure semantics are unchanged: a provider error
/// propagates to the caller, which treats community summarisation as optional.
interface LLMEnrichmentClient {
  summarizeCommunity(input: CommunitySummaryInput): string;
}

/// Explicitly removed by this feature, recorded here so the deletion is reviewable:
///   - `labelRelationships(entityPairs, chunkText): LabeledRelationship[]`
///   - `LabeledRelationship { sourceEntity, targetEntity, relationshipType }`
///   - `EnrichmentRelationshipType { uses, implements, depends_on }` (the narrowed subset)
///   - the JSON-schema response-format constraint built for the llama.cpp adapter
///   - the per-item-skip vs whole-call-raise rule that only applied to relationship labelling
/// All four provider adapters drop the method; the factory and provider registry are untouched.
model RemovedSurface {
  /// Present only to document the removal; carries no runtime meaning.
  removedMethods: string[];
}

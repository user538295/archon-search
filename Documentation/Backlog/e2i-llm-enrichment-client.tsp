namespace LLMEnrichmentContract;

/** Possible typed relationship labels produced by LLM extraction. */
enum RelationshipKind {
  uses,
  implements,
  depends_on,
  related_to,
}

/** Input for a single community summarization call. */
model SummaryRequest {
  chunkTexts: string[];
  entityNames: string[];
}

/** Bare entity-pair IDs for relationship labelling; names are not passed to the LLM client. */
model EntityPairIds {
  sourceId: string;
  targetId: string;
}

/** Input for a batched relationship-labelling call (one chunk, many pairs). */
model RelationshipRequest {
  chunkText: string;
  entityPairs: EntityPairIds[];
}

/** LLM-labelled relationship for one entity pair. */
model LabeledRelationship {
  sourceId: string;
  targetId: string;
  label: RelationshipKind;
}

/**
 * Internal contract between CommunityBuilder / GraphExtractor and the LLM adapter.
 * Callers handle null/empty returns as the silent-fallback path.
 */
interface LLMEnricher {
  /** Returns summary text, or null on any LLM failure (quota, timeout, key missing). */
  summarizeCommunity(req: SummaryRequest): string | void;
  /** Returns labelled pairs; empty array on LLM failure. */
  labelRelationships(req: RelationshipRequest): LabeledRelationship[];
}

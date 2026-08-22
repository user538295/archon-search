// C2 — Graph extraction result and the degradation contract (internal logical seam).
//
// Layer edge: Use Cases (SearchPipeline) -> Interface Adapters (GraphExtractor).
// The `extract` seam itself does not move. What changes is the aggregate it returns:
// `llmFallbackUsed` disappears with the LLM relationship path, and the meaning of
// `degraded` / `fatalError` is re-anchored from spaCy to the new artifact and package.

namespace GraphExtractionSeam;

enum EntityType {
  person,
  concept,
  system,
  event,
  code_symbol,
}

enum RelationshipType {
  uses,
  implements,
  depends_on,
  related_to,
  synonym_of,
  calls,
  imports,
  defines,
  inherits,
}

/// One chunk handed to the extractor. `symbolType` non-null routes the chunk to the
/// AST path and it never reaches the prose engine.
model ChunkInput {
  chunkId: string;
  text: string;
  symbolType: string | null;
  symbolSubtype: string | null;
  containingFunction: string | null;
  containingClass: string | null;
  sourcePath: string | null;
}

model GraphNode {
  /// Stable id = hash(entityType + entityName). Changing an entity's type re-keys the node.
  id: string;
  entityName: string;
  entityType: EntityType;
  sourceDocId: string;
  collectionName: string;
}

model GraphEdge {
  /// Stable id = hash(sourceNodeId + targetNodeId + relationshipType). Direction matters,
  /// so a distinct relationship type yields a distinct id and merges additively.
  id: string;
  sourceNodeId: string;
  targetNodeId: string;
  relationshipType: RelationshipType;
  sourceDocId: string;

  /// Provenance tag. Decided (Q4): engine-asserted typed prose edges do NOT stamp a value —
  /// they stay unset exactly like co-occurrence edges. Introducing a tag here would collide
  /// with the def/ref tag precedence the sweep already applies. The unsupported-edge sweep
  /// discriminates on `relationshipType` instead, and its allowlist widens from `related_to`
  /// alone to `related_to` + `uses` + `implements` + `depends_on`.
  extractionMethod: string | null;
}

model GraphMention {
  entityId: string;
  chunkId: string;
  docId: string;
}

/// The aggregate returned by `extract`. `llmFallbackUsed` is removed by this feature.
model GraphExtractionResult {
  nodes: GraphNode[];
  edges: GraphEdge[];
  mentions: GraphMention[];

  /// Operator-facing, sanitized strings only — never an exception message.
  warnings: string[];

  /// True when the prose half could not run AT ALL. The caller deletes the document's existing
  /// graph rows so a code-symbols-only result never leaves unsupported prose edges behind.
  /// Does NOT cover a loadable, relations-incapable artifact (S59, M9): when the recorded
  /// capability assert is `relations_not_supported`, the engine still loads and still returns
  /// entities — `extract` calls `inference()` normally and persists whatever entities and
  /// (empty) relations come back, with `degraded` staying `false`.
  degraded: boolean;

  /// Non-null ONLY when the extraction package itself is not importable. The caller aborts
  /// the ingest before persisting anything. A missing or unusable model artifact is
  /// `degraded`, not `fatalError`.
  fatalError: string | null;
}

/// Decided (Q9): the relation-capability check does NOT live on this seam and never runs
/// inside `extract`. It rides the existing BACKGROUND model-validation probe — the same
/// non-blocking task that already produces `provider_warnings` — so the listening socket
/// binds regardless of the result, the probe never raises, and a failed assert DEGRADES
/// (one actionable warning on `GET /status`) rather than hard-failing startup. That resolves
/// the conflict the brief left open between "assert capability at startup" and "the
/// degradation path is retained verbatim": the assert is real, its disposition is degrade.
///
/// Decided (Q8): one shared engine instance, loaded once per process behind a lock — no LRU,
/// no size knob. The bounded-waiter shape from ADR-11 is kept but its disposition is
/// INVERTED: a wait timeout degrades the document (auxiliary-write invariant), it never
/// returns 503 and never fails the ingest.
model StartupCapabilityDisposition {
  /// Present only to document the decision; carries no runtime meaning.
  surfacedVia: string;
}

interface ProseAndCodeExtractor {
  extract(chunks: ChunkInput[], docId: string, collection: string): GraphExtractionResult;
}

/** E1c internal logical seam — ExplainPipelineResult ↔ route handler.
 *
 * Partial view: shows only the E1c-delta fields added to the explain pipeline boundary.
 * Full shapes: pipeline.py (ExplainPipelineResult, ScoredSearchCandidate), _diagnostics.py
 */
namespace E1cPipelineExplain;

/** Mirrors routes_explain.GraphProvenance — carried on each graph-retrieved candidate. */
model GraphProvenance {
  steps: TraversalStep[];
}

model TraversalStep {
  entity: string;
  entityId: string;
  relationship: string | null;
  communityId: string | null;
  chunkId: string | null;
}

/**
 * E1c delta on ScoredSearchCandidate (_diagnostics.py).
 * graph_provenance: null  → chunk retrieved by standard hybrid search.
 * graph_provenance: steps → chunk retrieved via graph traversal.
 */
model ScoredSearchCandidateE1cDelta {
  graphProvenance: GraphProvenance | null;
}

/**
 * E1c delta on ExplainPipelineResult (pipeline.py).
 * graph_mode_applied carries which mode actually ran; null when graphMode=None
 * was passed or the graph layer was not invoked.
 */
model ExplainPipelineResultE1cDelta {
  graphModeApplied: "naive" | "local" | "global" | null;
}

/**
 * Interface the route handler calls (Use Cases → Interface Adapters).
 * E1c adds graph_mode kwarg; pipeline returns ExplainPipelineResult enriched
 * with graphModeApplied and candidates enriched with graphProvenance.
 */
interface PipelineExplainBoundary {
  /** graph_mode: null = standard explain; "naive"/"local"/"global" = graph-aware explain. */
  explain(graphMode: "naive" | "local" | "global" | null): ExplainPipelineResultE1cDelta;
}

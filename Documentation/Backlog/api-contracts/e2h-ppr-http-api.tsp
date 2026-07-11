// C1 — PPR HTTP API seam: SearchRequest / SearchResponse / ExplainRequest / ExplainResponse
// Delta specification for archon_search/server/routes_search.py and routes_explain.py.
import "@typespec/http";
import "@typespec/openapi3";

using TypeSpec.Http;

namespace E2h.PprApi;

// ── Shared types ────────────────────────────────────────────────────────────

/** Graph traversal mode; "ppr" added by E2h. */
union GraphMode {
  naive: "naive",
  local: "local",
  global: "global",
  ppr: "ppr",
}

// ── Search endpoint ─────────────────────────────────────────────────────────

model SearchRequest {
  collection?: string | null;
  query: string;

  @doc("Graph traversal mode; null means standard hybrid search.")
  graphMode?: GraphMode | null;

  @doc("Scope filter; mutually exclusive with graphMode (returns 422 if both set).")
  scopeFilter?: string | null;
}

model SearchResponse {
  acl_filtered: boolean;
  graph_expansion_applied: boolean;

  @doc("PPR seed entity count. Null when PPR not used. 0 when PPR ran but matched no entities.")
  ppr_entities_matched?: int32 | null;
}

// ── Explain endpoint ─────────────────────────────────────────────────────────

model ExplainRequest {
  query: string;
  collection?: string | null;

  @doc("Graph traversal mode for explain; 'ppr' added by E2h.")
  graphMode?: GraphMode | null;
}

model ExplainResponse {
  @doc("Mode actually applied; null when standard hybrid was used.")
  graph_mode_applied?: GraphMode | null;

  @doc("PPR seed entity count. Null when PPR not used. 0 when PPR ran but matched no entities.")
  ppr_entities_matched?: int32 | null;
}

// ── Route declarations ────────────────────────────────────────────────────────

@route("/search")
interface SearchOps {
  @post search(@body body: SearchRequest): SearchResponse;
}

@route("/explain")
interface ExplainOps {
  @post explain(@body body: ExplainRequest): ExplainResponse;
}

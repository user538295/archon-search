import "@typespec/http";
using Http;

// E2g: new HTTP/API seam — GET /graph/{collection}/impact/{symbol}. Follows the
// existing routes_graph.py shape (guard graph.enabled -> 422, guard collection
// exists -> 404) and GraphInspectionResponse naming convention (schemas.py).
// The response shape is a direct structural mirror of C4's ImpactResult
// (e2g-graph-traversal.tsp) — grouped direct vs. indirect per side, each with
// its own truncation flag and omitted_count — so BE-9 does zero lossy
// transformation between the traversal layer and this route (Critical #1).
@service(#{ title: "E2g Graph Impact API" })
namespace E2gGraphImpactApi;

model ImpactEdge {
  entity_id: string;
  entity_name: string;
  relationship_type: string;
  extraction_method: string;
  depth: int32;
}

// Mirrors C4's ImpactGroup exactly (e2g-graph-traversal.tsp).
model ImpactGroup {
  direct: ImpactEdge[];
  indirect: ImpactEdge[];
  truncated: boolean;
  omitted_count: int32;
}

model GraphImpactResponse {
  symbol: string;
  callers: ImpactGroup;
  callees: ImpactGroup;
  depth_used: int32;
}

@route("/graph/{collection}/impact/{symbol}")
interface GraphImpact {
  @get impact(
    @path collection: string,
    @path symbol: string,
    // Disambiguates which node `symbol` resolves to when more than one
    // same-named code_symbol node exists (Critical #2). Omitted: resolves to
    // the highest-PageRank match among same-named nodes.
    @query file_path?: string,
    // Optional; BE-9 fills in DEFAULT_IMPACT_DEPTH = 2 (see e2g-graph-traversal.tsp)
    // before calling `compute_impact`, which declares `depth` REQUIRED. Values
    // above MAX_IMPACT_DEPTH = 5 are clamped to the cap by the Adapter.
    @query depth?: int32,
    // Optional; defaults to "both" (callers + callees) when omitted.
    @query direction?: string,
    @query extraction_method_filter?: string,
  ): GraphImpactResponse;
}

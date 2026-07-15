namespace E2gGraphTraversal;

// E2g: new Adapter-layer seam on GraphStore (graph_store.py) — no existing
// multi-hop traversal exists (get_neighbours/get_edges_for_nodes are first-degree
// only, graph_store.py:460,507). compute_impact is new BFS-with-depth-cap logic
// built on those two primitives. pagerank_score reads a PERSISTED nodes-table
// column (resolved: background recompute, community_builder.py's
// asyncio.to_thread + maintenance_loop.py's debounce-reschedule pattern —
// not calculated inline per call). write_pagerank_scores is the matching write
// seam BE-7 uses to persist a recompute pass (Major #8) — without it BE-7 would
// have to reach past this Adapter into graph_store.py directly.
//
// `code_symbol` node identity (Critical #2): `make_stable_entity_id` derives the
// node ID from `(entity_type, name)` alone — no file/module qualifier — so two
// unrelated same-named symbols in different files collapse onto one node today
// (verified: graph_extractor.py:211, `_code_symbol_name` returns bare
// `containing_function`/`containing_class`/path-stem, graph_types.py:60).
// BE-2 qualifies the `name` passed to `make_stable_entity_id` for code_symbol
// nodes with the chunk's `source_path` so same-named symbols in different files
// get distinct nodes. `compute_impact`'s optional `file_path` disambiguates
// which node `symbol` resolves to when more than one node shares that name;
// omitted, it resolves to the node with the highest PageRank among matches
// (documented default, not silently arbitrary).
enum ImpactDirection {
  callers,
  callees,
  both,
}

model ImpactEdge {
  entity_id: string;
  entity_name: string;
  relationship_type: string;
  extraction_method: string;
  depth: int32;
}

// truncated is true only when the capped population overflows — mirrors the
// existing _truncate_graph binary-flag contract (graph_inspector.py:127-220),
// extended here with a per-group omitted count so no group is silently partial.
model ImpactGroup {
  direct: ImpactEdge[];
  indirect: ImpactEdge[];
  truncated: boolean;
  omitted_count: int32;
}

model ImpactResult {
  symbol: string;
  callers: ImpactGroup;
  callees: ImpactGroup;
  depth_used: int32;
}

// Default/cap for `depth`, resolved at Critical #4/Major #4: 2 is a sane default
// ripple distance for a blast-radius answer; 5 is the hard cap regardless of what
// a caller requests. `direction` defaults to `both` (callers + callees) when omitted.
// These numeric defaults are stated once here and must match C1/C2 exactly — BE-9
// is the single place defaults get filled in before calling this Adapter, since
// `depth`/`direction` are declared REQUIRED on this interface (Major #4).
alias DEFAULT_IMPACT_DEPTH = 2;
alias MAX_IMPACT_DEPTH = 5;

interface GraphTraversalStore {
  // All three methods below take `collection` because every underlying primitive
  // they are built on (`get_neighbours(collection, entity_ids, ns)`,
  // `get_edges_for_nodes(collection, entity_ids, ns)`, and every
  // `GraphStoreProtocol` method) requires it — nodes/edges tables are resolved
  // per-collection-per-namespace (Critical #2/graph_store.py:460,507). Parameter
  // order follows the codebase's existing convention: `collection` is NOT last,
  // `ns` always is (graph_store.py's ns-last convention, unchanged by this
  // feature — see "What does NOT change").
  //
  // `extraction_method_filter` is a PRE-filter on traversal, not a post-filter
  // on the result: excluded edge types are never followed, so `depth_used` and
  // each group's `omitted_count` are computed relative to the filtered graph
  // only (Major #10) — a filtered-out edge sitting at an intermediate hop
  // blocks traversal past it rather than being silently skipped over.
  //
  // `depth` is required here (caller must always pass a resolved value — BE-9
  // fills in DEFAULT_IMPACT_DEPTH when the Presentation layer receives none —
  // and this Adapter is the single place MAX_IMPACT_DEPTH is enforced regardless
  // of what was requested).
  compute_impact(
    collection: string,
    symbol: string,
    depth: int32,
    direction: ImpactDirection,
    extraction_method_filter?: string,
    file_path?: string,
    ns: string,
  ): ImpactResult;

  // void = no score computed yet for this node (e.g. before first PageRank pass).
  pagerank_score(collection: string, entity_id: string, ns: string): float32 | void;

  // Write path for the background PageRank recompute (BE-7). Overwrites the
  // persisted score for every entity_id present in `scores`; entities absent
  // from `scores` are left untouched. `collection` is NOT last, `ns` is last,
  // per the existing GraphStore ns-last-parameter convention.
  write_pagerank_scores(collection: string, scores: Record<float32>, ns: string): void;
}

# Feature Brief: E2b — Entity Graph Inspection Endpoint

## Problem
Once the E1 graph is built, there is no way to inspect it. Operators and developers cannot verify extraction quality, debug missing relationships, or feed graph topology into external tools — the knowledge graph is a write-only black box after ingest.

## Goal
Two REST endpoints expose the E1 knowledge graph as structured adjacency JSON — a node list with chunk-frequency salience and a co-occurrence-weighted edge list — so callers can inspect, visualise, and validate the graph produced by E1 extraction, both within a single collection and merged across multiple collections.

## Users & Context
Operators and developers who have enabled `[graph] enabled = true`, run ingest, and now want to confirm the graph is correct before building workflows on top of it. They are in a diagnostic or integration context: checking extraction results, building a visualisation, or feeding the graph into an external tool. Cross-collection users have deliberately split related content across collections and want to see how entities connect across corpus boundaries.

## Core Flow
1. Caller enables graph extraction (`[graph] enabled = true`) and ingests documents — E1 runs and populates graph tables.
2. Caller issues `GET /graph/{collection}` (single collection) or `GET /graph/cross-collection?collections=a,b` (merged view).
3. Server validates that `graph.enabled = true` and all named collections exist; returns 422 or 404 if not.
4. Server reads nodes and edges from graph tables. For cross-collection, nodes are deduplicated by `entity_id` (stable hash from `make_stable_entity_id`); salience is merged as a chunk-count-weighted average across collections; edges from all collections are concatenated.
5. If the node or edge count exceeds the configured ceiling, the response is truncated and `truncated: true` is set.
6. Response contains a node list (`entity_id`, `entity_name`, `entity_type`, `entity_subtype`, `chunk_count`, `salience`) and an edge list (`source_node_id`, `target_node_id`, `relationship_type`, `weight`, `source_chunk_ids`).
7. Caller uses the adjacency data for inspection, visualisation, or downstream tooling.

## In Scope
- `GET /graph/{collection}` — full node and edge list for one collection
- `GET /graph/cross-collection?collections=a,b` — nodes deduplicated by `entity_id`, edges merged, across named collections
- 422 guard when `graph.enabled = false` (both endpoints)
- 404 when a named collection does not exist
- **Schema extension**: `GraphNode` gains `chunk_count: int`; `GraphEdge` gains `weight: int` (co-occurrence count) and `source_chunk_ids: list[str]` — stored in `graph_store.py` LanceDB tables, updated via `merge_insert()` upsert on every ingest pass
- **Salience formula**: `salience = node.chunk_count / collection_meta.chunk_count` — derived at query time from live collection metadata (not stored on the node); always current without a recompute pass
- **Weight formula**: `weight = number of distinct chunks in which both source and target entities co-occur` — accumulated per edge across ingest passes
- **Re-extraction**: existing graph rows do not carry the new fields; old rows get nullable-safe zero defaults (`chunk_count=0`, `weight=0`, `source_chunk_ids=[]`); operators must re-ingest to populate them
- Size guard: `[graph] max_inspection_nodes = 5000` and `[graph] max_inspection_edges = 25000` (operator-configurable); `truncated: bool` field in response
- OpenAPI contract update (`GET /openapi.json`)
- MCP `get_graph` tool: summarised form — node count, edge count, entity type distribution, top-20 highest-salience nodes, top-20 highest-weight edges (not full adjacency JSON; full graph is REST-only)
- MCP `get_graph_cross_collection` tool: same summarised form for the merged graph
- BREAKING.md entry for any changes to previously documented graph fields

## Out of Scope
- TF-IDF-style cross-collection salience weighting — deferred to E2c (roadmap item added)
- Pagination — size guard + `truncated` flag is the E2b strategy; cursor pagination deferred
- Graph filtering (by entity type, relationship type, depth) — full graph is the inspection primitive; subgraph queries deferred

## Key Decisions
- **Extend schema to match roadmap spec**: topology without salience or edge weight is less useful for evaluating extraction quality; re-extraction cost is accepted as the right tradeoff.
- **Salience = chunk frequency, not degree centrality**: chunk frequency directly measures how broadly an entity is discussed, which is the right signal for inspection. Degree centrality rewards connectivity, which favours noisy high-frequency entities. TF-IDF upgrade deferred to E2c.
- **Weight = co-occurrence count, not extractor confidence**: the current spaCy/C3 extractor produces no confidence scores; co-occurrence count is honest, cheap, and computable today. Upgrade path is clear once the LLM extractor is wired.
- **Cross-collection salience merge = weighted average by collection chunk count**: keeps salience on [0, 1]; larger collections contribute proportionally more to the merged score; more meaningful than max (ignores breadth) and avoids the sum's scale problem (values > 1.0 in a nominally bounded field).
- **MCP tool returns summary, not full adjacency**: LLM callers cannot usefully reason over thousands of nodes in a single tool response; top-20 nodes/edges gives enough signal for agent-level questions. Full graph available via REST.
- **Size guard defaults: 5 000 nodes / 25 000 edges**: covers most real deployments (small-to-medium knowledge bases) without firing on typical corpora; configurable via `[graph]` TOML for operators who need more.
- **Incremental accumulation — raw counts stored, salience derived at read time**: `GraphNode` stores `chunk_count: int` (incremented per new chunk containing the entity); `GraphEdge` stores `weight: int` (incremented per new chunk containing both entities). Salience is derived at query time as `chunk_count / collection_meta.chunk_count`, where `chunk_count` is already maintained incrementally in the meta table (B5 pattern). Ingest cost is O(entities in new batch), not O(all nodes + edges). Uses `table.merge_insert()` upsert — same infrastructure as the B5 centroid update.

## Edge Cases & Constraints
- **Graph not yet built** (`graph.enabled = true` but no ingest has run): `get_all_nodes` returns empty — valid 200 with `nodes: []`, `edges: []`, `truncated: false`.
- **Stale rows after schema extension**: nodes and edges extracted before the schema change have `chunk_count=0`, `salience=0.0`, `weight=0`, `source_chunk_ids=[]`. The response does not flag staleness — document this in the API reference; operators must re-extract.
- **Cross-collection node deduplication**: same entity in N collections → one merged node with `chunk_count = sum(chunk_counts)` and `salience = weighted_avg(saliences, weights=collection_chunk_counts)`. Edge lists are concatenated without deduplication.
- **Empty or single-item `collections` param on cross-collection**: 422 with clear message; at least two collections required.
- **Truncation ordering**: which nodes and edges appear when the ceiling is hit is LanceDB scan order (undefined). `truncated: true` is the only signal; no guarantee of which appear.
- **`graph.enabled = false`**: 422 with `{"detail": "graph inspection requires [graph] enabled=true in server config"}` — consistent with `graph_mode` guards in `routes_search.py`.
- **Namespace isolation**: collection names are namespace-qualified in the store layer; no additional filter needed.
- **Auth**: standard Bearer token; no new permission scope.

## Open Questions
None — all decisions resolved.

## Future Iterations
- **E2c**: Upgrade salience to TF-IDF-style scoring — entity chunk frequency in this collection weighted down by how common the entity is across all collections; requires cross-collection entity frequency table or global scan at extraction time
- Cursor pagination for programmatic traversal of graphs that exceed the size guard
- Filtered subgraph view: `GET /graph/{collection}?entity_type=ORG&seed=entity_id&depth=2`
- Community-level graph inspection (complements Leiden communities from E1b)

## Recommendation
E2b is the right feature to build now — the graph exists, the read methods are there, and without this endpoint the E1 investment is unverifiable. The full scope (schema extension, cross-collection, both REST and MCP) is correct. The hardest implementation task is the salience and weight accumulation across multiple ingest passes — make sure the extractor updates these fields incrementally on re-ingest rather than overwriting them, or repeated ingest of a growing corpus will produce incorrect values.

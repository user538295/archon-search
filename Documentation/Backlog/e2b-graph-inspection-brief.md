# Feature Brief: E2b — Entity Graph Inspection Endpoint

## Problem
Once the E1 graph is built, there is no way to inspect it. Operators and developers cannot verify extraction quality, debug missing relationships, or feed graph topology into external tools — the knowledge graph is a write-only black box after ingest. Every serious GraphRAG competitor ships an inspection surface (graphify: interactive HTML + GraphML/Neo4j/Obsidian exports; LightRAG: WebUI graph viewer; codebase-memory-mcp: 3D UI + Cypher engine; Microsoft GraphRAG: GraphML export); archon-search ships aggregate counts only (`GET /status`).

## Goal
Two REST endpoints expose the E1 knowledge graph as structured adjacency JSON — a node list with chunk-frequency salience and a co-occurrence-weighted edge list with chunk-level provenance — plus GraphML export, so callers can inspect, visualise, validate, and export the graph produced by E1 extraction, both within a single collection and merged across multiple collections.

## Users & Context
Operators and developers who have enabled `[graph] enabled = true`, run ingest, and now want to confirm the graph is correct before building workflows on top of it. They are in a diagnostic or integration context: checking extraction results, building a visualisation, or feeding the graph into an external tool (Gephi, yEd, Neo4j, Obsidian graph plugins all consume GraphML). Cross-collection users have deliberately split related content across collections and want to see how entities connect across corpus boundaries.

## Core Flow
1. Caller enables graph extraction (`[graph] enabled = true`) and ingests documents — E1 runs and populates graph tables; the pipeline graph hook now also writes the mentions incidence table (see In Scope).
2. Caller issues `GET /graph/{collection}` (single collection) or `GET /graph/cross-collection?collections=a,b` (merged view). Optional `?format=graphml` on both.
3. Server validates that `graph.enabled = true` and resolves every named collection through the namespace-gated collection-meta lookup (same gate as `/search`); returns 422 or 404 if not.
4. Server reads nodes, edges, and mentions from graph tables. Derived at read time: `chunk_count` per node (distinct mention chunk_ids), `salience = chunk_count / collection_meta.chunk_count` (live meta), `weight` per edge (count of chunks where both endpoints are mentioned), `source_chunk_ids` per edge (that chunk set, capped). For cross-collection: nodes deduplicated by `entity_id` (stable hash from `make_stable_entity_id`) with `chunk_count` summed and salience merged as a chunk-count-weighted average; edges deduplicated by edge `id` with `weight` summed and `source_chunk_ids` unioned (capped).
5. If the node or edge count exceeds the configured ceiling, the response is deterministically truncated (see Key Decisions) and `truncated: true` is set.
6. Response contains a node list (`entity_id`, `entity_name`, `entity_type`, `entity_subtype`, `chunk_count`, `salience`) and an edge list (`source_node_id`, `target_node_id`, `relationship_type`, `weight`, `source_chunk_ids`).
7. With `format=graphml`, the same (post-truncation) graph is serialised via networkx as `application/graphml+xml` for direct import into external graph tooling.
8. Caller uses the adjacency data for inspection, visualisation, or downstream tooling.

## In Scope
- `GET /graph/{collection}` — full node and edge list for one collection; `?format=json|graphml` (default `json`)
- `GET /graph/cross-collection?collections=a,b` — nodes deduplicated by `entity_id`, edges deduplicated by edge `id`, across named collections; same `format` param
- 422 guard when `graph.enabled = false` (both endpoints); 404 when a named collection does not exist in the caller's namespace; 422 when `collections` has fewer than two entries
- **New mentions incidence table** `_archon_graph_{col}_mentions` (`entity_id: utf8`, `chunk_id: utf8`, `doc_id: utf8`): written by the pipeline post-ingest graph hook — **delete rows `WHERE doc_id = {doc}` then add the new batch** (mirrors the chunk table's `delete_document`), making re-ingest idempotent. `GraphNode`/`GraphEdge` LanceDB schemas are **unchanged** — no new columns.
- **Extractor change**: `GraphExtractionResult` gains `mentions: list[GraphMention]` (`entity_id`, `chunk_id`) — the per-chunk entity incidence the extractor currently computes and then discards (`graph_extractor.py` dedupes nodes/edges into dicts, destroying chunk-level counts). No change to node/edge production.
- **Derived-at-read fields** (never stored on nodes/edges): `chunk_count`, `salience`, `weight`, `source_chunk_ids` — all computed from one bounded mentions scan per request. `source_chunk_ids` is capped at 20 per edge in the response; `weight` always carries the true count.
- **Salience formula**: `salience = node.chunk_count / collection_meta.chunk_count` — derived at query time from live collection metadata (denominator maintained incrementally by the B5 pattern in `store.py`); always current without a recompute pass
- **Weight formula**: `weight = number of distinct chunks in which both source and target entities are mentioned` — computed from the mentions intersection
- **Deterministic truncation**: nodes sorted by (`chunk_count` desc, `entity_id` asc), top `max_inspection_nodes` kept; edges filtered to surviving endpoints, sorted by (`weight` desc, `id` asc), top `max_inspection_edges` kept; `truncated: true` when either cap fires
- Size guard: `[graph] max_inspection_nodes = 5000` and `[graph] max_inspection_edges = 25000` (operator-configurable); plus a module-level mentions scan ceiling constant (the `_EXPIRING_SCAN_CEILING` pattern) that also sets `truncated: true` when hit
- **GraphML export** via networkx (`networkx>=3.0` is already in the `[graph]` extras — zero new dependencies)
- **Re-extraction**: graph rows written before E2b have no mentions; their derived fields read as `chunk_count=0`, `salience=0.0`, `weight=0`, `source_chunk_ids=[]`; operators must re-ingest to populate them
- OpenAPI contract update (`GET /openapi.json`) + snapshot regeneration
- MCP `get_graph` tool: summarised form — node count, edge count, mention count, entity type distribution, top-20 highest-salience nodes, top-20 highest-weight edges (not full adjacency JSON; full graph is REST-only)
- MCP `get_graph_cross_collection` tool: same summarised form for the merged graph
- BREAKING.md entry for any changes to previously documented graph fields

## Out of Scope
- TF-IDF-style cross-collection salience weighting — deferred to E2c (trivially derivable from mentions once this ships)
- Graph GC / stale-row reconciliation — deferred to E2d; until then inspection **will** surface orphaned nodes/edges and dead chunk references (documented below)
- Namespace-scoped graph table names — deferred to E2d (fixes the open `archon-search.toml.example` multi-namespace warning)
- Pagination — deterministic truncation + `truncated` flag is the E2b strategy; cursor pagination deferred
- Graph filtering (by entity type, relationship type, depth) — full graph is the inspection primitive; subgraph queries deferred
- Built-in HTML graph viewer — deferred to E2j
- Mermaid / DOT / Cypher export formats — GraphML covers the interchange need; others deferred

## Key Decisions
- **Mentions incidence table instead of counter columns — supersedes the earlier "incremental accumulation via `merge_insert()`" design, which was unimplementable as written.** LanceDB's `merge_insert("id").when_matched_update_all()` (the only upsert in the codebase, `graph_store.py write_graph`) **replaces** matched rows — it cannot increment a counter across ingest passes. A read-modify-write counter would double-count on re-ingest because a document's prior contribution is unknown once summed. Persisting the `(entity_id, chunk_id, doc_id)` incidence instead makes ingest idempotent per document (chunk IDs are deterministic `{doc_id}-{idx:06d}`, `pipeline.py:522`; doc-scoped delete-then-add mirrors `delete_document`), keeps the node/edge schemas untouched, and gives E2c (TF-IDF needs per-collection entity frequencies), E2d (GC needs chunk↔entity incidence to find orphans), and E2h (PPR needs mention-weighted seeds) their data for free.
- **Salience = chunk frequency, not degree centrality**: chunk frequency directly measures how broadly an entity is discussed, which is the right signal for inspection. Degree centrality rewards connectivity, which favours noisy high-frequency entities. TF-IDF upgrade deferred to E2c.
- **Weight = co-occurrence count, not extractor confidence**: the current spaCy/C3 extractor produces no confidence scores; co-occurrence count is honest, cheap, and computable from mentions. Upgrade path is clear once the LLM extractor (E2i) or typed code edges (E2g) land.
- **Cross-collection node merge = chunk-count-weighted average salience**: keeps salience on [0, 1]; larger collections contribute proportionally more; more meaningful than max (ignores breadth) and avoids the sum's scale problem.
- **Cross-collection edges are deduplicated by edge `id` with weights summed** — NOT concatenated. Edge IDs are stable hashes of `(source_id, target_id, relationship_type)`, so the same conceptual edge in two collections produces the **same** `id`; concatenation would emit duplicate keys in one response.
- **Deterministic truncation, highest-signal-first**: LanceDB scan order is undefined; an inspection view that randomly samples the graph is useless for validation and non-reproducible for tooling. Sorting by salience/weight before capping keeps the most informative subgraph, keeps edges consistent with retained nodes, and makes responses reproducible. Cost is negligible: truncation only happens after a full scan that `get_all_nodes`/`get_all_edges` already perform.
- **GraphML as the one export format**: parity with microsoft/graphrag and graphify's export story at near-zero cost (networkx is already a `[graph]` dependency); GraphML is consumed by Gephi, yEd, Neo4j importers, and Obsidian tooling. JSON adjacency remains the API-native default.
- **MCP tool returns summary, not full adjacency**: LLM callers cannot usefully reason over thousands of nodes in a single tool response; top-20 nodes/edges gives enough signal for agent-level questions (the `graph_stats` + god-nodes pattern competitors converge on). Full graph available via REST.
- **Size guard defaults: 5 000 nodes / 25 000 edges**: covers most real deployments without firing on typical corpora; configurable via `[graph]` TOML.

## Edge Cases & Constraints
- **Graph not yet built** (`graph.enabled = true` but no ingest has run): valid 200 with `nodes: []`, `edges: []`, `truncated: false`.
- **Stale rows after schema extension**: nodes and edges extracted before E2b have no mentions — derived fields read 0/0.0/[]. The response does not flag staleness; document in the API reference; operators must re-ingest.
- **Stale mentions until E2d**: nothing currently cleans graph tables on document delete, sync delete, orphan cleanup, or E2a TTL expiry (verified: `delete_document`, `prune_expired_chunks`, and the maintenance loop never touch `_archon_graph_*`). `source_chunk_ids` may reference dead chunks and counts may overstate until E2d ships graph GC. Document this prominently in the API reference — inspection exposes it; it does not cause it.
- **Namespace sharing**: graph table names carry no namespace segment (`_archon_graph_{col}_*`) — same-named collections in different namespaces share graph tables (documented MULTI-NAMESPACE WARNING in `archon-search.toml.example`). The endpoints resolve collections through the namespace-gated meta lookup (cross-namespace requests 404 at the gate, consistent with the rest of the API), but the underlying rows are shared; namespace-scoped tables land in E2d.
- **Cross-collection node deduplication**: same entity in N collections → one merged node with `chunk_count = sum(chunk_counts)` and `salience = weighted_avg(saliences, weights=collection_chunk_counts)`.
- **Empty or single-item `collections` param on cross-collection**: 422 with clear message; at least two collections required.
- **`graph.enabled = false`**: 422 with `{"detail": "graph inspection requires [graph] enabled=true in server config"}` — consistent with `graph_mode` guards in `routes_search.py`.
- **Mentions scan ceiling**: one full scan of a narrow 3-column table per request, bounded by the ceiling constant; when hit, derived counts cover the scanned subset and `truncated: true` is set.
- **GraphML with truncation**: the export serialises the post-truncation graph; `truncated` is emitted as a graph-level GraphML attribute.
- **Auth**: standard Bearer token; no new permission scope.

## Competitive Context (verified 2026-07-03)
| Capability | Bar-setter | E2b answer |
|---|---|---|
| Adjacency/API inspection | graphify (`graph.json`, 7+ MCP graph tools), codebase-memory-mcp (Cypher) | Both REST endpoints + 2 MCP summary tools |
| Graph export | graphify (Neo4j/GraphML/Obsidian/Mermaid), MS GraphRAG (GraphML) | GraphML now; others deferred |
| Visual browser | Understand-Anything dashboard, LightRAG WebUI, codebase-memory-mcp 3D | Deferred to E2j (adjacency JSON is its data source) |
| Provenance to chunks | MS GraphRAG citations, Graphiti episodes | `source_chunk_ids` per edge + mentions table |

## Open Questions
None — all decisions resolved.

## Future Iterations
- **E2c**: TF-IDF salience (IDF denominator = number of collections whose graph mentions the entity — now a cheap cross-table lookup)
- **E2d**: graph lifecycle hygiene — doc-scoped graph cleanup on delete/re-ingest, maintenance-loop graph GC, namespace-scoped table names, staleness counters in `GET /status`
- **E2h**: PPR retrieval mode seeded with mention weights
- **E2j**: single-file HTML graph viewer over the adjacency JSON
- MCP `get_entity_chunks` tool (entity → chunks provenance lookup, enabled by mentions)
- Committable graph artifact export (codebase-memory-mcp's team-shared snapshot pattern)
- Cursor pagination; filtered subgraph view (`?entity_type=ORG&seed=entity_id&depth=2`); Mermaid/DOT/Cypher exports

## Recommendation
E2b is the right feature to build now — the read methods exist (`get_all_nodes`/`get_all_edges`), the E1 investment is unverifiable without it, and every credible competitor treats inspection as table stakes. The hardest implementation tasks are (1) the extractor change that stops discarding per-chunk incidence and (2) the doc-scoped delete-then-add mentions write in the pipeline hook — get the re-ingest idempotency test in first (ingest same file twice → identical derived counts), because it is the failure mode the previous counter-column design could not survive. Derivation assembly (scan + groupby + intersections) is straightforward Python over bounded data. Ship GraphML in the same slice; it is an afternoon of work on top of the JSON path and doubles the feature's external value.

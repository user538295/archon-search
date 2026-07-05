# Feature Brief: E2c — Graph Salience Upgrade (TF-IDF Entity Scoring)

## Problem
Graph inspection nodes are currently ranked by raw chunk frequency within a single collection, which over-ranks ubiquitous entities (common technical terms, company names that appear everywhere) and gives no signal about whether an entity is distinctive to this collection versus noise shared across all collections.

## Goal
`GET /graph/{collection}` and `GET /graph/cross-collection` return nodes ranked by a TF-IDF-style salience score when `?salience=tfidf` is passed, surfacing entities that are meaningfully concentrated in fewer collections over those that are uniformly spread everywhere. On a multi-collection fixture, domain-specific entities outrank ubiquitous ones under `tfidf`. Single-collection graphs produce identical node ordering under both modes **when the namespace contains exactly one collection** (IDF collapses to a constant factor `log(2)` that cancels in ranking). When a namespace has multiple collections but a single collection is inspected, tfidf ordering MAY differ from frequency ordering — this is correct and intentional behavior.

## Users & Context
Operators and developers calling the graph inspection endpoints to understand which entities are most distinctive to a collection — building visualisations, debugging extraction quality, or feeding graph structure into downstream tooling.

## Core Flow
1. Caller issues `GET /graph/{collection}?salience=tfidf` (or `GET /graph/cross-collection?salience=tfidf`).
2. Server fetches all entity sets from every collection node table in the requesting namespace (one LanceDB read per collection — a single batch pass).
3. For each node in the result set, computes: `salience_tfidf = (chunk_count / total_chunks_in_collection) × log(total_collections / collections_containing_entity + 1)`. ⚠️ NOTE: The formula as written contains an ambiguity. The authoritative resolved formula is `log((N+1) / collections_containing_entity)` (see Q1 in the team plan). That formula, not the one above, is what satisfies the edge cases described in this brief.
4. Nodes are sorted and truncated by TF-IDF salience (deterministic: highest score first, then `entity_id` asc for ties).
5. Response is returned with `salience_mode: "tfidf"` echoed back alongside the scored nodes.
6. Caller omitting `?salience=` gets the current `frequency` mode unchanged — no breaking change.

## In Scope
- New `?salience=frequency|tfidf` query parameter on both `GET /graph/{collection}` and `GET /graph/cross-collection`
- Batched request-time IDF computation: one pass fetching all entity ID sets across namespace-scoped collections, denominator computed in-process
- `salience_mode: Literal["frequency", "tfidf"]` field added to both response schemas (`GraphInspectionResponse` and `CrossCollectionGraphInspectionResponse`)
- New `GraphStore` method (e.g., `get_entity_presence_across_collections(namespace) -> dict[entity_id, int]`) that returns the IDF denominator map in a single batch
- IDF scoped strictly to the requesting namespace (no cross-namespace reads)
- Unit tests: multi-collection fixture confirms domain-specific entities outrank ubiquitous ones under `tfidf`; single-collection confirms ordering is identical between modes
- `ponytail:` comment in `graph_inspector.py` naming the N-table ceiling and pre-computation as the upgrade path

## Out of Scope
- Storing or pre-computing IDF values — request-time batch scan is correct and sufficient; pre-computation adds invalidation complexity with no user-visible benefit at current scale
- Changing the default salience mode — `frequency` remains the default to avoid breaking existing integrations
- MCP tools (`get_graph`, `get_graph_cross_collection`) gaining a `salience_mode` parameter — MCP tools return summary dicts and are already simplified views; add in a follow-up if there is demand
- Cross-namespace IDF computation — this would be a data isolation violation in a multi-tenant deployment

## Key Decisions
- **Batch scan over per-entity fan-out**: computing IDF via one pass across all collection node tables (N reads total) rather than N reads per entity prevents quadratic fan-out and keeps request latency bounded.
- **Namespace-scoped IDF**: IDF denominator counts only collections accessible to the requesting namespace; global computation would silently leak information about other tenants' data into salience scores.
- **Echo `salience_mode` in response**: consistent with how `graph_mode_applied`, `graph_expansion_applied`, and `truncated` are already echoed; allows callers to detect the effective mode without parsing query params.
- **No new default**: keeping `frequency` as default avoids a silent breaking change for existing callers who do not pass `?salience=`.

## Edge Cases & Constraints
- **Single-collection namespace**: IDF degenerates to `log((1+1)/1) = log(2)` for every entity — a constant factor that cancels in ranking. Node order is identical to `frequency` mode. Acceptable and correct.
- **Entity present in all collections**: IDF term = `log((N+1)/N) = log(1 + 1/N)` ≈ 0 for large N. Entity effectively suppressed. This is the desired behavior for ubiquitous noise.
- **Entity present in no other collection**: IDF term = `log((N+1)/1) = log(N+1)` — maximum boost. Correct.
- **Zero total collections**: guard against division by zero; if namespace has no collections, return empty graph (already guarded by existing 404 path).
- **Truncation order**: deterministic sort (`salience_tfidf` desc, then `entity_id` asc) applied before cap, same as existing `frequency` truncation. `truncated: bool` flag already in response.
- **`max_inspection_nodes` / `max_inspection_edges` config**: unchanged; truncation still respects operator ceiling from `[graph]` TOML config.

## Open Questions
- Should `graph_inspector.py`'s `inspect_collection` and `inspect_cross_collection` signatures receive a `salience_mode` enum or a plain string? (Implementation detail — no user-visible impact either way.)
- Should the batch entity-presence method live on `GraphStore` or be a free function in `graph_inspector.py`? `GraphStore` is the correct layer (it owns table access), but this is a planning decision.

## Future Iterations
- **MCP tool support**: expose `salience_mode` parameter on `get_graph` and `get_graph_cross_collection` MCP tools once there is demand.
- **Pre-computed IDF cache**: if inspection latency becomes measurable (large N collections, large graphs), compute and store the `entity_id → collection_count` map during `build-communities` and invalidate on ingest. The `ponytail:` comment in the code marks this upgrade path.
- **Configurable default salience**: operator-level TOML setting `[graph] default_salience_mode = "tfidf"` once the signal is validated in production.

## Recommendation
Build this. The signal quality improvement is real — frequency ranking is actively misleading for multi-collection deployments, and this is the natural next step after E2b shipped the mentions incidence table that makes IDF computation cheap. The scope is tight: three files touched (`graph_store.py`, `graph_inspector.py`, `routes_graph.py`), one schema change (`schemas.py`). The hardest part is getting the batch IDF scan right without introducing N-squared behavior — the brief specifies the correct approach. Do not compromise on namespace isolation; cross-namespace IDF would be a silent security issue.

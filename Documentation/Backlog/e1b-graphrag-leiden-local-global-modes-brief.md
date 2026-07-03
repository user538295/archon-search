# Feature Brief: E1b — GraphRAG Leiden Community Detection + Local/Global Retrieval Modes

## Problem
Users with large, relationship-dense corpora need two retrieval modes that naive entity expansion (E1a) cannot provide: focused questions about a concept cluster ("How does the auth subsystem work?") benefit from community-scoped retrieval, while synthesis questions ("What are the main architectural patterns?") need corpus-wide coverage. Both are impossible with single-entity expansion alone.

## Goal
After running `archon-search graph build-communities <collection>`, Leiden community detection clusters the entity graph into coherent groups, each represented by an MMR-selected set of member chunks. `graph_mode=local` retrieves the community containing query entities plus their member chunks; `graph_mode=global` retrieves representative chunks from all communities, reranked against the query. Both modes pass the eval gate `graph_mrr >= baseline_mrr`.

## Users & Context
Operators who have already run E1a entity extraction and want deeper graph-aware retrieval. They run `build-communities` once after bulk ingest (or whenever the corpus changes significantly), then end users query with `graph_mode=local` for focused questions and `graph_mode=global` for broad synthesis — no other change to their workflow.

## Core Flow

**Setup (operator, once per corpus change):**
1. Operator runs `archon-search graph build-communities <collection>`.
2. archon-search loads `_graph_nodes` and `_graph_edges` from LanceDB (built by E1a).
3. Leiden algorithm clusters entities into communities using `leiden_resolution` and `max_community_size` config.
4. For each community, MMR selects `community_summary_chunks` (default 3) chunks from member entities — the chunks whose embeddings are most representative and mutually diverse.
5. If `[graph].extraction_model` is set, those K chunks are sent to the LLM to generate an abstractive prose summary instead.
6. Community membership and representative chunk IDs are written to a `_communities` LanceDB table per collection.
7. Command exits; collections are now ready for `local` and `global` modes.

**Query time — `graph_mode=local`:**
1. User sends `POST /search` (or MCP `search`) with `graph_mode=local`.
2. Pipeline identifies entities in the query string (reusing E1a's entity resolver).
3. Looks up which communities contain those entities.
4. Retrieves community representative chunks + all member chunks for matched communities.
5. Merges with normal hybrid search results; feeds combined set to the reranker.
6. Returns top-k results via the standard `SearchResult` schema.

**Query time — `graph_mode=global`:**
1. User sends `POST /search` with `graph_mode=global`.
2. Pipeline retrieves the representative chunk set for every community in the collection.
3. Reranks all community representatives against the query.
4. Returns top-k results; no entity matching step needed.

## In Scope
- `archon-search graph build-communities <collection>` CLI command
- Leiden community detection via `leidenalg` + `igraph` (bundled in `archon-search[graph]` extra alongside Kuzu)
- Per-collection `_communities` LanceDB table: `community_id`, `entity_ids[]`, `representative_chunk_ids[]`, `summary_text` (null when LLM disabled)
- MMR over community member chunk embeddings for representative chunk selection (local default, zero LLM)
- Optional LLM abstractive summary when `[graph].extraction_model` is set (opt-in upgrade)
- `graph_mode=local` on `POST /search` and MCP `search`
- `graph_mode=global` on `POST /search` and MCP `search`
- Config additions: `[graph].leiden_resolution` (default `1.0`), `[graph].max_community_size` (default `10`), `[graph].community_summary_chunks` (default `3`)
- `GET /status` community stats: `community_count`, `last_built_at` per collection
- Eval gate: `graph_mrr >= baseline_mrr` for both local and global modes (separate eval fixtures from E1a naive mode)

## Out of Scope
- Graph-path provenance in `/explain` (traversal chain display) → E1c
- Auto-triggering `build-communities` on ingest — explicit command only in E1b; auto-trigger is a future iteration
- Cross-collection entity resolution → E8
- Graph visualisation or admin UI → E8
- `graph_mode=naive` changes — owned by E1a, unchanged here

## Key Decisions

- **MMR local default, LLM opt-in**: Preserves local-first posture; MMR over existing embeddings captures community diversity without any network call. LLM synthesis activates only when `extraction_model` is set — the same config knob introduced in E1a.
- **Explicit `build-communities` command**: Community detection is a batch operation with variable cost (Leiden CPU + optional LLM calls). Consistent with the explicit `graph migrate` pattern from E1a — no surprise compute spend on ingest.
- **Global mode = all community representatives, reranked**: Bounds the result set to N communities × `community_summary_chunks` chunks fed to the reranker; avoids unbounded BFS traversal. The reranker handles scoring against the query.
- **`_communities` table, not synthetic chunks**: Community membership and representative IDs are stored separately; no duplication of chunk content into the main table.
- **Leiden defaults `resolution=1.0`, `max_community_size=10`**: Standard Leiden defaults that work for most corpora; operators who need larger or tighter clusters can tune via TOML.
- **`community_summary_chunks=3`**: Three diverse chunks cover a community's core theme and edge concepts without overwhelming the reranker context window.

## Edge Cases & Constraints

- **`build-communities` run before E1a extraction**: Command must check that `_graph_nodes` and `_graph_edges` tables exist and are non-empty; exit with a clear error if not.
- **Community detection on a graph with < 2 entities**: Leiden degenerates; treat the entire graph as one community. Log a warning, do not error.
- **`max_community_size` exceeded**: Communities larger than the limit are split by re-running Leiden at higher resolution on the subgraph. Cap applies post-detection, not as a Leiden parameter.
- **`graph_mode=local` with no matched community**: Query entities found in the graph but no community match (e.g., isolated nodes) — fall back to naive mode expansion silently, note in response debug metadata.
- **`graph_mode=local` with no graph entities at all**: No entities recognised in query — fall back to standard hybrid search, same as `graph_mode=None`.
- **`graph_mode=global` on a collection with no communities built**: Return a clear error (`graph_communities_not_built`) rather than silently running standard search. Operator must run `build-communities` first.
- **LLM summary generation failure**: If `extraction_model` is set and the LLM call fails for a community, fall back to MMR representative chunks for that community; do not fail the entire `build-communities` run.
- **Stale communities after re-ingest**: `_communities` table is valid until `build-communities` is re-run; new entities added by subsequent ingest are not reflected. `GET /status` exposes `last_built_at` so operators can detect staleness. A future iteration can warn when ingest post-dates the last community build.
- **ACL**: `_communities` table inherits collection ACL. Community representative chunks retrieved at query time are subject to the same namespace access check as normal search results.
- **leidenalg absent**: If `archon-search[graph]` is not installed but `build-communities` is invoked, exit with a clear error and install hint — same pattern as Kuzu.

## Open Questions
- Should `build-communities` accept `--all` to rebuild communities across all collections in a namespace? Useful for bulk re-ingest workflows.
- Should `graph_mode=local` results be visually distinguishable from normal results in the response schema (e.g., a `_retrieval_source: "community"` field on each result)? Useful for E1c's explain extension and for operator debugging, but adds schema surface.

## Future Iterations
- **Auto-trigger `build-communities`** after ingest when edge delta exceeds a configurable threshold — eliminates the manual step for operators who want always-fresh communities.
- **`build-communities --all`** — rebuild across all collections in one command.
- **Community staleness warning** — emit a warning in `GET /status` and search responses when ingest has occurred since `last_built_at`.
- **E1c** — Graph-path provenance in `/explain`: each graph-retrieved chunk shows `(query_entity → community → representative_chunk)` traversal chain.

## Recommendation
E1b is the right next step after E1a — it unlocks the two retrieval modes that make GraphRAG genuinely useful for complex corpora. The MMR-over-embeddings approach for community summaries is the correct local-first design: it reuses existing embeddings, adds no dependencies beyond `leidenalg`/`igraph` (already in the `[graph]` extra), and produces better community coverage than a single centroid chunk. The hardest part is Leiden parameter tuning — `leiden_resolution` directly controls community granularity and the defaults will be wrong for some corpora. Do not ship without the eval gate wired; without `graph_mrr >= baseline_mrr`, there is no signal that local/global modes are helping rather than hurting retrieval quality.

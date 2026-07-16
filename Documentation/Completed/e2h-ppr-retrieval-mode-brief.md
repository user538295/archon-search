# Feature Brief: E2h — PPR Retrieval Mode

## Problem

When a user asks a question that requires connecting two or more separate facts — "which services call the authentication module?" or "what concepts link Kubernetes to our deployment pipeline?" — the existing search modes either flood results with loosely related content (naive mode) or rely on pre-built community clusters that may not capture the specific connection the query needs (local/global modes). There is no mode that walks the graph precisely from the query's own concepts to find the most relevant bridge content.

## Goal

Add a new search mode (`graph_mode: "ppr"`) that starts from the entities in the query, walks the connection graph to find strongly related entities, and blends the best-connected chunks into the standard search results — measurably improving recall on multi-hop questions without hurting results on simple ones. Success is confirmed by the E2e two-sided eval gate: bridge multi-hop recall up, HotpotQA negative control flat.

## Users & Context

Developers and knowledge workers searching corpora where answers span multiple documents or code files — a question about a concept that connects through several intermediate topics, or a code query that needs to follow call chains across files. They are already using `graph_mode` today and want a more principled option than `naive`.

## Core Flow

1. User sends a search request with `graph_mode: "ppr"` (via REST, MCP, or CLI).
2. The system extracts word combinations from the query and looks them up against known entity names in the graph.
3. Matched entities become the starting seeds. The system spreads a "random walk" (Personalized PageRank — a standard algorithm for scoring graph nodes by their relevance to a starting point) outward through connections: co-occurrence links, synonym edges (E2f), and code call/import/definition edges (E2g). Each entity receives a score representing how strongly it connects back to the query seeds.
4. The top 20 highest-scored entities' associated chunks are retrieved.
5. Those chunks are blended into the hybrid search results (vector + full-text) using the same weighted merge formula (RRF) the rest of the pipeline uses.
6. The reranker scores the combined set and returns the final ranked list.
7. The `/explain` endpoint shows which entities were matched, their PPR scores, and which traversal steps contributed each chunk. If no entities matched the query, `ppr_entities_matched: 0` appears there — the result set is still valid hybrid search.

## In Scope

- `graph_mode: "ppr"` on `/search`, `/explain`, and MCP `search` tool — same surface as existing `naive`, `local`, `global` modes.
- Entity seeding via n-gram match against graph node names, personalization vector weighted by mention counts (how often each matched entity appears across chunks).
- PPR walk over all edge types: co-occurrence, `synonym_of` (E2f), and typed def/ref edges (`calls`, `imports`, `defines`, `inherits`) from E2g.
- Top-K entity chunks blended additively into hybrid RRF output.
- `ppr_entities_matched: int` surfaced in `/explain` (and optionally in the main search response).
- Two config keys: `[graph] ppr_damping = 0.85` and `[graph] ppr_top_entities = 20`.
- Naive mode expansion cap — bundled here; documented as a behavior change in `BREAKING.md`.
- E2e two-sided eval gate: bridge multi-hop recall must improve; HotpotQA negative control must not regress.

## Out of Scope

- Per-request overrides for `ppr_damping` or `ppr_top_entities` — config-only, consistent with every other graph setting. Add per-request knobs only when a real caller need is demonstrated.
- LLM-based entity extraction for better seed matching — this is E2i, which remains opt-in and offline-capable.
- Uniform PPR without entity seeding (spreading from all nodes equally) — this is not the HippoRAG pattern and loses the query-specificity that makes PPR valuable.
- New graph tables or a `STORE_SCHEMA_VERSION` bump — PPR reads the existing nodes, edges, and mentions tables written by E2b–E2g.

## Key Decisions

- **Bundle the naive mode cap here:** The naive expansion limit is a defect fix (all neighbors, no ceiling) and a small code change; separating it into its own item creates unnecessary coordination overhead. It ships with E2h, documented as a behavior change in `BREAKING.md`.
- **Silent fallback when no entities match:** If the query yields zero entity matches, the system returns standard hybrid search results — no error, no empty response. `ppr_entities_matched: 0` in `/explain` is the caller's signal that the graph did not fire.
- **Config-only for PPR knobs:** `ppr_damping` and `ppr_top_entities` live in `archon-search.toml`. There are no per-request overrides.

## Edge Cases & Constraints

- **No entity match:** Silent fallback to hybrid search; `ppr_entities_matched: 0` in `/explain`. Consistent with the fallback pattern on HyDE and RAG Fusion.
- **`scope_filter` + `graph_mode` are mutually exclusive:** Existing guard applies; PPR does not change this constraint.
- **Graph not built for a collection:** Existing `GraphCommunitiesNotBuiltError` pattern applies — PPR requires the graph tables to exist, same as `local`/`global` modes.
- **`[graph] enabled = false`:** PPR request returns the same 422 as all other graph modes.
- **Naive mode cap change:** Existing users of `graph_mode: "naive"` will see a capped expansion (bounded by `ppr_top_entities` or a separate `naive_max_expansion` key — see Open Questions). Documented in `BREAKING.md` under `[next release]`.
- **PPR requires the `[graph]` extra:** `pip install archon-search[graph]` is already required for any graph mode; no new dependency.

## Open Questions

- **RRF blending mechanism:** The roadmap says PPR chunks blend "additively into hybrid RRF." The current `local`/`global` modes prepend community chunks to the candidate list and then rerank — they do not add a third RRF stream. Plan-maker should decide whether PPR introduces a true third RRF stream (weighted by `[graph] ppr_rrf_weight`) or follows the existing prepend-then-rerank pattern. The third-stream approach is architecturally cleaner but more work.
- **Naive cap value:** Should the cap use `ppr_top_entities` (shared knob) or a separate `[graph] naive_max_expansion_terms` config key? Separate key is explicit but adds surface; shared key is simpler but couples two unrelated concepts.
- **`ppr_entities_matched` placement:** Should this appear in the main `SearchResponse` schema or only in `ExplainResponse`? Adding it to the search response means every caller sees the signal without hitting `/explain`; adding it only to `/explain` keeps the search response lean.
- **Eval gate threshold:** Does E2h require a new `graph_ppr_recall_at_5` threshold row in `thresholds.toml`, or is it sufficient to show that the existing E2e bridge and negative-control thresholds still pass with `ppr` mode active?

## Future Iterations

- Per-request overrides for `ppr_top_entities` — expose if callers demonstrate a real query-level need.
- PPR as the default for `graph_mode` when the graph is fully built (replacing naive as the entry-level mode) — reasonable once eval validates the lift is consistent.
- E2i LLM enrichment: better entity extraction at ingest means better seed quality for PPR, compounding the lift.

## Recommendation

This is the right feature to build next. The infrastructure — entity graph, synonym edges, def/ref edges, mentions table, igraph — is entirely in place; E2h is the retrieval wiring that makes it pay off at query time. The HippoRAG benchmark numbers (2WikiMultiHopQA R@5: 68.2 → 89.1) represent the realistic upside for multi-hop queries. The hardest part is the RRF blending decision in Open Questions — it has architectural consequences for how graph modes work going forward and should be settled before implementation begins. What must not be compromised: the two-sided eval gate. PPR must improve bridge recall AND leave simple-query results alone; if it can only do one, it is not ready to ship.

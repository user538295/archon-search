# Feature Brief: Entity Resolution v1 — Synonym Edges

## Problem

The graph collects entities from documents but fragments them by name: "K8s", "Kubernetes", and "kubernetes cluster" exist as three disconnected nodes with no relationship. A graph-mode search for one misses all content indexed under the others.

## Goal

The graph automatically links name-variants and abbreviations of the same entity via synonym edges. Once linked, all graph retrieval modes (naive expansion, local/global community, and the future PPR mode) traverse those edges transparently — so searching for "K8s" also surfaces Kubernetes content without any query rewriting, operator configuration, or additional user action.

## Users & Context

Developers and knowledge workers who have enabled graph mode (`graph.enabled = true`) and ingested a corpus of technical documents or code. They expect search to understand that "K8s" and "Kubernetes" are the same thing. They will never run a graph maintenance command; the system must handle this itself.

## Core Flow

1. User ingests documents — the normal ingest flow, nothing new.
2. After ingest completes, the server schedules a low-priority background enrichment task (same pattern as the existing community-rebuild worker).
3. The task embeds every entity name extracted from the new documents using the collection's fastembed model (the same model that already embeds document text for search).
4. It runs an approximate-nearest-neighbor search over all existing entity-name embeddings in the same collection, restricted to entities of the same type.
5. Pairs with cosine similarity ≥ `[graph] synonym_threshold` (default 0.85) receive a `synonym_of` edge.
6. The task then rebuilds communities to reflect the updated graph structure.
7. From this point forward, all graph retrieval traverses synonym edges transparently: naive expansion follows them, community membership reflects them, `/explain` shows them in the traversal path.

## In Scope

- **Embedding-based synonym edge detection**: cosine similarity ≥ configurable threshold (default 0.85), same entity-type only.
- **Operator alias file**: `[graph] alias_file` (TOML format) for manually pinning known synonym pairs the ANN may miss or get wrong (e.g., domain-specific abbreviations).
- **Graph health metrics** exposed in `GET /status` and `GET /graph/{collection}`: dedup-merge rate, singleton-node %, orphan rate, connected-component count. Catches extraction regressions (e.g., a spaCy model upgrade fragmenting entities).
- **Self-maintaining background job**: synonym detection + community rebuild trigger automatically after every ingest. Debounced per-collection — one rebuild runs at a time; subsequent ingest completions queue behind it via the existing `RebuildState.pending` pattern.
- **Strengthened E2e eval gate**: bridge multi-hop recall must improve with synonym edges active; HotpotQA negative control must not regress. Acceptance tests must exercise real graph retrieval (not just the hybrid fallback that the current E2e tests fall back to due to the BE-3/BE-6 gap).

## Out of Scope

- **LLM-based entity merging** — deferred to E2i; costs money and slows ingest by definition.
- **Cross-entity-type synonym detection** — too many false positives given current spaCy extraction accuracy (e.g., "Apple" the company vs. "Apple" the concept).
- **Migration backfill for pre-existing nodes** — not needed; this is a greenfield deployment. All entity nodes created after E2f ships will have embeddings from day one.
- **PPR traversal** — E2h; synonym edges will be traversed by PPR automatically when it ships; no changes to synonym logic needed then.
- **Graph viewer** — E2j; synonym groups will be visualizable there once it ships.

## Key Decisions

- **Embedding over string similarity**: "K8s" and "Kubernetes" have edit distance 8 and Jaro-Winkler ≈ 0.55 — string distance cannot link them. Embedding captures meaning, not spelling. String normalization (case variants) is already handled by the stable-ID hash, which lowercases entity names before hashing.
- **Self-maintaining background job, not a manual command**: The zero-operator UX is the product's differentiator. A graph that requires an explicit ceremony after ingest will be silently broken for the majority of users who never run it.
- **Same entity-type restriction**: Synonym edges only connect entities of the same type (system↔system, concept↔concept). Prevents cross-type false merges.
- **TOML alias file**: Fits the project-wide config paradigm. Entries of the form `"K8s" = "Kubernetes"` pin merges that ANN might miss (domain jargon, proprietary abbreviations) or that an operator wants to force regardless of threshold.
- **Vector column on graph nodes table only**: Adding a name-embedding column touches only the per-collection graph nodes table (`_archon_graph_{ns}__{col}_nodes`), not the shared chunk table. No `STORE_SCHEMA_VERSION` bump, no `POST /collections/{name}/migrate` call required.

## Edge Cases & Constraints

- **Threshold calibration**: `synonym_threshold = 0.85` is the default. Practitioner evidence marks <85% resolution accuracy as the point where a knowledge graph becomes "toxic" (one bad merge poisons every path through it). Recommended tuning range: 0.82–0.90. Health metrics (dedup-merge rate) are the signal: a sudden spike means the threshold is too loose.
- **Background job failure**: Logged as WARNING, retried on next ingest. Search is never blocked — graph enrichment is always best-effort and never on the critical path.
- **Duplicate rebuild prevention**: The existing `RebuildState.pending` pattern in `maintenance_loop.py` ensures N concurrent ingests don't spawn N parallel community rebuilds. E2f hooks into this same mechanism.
- **E2e baseline gap (BE-3/BE-6 debt)**: Current E2e tests skip `lancedb_root` + `build_communities_for_eval`, so `community_backend_map` stays None and graph retrieval falls through to `StubGraphExpander`. E2f acceptance criteria must include a fixture test that wires up a real (in-memory) graph with synonym edges and verifies that traversal actually reaches the linked entities in ranked results — not just that the eval metric clears the hybrid-fallback floor.

## Open Questions

- Should `synonym_of` edges share the existing edges table (with `relationship_type = "synonym_of"`) or live in a separate synonym table? Tradeoff: shared table keeps the graph traversal API uniform; separate table allows targeted queries (e.g., "list all synonym groups") without scanning all edges.
- How does the alias file interact with ANN-detected synonyms? Does a manual alias suppress the ANN check for that pair, or are both stored as `synonym_of` edges tagged with `extraction_method: "manual" | "embedding"`? The latter enables auditing which merges were automatic.
- Should `[graph] enrichment_auto` be an explicit config flag (default `true` when `graph.enabled = true`), or always on when graph is enabled? An explicit flag makes it testable and disableable without disabling the whole graph.
- What is the debounce granularity — per-collection (consistent with the existing community-rebuild pattern) or global? Per-collection is strongly preferred to avoid one busy collection blocking another.

## Future Iterations

- **E2h (PPR retrieval)**: will traverse synonym edges automatically — Personalized PageRank seeds on matched entities, then walks co-occurrence + synonym + def/ref edges. No changes to synonym logic needed.
- **E2g (code def/ref graph)**: adds `calls`/`imports`/`defines` edges to the same edge table; synonym detection scope stays name-based and is unaffected.
- **E2i (LLM enrichment)**: may catch semantic synonyms embedding misses (e.g., "authentication" ↔ "auth" in a low-frequency corpus). Synonym edges from E2f and E2i coexist by `extraction_method` tag.
- **E2j (graph viewer)**: will visualize synonym groups as node clusters — E2f's synonym edges are the input; no changes needed in E2f for this.

## Recommendation

E2f is the right feature to build now. The graph has been collecting entities through multiple phases; synonym edges are the first step that makes those entities actively useful for multi-hop retrieval rather than just enriching single-document context. The zero-operator design (self-maintaining background job) is non-negotiable — a graph that silently fragments after ingest is worse than no graph. The hardest part is calibrating the threshold and testing it honestly: the E2e baseline gap must be closed in the acceptance criteria, not papered over. Ship synonym edges with real retrieval tests or don't call it shipped.

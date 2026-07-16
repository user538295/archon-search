# Feature Brief: E2g — Code Def/Ref Typed Graph (tree-sitter edges, PageRank salience, impact tool)

## Problem

Code files currently land in the knowledge graph as lone dots with no connecting lines (each code chunk produces exactly one `code_symbol` node and zero edges — `graph_extractor.py:209-227`), so graph-powered search adds nothing for code — even though published evidence says code is where knowledge graphs help most (RepoGraph: +32.8% average relative on SWE-bench).

## Goal

Code becomes a connected, queryable graph: functions and classes are linked by real relationships (calls, imports, defines, inherits), the most important symbols rank first when browsing, and a user or AI agent can ask "what breaks if I change X?" and get a trustworthy, depth-controlled answer. Success is proven by a quality gate: connection-style code queries must score measurably better with the new edges than without them, and no existing quality score may drop.

## Users & Context

- **AI agents** connected over MCP, working on a codebase: before editing a function they ask for its blast radius; while exploring they follow call/import links instead of guessing from text similarity.
- **Developers and scripts** using the plain HTTP API: same impact question from a terminal or CI job, plus richer graph browsing where important symbols surface first.
- Both are mid-task on a real, multi-file, multi-language repository — they need answers that cross file boundaries and are honest about certainty.

## Core Flow

1. The user ingests code files exactly as today (watcher, CLI, or API — nothing new to learn).
2. During ingest, code files are split along natural boundaries — whole functions and classes — instead of fixed-size windows, so each indexed piece is a coherent unit (cAST-style split/merge on the syntax tree).
3. In the same pass, the parser extracts definitions and references and writes typed connections into the graph: calls, imports, defines, inherits. Every connection carries an honesty label — "proven" for links the parser can verify (same-file calls, explicit imports) and "best guess" for cross-file name matches (`extraction_method: "extracted" | "inferred"`).
4. An importance score is computed for every code symbol (PageRank — symbols the rest of the code points at score high). Graph browsing gains an importance sort mode alongside the existing frequency and rarity modes, and impact answers are ordered by it. Search-result ranking is untouched.
5. The user or agent asks "what is affected if I change X?" — via the new `graph_impact` MCP tool or its HTTP twin. The answer lists callers and callees, separated, with the ripple effect to a settable depth (sane default, hard cap), grouped direct vs. indirect, with counts that make any truncation visible.
6. Before release, the new quality gate must pass: purpose-built connection questions score better with def/ref edges than with today's co-occurrence graph, and every existing quality floor holds.

## In Scope

- **AST-aware chunking** for code files: split/merge on syntax-tree boundaries instead of fixed token windows (chunker-level; improves plain code search independently of the graph).
- **Def/ref typed edges** — the first real producer for the typed-relationship vocabulary: `calls`, `imports`, `defines`, `inherits`, each tagged proven vs. best-guess. Language rollout **sequenced**: Python + TypeScript first (proves the design), then JavaScript, Go, Rust, Java, Bash (parsers already installed), then **Swift and C#** last (two new parsers to add and verify; may slip to an immediate fast-follow if their parsers prove incompatible — they must not block the release). SwiftUI is covered by Swift — `.swift` files include it; no separate work exists.
- **PageRank importance scoring** for code symbols — surfaced in graph browsing (new sort mode) and impact-tool ordering only.
- **`graph_impact`** on both surfaces: MCP tool for agents and an HTTP endpoint for people and scripts.
- **The code-lane quality gate itself** — it does not exist yet (the roadmap's "gated by the E2e code lane" refers to a lane E2e explicitly deferred into this feature): new connection-question fixtures, an A/B of def/ref expansion vs. co-occurrence, wired into the existing threshold/baseline machinery.
- **Setup experience**: the wizard installs and configures everything automatically (both optional bundles — code parsers and graph libraries — plus the new Swift/C# parsers). Manual configuration that enables the graph without the code parsers gets a loud one-time startup warning and a visible health/status indication; the server starts fine and prose graphing still works.

## Out of Scope

- **Importance scores influencing search rankings** — reserved for E2h (PPR), which will traverse these same edges with a principled method; letting PageRank leak into ranking here would double the gating burden for no proven gain.
- **LLM-derived relationship edges** (`uses`/`implements`/`depends_on` producers) — E2i's territory; this feature stays deterministic and zero-cost at ingest.
- **Language-server-grade cross-file resolution** — building a compiler front-end per language is out of proportion; best-guess matching with honesty labels is the deliberate ceiling.
- **Languages beyond the nine named** — follow-ups once the per-language pattern is proven.
- **A SwiftUI-specific parser** — SwiftUI is Swift; there is nothing separate to build.

## Key Decisions

- **All five parts ship together** (chunking, edges, PageRank, impact tool, eval gate) rather than splitting chunking out: one release, matching the roadmap as specced. Accepted trade-off: two retrieval-affecting changes land at once, so the eval must be staged internally to attribute movement (see Open Questions).
- **Best-guess cross-file matching, labeled** over proven-only edges: a caller list with an honesty label beats an empty one; the roadmap's own `extracted | inferred` tag presupposes this. Common-name false links are the accepted cost, mitigated by filtering on the label.
- **Impact answers = callers + callees + ripple effect with settable, capped depth** over direct-callers-only: matches the "blast radius" promise and competitor tooling; readability is protected by caps, direction grouping, and truncation counts.
- **Nine languages, sequenced last-risk-last**: Python + TypeScript prove the design; Swift + C# (new dependencies) go last and may slip to a fast-follow without blocking.
- **PageRank stays out of search ranking** (browsing + impact ordering only): zero retrieval risk now; E2h is the principled path for graph-influenced ranking.
- **Two-sided quality bar**: measurably better on connection questions AND no regression anywhere else — a gate that only checks non-regression could pass while the feature silently adds nothing (a failure mode this project has already documented once).
- **Both surfaces for the impact tool** (MCP + HTTP): every graph capability so far is reachable by both agents and humans; the HTTP route is a thin addition.
- **Wizard automates setup; manual config degrades loudly and gracefully**: guided users never see the two-bundle problem; manual users get a clear, non-blocking warning (consistent with how missing parsers degrade today).

## Edge Cases & Constraints

- **Graph enabled but code parsers missing** (first feature requiring both optional bundles — `[code]` + `[graph]` — simultaneously): server starts, prose graphing works, code graphing is skipped with a loud one-time startup warning and a health/status field naming the fix. The wizard path never hits this.
- **Common names produce false connections** ("run", "get", "init" match many definitions): such edges carry the best-guess label; impact output must let callers filter to proven-only.
- **Hub symbols with huge blast radii**: depth default + hard cap + per-group counts keep answers bounded and make truncation explicit — never a silently partial answer.
- **Swift/C# parser incompatibility discovered mid-build**: those two languages slip to a fast-follow; the release proceeds with seven.
- **Existing collections don't retroactively gain edges**: def/ref edges appear for newly ingested/re-ingested files; whether a backfill pass is offered is an open question — either way the behavior must be documented, not silent.
- **Graph hygiene is inherited**: existing garbage collection is type-agnostic (orphan cleanup deletes edges regardless of type), so def/ref edges get lifecycle management for free; the graph-view truncation treatment of def/ref edges needs an explicit decision (synonym edges are exempt from caps today — `graph_inspector.py:136-152`).
- **Two ranking-affecting changes in one release** (chunking + edges): the gate must measure them separately (staged A/B) or a regression in one can hide behind an improvement in the other.

## Open Questions

_(Technical register — for `/plan-maker` and engineers.)_

1. **RelationshipType vocabulary**: add `calls`/`imports`/`defines`/`inherits` as new enum members (recommended; reserve existing unused `uses`/`implements`/`depends_on` for E2i) — `graph_types.py:50-57`. Cascades: enum-count tests, `GraphEdgeInspection`, `GraphEdgeResponse`, OpenAPI snapshot (regen with `uv run --python 3.12`).
2. **Edge direction + weight**: `make_stable_edge_id` is direction- and type-sensitive (good — `calls` A→B is distinct). `GraphEdge` has no `weight` field; decide unweighted PageRank vs. schema addition (schema change touches `graph_store.py:140-151` + pre-existing-table column guard, E2f pattern).
3. **PageRank storage/recompute**: computed at read time like TF-IDF salience (`graph_inspector.py`) vs. persisted on nodes with a recompute trigger. If persisted, the MaintenanceLoop debounce pattern (`schedule_synonym_enrichment`, `jobs/maintenance_loop.py:584`) is the precedent; PageRank is cross-document, so the deferred seam fits it, while def/ref extraction is per-document and fits inline in the post-persist never-propagate block (`pipeline.py:632-663`).
4. **Inferred-match scope**: name-based cross-file resolution scoped to the collection (recommended) or namespace-wide? Canonicalization when both an `extracted` and an `inferred` edge exist for the same pair+type (stable-ID collision semantics under `merge_insert`).
5. **AST chunking mechanics**: cAST split/merge replaces the Chonkie token pass for code files only (`pipeline.py:479-487`); must preserve the C3c coordinate-space invariant (parser is a raw `read_text()` pass-through for code — source text == chunker input) and respect the existing chunk-size budget. Decide fallback when tree-sitter is absent (today's token chunking).
6. **Swift/C# grammars**: verify PyPI availability and ABI compatibility with the pinned `tree-sitter>=0.25` coupling (`pyproject.toml:36-45`); add `.swift`/`.cs` to `CODE_EXTENSIONS` (`code_enricher.py:55`) — note these files gain C3c scope enrichment for the first time as a side effect; wizard grammar-install step extension.
7. **Eval lane construction**: bespoke def/ref fixtures vs. RepoBench-R subset (licensing + fixture size); new `EvalMetrics` fields follow the BE-8 atomic-update pattern (runner.py ×5, `test_eval_suite.py`, `test_baseline_contract.py`, `thresholds.toml`, `regenerate.py`); the A/B must wire `lancedb_root` + real expander or the stub silently defeats the gate (T-4 lesson); stage measurements: chunking-only lane vs. edges-only lane so attribution survives the joint release.
8. **`graph_impact` contract**: parameter set (symbol ref, depth default/cap, direction filter, `extraction_method` filter, result caps + truncation counts); REST route shape in `routes_graph.py` + OpenAPI snapshot; MCP tool validation via module-level `_validate_*` helpers returning `McpErrorResponse | None`.
9. **Inspector caps**: do def/ref edges count toward `_truncate_graph`'s edge cap (recommended) or get a synonym-style exemption? Both exemption points are separate if exempted (E2f learning).
10. **Backfill**: re-ingest-only (recommended, documented) vs. a maintenance-pass backfill for existing collections.
11. **Doc debt to fold into close-out**: `130_data_architecture_and_persistence.md:158` edge schema is stale (missing `weight`-adjacent fields, `relationship_type`, `extraction_method`); `600_api_reference` MCP table says 17 tools and omits `get_graph`/`get_graph_cross_collection`; stale Kuzu references in `graph_extractor.py` docstring and the `backend_threshold_edges` warning text (`pipeline.py:651`).

## Future Iterations

- More languages beyond the nine (Kotlin, C/C++, Ruby, PHP) once per-language rollout is routine.
- **E2h PPR**: Personalized PageRank retrieval mode traversing these def/ref edges — where graph importance finally influences search ranking, properly gated.
- **E2i LLM enrichment**: `uses`/`implements`/`depends_on` edges tagged `extraction_method: "llm"`, coexisting by tag.
- Language-server integration for resolution-grade accuracy, if best-guess precision proves insufficient in practice.

## Recommendation

This is the right feature to build now — it's the highest-impact remaining graph item, the evidence for code graphs is the strongest in the field, and every consumer (search expansion, communities, the coming PPR mode) picks up the new edges automatically. The hardest parts are the two the roadmap glossed over: honest cross-file matching (the difference between a useful impact tool and a noisy one) and building the quality gate that the roadmap wrongly assumed existed. Do not compromise on the honesty labels or the two-sided gate — and since all five parts ship together by explicit choice, insist on staged eval measurement so chunking and edges are attributed separately.

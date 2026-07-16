# Feature Brief: E2i — Opt-in LLM Graph Enrichment

## Problem
When an operator has a large document corpus and asks broad questions ("what are the main themes?" / "how does X relate to Y across the whole collection?"), the search results miss connections that span many documents — because today's graph is built from statistical co-occurrence only, with no language understanding. This occurs whenever the graph's community summaries are empty (`summary_text = null`) and relationship edges are generic (`related_to` only).

## Goal
When an operator sets `extraction_model` in config, two things improve automatically:
1. Every graph community gets an LLM-written summary describing what that cluster of documents is about — surfaced in `local` and `global` search modes and in the graph inspection endpoint.
2. Relationship edges between entities gain meaningful types (`uses`, `implements`, `depends_on`) instead of the generic "related to" label — so PPR and graph traversal paths are more precise.

Success is observable: `GET /graph/{col}` reports `communities_summarized > 0`, and `global` search answers corpus-level questions with richer, more coherent context. Default behavior (no `extraction_model` set) is byte-identical to pre-E2i.

## Users & Context
Operators who have an Anthropic API key (or, in future, a local Ollama instance) and a corpus where broad thematic questions matter — knowledge bases, research document collections, large code repositories. They configure the key once; the system handles enrichment in the background on every community rebuild.

## Core Flow

1. Operator sets `extraction_model = "anthropic:claude-haiku-4-5-20251001"` in `~/.archon-search/archon-search.toml` under `[graph]`.
2. Operator runs `archon-search graph build-communities <collection>` (or the maintenance loop triggers a rebuild automatically after ingest).
3. For each community, the system selects the representative document chunks (already stored as `representative_chunk_ids`) and sends them to the configured LLM with a summarization prompt.
4. The LLM's response is written as `summary_text` on the community record.
5. In parallel, during entity extraction, the LLM is asked to label relationships between entity pairs — producing typed edges (`uses`, `implements`, `depends_on`) instead of generic `related_to` edges, tagged `extraction_method = "llm"`.
6. If the LLM is unavailable (quota exceeded, network error, key missing), the step logs a warning and continues — communities are still built, relationships are still extracted via spaCy, no ingest fails.
7. `GET /graph/{col}` now reports how many communities have summaries, how many are missing, and which ones — so operators know the enrichment is working.
8. `local` and `global` search modes use `summary_text` when present; fall back to representative chunks when absent.

## In Scope
- Community summarization: fill the `_generate_llm_summary()` stub in `community_builder.py`
- Typed-relationship extraction: produce `uses` / `implements` / `depends_on` edges tagged `extraction_method = "llm"` alongside spaCy co-occurrence edges
- Config extension: `extraction_model` string uses `"provider:model"` format (e.g. `"anthropic:claude-haiku-4-5-20251001"`); new fields for timeout, rate limit, and per-pass community cap
- Maintenance-loop `summary_refresh` policy: re-summarize only communities whose membership changed since last build
- Graph inspection response (`GET /graph/{col}`): add `communities_summarized`, `communities_total`, `unsummarized_community_ids` (capped at 100)
- Startup warning when `extraction_model` is set but any collection has unsummarized communities
- Deterministic LLM stub for all tests — no live API dependency in the test suite

## Out of Scope
- Ollama / local model support — config format is forward-compatible (`"provider:model"`), but only the Anthropic client is implemented now; Ollama support lands when G10 (provider matrix) ships
- Dashboard UI for summary health — the data is exposed via `GET /graph/{col}` now; the UI is a separate frontend concern
- Multi-pass gleaning or prompt auto-tuning (MS GraphRAG-style) — cost and non-determinism, not justified
- Schema/ontology-constrained extraction — requires LLM at every ingest by default, violates local-first posture
- Query-time LLM traversal (DRIFT-style) — LazyGraphRAG proves this is unnecessary
- Typed extraction for E2j — typed extraction is included here (1A decision); E2j is not pre-scoped

## Key Decisions
- **Both capabilities in one ticket**: Community summaries and typed extraction share an LLM client, config section, and test harness — splitting them would duplicate that wiring for marginal benefit.
- **Anthropic-only client now, `"provider:model"` config format**: Matches the proven HyDE/RAG Fusion pattern (`hyde.py`, `rag_fusion.py`); the string format makes G10 provider routing a non-breaking addition later with no operator config migration.
- **Silent fallback on all failure paths**: Any LLM error (timeout, quota, missing key, API error) logs a WARNING and proceeds — summaries stay `null`, edges stay `related_to`, ingest never fails.
- **Dashboard data now, dashboard UI later**: `GET /graph/{col}` exposes `unsummarized_community_ids` so a future dashboard is a pure front-end concern with no backend work required.

## Edge Cases & Constraints
- **`extraction_model` unset**: Behavior is byte-identical to pre-E2i. `_generate_llm_summary()` is never called. No token cost, no API dependency.
- **LLM unavailable mid-rebuild**: Failed communities keep `summary_text = null`; they appear in `unsummarized_community_ids`; the next maintenance-loop rebuild retries them.
- **Community membership changes after summary is written**: The `summary_refresh` policy re-summarizes only affected communities (those whose `entity_ids` changed since `built_at`), not the full collection.
- **Per-pass cost cap**: New config fields `max_communities_to_summarize` (default: unlimited) and `extraction_token_budget` (default: no hard cap, warnings logged) prevent runaway costs on large corpora.
- **`unsummarized_community_ids` list cap**: Capped at 100 entries in the API response to prevent oversized payloads on large collections; the counts (`communities_summarized`, `communities_total`) are always exact.
- **Telemetry invariant**: Community texts and LLM prompts are never logged — the structural no-raw-query guarantee (`TelemetryEntry` has no `query` field) extends to graph enrichment. CI guard enforces this.
- **Eval gate**: All changes must pass the deterministic eval gate (E2e frozen fixtures) before shipping; typed-extraction tests use a stub LLM that returns fixed relationship labels.
- **`extraction_method` precedence**: `"extracted"` (static def/ref from E2g) always wins over `"llm"` — an LLM-inferred edge cannot downgrade a statically verified one.

## Open Questions
- Should `extraction_token_budget` be a per-community limit (tokens per prompt) or a per-rebuild aggregate cap? The per-rebuild aggregate is simpler to implement but harder for operators to reason about at collection scale.
- `_generate_llm_summary()` currently receives `chunk_texts: list[str]` — should the prompt also receive `entity_ids` (the community membership list) as structured context, or is chunk text sufficient for the summarization quality target?
- For typed-relationship extraction: does the LLM receive entity pairs one at a time (precise, expensive) or all pairs per chunk in a single batched prompt (cheaper, less precise)? Batching is the obvious default but needs a token-budget check.
- How should `summary_refresh` detect membership change? Options: compare `built_at` against the latest ingest timestamp for any entity in the community (simpler), or store a membership hash alongside `built_at` and diff on rebuild (exact but adds a field to the community schema).

## Future Iterations
- **Ollama / local model support (G10)**: The `"provider:model"` config format is ready; G10 adds the routing layer.
- **Dashboard UI**: `GET /graph/{col}` already exposes the data; a visual health panel is a front-end addition.
- **Typed extraction for prose relations**: E2g added code-symbol typed edges (`calls`, `imports`, `defines`, `inherits`); E2i adds LLM-typed prose relations (`uses`, `implements`, `depends_on`); a future iteration could extend the `RelationshipType` enum with domain-specific types driven by an operator-supplied ontology.
- **Per-community summary freshness decay**: Mark summaries stale after `N` days even without membership change, for corpora that evolve in meaning over time.

## Recommendation
E2i is the right feature to build now — the infrastructure is in place (schema, stubs, LLM client pattern, hook architecture), and community summaries are the one remaining gap between archon-search and the 72–83% comprehensiveness win MS GraphRAG demonstrated on corpus-level questions. The hardest part is the typed-extraction prompt design: getting an LLM to reliably label `uses` vs. `implements` vs. `depends_on` for arbitrary prose entity pairs is not trivial, and the eval gate (E2e frozen fixtures) must catch regressions before ship. The one thing that must not be compromised: the default path must remain byte-identical to pre-E2i — no operator who hasn't set `extraction_model` should see any behavior change.

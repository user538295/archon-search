# Brief: Retire the standalone global embedder — every consumer resolves through EmbedderCache

**ID:** 2026-08-19-100 · **Severity:** P2 · **Status:** Open — deferred until [[2026-08-19-040-double-embedder-load-startup-brief.md]] lands
**Found during:** the 040 fix review (Option B vs C comparison), rooted in [[2026-08-19-000-oom-crash-incident-report.md]]

## Problem

Two embedder objects can exist for the same model: the standalone global (`app.py:870`,
threaded into the pipeline as `_global_embedder`, `pipeline.py:337`) and the per-model instance
inside `EmbedderCache` (ADR 08). 040's Option B pins the *default* model to one shared
instance, but the architecture still allows the bug class: any new consumer can grab the wrong
object, and every warm check or memory audit must know about both. ADR 08's own goal —
"duplicate model instances in memory … unacceptable" — is only met by removing the second
object, not patching around it.

## Suspected correctness bug (verify FIRST — failing repro before any refactor)

Graph search modes drop the caller's per-collection embedder. Verified code facts:

- `pipeline.search` dispatches to `_search_ppr_mode` (`pipeline.py:1029`) and
  `_search_graph_mode` (`pipeline.py:1034-1039`) **without forwarding `embedder`**.
- Neither method has an embedder parameter (`pipeline.py:1301-1309`, `pipeline.py:1214-1222`);
  both embed via `self._global_embedder` internally (e.g. `pipeline.py:1405`).

For a collection pinned to a non-default `active_embedding_model`
(`config.resolve_active_model`), the query vector comes from the WRONG model against vectors
indexed with the pinned model — similarity would be garbage. User-visible impact is
**unproven**: repro sketch — two collections, one pinned to a different model, search with
`graph_mode="ppr"` (and `"local"`/`"global"`), assert which model embeds the query. If it
reproduces, fix at the dispatch (forward the resolved embedder) as its own P2 before or within
this refactor.

## Scope of work (measured 2026-08-19)

- `pipeline.py` alone: **36** `_global_embedder` uses to rethread.
- 10 source files reference the global: `pipeline.py`, `server/app.py`, `server/mcp.py`,
  `server/routes_search.py`, `server/routes_explain.py`, `server/routes_openai_shim.py`,
  `server/readiness.py`, `sync.py`, `eval/runner.py`, `eval/_tracing.py`.
- **76** test files touch it.
- Up-front design decision: pipeline holds the cache and resolves per collection internally,
  vs. every caller passes a resolved embedder (HTTP routes already do the latter —
  `routes_search.py:341-348`).
- `/status` warm-flag semantics need redefining (warm *which* model?) — possible wire-contract
  change; `GET /openapi.json` is authoritative, record breaking changes in `BREAKING.md`.
- Behavioral change: graph/PPR modes start embedding with each collection's own model —
  retrieval results change, so the eval regression gate (`tests/eval/`) is mandatory.
- Estimate: 3–5 days, most of it re-validating retrieval quality, not writing code.

## Why this is not part of 040

040 is a P1 memory fix with zero behavioral risk; this changes retrieval behavior and needs its
own eval-gated plan. Only Option B's seed/pin (~15 lines + tests) becomes throwaway when this
lands; the sequencing and probe-skip work survives unchanged.

## Verification

- The suspected-bug repro above turned into a permanent regression test.
- `grep -rn "_global_embedder\|app\.state\.embedder" archon_search/` returns nothing.
- Full eval suite within thresholds (`tests/eval/thresholds.toml`).
- `/status` and `/ready` behavior matches the redefined contract; OpenAPI snapshot regenerated.

## References

- [[2026-08-19-040-double-embedder-load-startup-brief.md]] — the P1 this defers to
- [[archon_search/pipeline.py]] — `_global_embedder` (:337), graph dispatch (:1029, :1034-1039),
  mode signatures (:1214-1222, :1301-1309), global embed (:1405)
- `Documentation/ADRs/08_per_collection_embedder_lru_cache.md` — the design goal this completes
- `Documentation/ADRs/04_multi_collection_router_with_centroid_preranking.md` — router embedding path

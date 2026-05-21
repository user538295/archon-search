# Review: ADRs/04_multi_collection_router_with_centroid_preranking.md

## Summary

The ADR is largely accurate as a description of `MultiCollectionRouter.rank()` and the tiered `get_pre_context()` policy, with defaults that match `SearchConfig`. The most material inaccuracies concern (1) the claim that "downstream search runs at most `max_parallel_collections` of them concurrently" — `max_parallel_collections` is loaded from config and exposed through `cli/config_cmd.py` but is never consumed by any runtime code path (pipeline, search routes, or router); it does not bound concurrency anywhere; and (2) two oversimplifications in the Decision steps that omit the pinned-collection filtering and the centroid-mismatch fallback semantics.

## Inaccuracies (numbered)

1. **Step 5 / "downstream search runs at most `max_parallel_collections` of them concurrently"** — Unsupported. `max_parallel_collections` is declared in `SearchConfig` (`config.py:44`), parsed from TOML (`config.py:180-184`), and written back by `cli/config_cmd.py:33`, but a repo-wide grep shows no consumer in `pipeline.py`, `router.py`, `routes_search.py`, `sync.py`, or anywhere else. It is dead config in the current code. The ADR presents it as an active concurrency cap.

2. **Step 4 / "Applies a confidence gate — if `max(similarity) < routing_confidence_threshold`, returns an empty shortlist"** — Partially accurate but missing a key branch. In `router.py:149-150`, when *no* collection has both a centroid and a matching `embedding_model`, the gate is **bypassed** and up to `shortlist_size` collections are returned unscored. The ADR presents the gate as unconditional.

3. **Step 3 / "Computes cosine similarity to each centroid whose `embedding_model` matches the active one"** — Accurate as far as it goes, but the ADR omits that mismatched / missing-centroid collections are not dropped — they are placed *after* scored ones in the returned list (`router.py:146, 157`). A reader would conclude such collections are excluded entirely.

4. **"Fetches collection metadata (cached after first call)"** — Imprecise. Metadata is cached only on success and only when at least the JSON-RPC call returns; on any error path (`router.py:83-117`) the method returns `[]` *without* populating `_cached_metadata`, so the next call retries. The ADR's phrasing implies unconditional first-call caching.

5. **Tier description: "≤3 routable collections skip the decomposer entirely; 4..`shortlist_size` skip centroid ranking; only beyond that does full pre-ranking engage"** — Mostly correct, but "skip the decomposer entirely" understates: Tier 1 also returns `None` (no `<search_collections>` block at all) and sets `_decomposer_was_invoked = False`; Tier 2 builds the block over **all** routable collections without ranking. The doc is correct in spirit but the boundary conditions (the `available_slots <= 0` short-circuit and the empty-metadata short-circuit, both at `router.py:183-189`) are unstated.

6. **"Operators mitigate via `pinned_collections`, which bypass the router"** — Partially correct. Pinning is implemented in `get_pre_context()` (`router.py:178-179`) by removing pinned names from the routable set before tiering, and in `routes_route.py:91-92` for the response. The ADR's wording suggests pinned collections bypass the *router as a whole*; in fact they bypass the *ranking/decomposer* stage but are still resolved by the same router method. Minor wording issue.

7. **"Each collection carries a centroid vector and an `embedding_model` tag in `CollectionMeta`"** — Accurate; `CollectionMeta` defines `centroid: list[float] | None` and `embedding_model: str` (`collection_meta.py:16-19`). Note `centroid` is optional (`None` allowed), which the Decision section glosses over.

8. **"Routing accuracy is tracked by the eval harness as a regression guard"** — Not verified in this review (out of scope of router/config files inspected). Flagged as unverified rather than wrong.

## Verified claims

- `MultiCollectionRouter` lives at `archon_search/router.py` (verified: module path and class name match).
- Cosine similarity implementation exists at `router.py:24-30` and is used in `rank()`.
- Defaults match the ADR: `routing_shortlist_size = 8`, `routing_confidence_threshold = 0.30`, `max_parallel_collections = 3` (`config.py:42-44`).
- Tiered policy in `get_pre_context()`: Tier 1 (`n_routable <= 3` skip), Tier 2 (`n_routable <= shortlist_size` no centroid ranking), Tier 3 (`n_routable > shortlist_size` centroid pre-ranking) — verified at `router.py:194-213`.
- Confidence-gate-fails-in-Tier-3 returns `None` and sets `decomposer_was_invoked = False` (`router.py:206-210`).
- Query is embedded via the shared `Embedder` (`router.py:165, 204`) — embedder is passed in by `routes_route.py:42-47`.
- Metadata fetch uses JSON-RPC over HTTP to `tools/call` with `get_collections_meta` (`router.py:72-77`).
- `_ROUTING_FIELDS` filters out fields like `last_indexed` to dodge datetime deserialization (`router.py:21`) — consistent with the ADR's silence on this internal detail.

## Unverifiable / ambiguous

- "Routing accuracy is tracked by the eval harness as a regression guard" — would require inspecting `tests/eval/` and `archon_search/eval/` to confirm there is a dedicated routing metric; not done in this review.
- "Centroids must be kept fresh — stale centroids degrade routing silently" — true as a property statement but not codified anywhere checked; depends on `sync.py` / `recompute_collection_meta` behavior, which was not inspected here.
- "Confidence threshold tuning is corpus-dependent; the default `0.30` is a pragmatic starting point, not a universally correct value" — opinion / guidance, not a verifiable factual claim.
- The Alternatives section ("LLM-based router … a decomposer LLM is still invoked downstream for harder cases") references a "decomposer" that is named in code only as a flag (`_decomposer_was_invoked`, `decomposer_invoked`) and a `<search_collections>` prompt block; the actual decomposer LLM call is not in `router.py` or `routes_route.py`. Ambiguous whether the decomposer is implemented elsewhere in this repo or is meant to live in the *caller* (e.g., an agent/router task consuming the `pre_context`). The `/route` endpoint only *produces* the pre-context; it never invokes an LLM.

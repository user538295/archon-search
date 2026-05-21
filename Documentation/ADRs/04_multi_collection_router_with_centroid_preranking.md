# 04. Multi-Collection Router with Centroid Pre-Ranking

**Status**: Accepted
**Date**: 2026-05-20
**Deciders**: archon-search maintainers

## Context

`archon-search` is multi-collection by design. As the number of collections
grows, naively fanning every query out to every collection becomes wasteful:
most collections are irrelevant to most queries, and parallel fan-out
multiplies retrieval and rerank cost. The system needs a routing layer that
selects a small, relevant subset of collections per query.

Each collection carries an optional centroid vector (`centroid: list[float] | None`)
and an `embedding_model` tag in `CollectionMeta`. When the centroid is
present and produced by the active embedding model, cosine similarity
between the query embedding and each centroid is a cheap, principled
relevance signal. Collections without a centroid, or with one produced by
a different embedding model, are handled as a fallback (returned after
scored collections, see Decision §3).

See [Architecture / 100 — System Architecture Overview](../Architecture/100_system_architecture_overview.md)
for where the router sits in the pipeline.

## Decision

Use **centroid pre-ranking** in `MultiCollectionRouter`
(`archon_search/router.py`). The router:

1. Fetches collection metadata via a JSON-RPC `tools/call` to
   `get_collections_meta`; the result is cached only on success. Any error
   path (timeout, HTTP error, JSON-RPC error, decode failure) returns `[]`
   without populating the cache, so the next call retries.
2. Embeds the query once via the shared `Embedder` (Tier 3 only — Tiers 1
   and 2 below skip embedding).
3. Computes cosine similarity to each centroid whose `embedding_model`
   matches the active one. Collections whose `centroid` is `None` or whose
   `embedding_model` does not match are **not dropped** — they are placed
   *after* the scored collections in the returned list.
4. Applies a confidence gate — if at least one collection was scored and
   `max(similarity) < routing_confidence_threshold`, returns an empty
   shortlist (the query is treated as unroutable). If **no** collection
   has a usable centroid (all unscored), the gate is bypassed and up to
   `routing_shortlist_size` unscored collections are returned.
5. Returns the top `routing_shortlist_size` collections.

A tiered `get_pre_context` policy avoids unnecessary work. Before tiering,
`pinned_collections` are removed from the routable set (they are searched
unconditionally by the caller, bypassing the ranking/decomposer stage —
not the router method itself). Short-circuits: if metadata is empty or
`available_slots <= 0`, the method returns `None` and sets
`decomposer_was_invoked = False`.

- **Tier 1** (`n_routable ≤ 3`): returns `None` (no `<search_collections>`
  block emitted); the caller searches all routable collections.
  `decomposer_was_invoked = False`.
- **Tier 2** (`4 ≤ n_routable ≤ shortlist_size`): builds the
  `<search_collections>` block over **all** routable collections without
  centroid ranking. `decomposer_was_invoked = True`.
- **Tier 3** (`n_routable > shortlist_size`): embeds the query, runs
  centroid pre-ranking, then emits the block over the shortlist. If the
  confidence gate fails (empty shortlist), returns `None` and sets
  `decomposer_was_invoked = False`.

Knobs (defaults from `SearchConfig` in `archon_search/config.py`):

- `routing_shortlist_size = 8`
- `routing_confidence_threshold = 0.30`
- `max_parallel_collections = 3` — **#Unverified**: declared in
  `SearchConfig`, parsed from TOML, and exposed via `archon-search config`,
  but no runtime code path (pipeline, search routes, router, sync) reads
  this value. It is currently inert config; the previous claim that
  downstream search "runs at most `max_parallel_collections` of them
  concurrently" is not implemented.

## Consequences

### Positive
- Bounds query cost as collections grow; latency and rerank work no longer
  scale linearly with total collection count.
- Confidence threshold makes "no relevant collection" an explicit, observable
  outcome rather than a silently noisy fan-out.
- All routing thresholds are config-tunable; no code change to retune.
- Routing accuracy is tracked by the eval harness as a regression guard.
  **#Unverified** — not confirmed against `tests/eval/` and
  `archon_search/eval/` in this revision.

### Negative
- Recall can drop when a genuinely relevant collection has a weak centroid
  (e.g., very heterogeneous corpus). Operators mitigate via
  `pinned_collections`, which bypass the ranking/decomposer stage (pinned
  names are still resolved by `get_pre_context`, but are excluded from the
  routable set before tiering and searched unconditionally by the caller).
- Centroids must be kept fresh — stale centroids degrade routing silently.
  **#Unverified** — freshness behavior depends on `sync.py` /
  `recompute_collection_meta` and was not inspected here.
- Confidence threshold tuning is corpus-dependent; the default `0.30` is a
  pragmatic starting point, not a universally correct value.

## Alternatives Considered

- **Always fan out to all collections**: Rejected — does not scale; multiplies
  retrieval and rerank cost without precision gains.
- **LLM-based router**: Rejected for the default path — adds latency, cost,
  and an external dependency. A decomposer LLM is still invoked downstream
  for harder cases, but only over the centroid-shortlisted set.
  **#Unverified** — the router only *produces* a `<search_collections>`
  prompt block and exposes a `decomposer_was_invoked` flag; the actual
  decomposer LLM call is not in `router.py` or `routes_route.py` and is
  presumed to live in the caller (e.g., an agent consuming `/route`).
- **Keyword / metadata routing**: Rejected as the primary signal —
  collection descriptions are too short and noisy to be reliable on their
  own; centroids carry the corpus's actual semantic mass.

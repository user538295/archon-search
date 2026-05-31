# Feature Brief: Per-Collection Embedding Model

## Problem
Collections are locked to the server's single global embedding model. Operators who need different embedding models per corpus (multilingual, domain-specific, model upgrades) cannot express that configuration — and any collection with a stored model that diverges from the global default is silently excluded from multi-collection search.

## Goal
An operator can assign a distinct embedding model to a collection at creation time and change it later. The server lazy-loads each required embedder on demand (LRU-cached), the router and search paths respect per-collection models, and model drift is visible and resolvable via an explicit reindex.

## Users & Context
Operators (self-hosted, technical) who manage multiple collections with different content types or languages. They are configuring the server via `archon-search.toml` and the REST/MCP API. The typical trigger is: "I want this collection to use a multilingual model without changing the default for everything else."

## Core Flow

1. Operator creates a collection via `POST /collections/` with an optional `embedding_model` field. If omitted, the global `[database].embedding_model` default is used.
2. Ingest runs against that collection — the pipeline looks up the collection's `embedding_model` from `CollectionMeta`, loads the embedder (from LRU cache or fresh), and embeds all chunks with it.
3. At search time (single-collection), the query is embedded with the collection's model, not the global one.
4. At search time (multi-collection), the query is embedded with the global/default model. Collections whose `embedding_model` differs are excluded from routing scoring and returned in `excluded_collections` with `reason: "embedding_model_mismatch"`. Collections can still be queried directly.
5. Operator changes a collection's model via `PATCH /collections/{name}`. The collection is marked `needs_reindex: true`. Searches still work using the old model until reindex completes.
6. Operator triggers explicit reindex via the existing job API. On completion, `needs_reindex` is cleared and the new model is in effect.
7. Health and status endpoints surface `needs_reindex: true` collections so operators know what's pending.

## In Scope

- `embedding_model` field on `POST /collections/` (optional; defaults to global).
- `PATCH /collections/{name}` to update `embedding_model`; sets `needs_reindex: true` on non-empty collections.
- Per-collection embedder dispatch in ingest pipeline and single-collection search.
- Lazy-load LRU embedder cache in the server runtime.
- Two new `archon-search.toml` config keys: `embedder_cache_size` (int, default `3`) and `eager_load_embedders` (bool, default `false`). When `eager_load_embedders = true`, all distinct models found in `CollectionMeta` are loaded at startup.
- `needs_reindex: bool` field added to `CollectionMeta`; surfaced on `GET /collections/{name}` and health/status.
- Router behavior unchanged: mismatched-model collections are unscored and excluded, same as today. `ExcludedCollection(reason="embedding_model_mismatch")` already surfaces this.
- Eval harness: existing deterministic backends used; add fixtures covering mixed-model collection setups.

## Out of Scope

- Multi-model query fanout (embedding the query in multiple models to search cross-model collections in one call) — deferred; router exclusion behavior is the correct default.
- Automatic reindex triggered by model change — operator must initiate explicitly.
- Reranker model per collection — separate concern, separate item.
- Multilingual tokenisation / FTS changes — those are C2.
- TOML-level per-collection model config — per-collection state lives in LanceDB `CollectionMeta`, not the config file.

## Key Decisions

- **Lazy-load + LRU cache**: consistent with fastembed's existing lazy-load pattern (ADR-02); `embedder_cache_size` in TOML controls eviction. `eager_load_embedders = true` pre-warms all models at startup for latency-sensitive setups.
- **Model set via API, not TOML**: `CollectionMeta.embedding_model` already persists per-collection state in LanceDB; TOML is for global defaults only. No dual source-of-truth.
- **Mutable model with explicit reindex**: collection model can be changed; `needs_reindex` flag marks the collection as pending. Reindex is always operator-initiated via the existing job API.
- **Cross-model routing stays exclusionary**: multi-collection search embeds the query once with the global model; mismatched collections are excluded from routing scoring. This is already implemented. Cross-model fanout is a future item.
- **`PATCH /collections/{name}` is a new endpoint**: accepts `{"embedding_model": "..."}`, sets `needs_reindex: true` on non-empty collections. Existing `POST /collections/` retains its 409-on-duplicate semantics unchanged.
- **CLI `ingest` gets no `--embedding-model` flag**: always reads model from `CollectionMeta` (or global default for new collections). Operators who need a non-default model on a new collection create it via API first, then ingest. Long-term home for model config in CLI is a future `archon-search collection create` subcommand.

## Edge Cases & Constraints

- **Empty collection model change**: if `chunk_count == 0`, changing `embedding_model` does not set `needs_reindex` — no data to reindex.
- **Reindex in progress + model change**: reject a second model change while a reindex job is running for that collection. Return HTTP 409.
- **Unknown model name at collection creation**: validate the model name against fastembed's supported list at API time; return HTTP 422 with a clear message if unrecognised.
- **LRU eviction during a search**: once an embedder is selected for a request, it must not be evicted mid-request. Hold a reference for the duration of the call.
- **`eager_load_embedders = true` + unknown model in metadata**: if a `CollectionMeta` row has an unrecognised model at startup, log a warning and skip that model — don't abort startup.
- **Single-collection search model lookup**: `GET /search?collection=foo` must embed the query with `foo`'s model, not the global one. This changes the hot path.
- **`/explain` endpoint**: must reflect the per-collection model used for the query vector in the routing path output.

## Open Questions

None.

## Future Iterations

- Multi-model query fanout: embed the query in each distinct model and merge results across all collections in one call.
- Model warm-up endpoint: `POST /collections/{name}/warm` to pre-load the embedder without running a search.
- Automatic reindex on model change with operator confirmation prompt in CLI.
- Per-collection reranker model (C-series, separate item).
- `archon-search collection create` CLI subcommand with `--embedding-model` flag — the right long-term home for model assignment from the CLI.

## Recommendation

This is the right feature to build at C1. The schema and exclusion logic are already in place — this is primarily a runtime dispatch problem (embedder cache) and a lifecycle problem (reindex signalling). The hardest part is the LRU embedder cache: it must be safe under concurrent async requests, and the `eager_load_embedders` path needs care at startup to avoid failing on stale/unrecognised model names in metadata. The single-collection search hot path change (query embedded with collection model, not global) is a breaking behaviour change for anyone who has set different per-collection models — document it in `BREAKING.md`. Do not compromise on the `needs_reindex` visibility: silent model drift is how silent degradation happens.

# 07. Description-Embedding Hybrid Routing

**Status**: Accepted
**Date**: 2026-05-28
**Deciders**: archon-search maintainers
**Extends**: ADR-04 (Multi-Collection Router with Centroid Pre-Ranking)

## Context

ADR-04 established centroid pre-ranking as the default routing strategy for
`MultiCollectionRouter`. Centroid similarity is a strong signal when a collection
has a coherent, dense corpus — the centroid captures the semantic mass of all
indexed chunks. However, it fails in a specific failure mode: **diffuse corpora**.

When a collection contains heterogeneous documents spanning multiple topics (e.g.,
a `faq` or `mixed` collection), the centroid vector ends up near the origin — a
low-norm vector that produces uniformly low cosine similarities against all
queries. This means a query that is genuinely routable to a diffuse collection
fails the confidence gate and the collection is silently dropped from the shortlist,
even though its description clearly describes the collection's purpose.

A collection's **description** (free-text, typically auto-generated via
`description_generator.py`) is a complementary signal: it is curated to explain
what the collection contains at a human-readable level, not at the chunk-vector
level. When the collection description is embedded with the same model as query
embeddings, description-embedding cosine similarity provides a second signal that
is orthogonal to centroid similarity. For diffuse corpora, the description signal
often dominates; for coherent corpora, the centroid signal is already strong and
blending in the description adds only marginal noise.

B4 (roadmap item 9) introduced this hybrid signal, adding a `description_embedding`
field to `CollectionMeta` (stored in the LanceDB metadata table), computed at
ingest time via `pipeline.py::recompute_collection_meta` and backfilled at startup
via `server/app.py::migrate_description_embedding`. The `MultiCollectionRouter`
gains an optional `strategy` constructor parameter.

See ADR-04 for the existing centroid-only routing architecture.

## Decision

1. **Centroid-only routing remains the default.** `routing_strategy = "centroid"`
   is the default in `SearchConfig` and in `archon-search.toml`. No operator action
   is needed to preserve the pre-B4 behavior.

2. **Hybrid routing is opt-in.** Setting `routing_strategy = "hybrid"` in
   `archon-search.toml` enables blending. The blend formula for a collection that
   has a valid `description_embedding` is:

   ```
   score = (1 - w) * centroid_score + w * description_score
   ```

   where `w = routing_description_weight` (default `0.3`, range `[0.0, 1.0]`).
   A collection is eligible for blending only when all of the following hold:
   - `col.description_embedding is not None`
   - `len(col.description_embedding) == len(query_embedding)`
   - `col.description_embedding` is not all-zeros

   Collections that fail any condition fall back to pure centroid scoring, which
   preserves the ADR-04 fallback contract (unscored collections are appended after
   scored ones).

3. **`description_embedding` is a new optional field on `CollectionMeta`.** It is
   stored as a JSON column in the LanceDB metadata table. Existing rows that lack
   the column are read as `None` (column-absent tolerance). The field is populated:
   - On `ingest_directory` via `recompute_collection_meta` in `pipeline.py`.
   - At server startup via `migrate_description_embedding` in `server/app.py`, which
     backfills any collection whose `description_embedding` is missing.

4. **The `get_collections_meta` MCP tool gains an opt-in
   `include_description_embedding: bool = False` parameter.** By default it strips
   `description_embedding` from the returned list. The single-collection
   `get_collection_meta` returns `description_embedding` unconditionally (the field
   is part of `CollectionMeta` and that tool always returns the full dataclass).
   This asymmetry is intentional: `get_collections_meta` is the bulk metadata path
   used by the router and by LLM consumers, where the embedding vectors inflate
   payload size; `get_collection_meta` is a per-collection diagnostic path.

5. **The eval harness gates hybrid routing.** The `routing_mrr_hybrid` threshold
   floor is set to the measured `routing_mrr_centroid` baseline value, enforcing
   Δ ≥ 0: hybrid must be at least as good as centroid on the eval corpus before it
   can be merged. This is documented in `tests/eval/README.md`.

## Consequences

### Positive

- Collections with diffuse corpora (FAQ, mixed, general-purpose) are no longer
  silently dropped by the confidence gate when their description clearly signals
  relevance.
- The centroid-only default path is unchanged; no operator migration is required.
- `description_embedding` is computed once per ingest/reindex (not per query), so
  the hybrid blend adds zero retrieval-time model calls compared to centroid-only.
- The `routing_description_weight` knob lets operators tune the blend without
  touching code.

### Negative

- Stale or low-quality descriptions (e.g., auto-generated from a small corpus)
  can produce misleading `description_embedding` vectors that degrade routing for
  coherent corpora. Operators must ensure descriptions are regenerated when a
  collection's content changes significantly.
- `description_embedding` is a large field (dim floats per collection, stored as
  JSON). The `get_collections_meta` bulk endpoint strips it by default to avoid
  payload inflation; strict-schema MCP clients for `get_collection_meta` must
  account for the additive field (see `BREAKING.md` B4 entry).
- B4 adds one more "column-absent = None" tolerance point to the metadata table
  schema. As further columns accumulate (B5, B6), a per-table `schema_version`
  marker becomes a prerequisite to avoid unbounded toleration logic. Tracked as
  a debt entry in `530_technical_debt_refactoring_roadmap.md`.

## Alternatives Considered

- **Always blend centroid and description by default**: Rejected — the eval
  harness showed that for coherent corpora the centroid is already strong and
  blending adds noise. The centroid-default / hybrid-opt-in split preserves ADR-04
  semantics for existing deployments.
- **Replace centroid with description-embedding entirely**: Rejected — description
  embeddings are only as good as the descriptions; centroid embeddings are computed
  directly from the corpus and carry real semantic mass. The blend is more robust
  than either alone.
- **Multi-centroid routing**: Considered but deferred. For highly heterogeneous
  corpora, a per-cluster centroid array would be more accurate than a single
  description embedding. This remains an open option for a future ADR if the
  single-description approach proves insufficient (see `B5-incremental-centroid-plan.md`
  for the deferred multi-centroid scope note).
- **LLM-based collection selection**: Rejected for the routing hot path — same
  reasoning as ADR-04. The decomposer LLM downstream operates on the shortlist
  produced by the router; the router itself must be cheap and local.

# 12. Local Prose Relations, Enrichment Narrowed to Summarisation

**Status**: Accepted
**Date**: 2026-09-05
**Deciders**: archon-search maintainers
**Supersedes**: partially supersedes [ADR C6 — llama.cpp Local LLM Provider](C6-local-llm-provider.md) — specifically its **relationship-labelling half**: the part of §(b) that describes `label_relationships` as one of the two wired enrichment operations, and every reference to typed relationship extraction going through an LLM adapter. C6's query-expansion decisions and its community-summarisation wiring are unchanged and still authoritative.

## Context

C6 wired graph enrichment for the first time with a two-method protocol
(`LLMEnrichmentClientProtocol`): `summarize_community` and `label_relationships`.
Typed prose relationships (`uses` / `implements` / `depends_on`) were produced by
an LLM: `GraphExtractor.extract` made one `label_relationships` call per plain-text
chunk with 2+ entities, constraining small local models with a JSON-schema
`response_format` and a narrowed 3-value relationship enum
(`_VALID_RELATIONSHIP_TYPES`). This had three costs: it needed a configured LLM
provider to produce any typed edge at all, it was per-chunk (economically hostile
to reasoning models — see BE-23), and it was monolingual in practice.

The `2026-08-19-035` multilingual-graph-NER feature replaced the entity-extraction
engine with a local `gliner` relation-extraction checkpoint
(`knowledgator/gliner-relex-multi-v1.0`), shipped as PyTorch weights and hosted in
`ProseExtractionBackend`. That engine returns **directed typed relations** locally,
in multiple languages, with no LLM call and no configured provider — the spike gate
(team plan) confirmed the checkpoint's relation capability against real corpora.
Once the local engine is the producer of typed prose edges, the LLM
`label_relationships` path is redundant: it duplicates a capability the engine now
owns, while carrying the provider-required, per-chunk, JSON-schema-fragility cost.

## Decision

**The enrichment protocol narrows to community summarisation only.**
`label_relationships`, its `LabeledRelationship` DTO, the narrowed
`_VALID_RELATIONSHIP_TYPES` enum, and the llama.cpp JSON-schema `response_format`
constraint are removed from the protocol (`graph_enrichment_protocol.py`) and from
all four adapters (`anthropic`, `llama_cpp`, `ollama`, `openai`). The single
surviving method, `summarize_community`, keeps its raise-on-failure semantics
unchanged. The `EnrichmentClientFactory`, the provider registry, and every
`[graph]` provider field survive — they now gate summarisation alone.

Corollaries recorded here (each decided in an earlier task of the same feature; this
ADR is their umbrella record):

- **Engine choice.** Typed prose relations are produced by the local `gliner`
  PyTorch checkpoint in `ProseExtractionBackend`, not by any LLM. This is the sole
  source of typed prose edges; they are additive alongside `related_to`
  co-occurrence edges. `GraphExtractor` no longer depends on the enrichment client
  at all (the constructor no longer takes one); the client is built once and
  injected only into `CommunityBuilder` for summaries.

- **`[graph].providers` posture and its non-inheritance.** `[graph].providers` is
  the graph engine's own execution-provider list, wizard-owned. Unlike
  `reranker_providers` (which falls back to `[database].providers` via
  `resolve_reranker_providers`), an **unset `[graph].providers` does not inherit**
  from any other section — it always means CPU. The divergence is deliberate: the
  graph engine's device placement is an independent, accelerator-gated choice with
  its own post-download validation, and silently inheriting the database's provider
  list would offer an accelerator the graph probe never validated.

- **Single, unbounded, resident model — deviation.** `ProseExtractionBackend` holds
  **one** model instance, loaded once per process behind a lock and kept resident
  for the process lifetime — there is no LRU cache and no bounded eviction, unlike
  the per-collection embedder cache (ADR 08/11). A knowledge-graph deployment loads
  exactly one graph engine, so the multi-model cache machinery those ADRs justify
  has no subject here; a single always-resident instance is simpler and correct for
  this one-model case. The memory budget is sized for that one resident model
  (~3.65 GiB at the pinned sub-batch size).

- **No `provider_notes` successor (deliberate absence).** The narrowing removes the
  only unactionable, permanent property `provider_notes` existed to carry (the LLM
  relationship-labelling capability caveat). `GET /status` → `model_validation`
  keeps `provider_warnings` (actionable strings only, load-bearing for
  `GET /ready` → `checks.models`) and gains **no** replacement channel for
  unactionable notes — there is deliberately nothing to put in one after this
  change. (The field's mechanical removal from the wire schema is BE-18; the
  decision that nothing replaces it is recorded here.)

## Consequences

### Positive

- Typed prose relationships are produced locally, multilingually, and **without a
  configured LLM provider** — the air-gap default now yields typed edges, which the
  LLM path never could.
- One protocol method instead of two; the JSON-schema `response_format`, the 422
  fallback, the narrowed relationship enum, and the LLM-merged-entity recovery
  heuristic (`_resolve_labeled_pair`) are all deleted — a net simplification across
  four adapters and `GraphExtractor`.
- The startup extraction-model probe (`model_validation._probe_extraction_model`)
  now exercises `summarize_community`, the one path a configured provider still
  serves, so its reasoning-model economic check still applies where it matters.

### Negative / Tradeoffs

- Relationship quality is now bounded by the local checkpoint rather than by a
  frontier LLM. An operator who previously pointed enrichment at a strong hosted
  model for relationship labelling loses that path; the engine's typed edges stand
  in for it. Community summarisation still uses the configured LLM.
- The enrichment provider is now used for strictly less work (summaries only), so
  the value of configuring one is lower for deployments that do not rebuild
  communities.

## Alternatives Considered

- **Keep `label_relationships` alongside the local engine (retain both producers).**
  Rejected: the spike gate's finding-2 pass means the local engine produces typed
  edges unconditionally, so retaining the LLM path would mean two producers of the
  same edge type, a provider-gated one duplicating a local one — dead weight and a
  second, divergent relationship vocabulary. (This retention was the plan's
  documented fallback for a finding-2 *failure*, which did not occur.)
- **Retarget the protocol's second method to something else** (e.g. entity typing).
  Rejected: no consumer needs it; YAGNI. The protocol should carry exactly what
  `CommunityBuilder` consumes.
- **A `provider_notes` successor for the "relations are local now" note.** Rejected:
  it is not an actionable operator warning and `provider_warnings` is graded for
  `GET /ready`; a permanent, unactionable note has no honest home on that surface.

## Cross-References

- [ADR C6 — llama.cpp Local LLM Provider](C6-local-llm-provider.md): this ADR
  narrows C6's enrichment contract to summarisation; C6's query-expansion decisions
  and community-summarisation wiring are unchanged.
- `archon_search/graph_enrichment_protocol.py`, `archon_search/enrichment/`: the
  narrowed protocol and the four summarisation-only adapters.
- `archon_search/graph_extractor.py`: local typed-relation edges; no enrichment
  client dependency.
- `archon_search/prose_extraction_backend.py`: the single resident `gliner`
  checkpoint that produces typed prose relations.
- `archon_search/model_validation.py`: `_probe_extraction_model` now probes
  `summarize_community`.
- `tsp_contract/llama-cpp-enrichment-protocol.tsp`: the internal logical seam,
  revised to declare summarisation alone.

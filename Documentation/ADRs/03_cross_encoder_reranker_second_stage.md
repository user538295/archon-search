# 03. Cross-Encoder Reranker as Second Stage

**Status**: Accepted
**Date**: 2026-05-20
**Deciders**: archon-search maintainers

## Context

Bi-encoder retrieval — dense vector ANN plus FTS fused with RRF in
`archon_search/store.py` — is fast but optimises for recall over a shortlist,
not precision at the very top of the result list. The pipeline retrieves a
larger candidate set (`top_k_retrieve`, default `15` in
`archon_search/config.py`) and must return a smaller, higher-quality
`top_k_return` (default `5`) to the caller.

A second-stage reranker that jointly attends to `(query, candidate)` pairs is
the standard remedy. It is more expensive per pair than a bi-encoder but only
runs on the small retrieved shortlist, not the whole corpus. The eval harness
tracks reranker lift; the snapshot value is recorded in
`tests/eval/baselines/baseline.md` (e.g. `reranker_lift = 0.0230`), while the
regression-guard mechanism itself lives in `tests/eval/thresholds.toml` plus
the eval harness.

## Decision

Add a cross-encoder rerank stage between retrieval and response, implemented
in `archon_search/reranker.py` and orchestrated by
`archon_search/pipeline.py`. Default model: **`Xenova/ms-marco-MiniLM-L-6-v2`**
(see `SearchConfig.reranker_model` and `archon-search.toml.example` →
`[database] reranker_model`). The reranker is backed by
`fastembed.rerank.cross_encoder.TextCrossEncoder` and receives the same
`providers` list as the embedder — there is no separate ONNX configuration
layer; `create_pipeline` propagates a single `cfg.providers` value into both
`ModelEmbedder` and `ModelReranker` (see
[ADR 02](02_fastembed_for_dense_embeddings.md)).

## Consequences

### Positive
- Improves precision at small `top_k_return` versus pure bi-encoder ranking.
  The eval harness exposes a `reranker_lift` metric (positive in the current
  baseline snapshot) but uses deterministic, label-blind backends, so this is
  a regression signal rather than a production precision measurement.
  #Unverified (as a real-world precision claim)
- MiniLM-L-6 is small enough to run on CPU with acceptable latency for the
  intended single-process deployment. #Unverified (no latency budget asserted
  in code; only a relative regression guard exists in the eval harness)
- Lazy model load — first-query cost only; subsequent calls reuse the
  in-process cached model (double-checked locking in `ModelReranker.predict`).
- The reranker is a clean seam: `RerankerBackend` is a Protocol and the
  backend is injectable via `create_pipeline(..., reranker_backend=...)`, so
  it can be swapped without touching retrieval code.

### Negative
- Adds per-query latency proportional to `top_k_retrieve` (bounded, since the
  shortlist is small).
- Extra model weights to download and cache on first run.
- Cross-encoder scores are not directly comparable across queries, so they
  are used for ranking only — not as an absolute relevance threshold.

## Alternatives Considered

#Unverified (rationale, not checkable against source)

- **Pure bi-encoder, no rerank**: Rejected — lower precision at top-k,
  measurable as a negative reranker-lift delta in the eval harness.
- **LLM-based reranker**: Rejected — adds external API dependency or a large
  local model, with order-of-magnitude higher latency and cost; conflicts
  with the local-first runtime stance.
- **Learning-to-rank over hand-crafted features**: Rejected — requires
  training data and infrastructure that this project does not own.

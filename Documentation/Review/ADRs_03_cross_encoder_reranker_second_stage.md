# Review: ADRs/03_cross_encoder_reranker_second_stage.md

## Summary

The ADR is largely accurate. All concrete, verifiable claims (default model name,
`top_k_retrieve=15`, `top_k_return=5`, config keys, file locations, shared
`providers` configuration with the embedder, lazy load, ranking-only score use,
existence of reranker-lift metric in the baseline) check out against the source.
One minor inaccuracy: the reranker uses `fastembed.TextCrossEncoder`, not a raw
ONNX provider; the phrasing "ONNX provider configuration" is technically correct
since fastembed runs on ONNX Runtime, but the reranker shares fastembed's
`providers=` kwarg rather than a separate ONNX config. The cross-reference to
ADR 02 is consistent with how `create_pipeline` wires both backends.

## Inaccuracies (numbered)

1. **Line 19-20 — eval reranker-lift citation is slightly misleading.**
   The ADR says reranker lift is tracked "as a regression guard (see
   `tests/eval/baselines/baseline.md`)." The baseline file does contain a
   `reranker_lift = 0.0230` entry, so the citation resolves, but the file is a
   snapshot baseline rather than the regression-guard mechanism itself; the
   guard lives in `tests/eval/thresholds.toml` + the eval harness. Minor — the
   referenced file does exist and does contain the metric.

2. **Line 29-30 — "ONNX provider configuration" phrasing.**
   `ModelReranker` accepts `providers: list[str] | None` and forwards it to
   `fastembed.rerank.cross_encoder.TextCrossEncoder(..., providers=...)`
   (reranker.py:33-35). It is the same `providers` list passed to the embedder,
   so the claim "shares the fastembed runtime and ONNX provider configuration
   with the embedder" is substantively true but phrased as if there is a
   separate ONNX configuration layer — there is not; it is a single `providers`
   list propagated by `create_pipeline` (pipeline.py:419-426).

No other inaccuracies found.

## Verified claims

- Bi-encoder stage in `archon_search/store.py` with RRF fusion — consistent
  with pipeline.py:301 calling `store.hybrid_search` before rerank.
- `top_k_retrieve` default `15` — confirmed at `config.py:39`.
- `top_k_return` default `5` — confirmed at `config.py:40`.
- Reranker implemented in `archon_search/reranker.py` and orchestrated by
  `archon_search/pipeline.py` — confirmed (reranker.py, pipeline.py:303).
- Default model `cross-encoder/ms-marco-MiniLM-L-6-v2` — confirmed at
  `config.py:35` and `archon-search.toml.example:26`.
- Config key location `[database] reranker_model` — confirmed at
  `archon-search.toml.example:26` and `config.py:145-146` (loaded from the
  `database` table).
- Shared fastembed runtime / providers with embedder — confirmed: both
  `ModelEmbedder` (embedder.py:18-30) and `ModelReranker`
  (reranker.py:21-35) accept and pass through the same `providers` argument
  drawn from `cfg.providers` (pipeline.py:421, 425).
- Lazy model load, first-query cost only — confirmed: double-checked-locking
  pattern in `ModelReranker.predict` (reranker.py:30-35), model is `None`
  until first `predict()`.
- Reranker only runs on the small shortlist — confirmed:
  `self._reranker.rerank(query, candidates, top_k=self._top_k_return)` is
  called after retrieval, with `candidates` capped at `top_k_retrieve`
  (pipeline.py:301-303).
- Reranker is a swappable seam — confirmed: `RerankerBackend` Protocol
  (reranker.py:13-15) and `reranker_backend` injection in `create_pipeline`
  (pipeline.py:412, 423).
- Cross-encoder scores used for ranking only — confirmed: results are sorted
  by the raw score with no thresholding (reranker.py:67).
- Eval baseline tracks `reranker_lift` — confirmed: `baseline.md` line 14
  shows `reranker_lift = 0.0230`.

## Unverifiable / ambiguous

- "Improves precision at small `top_k_return` versus pure bi-encoder ranking" —
  empirical claim. The eval harness exposes `reranker_lift`, and the current
  baseline value (`0.0230`) is positive, which is consistent with the claim,
  but the ADR does not quantify and the eval backends are documented as
  deterministic / label-blind, so this number is a regression signal rather
  than a real-world precision measurement. Cannot be confirmed as a production
  precision improvement from code alone.
- "Small enough to run on CPU with acceptable latency" — qualitative; no
  latency budget or measurement is asserted in code. The eval harness has a
  latency-regression guard (per CLAUDE.md / 210_performance_and_scalability),
  but "acceptable" is unquantified here.
- "First-query cost only; subsequent calls are warm" — the lazy-load mechanism
  is verified, but "warm" implies an in-process cached model which the code
  does provide; framing is fine, just not separately measurable.
- Alternatives Considered (pure bi-encoder, LLM reranker, learning-to-rank) —
  rationale, not verifiable against source.
- Status "Accepted" and Date "2026-05-20" — administrative metadata; not
  checkable against code.
